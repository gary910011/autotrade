# core/sta_phy.py
from core.dut import run_mssh_once


def get_sta_phy_snapshot(iface: str = "wlan0") -> dict:
    """
    Snapshot STA PHY state during traffic.
    """
    out = {}

    try:
        out["iw_link"] = run_mssh_once(f"iw {iface} link").strip()
    except Exception as e:
        out["iw_link"] = f"ERR: {e}"

    try:
        out["nrate"] = run_mssh_once(f"wl -i {iface} nrate").strip()
    except Exception as e:
        out["nrate"] = f"ERR: {e}"

    try:
        out["rate"] = run_mssh_once(f"wl -i {iface} rate").strip()
    except Exception as e:
        out["rate"] = f"ERR: {e}"

    return out
