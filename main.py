from __future__ import annotations

import time
import argparse
import socket
from typing import List, Optional

import config

from core.asus_ap import AsusAP          # ASUS = AP (STA_* modes)
from core.asus_pc import AsusPC          # PC-side control (ASUS = STA in AP_* RX rate lock)
from core.sta import STARole
from core.iperf import (
    run_iperf_sta_tx,
    run_iperf_sta_rx,
    run_iperf_ap,
)
from core.dut import (
    run_mssh_once,
    run_mssh_stream,
    stop_all_iperf_clients,
)
from core import wifi_channel


# ==================================================
# MODE dispatch table
# ==================================================

MODE_TABLE = {
    "AP_TX": {
        "need_dut_ap": True,
        "need_asus_ch_bw": False,
        "need_asus_rate": False,
        "need_sta_prepare": False,
        "direction": "TX",
    },
    "AP_RX": {
        "need_dut_ap": True,
        "need_asus_ch_bw": False,
        "need_asus_rate": False,
        "need_sta_prepare": False,
        "direction": "RX",
    },
    "STA_TX": {
        "need_dut_ap": False,
        "need_asus_ch_bw": True,
        "need_asus_rate": False,
        "need_sta_prepare": True,
        "runner": run_iperf_sta_tx,
    },
    "STA_RX": {
        "need_dut_ap": False,
        "need_asus_ch_bw": True,
        "need_asus_rate": True,
        "need_sta_prepare": True,
        "runner": run_iperf_sta_rx,
    },
}


# ==================================================
# Interactive helpers
# ==================================================

def ask(prompt: str, choices: dict):
    while True:
        print(prompt)
        for k, v in choices.items():
            print(f"{k}) {v}")
        sel = input("> ").strip()
        if sel in choices:
            return choices[sel]
        print("Invalid choice, try again.\n")


def interactive_args() -> argparse.Namespace:
    print("\n=== Wi-Fi Throughput Test (Interactive Mode) ===\n")

    role = ask("Select role:", {"1": "AP", "2": "STA"})
    direction = ask("Select direction:", {"1": "TX", "2": "RX"})
    mode = f"{role}_{direction}"

    bw_sel = ask(
        "Select bandwidth:",
        {"1": "ALL", "2": 20, "3": 40, "4": 80},
    )
    bw = [20, 40, 80] if bw_sel == "ALL" else [bw_sel]

    ch_sel = ask(
        "Select channel:",
        {"1": "ALL", "2": 36, "3": 149},
    )
    ch = [36, 149] if ch_sel == "ALL" else [ch_sel]

    mcs_sel = ask(
        "Select MCS:",
        {"1": "ALL", "2": "SINGLE"},
    )

    if mcs_sel == "ALL":
        mcs = "auto"
    else:
        while True:
            mcs = input("Enter MCS (0-9): ").strip()
            if mcs.isdigit() and 0 <= int(mcs) <= 9:
                break
            print("Invalid MCS, must be 0~9")

    dur = input(f"Duration (sec) [default {config.IPERF_DURATION}]: ").strip()
    duration = int(dur) if dur else config.IPERF_DURATION

    return argparse.Namespace(
        mode=mode,
        bw=bw,
        ch=ch,
        mcs=mcs,
        duration=duration,
    )


def wait_sta_connected(timeout=20):
    print("[AP] wait for STA association...")
    for sec in range(timeout):
        out = run_mssh_once(f"wl -i {config.AP_IFACE} assoclist || true")
        if out.strip():
            print(f"[AP] STA associated (after {sec+1}s)")
            return True
        time.sleep(1)
    raise RuntimeError("STA not associated after AP bring-up")


# ==================================================
# CLI parser
# ==================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wi-Fi Throughput Automation")
    parser.add_argument("--mode", choices=MODE_TABLE.keys())
    parser.add_argument("--bw", nargs="+", type=int, choices=[20, 40, 80])
    parser.add_argument("--ch", nargs="+", type=int, choices=[36, 149])
    parser.add_argument("--mcs", default="auto")
    parser.add_argument("--duration", type=int, default=config.IPERF_DURATION)

    args = parser.parse_args()

    if args.mode is None:
        return interactive_args()

    if args.bw is None:
        args.bw = [20, 40, 80]
    if args.ch is None:
        args.ch = [36, 149]

    return args


# ==================================================
# Utilities
# ==================================================

def parse_mcs_list(bw: int, mcs_arg: str) -> List[int]:
    if mcs_arg != "auto":
        return [int(mcs_arg)]
    if bw == 20:
        return list(range(8, -1, -1))
    return list(range(9, -1, -1))


