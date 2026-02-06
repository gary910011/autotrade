"""
ASUS CFG Restore Tool
====================

Environment preparation ONLY.
This script is NOT part of throughput pipeline.

Responsibilities:
1) nvram restore CFG
2) reboot (AP/STA both: always reboot after restore, per your request)
3) HARD verification: ASUS ↔ DUT ↔ iperf server data plane ready

Key design decisions (based on your field debug):
- DO NOT trust 'Mode:' in wl status (ASUS/DUT both may lie)
- For ASUS(AP): do NOT rely on wl status SSID. Instead:
    - reboot after nvram restore
    - verify from DUT side by scanning SSID and associating
- For ASUS(STA): reboot after restore + verify valid BSSID on ASUS
- For DUT:
    - always soft reset Wi-Fi state before role switch
    - when target=sta: force DUT AP bring-up (hostapd mandatory)
    - when target=ap: force DUT STA connect to ASUS AP (wpa_supplicant mandatory)

NEW (Band-aware):
- --band 5G/2G affects:
    - target=sta: DUT AP hostapd conf selection
    - target=ap: DUT STA scan/association SSID selection
    - ASUS STA assoc verify iface selection (2G -> eth6, 5G -> eth7)
"""

from __future__ import annotations

import argparse
import sys
import time

from core.asus_ap import AsusAP
from core.dut import run_mssh_once
import config


# ==================================================
# Helpers
# ==================================================

def wait(seconds: float, reason: str = ""):
    if reason:
        print(f"[WAIT] {reason} ({seconds}s)")
    time.sleep(seconds)


def _has_valid_bssid(text: str) -> bool:
    s = (text or "").lower()
    return "bssid:" in s and "00:00:00:00:00:00" not in s


def _dut_scan_has_ssid(iface: str, ssid: str) -> bool:
    cmd = f"iw dev {iface} scan | grep -q '{ssid}' && echo FOUND || echo MISS"
    out = run_mssh_once(cmd, timeout=25, ignore_error=True) or ""
    return "FOUND" in out


