# core/ap.py
from __future__ import annotations

import time
from typing import Dict

from config import AP_IFACE, AP_IP, AP_NETMASK, BW_TEMPLATE
from core.dut import run_mssh_once
from core.asus_sta import AsusSTA
from core import wifi_channel


# ============================================================
# Common helpers
# ============================================================

def cleanup_hostapd():
    run_mssh_once("killall hostapd || true", ignore_error=True)
    run_mssh_once("pkill -9 hostapd || true", ignore_error=True)
    run_mssh_once("rm -rf /var/run/hostapd", ignore_error=True)


def wait_ap_ready(timeout: float = 8.0):
    start = time.time()
    last = ""
    while time.time() - start < timeout:
        try:
            last = run_mssh_once(
                f"hostapd_cli -i {AP_IFACE} status",
                timeout=3,
            )
            if "state=ENABLED" in last:
                print("✅ AP is ready")
                return
        except Exception:
            pass
        time.sleep(0.5)

    raise RuntimeError(
        f"❌ hostapd not ENABLED (last={last.splitlines()[0] if last else 'EMPTY'})"
    )


def _bring_iface_up():
    run_mssh_once(
        f"ifconfig {AP_IFACE} {AP_IP} netmask {AP_NETMASK} up",
        ignore_error=True,
    )


# ============================================================
# 5 GHz AP path (保持原行為)
# ============================================================

def _setup_ap_5g(*, bw: int, ch: int) -> Dict:
    """
    Original 5 GHz AP bring-up path.
    """
    print(f"[AP][5G] setup BW={bw} CH={ch}")

    return wifi_channel.set_ap_channel_and_bw(
        bw=bw,
        ch=ch,
    )


# ============================================================
# 2.4 GHz AP path（新增，不影響 5G）
# ============================================================

def _setup_ap_2g(*, ch: int) -> Dict:
    """
    2.4 GHz AP bring-up path.
    - BW 固定 20
    - Channel 通常固定 6（由 hostapd conf 決定）
    - 不使用 wl chanspec
    """
    print("[AP][2G] setup BW=20 CH=6 (forced)")

    cleanup_hostapd()
    _bring_iface_up()

    # 直接用你指定的 2.4G hostapd conf
    run_mssh_once(
        f"hostapd -B {wifi_channel.config.HOSTAPD_CONF_2G_20M}",
        timeout=6,
    )

    wait_ap_ready()

    return {
        "band": "2G",
        "bw": 20,
        "channel": ch,
        "status": "OK",
    }


# ============================================================
# Public API (唯一 AP 入口，給 main.py 用)
# ============================================================

def setup_ap(*, band: str, bw: int, ch: int) -> Dict:
    """
    Unified AP setup entry.

    band:
      - "5G" → 原本行為
      - "2G" → 新增 2.4 GHz AP
    """
    band = band.upper()

    if band == "5G":
        return _setup_ap_5g(bw=bw, ch=ch)

    if band == "2G":
        return _setup_ap_2g(ch=ch)

    raise ValueError(f"Unsupported band for AP: {band}")


# ============================================================
# AP_RX helper（保持原語意，僅補 band）
# ============================================================

def setup_ap_rx(*, band: str, bw: int, ch: int):
    """
    AP_RX flow:
      - setup AP
      - wait ASUS (STA) associate
      - return AsusSTA handle for RX rate sweep
    """
    setup_ap(band=band, bw=bw, ch=ch)

    asus = AsusSTA(host="192.168.50.1")
    asus.connect_once()
    asus.wait_associated()

    return asus