def _wait_tcp_port(host: str, port: int, timeout_sec: int = 30, interval_sec: float = 1.0) -> bool:
    """
    Wait until (host, port) is reachable from THIS PC.
    This is the correct barrier for 'PC can SSH into ASUS'.
    """
    deadline = time.time() + timeout_sec
    last_err = None

    while time.time() < deadline:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        try:
            s.connect((host, port))
            s.close()
            return True
        except OSError as e:
            last_err = e
        finally:
            try:
                s.close()
            except Exception:
                pass

        time.sleep(interval_sec)

    print(f"[ASUS-PC][WAIT] TCP {host}:{port} not reachable after {timeout_sec}s (last_err={last_err})")
    return False


def _asus_pc_set_rate_with_retry(
    asus_pc: AsusPC,
    mcs: int,
    bw: int,
    host: str,
    port: int,
    retries: int = 5,
    wait_port_timeout_sec: int = 30,
):
    """
    Robust wrapper:
    - wait until PC can reach ASUS SSH port
    - connect(force=True)
    - set rate
    """
    for attempt in range(1, retries + 1):
        print(f"[ASUS-PC][RATE] attempt {attempt}/{retries}: waiting SSH port ready...")
        ok = _wait_tcp_port(host, port, timeout_sec=wait_port_timeout_sec, interval_sec=1.0)
        if not ok:
            continue

        try:
            asus_pc.connect(force=True)
            return asus_pc.set_rx_rate_5g(
                mcs=mcs,
                bw=bw,
                nss=2,
                sgi=True,
                ldpc=True,
            )
        except Exception as e:
            print(f"[ASUS-PC][RATE][WARN] failed attempt {attempt}/{retries}: {e}")
            time.sleep(2.0)

    raise RuntimeError(f"❌ ASUS-PC set rate failed after {retries} retries (host={host} port={port})")


def _dut_ap_set_rate_5g(
    mcs: int,
    bw: int,
    *,
    nss: int = 2,
    sgi: bool = True,
    ldpc: bool = True,
) -> None:
    """
    AP_TX requires rate lock on DUT (AP side).
    """
    flags = []
    if sgi:
        flags.append("--sgi")
    if ldpc:
        flags.append("--ldpc")

    # NOTE: interface uses DUT AP iface (wlan1 by default)
    cmd = (
        f"wl -i {config.AP_IFACE} 5g_rate "
        f"-v {mcs} -b {bw} -s {nss} "
        + " ".join(flags)
    ).strip()

    print(f"[AP_TX][RATE] lock DUT rate MCS={mcs} bw={bw} nss={nss} sgi={int(sgi)} ldpc={int(ldpc)}")
    # Give it a slightly longer timeout since wl can occasionally stall
    run_mssh_once(cmd, timeout=getattr(config, "MSSH_TIMEOUT_RATE", 15))


def _warmup_ap_tx(sec: int = 5) -> None:
    """
    Warm-up for AP_TX: normal direction (DUT -> server).
    """
    print(f"[AP_TX][WARMUP] {sec}s iperf (wait link/rate stabilize)")
    stop_all_iperf_clients()

    warmup_cmd = (
        f"iperf3 --forceflush "
        f"-c {config.IPERF_SERVER_AP_TX} "
        f"-p {config.IPERF_PORT_AP_TX} "
        f"-i 1 -t {sec}"
    )
    for _ in run_mssh_stream(warmup_cmd):
        pass

    stop_all_iperf_clients()
    print("[AP_TX][WARMUP] done")


# ==================================================
# Main
# ==================================================