def dut_wait_scan_ssid(iface: str, ssid: str, timeout: int = 90, interval: float = 5.0):
    print(f"[WAIT][DUT] scan SSID='{ssid}' on {iface}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok = _dut_scan_has_ssid(iface, ssid)
        print(f"[DUT] scan -> {'FOUND' if ok else 'MISS'}")
        if ok:
            print("✅ DUT can see ASUS AP beacon")
            return
        time.sleep(interval)
    raise RuntimeError(f"DUT scan timeout: cannot find SSID '{ssid}' on {iface}")


def dut_ping(ip: str, retry: int = 15, interval: float = 1.0):
    print(f"[VERIFY][DUT] ping {ip}")
    for i in range(1, retry + 1):
        out = run_mssh_once(
            f"ping -c 1 -W 1 {ip}",
            timeout=6,
            ignore_error=True,
        ) or ""
        if "1 received" in out or "1 packets received" in out:
            print(f"[DUT] ping OK ({i}/{retry})")
            return
        print(f"[DUT] ping retry {i}/{retry}")
        time.sleep(interval)
    raise RuntimeError(f"DUT cannot ping {ip}")


def dut_iperf_dryrun(server: str, port: int):
    if not server:
        print("[SKIP] iperf dryrun: server not configured")
        return

    print(f"[VERIFY][DUT] iperf3 dryrun {server}:{port}")
    out = run_mssh_once(
        f"iperf3 -c {server} -p {port} -t 1",
        timeout=12,
        ignore_error=True,
    ) or ""

    low = out.lower()
    if "no route to host" in low or "network is unreachable" in low:
        raise RuntimeError("iperf dryrun failed: routing not ready")
    if "unable to connect" in low:
        raise RuntimeError("iperf dryrun failed: cannot connect")

    print("[DUT] iperf dryrun OK")


def _asus_iface_for_band(band: str) -> str:
    """
    Decide which ASUS wl interface to query for association status.
    2G -> eth6 (typical), 5G -> eth7 (typical)
    """
    band = (band or "5G").upper()
    if band == "2G":
        return getattr(config, "ASUS_AP_IFACE_2G", "eth6")
    return getattr(config, "ASUS_AP_IFACE_5G", "eth7")


# ==================================================
# DUT Wi-Fi RESET (CRITICAL)
# ==================================================

def dut_wifi_soft_reset():
    """
    Soft reset DUT Wi-Fi state.
    Must be called whenever switching roles.

    Goals:
    - kill hostapd + wpa_supplicant
    - bring down wlan0/wlan1
    - clear common conflicting routes on 192.168.50.0/24
    """
    print("[RESET] DUT Wi-Fi soft reset")

    cmds = [
        "killall hostapd || true",
        "pkill -9 hostapd || true",
        "rm -rf /var/run/hostapd || true",
        "killall wpa_supplicant || true",
        "pkill -9 wpa_supplicant || true",

        "ifconfig wlan0 down || true",
        "ifconfig wlan1 down || true",

        "ip route del 192.168.50.0/24 dev wlan0 2>/dev/null || true",
        "ip route del 192.168.50.0/24 dev wlan1 2>/dev/null || true",
        "ip route del default dev wlan0 2>/dev/null || true",
        "ip route del default dev wlan1 2>/dev/null || true",
        "ip route del default via 192.168.10.2 dev wlan1 2>/dev/null || true",
    ]
    for c in cmds:
        run_mssh_once(c, ignore_error=True)

    time.sleep(2)
    print("✅ DUT Wi-Fi soft reset done")


# ==================================================
# DUT role: AP (when ASUS is STA)  [target=sta]
# ==================================================

def ensure_dut_ap_ready(*, band: str):
    """
    DUT AP (wlan1) mandatory for target=sta (ASUS becomes STA and associates to DUT AP)

    Band-aware:
    - 5G: use config.DUT_HOSTAPD_CONF (legacy default)
    - 2G: use config.HOSTAPD_CONF_2G_20M or config.DUT_HOSTAPD_CONF_2G
    """
    band = (band or "5G").upper()
    print(f"[ENV] Ensuring DUT AP is ready (target=sta, band={band})")

    dut_ap_ip = getattr(config, "DUT_AP_IP", "192.168.50.100")
    dut_net = "192.168.50.0/24"
    dut_mask = "255.255.255.0"

    if band == "2G":
        hostapd_conf = (
            getattr(config, "DUT_HOSTAPD_CONF_2G", None)
            or getattr(config, "HOSTAPD_CONF_2G_20M", None)
            or "/var/gm9k_2p4G_test3.conf"
        )
    else:
        hostapd_conf = getattr(config, "DUT_HOSTAPD_CONF", "/var/gm9k_cw80_test3.conf")

    cmds = [
        "killall hostapd || true",
        "pkill -9 hostapd || true",
        "rm -rf /var/run/hostapd || true",

        "ifconfig wlan1 down || true",
        "ifconfig wlan1 up || true",

        f"ifconfig wlan1 {dut_ap_ip} netmask {dut_mask} up",
        f"ip route replace {dut_net} dev wlan1 src {dut_ap_ip}",

        f"hostapd -B {hostapd_conf}",
    ]
    for c in cmds:
        run_mssh_once(c, ignore_error=True)

    time.sleep(5)

    st = run_mssh_once("wl -i wlan1 status", timeout=10, ignore_error=True) or ""
    print("[DUT] wl status (post-bringup)\n" + st)

    if not any(line.strip().lower().startswith("ssid:") and '""' not in line for line in st.splitlines()):
        raise RuntimeError("DUT AP not ready: SSID not present after hostapd bring-up")

    print(f"✅ DUT AP ready (hostapd running, SSID present) conf={hostapd_conf}")


# ==================================================
# DUT role: STA (when ASUS is AP)  [target=ap]
# ==================================================

def ensure_dut_sta_connect_asus_ap(*, band: str):
    """
    When target=ap (ASUS becomes AP), DUT must connect as STA on wlan0.

    Band-aware SSID:
    - 5G: ASUS_AP_SSID_5G or ASUS_AP_SSID (legacy) default Garmin-5678
    - 2G: ASUS_AP_SSID_2G default Garmin-1234
    """
    band = (band or "5G").upper()
    print(f"[ENV] Ensuring DUT STA connects to ASUS AP (target=ap, band={band})")

    if band == "2G":
        ssid = getattr(config, "ASUS_AP_SSID_2G", "Garmin-1234")
    else:
        ssid = getattr(config, "ASUS_AP_SSID_5G", None) or getattr(config, "ASUS_AP_SSID", "Garmin-5678")

    wpa_conf = getattr(config, "DUT_WPA_CONF", "/var/wpa_supplicant.conf")

    dut_sta_ip = getattr(config, "DUT_STA_IP", "192.168.50.101")
    dut_net = "192.168.50.0/24"
    dut_mask = "255.255.255.0"

    run_mssh_once("ifconfig wlan0 up || true", ignore_error=True)
    dut_wait_scan_ssid("wlan0", ssid, timeout=90, interval=5)

    cmds = [
        "killall wpa_supplicant || true",
        "pkill -9 wpa_supplicant || true",
        "ifconfig wlan0 down || true",
        "ifconfig wlan0 up || true",
        f"wpa_supplicant -B -i wlan0 -c {wpa_conf}",
    ]
    for c in cmds:
        run_mssh_once(c, ignore_error=True)

    print("[WAIT][DUT] wlan0 association")
    deadline = time.time() + 60
    while time.time() < deadline:
        link = run_mssh_once("iw wlan0 link", timeout=8, ignore_error=True) or ""
        print("[DUT] iw wlan0 link\n" + link)
        if "Connected to" in link:
            print("✅ DUT wlan0 associated")
            break
        time.sleep(3)
    else:
        raise RuntimeError("DUT wlan0 association timeout")

    align = [
        f"ifconfig wlan0 {dut_sta_ip} netmask {dut_mask} up",
        f"ip route replace {dut_net} dev wlan0 src {dut_sta_ip}",
        "ip route del 192.168.50.0/24 dev wlan1 2>/dev/null || true",
    ]
    for c in align:
        run_mssh_once(c, ignore_error=True)

    print("[DUT] ip addr show wlan0\n" + (run_mssh_once("ip addr show wlan0", ignore_error=True) or ""))
    print("[DUT] ip route\n" + (run_mssh_once("ip route", ignore_error=True) or ""))


# ==================================================
# ASUS verification (minimal + correct)
# ==================================================

def wait_asus_sta_assoc(ap: AsusAP, *, band: str, timeout: int = 120):
    """
    ASUS is STA (target=sta). Verify association by checking valid BSSID on the correct band interface.
    - 2G: wl -i eth6 status
    - 5G: wl -i eth7 status
    """
    iface = _asus_iface_for_band(band)
    print(f"[WAIT] ASUS STA association (band={band}, iface={iface})")
    deadline = time.time() + timeout

    while time.time() < deadline:
        st = ap.exec(f"wl -i {iface} status", sleep=0)
        print(f"[ASUS] wl -i {iface} status\n" + st)
        if _has_valid_bssid(st):
            print("✅ ASUS STA associated")
            return
        time.sleep(3)

    raise RuntimeError(f"ASUS STA association timeout (band={band}, iface={iface})")


# ==================================================
# Main
# ==================================================

def main():
    p = argparse.ArgumentParser("ASUS CFG restore tool")
    p.add_argument("--target", required=True, choices=["ap", "sta"])
    p.add_argument("--cfg", required=True)
    p.add_argument("--band", choices=["5G", "2G"], default="5G")
    p.add_argument("--reboot", choices=["always", "never"], default="always")
    args = p.parse_args()

    def _wait_asus_ssh_ready(ip: str, timeout: int = 180, interval: float = 2.0):
        print("[WAIT] ASUS SSH ready")
        deadline = time.time() + timeout
        last_err = None

        while time.time() < deadline:
            try:
                a = AsusAP(host=ip)
                _ = a.exec("echo READY", sleep=0)
                a.close()
                print("✅ ASUS SSH ready")
                return
            except Exception as e:
                last_err = e
                time.sleep(interval)

        raise RuntimeError(f"ASUS SSH not ready (timeout). last_err={last_err}")

    def _connect_asus(ip: str, timeout: int = 180):
        _wait_asus_ssh_ready(ip, timeout=timeout, interval=2.0)
        return AsusAP(host=ip)

    asus_ip = getattr(config, "ASUS_AP_IP", None) or getattr(config, "ASUS_AP_HOST", None)
    if not asus_ip:
        raise RuntimeError("ASUS_AP_IP not configured")

    iperf_ap = getattr(config, "IPERF_SERVER_AP_TX", "")
    iperf_sta = getattr(config, "IPERF_SERVER_STA_TX", "")

    print("========================================")
    print("[TOOL] ASUS CFG RESTORE")
    print(f" target = {args.target}")
    print(f" band   = {args.band}")
    print(f" cfg    = {args.cfg}")
    print(f" asusIP = {asus_ip}")
    print("========================================")

    # Always sanitize DUT state
    dut_wifi_soft_reset()

    # Pre-condition: if ASUS will become STA, DUT AP must exist before reboot
    if args.target == "sta":
        ensure_dut_ap_ready(band=args.band)

    # Restore + reboot
    ap = _connect_asus(asus_ip, timeout=180)
    ap.exec(f"nvram restore {args.cfg}", sleep=1)

    if args.reboot == "always":
        print("[TOOL] reboot = True")
        try:
            ap.exec("reboot", sleep=0)
        except Exception as e:
            print(f"[INFO] reboot SSH drop (expected): {e}")

        try:
            ap.close()
        except Exception:
            pass

        wait(80, "waiting ASUS reboot")
        ap = _connect_asus(asus_ip, timeout=180)
        wait(10, "post reboot settle")
    else:
        print("[TOOL] reboot = False")

    # Post-restore bring-up
    print("========== POST-RESTORE BRING-UP ==========")
    if args.target == "sta":
        wait_asus_sta_assoc(ap, band=args.band, timeout=120)
    else:
        ensure_dut_sta_connect_asus_ap(band=args.band)

    # Data-plane verify
    print("========== VERIFY DATA PLANE ==========")
    if args.target == "ap":
        dut_ping(asus_ip)
        dut_iperf_dryrun(iperf_sta, getattr(config, "IPERF_PORT_STA_TX", 5201))
    else:
        if not iperf_ap:
            raise RuntimeError("IPERF_SERVER_AP_TX not configured (required for target=sta)")
        dut_ping(iperf_ap)
        dut_iperf_dryrun(iperf_ap, getattr(config, "IPERF_PORT_AP_TX", 5201))

    print("========================================")
    print("✅ ENVIRONMENT READY — main.py can run")
    print("========================================")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\n❌ FAILED: {e}")
        sys.exit(1)
