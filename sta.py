# core/sta.py
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Dict

from core.dut import run_mssh_once

# -----------------------------
# Optional config integration
# -----------------------------
try:
    from config import MSSH_TIMEOUT_SHORT, MSSH_TIMEOUT_RATE  # type: ignore
except Exception:
    MSSH_TIMEOUT_SHORT = 10
    MSSH_TIMEOUT_RATE = 15

try:
    from config import STA_WLAN0_IP  # type: ignore
except Exception:
    STA_WLAN0_IP = "192.168.50.101"


@dataclass
class StaLinkInfo:
    connected: bool
    ssid: Optional[str] = None
    bssid: Optional[str] = None
    freq_mhz: Optional[int] = None
    signal_dbm: Optional[int] = None
    raw: str = ""


# =========================================================
# Low-level helpers
# =========================================================
def _kill_wpa_supplicant(iface: str) -> None:
    """
    Best-effort cleanup for wpa_supplicant.
    Used ONLY before a fresh bring-up.
    """
    run_mssh_once("killall wpa_supplicant || true", timeout=MSSH_TIMEOUT_SHORT, ignore_error=True)
    run_mssh_once("pkill -9 wpa_supplicant || true", timeout=MSSH_TIMEOUT_SHORT, ignore_error=True)
    run_mssh_once(f"rm -f /var/run/wpa_supplicant/{iface} || true", timeout=MSSH_TIMEOUT_SHORT, ignore_error=True)
    run_mssh_once("rm -f /var/run/wpa_supplicant/wlan0 || true", timeout=MSSH_TIMEOUT_SHORT, ignore_error=True)


# =========================================================
# STA role object
# =========================================================
class STARole:
    """
    DUT STA role (wlan0).

    IMPORTANT:
    - STA_TX must use legacy async bring-up (see sta_bringup_legacy)
    - DO NOT add retry/poll logic into legacy path
    """

    def __init__(
        self,
        iface: str = "wlan0",
        wpa_conf: str = "/var/wpa_supplicant.conf",
        ip: str = STA_WLAN0_IP,
    ):
        self.iface = iface
        self.wpa_conf = wpa_conf
        self.ip = ip

    def _cmd(self, cmd: str, timeout: int = MSSH_TIMEOUT_SHORT, ignore_error: bool = False) -> str:
        return run_mssh_once(cmd, timeout=timeout, ignore_error=ignore_error)

    # -----------------------------
    # Link helpers (read-only)
    # -----------------------------
    def get_link_info(self) -> StaLinkInfo:
        out = self._cmd(f"iw {self.iface} link", ignore_error=True).strip()
        if not out or "Not connected." in out:
            return StaLinkInfo(connected=False, raw=out)

        ssid = bssid = None
        freq = sig = None

        for ln in out.splitlines():
            s = ln.strip()
            if s.startswith("Connected to "):
                parts = s.split()
                if len(parts) >= 3:
                    bssid = parts[2]
            elif s.startswith("SSID:"):
                ssid = s.split("SSID:", 1)[1].strip()
            elif s.startswith("freq:"):
                try:
                    freq = int(s.split("freq:", 1)[1].strip())
                except Exception:
                    pass
            elif s.startswith("signal:"):
                try:
                    sig = int(s.split("signal:", 1)[1].split()[0])
                except Exception:
                    pass

        return StaLinkInfo(
            connected=True,
            ssid=ssid,
            bssid=bssid,
            freq_mhz=freq,
            signal_dbm=sig,
            raw=out,
        )

    def wait_connected(self, timeout_sec: float = 20.0, poll_sec: float = 0.5) -> StaLinkInfo:
        deadline = time.time() + timeout_sec
        last = StaLinkInfo(connected=False, raw="")
        while time.time() < deadline:
            last = self.get_link_info()
            if last.connected:
                return last
            time.sleep(poll_sec)
        return last

    # -----------------------------
    # Rate control
    # -----------------------------
    def set_rate(
        self,
        mcs: int,
        bw: int,
        nss: int,
        sgi: bool = True,
        ldpc: bool = True,
    ) -> Dict:
        cmd = f"wl -i {self.iface} 5g_rate -v {mcs} -b {bw} -s {nss}"
        if sgi:
            cmd += " --sgi"
        if ldpc:
            cmd += " --ldpc"

        print(f"[STA][RATE] {cmd}")
        out = self._cmd(cmd, timeout=MSSH_TIMEOUT_RATE, ignore_error=False)

        return {
            "role": "STA",
            "iface": self.iface,
            "mcs": mcs,
            "bw": bw,
            "nss": nss,
            "sgi": sgi,
            "ldpc": ldpc,
            "raw": out,
        }


# =========================================================
# LEGACY bring-up (PROVEN STABLE)
# =========================================================
def sta_bringup_legacy(
    *,
    iface: str = "wlan0",
    ip: str = STA_WLAN0_IP,
    wpa_conf: str = "/var/wpa_supplicant.conf",
    settle_sec: int = 5,
) -> Dict:
    """
    Legacy async STA bring-up.

    THIS IS INTENTIONAL.
    - No retry
    - No poll
    - No kill-loop
    - No foreground / PTY tricks

    Matches manual golden flow exactly:
      ifconfig up
      wpa_supplicant -B ...
      (wait)
      ifconfig ip
    """

    run_mssh_once("killall wpa_supplicant || true", ignore_error=True)
    run_mssh_once(f"ifconfig {iface} up", ignore_error=False)
    run_mssh_once(f"wpa_supplicant -B -c {wpa_conf} -i {iface}", ignore_error=False)

    time.sleep(settle_sec)

    run_mssh_once(f"ifconfig {iface} {ip}", ignore_error=False)

    link = run_mssh_once(f"iw {iface} link", ignore_error=True)

    return {
        "role": "STA",
        "iface": iface,
        "ip": ip,
        "connected": bool(link and "Connected to" in link),
        "raw": link,
    }


