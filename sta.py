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
    # 你目前的 config.py 不一定有這些欄位，所以用 try/except 保護
    from config import MSSH_TIMEOUT_SHORT, MSSH_TIMEOUT_RATE  # type: ignore
except Exception:
    MSSH_TIMEOUT_SHORT = 10
    MSSH_TIMEOUT_RATE = 15

try:
    from config import STA_WLAN0_IP  # type: ignore
except Exception:
    # 你在指令中用過的預設值（ASUS Router subnet 常見 192.168.50.0/24）
    STA_WLAN0_IP = "192.168.50.101"


@dataclass
class StaLinkInfo:
    connected: bool
    ssid: Optional[str] = None
    bssid: Optional[str] = None
    freq_mhz: Optional[int] = None
    signal_dbm: Optional[int] = None
    raw: str = ""


def _kill_wpa_supplicant(iface: str) -> None:
    """
    Best-effort cleanup for wpa_supplicant.
    Keep it conservative to avoid breaking existing flows:
      - killall/pkill
      - remove control socket (common issue)
    """
    run_mssh_once("killall wpa_supplicant || true", timeout=MSSH_TIMEOUT_SHORT, ignore_error=True)
    run_mssh_once("pkill -9 wpa_supplicant || true", timeout=MSSH_TIMEOUT_SHORT, ignore_error=True)
    # control socket path can vary; these are typical
    run_mssh_once(f"rm -f /var/run/wpa_supplicant/{iface} || true", timeout=MSSH_TIMEOUT_SHORT, ignore_error=True)
    run_mssh_once("rm -f /var/run/wpa_supplicant/wlan0 || true", timeout=MSSH_TIMEOUT_SHORT, ignore_error=True)


