# core/ap.py
import time
from config import AP_IFACE, AP_IP, AP_NETMASK
from core.dut import run_mssh_once
from core.asus_sta import AsusSTA

def cleanup_hostapd():
    run_mssh_once("killall hostapd || true", ignore_error=True)
    run_mssh_once("pkill -9 hostapd || true", ignore_error=True)
    run_mssh_once("rm -rf /var/run/hostapd", ignore_error=True)

def setup_ap_rx(bw, ch):
    setup_ap(bw)

    asus = AsusSTA(host="192.168.10.1")
    asus.connect_once()
    asus.wait_associated()

    return asus   # 交給 main.py 做 MCS sweep

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


def start_ap(conf_path: str):
    """
    🔒 ONLY place to start hostapd
    """

    print(f"[AP] start AP with conf={conf_path}")

    # 1) stop hostapd
    cleanup_hostapd()

    # 2) bring interface up BEFORE hostapd
    run_mssh_once(
        f"ifconfig {AP_IFACE} {AP_IP} netmask {AP_NETMASK} up"
    )

    # 3) start hostapd LAST
    run_mssh_once(
        f"hostapd -B -i {AP_IFACE} {conf_path}",
        timeout=6,
    )

    # 4) wait ENABLED
    wait_ap_ready()

# ============================================================
# Stable interface for main.py (AP role)
# ============================================================

from config import BW_TEMPLATE


def setup_ap(bw: int):
    """
    Stable AP setup interface expected by main.py.

    Flow:
      - BW -> hostapd conf (via BW_TEMPLATE)
      - start_ap(conf)
      - wait_ap_ready()
    """
    if bw not in BW_TEMPLATE:
        raise ValueError(f"Unsupported BW for AP: {bw}")

    conf_path = BW_TEMPLATE[bw]
    print(f"[AP] setup_ap BW={bw} conf={conf_path}")

    start_ap(conf_path)
