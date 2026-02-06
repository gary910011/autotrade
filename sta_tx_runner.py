# core/sta_tx_runner.py
from __future__ import annotations

import time
from typing import Dict

from config import (
    ASUS_AP_HOST, ASUS_AP_USER, ASUS_AP_PASS, ASUS_AP_PORT, ASUS_AP_IFACE_5G,
    ASUS_AP_APPLY_WAIT_SEC,
    STA_IFACE,
    TEST_BW_LIST, TEST_CHANNEL_LIST, TEST_MCS_TABLE,
    IPERF_DURATION,
    LOG_DIR,
)
from core.dut import run_mssh_once, stop_all_iperf_clients
from core.iperf import run_iperf_client_stream
from utils.logger import TputLogger


# -------------------------------------------------
# DUT helpers
# -------------------------------------------------

def _dut_wait_link_up(timeout_sec: float = 30.0) -> Dict:
    start = time.time()
    last = ""
    while time.time() - start < timeout_sec:
        try:
            last = run_mssh_once(
                f"iw {STA_IFACE} link",
                timeout=5,
                ignore_error=True,
            ) or ""
            if "Connected to" in last:
                return {"connected": True, "raw": last}
        except Exception:
            pass
        time.sleep(0.5)
    return {"connected": False, "raw": last}


def _dut_sta_setup_once() -> Dict:
    from core.sta import STARole
    sta = STARole()
    return sta.setup(wait_link=True, link_timeout_sec=30)


def _dut_kill_iperf() -> None:
    stop_all_iperf_clients()


def _dut_set_rate_and_log(logger: TputLogger, bw: int, mcs: int) -> None:
    cmds = [
        f"wl -i {STA_IFACE} 5g_rate -v {mcs} -b {bw} -s 2 --sgi --ldpc",
        f"wl -i {STA_IFACE} nrate",
        f"wl -i {STA_IFACE} rate",
    ]

    logger.write("# ===== DUT RATE CONFIG =====")
    for c in cmds:
        out = run_mssh_once(c, timeout=5, ignore_error=True) or ""
        logger.write(f"$ {c}")
        if out.strip():
            logger.write(out)
    logger.write("")


# -------------------------------------------------
# STA_TX production runner
# -------------------------------------------------

def run_sta_tx_production() -> None:
    print("=== MODE: STA_TX (PRODUCTION) ===")

    # 0) STA connect once
    sta_info = _dut_sta_setup_once()
    print(f"[STA_TX] STA setup: {sta_info}")
    if not sta_info.get("connected", False):
        raise RuntimeError("STA not connected, abort STA_TX")

    # 1) Create run-level logger directory ONCE
    run_logger = TputLogger.create_run_dir(LOG_DIR)

    # 2) ASUS AP controller
    from core.asus_ap import AsusAP
    ap = AsusAP(
        host=ASUS_AP_HOST,
        user=ASUS_AP_USER,
        password=ASUS_AP_PASS,
        port=ASUS_AP_PORT,
        iface_5g=ASUS_AP_IFACE_5G,
    )

    try:
        for bw in TEST_BW_LIST:
            for ch in TEST_CHANNEL_LIST:
                print(f"\n[STA_TX] ASUS_AP set CH={ch}, BW={bw}")
                r_ap = ap.set_5g(channel=ch, bw=bw)
                print(f"[STA_TX] ASUS_AP set_5g: {r_ap}")

                time.sleep(max(1.0, ASUS_AP_APPLY_WAIT_SEC))

                link = _dut_wait_link_up(timeout_sec=30)
                if not link.get("connected", False):
                    raise RuntimeError(
                        f"STA link down after AP change (CH={ch} BW={bw})"
                    )

                for mcs in TEST_MCS_TABLE[bw]:
                    print(f"\n[STA_TX] DUT set RATE: BW={bw}, MCS={mcs}")

                    _dut_kill_iperf()

                    logger = TputLogger(
                        base_dir=run_logger,
                        band="5G",
                        bw_mhz=bw,
                        mode="STA",
                        direction="TX",
                        channel=ch,
                        mcs=mcs,
                    )
                    log_path = logger.create()
                    print(f"[LOG] saving to {log_path}")

                    logger.write_header([
                        "# ===== META =====",
                        "# MODE=STA_TX",
                        f"# ASUS_AP={ASUS_AP_HOST}",
                        f"# AP_CH={ch}",
                        f"# AP_BW={bw}",
                        f"# DUT_IFACE={STA_IFACE}",
                        f"# IPERF_DURATION={IPERF_DURATION}",
                        f"# START_TS={time.strftime('%Y-%m-%d %H:%M:%S')}",
                        "",
                        "# ===== ASUS AP CONFIG =====",
                        r_ap.get("raw", "").strip(),
                        "",
                    ])

                    # DUT rate config (TX side)
                    _dut_set_rate_and_log(logger, bw=bw, mcs=mcs)

                    logger.write("# ===== IPERF =====")

                    try:
                        for line in run_iperf_client_stream(
                            duration=IPERF_DURATION
                        ):
                            print(line, end="")
                            logger.write(line.rstrip("\n"))
                    finally:
                        logger.close()
                        _dut_kill_iperf()

    except KeyboardInterrupt:
        print("\n🛑 [STA_TX] Stop requested by user")
        _dut_kill_iperf()

    finally:
        try:
            ap.close()
        except Exception:
            pass

    print("=== STA_TX DONE ===")
