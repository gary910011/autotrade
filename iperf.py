# core/iperf.py
from __future__ import annotations

from typing import Optional, Dict, Literal, List
from pathlib import Path
from datetime import datetime

import config
from core.dut import run_mssh_stream, stop_all_iperf_clients
from core.sta import sta_prepare, set_sta_rate
from utils.logger import TputLogger

# ==================================================
# AP_TX warm-up settings (legacy, kept for compatibility)
# ==================================================
AP_TX_WARMUP_SEC = 5

StaDirection = Literal["TX", "RX"]

# ==================================================
# Run-level directory (created once per execution)
# ==================================================
_RUN_DIR: Optional[Path] = None


def _get_run_dir() -> Path:
    global _RUN_DIR

    if _RUN_DIR is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        _RUN_DIR = Path(config.LOG_DIR) / f"run_{ts}"
        _RUN_DIR.mkdir(parents=True, exist_ok=True)
        print(f"[RUN] log root = {_RUN_DIR}")

    return _RUN_DIR


# ==================================================
# Shared helper
# ==================================================
def _create_logger(
    *,
    role: str,
    direction: str,
    bw: int,
    channel: int,
    mcs: int,
    nss: int,
    server: str,
    duration: int,
    preamble: Optional[List[str]],
) -> TputLogger:

    run_dir = _get_run_dir()

    logger = TputLogger(
        base_dir=run_dir,
        band="5G",
        bw_mhz=bw,
        mode=role,
        direction=direction,
        channel=channel,
        mcs=mcs,
    )
    logger.create()

    header = [
        f"# MODE={role}_{direction}",
        f"# BW={bw}MHz CH={channel} MCS={mcs} NSS={nss}",
        f"# SERVER={server}",
        f"# DURATION={duration}s",
        f"# START_TIME={datetime.now().isoformat()}",
    ]
    logger.write_header(header)

    if preamble:
        logger.write("")
        logger.write("# ===== PREAMBLE =====")
        for line in preamble:
            logger.write(line)
        logger.write("# ====================")
        logger.write("")

    return logger


# ==================================================
# STA (TX / RX)
# ==================================================
def run_iperf_sta(
    *,
    direction: StaDirection,
    bw: int,
    channel: int,
    mcs: int,
    duration: int,
    nss: int = 2,
    preamble: Optional[List[str]] = None,
) -> Dict:

    print(f"[STA_{direction}] BW={bw} CH={channel} MCS={mcs}")

    sta_info = sta_prepare(
        iface=config.STA_IFACE,
        ip=config.STA_IP,
        wpa_conf=config.STA_WPA_CONF,
        retry=3,
        link_timeout_sec=20.0,
    )
    if not sta_info.get("connected"):
        raise RuntimeError("[STA] not connected")

    if direction == "TX":
        set_sta_rate(mcs=mcs, bw=bw, nss=nss)

    if direction == "TX":
        server_ip = config.IPERF_SERVER_STA_TX
        server_port = config.IPERF_PORT_STA_TX
        reverse = ""
    else:
        server_ip = config.IPERF_SERVER_STA_RX
        server_port = config.IPERF_PORT_STA_RX
        reverse = "-R"

    cmd = (
        f"iperf3 --forceflush "
        f"-c {server_ip} -p {server_port} "
        f"-i 1 -t {duration} {reverse}"
    ).strip()

    stop_all_iperf_clients()

    logger = _create_logger(
        role="STA",
        direction=direction,
        bw=bw,
        channel=channel,
        mcs=mcs,
        nss=nss,
        server=f"{server_ip}:{server_port}",
        duration=duration,
        preamble=preamble,
    )

    for line in run_mssh_stream(cmd):
        line = line.rstrip()
        if line:
            print(f"[iPerf] {line}")
            logger.write(line)

    logger.close()
    return {"ok": True}


def run_iperf_sta_tx(bw: int, channel: int, mcs: int, duration: int) -> Dict:
    return run_iperf_sta(
        direction="TX",
        bw=bw,
        channel=channel,
        mcs=mcs,
        duration=duration,
    )


def run_iperf_sta_rx(bw: int, channel: int, mcs: int, duration: int) -> Dict:
    return run_iperf_sta(
        direction="RX",
        bw=bw,
        channel=channel,
        mcs=mcs,
        duration=duration,
    )


# ==================================================
# AP (TX / RX)
# ==================================================
def run_iperf_ap(
    *,
    direction: StaDirection,
    bw: int,
    channel: int,
    mcs: int,
    duration: int,
    nss: int = 2,
    preamble: Optional[List[str]] = None,
    warmup: bool = False,   # ← 新增：不影響舊呼叫
) -> Dict:

    print(f"[AP_{direction}] BW={bw} CH={channel} MCS={mcs}")

    server_ip = config.IPERF_SERVER_AP_TX
    server_port = config.IPERF_PORT_AP_TX
    reverse = "-R" if direction == "RX" else ""

    cmd = (
        f"iperf3 --forceflush "
        f"-c {server_ip} -p {server_port} "
        f"-i 1 -t {duration} {reverse}"
    ).strip()

    # --------------------------------------------------
    # WARM-UP MODE (no logging)
    # --------------------------------------------------
    if warmup:
        stop_all_iperf_clients()
        for _ in run_mssh_stream(cmd):
            pass
        stop_all_iperf_clients()
        print("[AP][WARMUP] done")
        return {"warmup": True}

    # --------------------------------------------------
    # REAL TEST
    # --------------------------------------------------
    stop_all_iperf_clients()

    logger = _create_logger(
        role="AP",
        direction=direction,
        bw=bw,
        channel=channel,
        mcs=mcs,
        nss=nss,
        server=f"{server_ip}:{server_port}",
        duration=duration,
        preamble=preamble,
    )

    for line in run_mssh_stream(cmd):
        line = line.rstrip()
        if line:
            print(f"[iPerf] {line}")
            logger.write(line)

    logger.close()
    return {"ok": True}


# ==================================================
# Legacy AP_TX warm-up (kept as-is)
# ==================================================
def warmup_iperf_ap_tx(
    *,
    server_ip: str,
    server_port: int,
    warmup_sec: int,
):
    if warmup_sec <= 0:
        return

    print(f"[AP_TX][WARMUP] {warmup_sec}s to stabilize link/rate")

    cmd = (
        f"iperf3 --forceflush "
        f"-c {server_ip} -p {server_port} "
        f"-i 1 -t {warmup_sec}"
    )

    stop_all_iperf_clients()
    for _ in run_mssh_stream(cmd):
        pass
    stop_all_iperf_clients()

    print("[AP_TX][WARMUP] done")


def run_iperf_ap_tx(bw: int, channel: int, mcs: int, duration: int) -> Dict:
    return run_iperf_ap(
        direction="TX",
        bw=bw,
        channel=channel,
        mcs=mcs,
        duration=duration,
    )


def run_iperf_ap_rx(bw: int, channel: int, mcs: int, duration: int) -> Dict:
    return run_iperf_ap(
        direction="RX",
        bw=bw,
        channel=channel,
        mcs=mcs,
        duration=duration,
    )