def main():
    args = parse_args()
    mode_cfg = MODE_TABLE[args.mode]
    is_rx_mode = args.mode in ("AP_RX", "STA_RX")

    print(f"\n=== MODE {args.mode} ===\n")

    # ASUS controller for STA_* modes (ASUS = AP on 192.168.50.1)
    asus_ap: Optional[AsusAP] = None
    if args.mode.startswith("STA_"):
        asus_ap = AsusAP(
            host=config.ASUS_AP_IP,
            port=config.ASUS_AP_PORT,
        )

    # PC-side ASUS controller for RX rate lock
    asus_pc: Optional[AsusPC] = AsusPC() if is_rx_mode else None

    sta = STARole()

    for bw in args.bw:
        for ch in args.ch:
            print(f"\n=== BW={bw} CH={ch} ===")

            try:
                # STA_* modes: connect ASUS(AP) early is fine (doesn't depend on DUT AP rebuild)
                if asus_ap:
                    print("[STA_*][ASUS-AP] connect (one session per BW/CH)")
                    asus_ap.connect(force=True)

                # ==================================================
                # DUT AP bring-up (AP_TX / AP_RX)
                # ==================================================
                if mode_cfg["need_dut_ap"]:
                    ap_info = wifi_channel.set_ap_channel_and_bw(bw=bw, ch=ch)
                    preamble = [
                        f"# DUT AP BW={bw} CH={ch}",
                        f"# Primary={ap_info['final_primary']} status={ap_info['status']}",
                    ]

                    if bw == 80:
                        wait_sta_connected(timeout=45)
                    else:
                        wait_sta_connected(timeout=20)

                    if is_rx_mode:
                        print("[RX] double-check association before warm-up")
                        wait_sta_connected(timeout=10)
                else:
                    preamble = None

                # ==================================================
                # ASUS AP channel / BW (STA_* modes)
                # ==================================================
                if mode_cfg.get("need_asus_ch_bw"):
                    # ASUS = AP, so use asus_ap
                    asus_ap.set_5g(channel=ch, bw=bw)

                # ==================================================
                # STA prepare (STA_TX / STA_RX)
                # ==================================================
                if mode_cfg.get("need_sta_prepare"):
                    print("[STA] setup (once per BW/CH)")
                    sta.setup()

                # ==================================================
                # MCS sweep
                # ==================================================
                mcs_list = parse_mcs_list(bw, args.mcs)
                first_mcs = True

                for mcs in mcs_list:
                    print(f"\n→ MCS {mcs}")

                    # -------------------------------------------------
                    # AP_TX: warm-up once per BW/CH before any rate lock
                    # -------------------------------------------------
                    if args.mode == "AP_TX" and first_mcs:
                        _warmup_ap_tx(sec=5)
                        first_mcs = False

                    # -------------------------------------------------
                    # AP_RX: warm-up once per BW/CH BEFORE first rate lock
                    # -------------------------------------------------
                    if args.mode == "AP_RX" and first_mcs:
                        print("[AP_RX][WARMUP] 5s iperf -R (wait link stable)")
                        stop_all_iperf_clients()
                        warmup_cmd = (
                            f"iperf3 --forceflush "
                            f"-c {config.IPERF_SERVER_AP_TX} "
                            f"-p {config.IPERF_PORT_AP_TX} "
                            f"-i 1 -t 5 -R"
                        )
                        for _ in run_mssh_stream(warmup_cmd):
                            pass
                        stop_all_iperf_clients()
                        print("[AP_RX][WARMUP] done")

                        # AP_RX: connect ASUS-PC only AFTER warm-up (barrier)
                        if asus_pc:
                            print("[RX][ASUS-PC] connect AFTER warm-up (barrier)")
                            _wait_tcp_port(config.ASUS_STA_IP, config.ASUS_AP_PORT, timeout_sec=60, interval_sec=1.0)
                            asus_pc.connect(force=True)

                        first_mcs = False

                    # -------------------------------------------------
                    # AP_TX: DUT rate lock per MCS (critical)
                    # -------------------------------------------------
                    if args.mode == "AP_TX":
                        _dut_ap_set_rate_5g(
                            mcs=mcs,
                            bw=bw,
                            nss=2,
                            sgi=True,
                            ldpc=True,
                        )
                        # small settle to let driver apply
                        time.sleep(0.5)

                    # -------------------------------------------------
                    # RX rate lock (PC → ASUS) with barrier+retry
                    # -------------------------------------------------
                    if is_rx_mode and asus_pc:
                        print(f"[{args.mode}][RATE] set ASUS RX rate via PC MCS={mcs}")
                        _asus_pc_set_rate_with_retry(
                            asus_pc=asus_pc,
                            mcs=mcs,
                            bw=bw,
                            host=(config.ASUS_STA_IP if args.mode.startswith("AP_") else config.ASUS_AP_IP),
                            port=config.ASUS_AP_PORT,
                            retries=5,
                            wait_port_timeout_sec=30,
                        )

                    # -------------------------------------------------
                    # REAL TEST
                    # -------------------------------------------------
                    if mode_cfg["need_dut_ap"]:
                        run_iperf_ap(
                            direction=mode_cfg["direction"],
                            bw=bw,
                            channel=ch,
                            mcs=mcs,
                            duration=args.duration,
                            preamble=preamble,
                        )
                    else:
                        mode_cfg["runner"](
                            bw=bw,
                            channel=ch,
                            mcs=mcs,
                            duration=args.duration,
                        )

            finally:
                if asus_ap:
                    try:
                        asus_ap.close()
                    except Exception:
                        pass
                if asus_pc:
                    try:
                        asus_pc.close()
                    except Exception:
                        pass

    print("\n✅ DONE")


if __name__ == "__main__":
    main()
