# core/wifi_channel.py
import re
import time
from typing import Dict

from core.dut import run_mssh_once
from config import BW_TEMPLATE, MSSH_TIMEOUT_SHORT

AP_IFACE = "wlan1"
RUNTIME_CONF = "/tmp/hostapd_runtime_wlan1.conf"

AP_READY_TIMEOUT_SEC = 15
AP_READY_POLL_SEC = 1

# === 80 MHz center channel mapping ===
CENTER_80M = {
    36: 42,
    149: 155,
}

# ======================================================
# Low-level safe exec wrapper (CLEAN LOG VERSION)
# ======================================================

def _sh(
    cmd: str,
    *,
    label: str | None = None,
    timeout_sec: int = 5,
    retry: int = 0,
    mssh_timeout: int | None = None,
) -> str:
    """
    Safe DUT shell execution with clean logging.

    - Only logs human-readable `label`
    - Hides shell wrapper / timeout logic
    - Prints diagnostics only on timeout or SSH error
    """
    if label:
        print(f"[DUT] {label}")

    last_out = ""

    for attempt in range(retry + 1):
        wrapped = (
            f"timeout {timeout_sec} sh -lc {cmd!r}; "
            f"rc=$?; "
            f"if [ $rc -eq 124 ]; then echo __REMOTE_TIMEOUT__; fi; "
            f"exit 0"
        )

        try:
            effective_timeout = (
                mssh_timeout
                if mssh_timeout is not None
                else max(MSSH_TIMEOUT_SHORT, timeout_sec + 5)
            )
            out = run_mssh_once(wrapped, timeout=effective_timeout)
        except Exception as e:
            if attempt < retry:
                time.sleep(0.5)
                continue
            print(f"[DUT][ERR] {label or cmd} (ssh error: {e})")
            return ""

        last_out = out.strip()

        if "__REMOTE_TIMEOUT__" in last_out:
            if attempt < retry:
                time.sleep(0.5)
                continue
            print(f"[DUT][TIMEOUT] {label or cmd} ({timeout_sec}s)")
            return ""

        return last_out

    return last_out


def _iface_up() -> None:
    """
    Ensure interface exists & is UP before wl operations.
    Log only once; retries stay silent.
    """
    _sh(
        f"ifconfig {AP_IFACE} up || true",
        label="ifconfig up",
        timeout_sec=5,
        retry=2,
    )



# ======================================================
# Parsing helpers
# ======================================================

_CHANSPEC_RE = re.compile(r"^(\d+)([lu]?)", re.IGNORECASE)
_PRIMARY_RE = re.compile(r"Primary channel:\s*(\d+)", re.IGNORECASE)


def _parse_primary_channel(wl_status: str) -> int:
    m = _PRIMARY_RE.search(wl_status or "")
    if not m:
        raise RuntimeError("Primary channel not found")
    return int(m.group(1))


# ======================================================
# Waiters
# ======================================================

def _wait_ap_enabled() -> bool:
    deadline = time.time() + AP_READY_TIMEOUT_SEC
    ctrl_sock = f"/var/run/hostapd/{AP_IFACE}"

    while time.time() < deadline:
        out = _sh(
            f"test -S {ctrl_sock} && echo READY || true",
            timeout_sec=2,
            retry=0,
        )
        if "READY" not in out:
            time.sleep(AP_READY_POLL_SEC)
            continue

        s = _sh(
            f"hostapd_cli -i {AP_IFACE} status || true",
            timeout_sec=3,
            retry=0,
        )
        if "state=ENABLED" in s:
            print("[AP] hostapd ENABLED")
            return True

        time.sleep(AP_READY_POLL_SEC)

    return False



# ======================================================
# hostapd conf patchers
# ======================================================

def _apply_channel_to_conf(ch: int) -> None:
    _sh(
        f"grep -q '^channel=' {RUNTIME_CONF} && "
        f"sed -i 's/^channel=.*/channel={ch}/' {RUNTIME_CONF} || "
        f"echo 'channel={ch}' >> {RUNTIME_CONF}",
        label=f"patch channel={ch}",
        timeout_sec=5,
        retry=1,
    )