# =========================================================
# Robust STA prepare (KEEP for STA_RX only)
# =========================================================
def sta_prepare(
    iface: str = "wlan0",
    ip: str = STA_WLAN0_IP,
    wpa_conf: str = "/var/wpa_supplicant.conf",
    *,
    retry: int = 3,
    link_timeout_sec: float = 20.0,
    poll_sec: float = 0.5,
    settle_sec: float = 2.0,          # ✅ 新增：與 iperf.py 相容
    retry_backoff_sec: float = 2.0,   # ✅ 新增：未來擴充也不會炸
    reuse_if_connected: bool = False, # ✅ 新增：保持介面一致
) -> Dict:
    """
    Robust STA bring-up with retry.
    USE ONLY FOR STA_RX.
    """

    role = STARole(iface=iface, wpa_conf=wpa_conf, ip=ip)
    last: Optional[Dict] = None

    for attempt in range(1, retry + 1):
        print(f"[STA] prepare attempt {attempt}/{retry}")

        # === clean start ===
        _kill_wpa_supplicant(iface)
        role._cmd(f"ifconfig {iface} up || true", ignore_error=True)

        # === start wpa_supplicant (legacy / tolerant) ===
        role._cmd(
            f"wpa_supplicant -B -c {wpa_conf} -i {iface}",
            ignore_error=True,
        )

        # === wait for link ===
        info = role.wait_connected(
            timeout_sec=link_timeout_sec,
            poll_sec=poll_sec,
        )

        # === give driver / firmware time to settle ===
        if settle_sec > 0:
            time.sleep(settle_sec)

        # === assign IP (legacy style, do not fail hard) ===
        role._cmd(f"ifconfig {iface} {ip} || true", ignore_error=True)

        last = {
            "role": "STA",
            "iface": iface,
            "ip": ip,
            "connected": info.connected,
            "raw": info.raw,
        }

        if info.connected:
            return last

        # === retry backoff (if any) ===
        if retry_backoff_sec > 0:
            time.sleep(retry_backoff_sec)

    raise RuntimeError(f"[STA] prepare failed after {retry} attempts, last={last}")



# =========================================================
# Backward compatibility
# =========================================================
_default_sta = STARole()


def setup_sta() -> Dict:
    return sta_bringup_legacy(
        iface=_default_sta.iface,
        ip=_default_sta.ip,
        wpa_conf=_default_sta.wpa_conf,
    )


def set_sta_rate(mcs: int, bw: int, nss: int, sgi: bool = True, ldpc: bool = True) -> Dict:
    return _default_sta.set_rate(mcs=mcs, bw=bw, nss=nss, sgi=sgi, ldpc=ldpc)


def sta_prepare_tx_once(
    iface: str = "wlan0",
    ip: str = STA_WLAN0_IP,
    wpa_conf: str = "/var/wpa_supplicant.conf",
    *,
    settle_sec: int = 5,
) -> Dict:
    """
    STA_TX legacy fast path
    - EXACT manual flow
    - NO retry
    - NO poll
    """

    run_mssh_once("killall wpa_supplicant || true", ignore_error=True)
    run_mssh_once(f"ifconfig {iface} up", ignore_error=False)
    run_mssh_once(f"wpa_supplicant -B -c {wpa_conf} -i {iface}", ignore_error=False)

    time.sleep(settle_sec)

    run_mssh_once(f"ifconfig {iface} {ip}", ignore_error=False)

    link = run_mssh_once(f"iw {iface} link", ignore_error=True)

    return {
        "role": "STA",
        "iface": iface,
        "ip": ip,
        # ⚠ STA_TX 不嚴格要求
        "connected": bool(link and "Connected to" in link),
        "raw": link,
    }

# =========================================================
# NEW: STA band-aware wpa_supplicant.conf updater
# =========================================================

import pathlib
import tempfile
import shutil
import subprocess


def update_and_upload_wpa_conf_for_band(
    *,
    band: str,
    local_conf_path: str,
    remote_conf_path: str = "/var/wpa_supplicant.conf",
) -> None:
    """
    Update SSID in wpa_supplicant.conf according to band, then upload to DUT.

    Band mapping (per your rule):
      - 2G -> ssid="Garmin-1234"
      - 5G -> ssid="Garmin-5678"
    """

    if band == "2G":
        target_ssid = "Garmin-1234"
    elif band == "5G":
        target_ssid = "Garmin-5678"
    else:
        raise ValueError(f"Unsupported band: {band}")

    src = pathlib.Path(local_conf_path)
    if not src.exists():
        raise FileNotFoundError(f"wpa_supplicant.conf not found: {src}")

    text = src.read_text(encoding="utf-8", errors="ignore")

    # very strict replace: only ssid="..."
    new_text, n = re.subn(
        r'ssid\s*=\s*".*?"',
        f'ssid="{target_ssid}"',
        text,
        count=1,
    )

    if n == 0:
        raise RuntimeError("No ssid=\"...\" entry found in wpa_supplicant.conf")

    # write to temp file
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tmp:
        tmp.write(new_text)
        tmp_path = tmp.name

    try:
        print(f"[STA][WPA] band={band} → ssid=\"{target_ssid}\"")
        print(f"[STA][WPA] upload {tmp_path} → DUT:{remote_conf_path}")

        # mscp upload
        subprocess.check_call(
            [
                "mscp",
                tmp_path,
                f"{config.DUT_HOST}:{remote_conf_path}",
            ]
        )
    finally:
        try:
            pathlib.Path(tmp_path).unlink()
        except Exception:
            pass
