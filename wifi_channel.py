# core/wifi_channel.py
from __future__ import annotations

import re
import time
from typing import Dict

import config
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
# hostapd conf patchers (5G path)
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
# WL channel/BW set (5G path)
# ======================================================

def _wl_set_chanspec(bw: int, ch: int) -> None:
    """
    Driver-level channel lock (5G usage).

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

    raise RuntimeError(f"❌ wl chanspec failed: BW={bw} CH={ch} (last={last_raw})")


# ======================================================
# Public API (5G original)
# ======================================================

def set_ap_channel_and_bw(bw: int, ch: int) -> Dict:
    """Bring up 5GHz AP with correct BW / channel (legacy behavior)."""
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
        f"ifconfig {AP_IFACE} 192.168.50.100 up || true",
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
        "band": "5G",
        "bw": bw,
        "requested_channel": ch,
        "final_primary": final_primary,
        "status": "OK",
    }


# ======================================================
# Unified entry (NEW): band-aware AP bring-up
# ======================================================

def set_ap_channel_and_bw_band(*, band: str, bw: int, ch: int) -> Dict:
    """
    Unified AP bring-up entry.
    - band: "5G" / "2G" (case-insensitive; also accepts "5", "2", "5ghz", "2.4g", etc.)
    """
    b = (band or "").strip().upper()
    if b in ("5G", "5", "5GHZ", "5GZ"):
        return set_ap_channel_and_bw(bw=bw, ch=ch)

    if b in ("2G", "2", "2.4G", "2G4", "2GHZ", "2.4GHZ"):
        # your spec: 2.4G AP is fixed CH6 / BW20 by conf
        return _set_ap_2g()

    raise ValueError(f"Unsupported band: {band!r}")


# ======================================================
# 2.4G AP bring-up (NEW)
# ======================================================

def _set_ap_2g() -> Dict:
    """
    2.4G AP bring-up:
      ifconfig wlan1 192.168.50.100 up
      hostapd -B /var/gm9k_2p4G_test3.conf
    """
    print("[AP] bring-up 2.4G (fixed BW=20 CH=6)")

    _sh("killall hostapd || true", label="killall hostapd", timeout_sec=4)
    _sh("pkill -9 hostapd || true", label="pkill hostapd", timeout_sec=4)
    _sh("rm -rf /var/run/hostapd || true", label="cleanup hostapd socket", timeout_sec=4)

    # hygiene (optional but safe)
    _sh("pkill -9 wpa_supplicant || true", label="kill wpa_supplicant", timeout_sec=3)

    _sh(
        f"ifconfig {AP_IFACE} 192.168.50.100 up || true",
        label="set AP IP (2G)",
        timeout_sec=5,
        retry=1,
    )

    # IMPORTANT: use your exact command style (no -i)
    _sh(
        f"hostapd -B {config.HOSTAPD_CONF_2G_20M}",
        label=f"start hostapd (2G) {config.HOSTAPD_CONF_2G_20M}",
        timeout_sec=8,
        retry=0,
    )

    if not _wait_ap_enabled():
        raise RuntimeError("❌ 2.4G AP bring-up failed (hostapd not ENABLED)")

    wl_status = _sh(
        f"wl -i {AP_IFACE} status || true",
        label="wl status (final, 2G)",
        timeout_sec=5,
    )

    # best-effort parse; but 2G might not show same format on some builds
    final_primary = 6
    try:
        final_primary = _parse_primary_channel(wl_status)
    except Exception:
        pass

    print(f"[AP] 2G Primary={final_primary} status=OK")

    return {
        "band": "2G",
        "bw": 20,
        "requested_channel": 6,
        "final_primary": final_primary,
        "status": "OK",
    }
