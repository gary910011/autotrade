"""
Prepare DUT AP Tool
===================

Bring up DUT as AP and verify it is ready.
This is an ENVIRONMENT tool, NOT part of throughput flow.
"""

from __future__ import annotations
import time
import sys

from core.dut import run_mssh_once
import config


def wait(seconds: float, reason: str = ""):
    if reason:
        print(f"[WAIT] {reason} ({seconds}s)")
    time.sleep(seconds)


def bringup_dut_ap():
    print("========================================")
    print("[TOOL] PREPARE DUT AP")
    print("========================================")

    # Kill leftovers (idempotent)
    cmds = [
        "killall hostapd || true",
        "pkill -9 hostapd || true",
        "rm -rf /var/run/hostapd",
        "ifconfig wlan1 down || true",
        "ifconfig wlan1 up || true",
    ]
    for c in cmds:
        run_mssh_once(c, ignore_error=True)

    # Bring up AP (assumes your existing runtime conf path)
    conf = getattr(config, "DUT_HOSTAPD_CONF", "/var/hostapd_runtime_wlan1.conf")
    print(f"[DUT] hostapd -B {conf}")
    run_mssh_once(f"hostapd -B {conf}")

    wait(5, "waiting hostapd")

    # Verify AP is up
    out = run_mssh_once("hostapd_cli -i wlan1 status", ignore_error=True)
    print("[DUT] hostapd status\n" + out)

    if "state=ENABLED" not in out:
        raise RuntimeError("DUT AP not enabled")

    print("✅ DUT AP READY")


def main():
    try:
        bringup_dut_ap()
    except Exception as e:
        print(f"❌ FAILED: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