class STARole:
    """
    DUT STA role (wlan0).

    Bring-up flow (based on your validated manual commands):
      - ifconfig wlan0 up
      - wpa_supplicant -B -c/var/wpa_supplicant.conf -iwlan0
      - iw wlan0 link (verify)
      - ifconfig wlan0 <ip>

    Rate control:
      - wl -i wlan0 5g_rate -v <mcs> -b <bw> -s <nss> [--sgi] [--ldpc]
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

    # -----------------------------
    # Basic helpers
    # -----------------------------
    def _cmd(self, cmd: str, timeout: int = MSSH_TIMEOUT_SHORT, ignore_error: bool = False) -> str:
        return run_mssh_once(cmd, timeout=timeout, ignore_error=ignore_error)

    # -----------------------------
    # Public API
    # -----------------------------
    def setup(
        self,
        wait_link: bool = True,
        link_timeout_sec: float = 20.0,
        settle_sec: float = 0.5,
    ) -> Dict:
        """
        Bring up STA and (optionally) wait until associated.
        Returns a dict suitable for logging / debugging.

        Notes:
        - Minimal behavior change vs your version.
        - Sequence aligned closer to your manual golden flow:
            ifconfig up -> wpa_supplicant -> iw link -> ifconfig ip
        """
        print(f"[STA] setup iface={self.iface} ip={self.ip} wpa_conf={self.wpa_conf}")

        # 1) stop existing wpa_supplicant (best-effort, stronger)
        _kill_wpa_supplicant(self.iface)

        # 2) iface up
        self._cmd(f"ifconfig {self.iface} up || true", ignore_error=True)

        # 3) start wpa_supplicant (keep your format but make it more standard/robust)
        # Your original: wpa_supplicant -B -c{conf} -i{iface}
        # Safer: spaces + explicit -c / -i
        self._cmd(
            f"wpa_supplicant -B -c {self.wpa_conf} -i {self.iface}",
            timeout=MSSH_TIMEOUT_SHORT,
            ignore_error=False,
        )

        if settle_sec > 0:
            time.sleep(settle_sec)

        # 4) verify link first (your manual flow checks link before static IP)
        if wait_link:
            info = self.wait_connected(timeout_sec=link_timeout_sec)
        else:
            info = self.get_link_info()

        # 5) configure static IP (keep your flow uses ifconfig <ip>)
        # Even if not connected yet, still apply IP (harmless) — but we report connected status based on iw link.
        self._cmd(f"ifconfig {self.iface} {self.ip} || true", ignore_error=True)

        result = {
            "role": "STA",
            "iface": self.iface,
            "ip": self.ip,
            "connected": bool(info.connected),
            "ssid": info.ssid,
            "bssid": info.bssid,
            "freq_mhz": info.freq_mhz,
            "signal_dbm": info.signal_dbm,
            "raw": info.raw,
        }

        if result["connected"]:
            print("✅ [STA] link up")
        else:
            print("⚠️ [STA] link not connected yet")

        return result

    def teardown(self) -> None:
        """
        Best-effort teardown.
        """
        print(f"[STA] teardown iface={self.iface}")
        _kill_wpa_supplicant(self.iface)

    def get_link_info(self) -> StaLinkInfo:
        """
        Parse `iw <iface> link`.
        """
        out = self._cmd(f"iw {self.iface} link", timeout=MSSH_TIMEOUT_SHORT, ignore_error=True).strip()
        if not out:
            return StaLinkInfo(connected=False, raw="")

        if "Not connected." in out:
            return StaLinkInfo(connected=False, raw=out)

        # Example (typical):
        # Connected to xx:xx:xx:xx:xx:xx (on wlan0)
        # SSID: Garmin-5678
        # freq: 5180
        # signal: -45 dBm
        bssid = None
        ssid = None
        freq = None
        sig = None

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
                    sig_str = s.split("signal:", 1)[1].strip().split()[0]
                    sig = int(sig_str)
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
        """
        Poll `iw link` until connected or timeout.
        """
        deadline = time.time() + timeout_sec
        last = StaLinkInfo(connected=False, raw="")
        while time.time() < deadline:
            last = self.get_link_info()
            if last.connected:
                return last
            time.sleep(poll_sec)
        return last

    def set_rate(
        self,
        mcs: int,
        bw: int,
        nss: int,
        sgi: bool = True,
        ldpc: bool = True,
    ) -> Dict:
        """
        Apply 5G fixed rate on STA interface via wl.

        Command template (your requirement):
          wl -i wlan0 5g_rate -v <mcs> -b <bw> -s <nss> --sgi --ldpc
        """
        if bw not in (20, 40, 80):
            raise ValueError(f"Unsupported BW: {bw}")
        if nss <= 0:
            raise ValueError(f"Invalid NSS: {nss}")
        if mcs < 0:
            raise ValueError(f"Invalid MCS: {mcs}")

        cmd = f"wl -i {self.iface} 5g_rate -v {mcs} -b {bw} -s {nss}"
        if sgi:
            cmd += " --sgi"
        if ldpc:
            cmd += " --ldpc"

        print(f"[STA][RATE] {cmd}")
        out = self._cmd(cmd, timeout=MSSH_TIMEOUT_RATE, ignore_error=False).strip()

        return {
            "role": "STA",
            "iface": self.iface,
            "mcs": mcs,
            "bw": bw,
            "nss": nss,
            "sgi": sgi,
            "ldpc": ldpc,
            "raw": out,
            "status": "OK",
        }


# -------------------------------------------------
# New: Robust STA bring-up helper for STA_TX/STA_RX pipelines
# (Added without breaking existing callers)
# -------------------------------------------------
def sta_prepare(
    iface: str = "wlan0",
    ip: str = STA_WLAN0_IP,
    wpa_conf: str = "/var/wpa_supplicant.conf",
    *,
    retry: int = 3,
    link_timeout_sec: float = 20.0,
    poll_sec: float = 0.5,
    settle_sec: float = 0.5,
    retry_backoff_sec: float = 2.0,
) -> Dict:
    """
    Robust STA bring-up with retries.
    This matches your desired manual flow and is safe to use in STA_RX.

    Steps per attempt:
      - kill wpa_supplicant (best effort)
      - ifconfig <iface> up
      - wpa_supplicant -B ...
      - wait 'iw <iface> link' connected
      - ifconfig <iface> <ip>

    Returns same dict format as STARole.setup()
    Raises RuntimeError after retries.
    """
    role = STARole(iface=iface, wpa_conf=wpa_conf, ip=ip)

    last: Optional[Dict] = None
    for attempt in range(1, retry + 1):
        print(f"[STA] prepare attempt {attempt}/{retry} iface={iface} ip={ip}")

        # Strong cleanup before each attempt
        _kill_wpa_supplicant(iface)

        # Bring interface up (best-effort)
        role._cmd(f"ifconfig {iface} up || true", ignore_error=True)

        try:
            # Start wpa
            role._cmd(
                f"wpa_supplicant -B -c {wpa_conf} -i {iface}",
                timeout=MSSH_TIMEOUT_SHORT,
                ignore_error=False,
            )

            if settle_sec > 0:
                time.sleep(settle_sec)

            # Wait link
            info = role.wait_connected(timeout_sec=link_timeout_sec, poll_sec=poll_sec)

            # Assign IP after link (your golden flow)
            role._cmd(f"ifconfig {iface} {ip} || true", ignore_error=True)

            last = {
                "role": "STA",
                "iface": iface,
                "ip": ip,
                "connected": bool(info.connected),
                "ssid": info.ssid,
                "bssid": info.bssid,
                "freq_mhz": info.freq_mhz,
                "signal_dbm": info.signal_dbm,
                "raw": info.raw,
            }

            if last["connected"]:
                print("✅ [STA] prepare OK")
                return last

            print("⚠️ [STA] prepare not connected (timeout)")

        except Exception as e:
            print(f"⚠️ [STA] prepare exception: {e}")

        time.sleep(retry_backoff_sec)

    raise RuntimeError(f"[STA] prepare failed after {retry} attempts, last={last}")


# -------------------------------------------------
# Backward-compatible functions (keep old callers alive)
# -------------------------------------------------
_default_sta = STARole()


def setup_sta() -> Dict:
    """
    Compatibility wrapper for old code that expects a function.
    """
    return _default_sta.setup()


def verify_sta() -> Dict:
    info = _default_sta.get_link_info()
    return {
        "role": "STA",
        "iface": _default_sta.iface,
        "connected": info.connected,
        "ssid": info.ssid,
        "bssid": info.bssid,
        "freq_mhz": info.freq_mhz,
        "signal_dbm": info.signal_dbm,
        "raw": info.raw,
    }


def set_sta_rate(mcs: int, bw: int, nss: int, sgi: bool = True, ldpc: bool = True) -> Dict:
    return _default_sta.set_rate(mcs=mcs, bw=bw, nss=nss, sgi=sgi, ldpc=ldpc)