def _apply_vht_center_if_needed(bw: int, ch: int) -> None:
    if bw != 80:
        return

    if ch not in CENTER_80M:
        raise RuntimeError(f"No 80MHz center mapping for channel {ch}")

    center = CENTER_80M[ch]
    print(f"[CONF] BW80 → vht center={center}")

    _sh(
        f"grep -q '^vht_oper_chwidth=' {RUNTIME_CONF} && "
        f"sed -i 's/^vht_oper_chwidth=.*/vht_oper_chwidth=1/' {RUNTIME_CONF} || "
        f"echo 'vht_oper_chwidth=1' >> {RUNTIME_CONF}",
        label="patch vht_oper_chwidth",
        timeout_sec=5,
        retry=1,
    )

    _sh(
        f"grep -q '^vht_oper_centr_freq_seg0_idx=' {RUNTIME_CONF} && "
        f"sed -i 's/^vht_oper_centr_freq_seg0_idx=.*/vht_oper_centr_freq_seg0_idx={center}/' {RUNTIME_CONF} || "
        f"echo 'vht_oper_centr_freq_seg0_idx={center}' >> {RUNTIME_CONF}",
        label="patch vht center freq",
        timeout_sec=5,
        retry=1,
    )


# ======================================================
# WL channel/BW set (CORRECT VERIFICATION)
# ======================================================

def _wl_set_chanspec(bw: int, ch: int) -> None:
    """
    Driver-level channel lock.

    Verification rules:
    - 20MHz : token == "36"
    - 40MHz : token == "36l" or "36u"
    - 80MHz : token startswith "36"
    """
    _iface_up()

    _sh("pkill -9 wpa_supplicant || true", label="kill wpa_supplicant", timeout_sec=3)
    _sh("pkill -9 hostapd || true", label="kill hostapd", timeout_sec=3)

    def _match(token: str) -> bool:
        m = _CHANSPEC_RE.match(token)
        if not m:
            return False

        got_ch = int(m.group(1))
        suffix = m.group(2)

        if bw == 20:
            return got_ch == ch and suffix == ""
        if bw == 40:
            return got_ch == ch and suffix in ("l", "u")
        if bw == 80:
            return got_ch == ch

        return False

    last_raw = None

    for attempt in range(1, 4):
        _sh(f"wl -i {AP_IFACE} down || true", label="wl down", timeout_sec=4)
        _sh(
            f"wl -i {AP_IFACE} chanspec {ch}/{bw}",
            label=f"wl chanspec {ch}/{bw}",
            timeout_sec=6,
        )
        _sh(f"wl -i {AP_IFACE} up || true", label="wl up", timeout_sec=6)

        cs = _sh(
            f"wl -i {AP_IFACE} chanspec || true",
            timeout_sec=5,
            retry=0,
        ).strip()

        if cs:
            token = cs.split()[0]
            last_raw = token
            if _match(token):
                print(f"[AP][WL] chanspec OK: {token}")
                return

        print(f"[AP][WL] chanspec retry {attempt}/3 (raw={last_raw})")
        time.sleep(1)

    raise RuntimeError(
        f"❌ wl chanspec failed: BW={bw} CH={ch} (last={last_raw})"
    )


# ======================================================
# Public API
# ======================================================

def set_ap_channel_and_bw(bw: int, ch: int) -> Dict:
    """Bring up AP with correct BW / channel."""
    print(f"[AP] bring-up BW={bw} CH={ch}")

    _sh("killall hostapd || true", label="killall hostapd", timeout_sec=4)
    _sh("pkill -9 hostapd || true", label="pkill hostapd", timeout_sec=4)
    _sh("rm -rf /var/run/hostapd || true", label="cleanup hostapd socket", timeout_sec=4)

    _iface_up()
    _wl_set_chanspec(bw, ch)

    conf_src = BW_TEMPLATE.get(bw)
    if not conf_src:
        raise RuntimeError(f"No hostapd template for BW={bw}")

    _sh(f"cp {conf_src} {RUNTIME_CONF}", label="copy hostapd conf", timeout_sec=6)
    _apply_channel_to_conf(ch)
    _apply_vht_center_if_needed(bw, ch)

    _sh(
        f"ifconfig {AP_IFACE} 192.168.10.100 up || true",
        label="set AP IP",
        timeout_sec=5,
    )

    _sh(
        f"hostapd -B -i {AP_IFACE} {RUNTIME_CONF}",
        label="start hostapd",
        timeout_sec=6,
    )

    if not _wait_ap_enabled():
        raise RuntimeError(f"❌ AP bring-up FAILED: BW={bw} CH={ch}")

    wl_status = _sh(
        f"wl -i {AP_IFACE} status || true",
        label="wl status (final)",
        timeout_sec=5,
    )

    try:
        final_primary = _parse_primary_channel(wl_status)
    except Exception:
        final_primary = ch

    print(f"[AP] BW={bw} CH={ch} Primary={final_primary} status=OK")

    return {
        "bw": bw,
        "requested_channel": ch,
        "final_primary": final_primary,
        "status": "OK",
    }
