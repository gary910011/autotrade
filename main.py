from __future__ import annotations

import time
import argparse
import socket
from typing import List, Optional

import config

from core.asus_ap import AsusAP          # ASUS SSH control (ASUS = AP in STA_*; also usable as generic ASUS SSH)
from core.asus_pc import AsusPC          # PC-side control (ASUS RX rate lock)
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
        "need_asus_rate": False,   # keep original semantics
        "need_sta_prepare": False,
        "direction": "RX",
    },
    "STA_TX": {
        "need_dut_ap": False,
        "need_asus_ch_bw": True,
        "need_asus_rate": False,
        "need_sta_prepare": False,
        "runner": run_iperf_sta_tx,
    },
    "STA_RX": {
        "need_dut_ap": False,
        "need_asus_ch_bw": True,
        "need_asus_rate": True,
        "need_sta_prepare": False,
        "runner": run_iperf_sta_rx,
    },
}

# ==================================================
# Ports (DO NOT "normalize" to 22)
# ASUS SSH/control port = 65535
# ==================================================
ASUS_SSH_PORT: int = int(getattr(config, "ASUS_AP_PORT", 65535))
ASUS_RATE_PORT: int = int(getattr(config, "ASUS_AP_PORT", 65535))


# ==================================================
# Band helpers (NEW, minimal-intrusive)
# ==================================================

def _supported_bands() -> List[str]:
    return list(getattr(config, "SUPPORTED_BANDS", ["5G", "2G"]))


def _default_band() -> str:
    return "5G"


def _band_default_bw(band: str) -> List[int]:
    # Prefer band-specific config, fallback to legacy lists
    band_bw = getattr(config, "BAND_BW", None)
    if isinstance(band_bw, dict) and band in band_bw:
        return list(band_bw[band])
    return [20, 40, 80] if band == "5G" else [20]


def _band_default_ch(band: str) -> List[int]:
    band_ch = getattr(config, "BAND_CHANNELS", None)
    if isinstance(band_ch, dict) and band in band_ch:
        return list(band_ch[band])
    return [36, 149] if band == "5G" else [6]


def _asus_set_2g_channel_best_effort(asus: AsusAP, ch: int = 6) -> None:
    """
    Best-effort runtime switch ASUS to 2.4G CH6 (or specified ch).

    Assumptions:
      - ASUS 2.4G iface can be configured via config.ASUS_AP_IFACE_2G (default eth6).
      - Uses wl runtime chanspec only (no nvram/webui).
    """
    iface2g = getattr(config, "ASUS_AP_IFACE_2G", "eth6")
    try:
        print(f"[ASUS][2G] switch 2.4G iface={iface2g} CH={ch} BW=20 (runtime)")
        asus.exec(f"wl -i {iface2g} down", sleep=1.5)
        asus.exec(f"wl -i {iface2g} chanspec {ch}/20", sleep=0.8)
        asus.exec(f"wl -i {iface2g} up", sleep=3.0)

        out = asus.exec(
            f"wl -i {iface2g} status | egrep -i 'Chanspec|Primary channel' || true",
            sleep=0.0,
        )
        if out:
            print(f"[ASUS][2G][VERIFY]\n{out}")
    except Exception as e:
        # Do not fail the run; user can manual-set ASUS 2.4G CH6 in WebUI.
        print(f"[ASUS][2G][WARN] runtime switch failed (please set via WebUI): {e}")


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

    band = ask("Select band:", {"1": "5G", "2": "2G"})

    role = ask("Select role:", {"1": "AP", "2": "STA"})
    direction = ask("Select direction:", {"1": "TX", "2": "RX"})
    mode = f"{role}_{direction}"

    # BW choices depend on band
    if band == "5G":
        bw_sel = ask(
            "Select bandwidth:",
            {"1": "ALL", "2": 20, "3": 40, "4": 80},
        )
        bw = [20, 40, 80] if bw_sel == "ALL" else [bw_sel]
    else:
        # 2.4G: start with BW20 only (per your plan)
        bw_sel = ask(
            "Select bandwidth (2.4G):",
            {"1": 20},
        )
        bw = [bw_sel]

    # CH choices depend on band
    if band == "5G":
        ch_sel = ask(
            "Select channel:",
            {"1": "ALL", "2": 36, "3": 149},
        )
        ch = [36, 149] if ch_sel == "ALL" else [ch_sel]
    else:
        # 2.4G: per your requirement, ASUS AP must be CH6
        ch_sel = ask(
            "Select channel (2.4G):",
            {"1": 6},
        )
        ch = [ch_sel]

    mcs_sel = ask(
        "Select MCS/Rate:",
        {"1": "ALL", "2": "SINGLE"},
    )

    if mcs_sel == "ALL":
        mcs = "auto"
    else:
        while True:
            if band == "5G":
                mcs_in = input("Enter MCS (0-9): ").strip()
                if mcs_in.isdigit() and 0 <= int(mcs_in) <= 9:
                    mcs = mcs_in
                    break
                print("Invalid MCS, must be 0~9")
            else:
                # 2G: allow 15~8 (11n) + 54 (11g) + 11 (11b)
                mcs_in = input("Enter 2.4G rate (MCS15~8 / 54 / 11): ").strip()
                if mcs_in.isdigit():
                    v = int(mcs_in)
                    if (8 <= v <= 15) or (v in (54, 11)):
                        mcs = mcs_in
                        break
                print("Invalid 2.4G rate, must be MCS15~8 or 54 or 11")

    dur = input(f"Duration (sec) [default {config.IPERF_DURATION}]: ").strip()
    duration = int(dur) if dur else config.IPERF_DURATION

    return argparse.Namespace(
        mode=mode,
        band=band,
        bw=bw,
        ch=ch,
        mcs=mcs,
        duration=duration,
    )


def wait_sta_connected(timeout=20):
    print("[AP] wait for STA association...")
    for sec in range(timeout):
        out = run_mssh_once("wl -i wlan1 assoclist || true")
        if out.strip():
            print(f"[AP] STA associated (after {sec+1}s)")
            return True
        time.sleep(1)
    raise RuntimeError("STA not associated after AP bring-up")


def cleanup_dut_ap() -> None:
    """
    Ensure DUT is not running hostapd from previous AP-mode tests before STA_* runs.
    """
    run_mssh_once("killall hostapd || true", ignore_error=True)
    run_mssh_once("pkill -9 hostapd || true", ignore_error=True)
    run_mssh_once("rm -rf /var/run/hostapd || true", ignore_error=True)


# ==================================================
# CLI parser
# ==================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Wi-Fi Throughput Automation")
    parser.add_argument("--mode", choices=MODE_TABLE.keys())

    # NEW: band (default 5G, backward compatible)
    parser.add_argument("--band", choices=_supported_bands(), default=_default_band())

    # Keep legacy choices for safety, but allow band-specific defaults when omitted
    parser.add_argument("--bw", nargs="+", type=int)
    parser.add_argument("--ch", nargs="+", type=int)

    parser.add_argument("--mcs", default="auto")
    parser.add_argument("--duration", type=int, default=config.IPERF_DURATION)

    args = parser.parse_args()

    if args.mode is None:
        return interactive_args()

    # Fill defaults per band when omitted
    if args.bw is None:
        args.bw = _band_default_bw(args.band)
    if args.ch is None:
        args.ch = _band_default_ch(args.band)

    return args

# ==================================================
# 2.4G Production Sweep Plan (EXPLICIT PHY ORDER)
# ==================================================

RATE_SWEEP_2G_PLAN = [
    ("11n", list(range(15, 7, -1))),  # MCS15 → MCS8
    ("11g", [54]),                    # OFDM 54M
    ("11b", [11]),                    # CCK 11M
]

# ==================================================
# Utilities
# ==================================================

def parse_mcs_list(band: str, bw: int, mcs_arg: str) -> List[int]:
    if mcs_arg != "auto":
        return [int(mcs_arg)]

    if band == "5G":
        if bw == 20:
            return list(range(8, -1, -1))
        return list(range(9, -1, -1))

    raise RuntimeError("2G sweep must use explicit RATE_SWEEP_2G_PLAN")


def _wait_tcp_port(host: str, port: int, timeout_sec: int = 30, interval_sec: float = 1.0) -> bool:
    """
    Wait until (host, port) is reachable from THIS PC.
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
        print(f"[ASUS-PC][RATE] attempt {attempt}/{retries}: waiting SSH port ready... host={host} port={port}")
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


# ==================================================
# Rate hygiene: clear 5g_rate override (AUTO)
# ==================================================

def _clear_5g_rate_auto(
    *,
    mode: str,
    asus_ctrl: Optional[AsusAP],
) -> None:
    """
    Your requested behavior:
      - AP_TX  : ASUS  wl -i wlan1 5g_rate auto
      - AP_RX  : DUT   wl -i wlan1 5g_rate auto
      - STA_TX : ASUS  wl -i wlan0 5g_rate auto
      - STA_RX : DUT   wl -i wlan0 5g_rate auto

    Execute ONCE per BW/CH before MCS sweep to avoid carrying over rate override.
    Best-effort: do not break existing test if this step fails.
    """
    try:
        if mode == "AP_TX":
            if not asus_ctrl:
                print("[RATE][AUTO][WARN] ASUS ctrl not available; skip clear for AP_TX")
                return
            print("[RATE][AUTO] AP_TX: clear ASUS eth7 5g_rate override (auto)")
            asus_ctrl.exec(f"wl -i eth7 5g_rate auto")

        elif mode == "AP_RX":
            print("[RATE][AUTO] AP_RX: clear DUT wlan1 5g_rate override (auto)")
            run_mssh_once("wl -i wlan1 5g_rate auto", ignore_error=True)

        elif mode == "STA_TX":
            if not asus_ctrl:
                print("[RATE][AUTO][WARN] ASUS ctrl not available; skip clear for STA_TX")
                return
            print("[RATE][AUTO] STA_TX: clear ASUS eth7 5g_rate override (auto)")
            asus_ctrl.exec("wl -i eth7 5g_rate auto")

        elif mode == "STA_RX":
            print("[RATE][AUTO] STA_RX: clear DUT wlan0 5g_rate override (auto)")
            run_mssh_once("wl -i wlan0 5g_rate auto", ignore_error=True)

        else:
            # unknown mode; keep silent
            return

    except Exception as e:
        # do NOT fail the whole run; just warn
        print(f"[RATE][AUTO][WARN] clear 5g_rate auto failed: {e}")


# ==================================================
# Main
# ==================================================

def main():
    args = parse_args()
    mode_cfg = MODE_TABLE[args.mode]
    is_rx_mode = args.mode in ("AP_RX", "STA_RX")
    band = getattr(args, "band", "5G")

    # -------------------------------------------------
    # Band propagation (prevent silent fallback)
    # -------------------------------------------------
    try:
        config.CURRENT_BAND = band
    except Exception:
        pass

    print(f"\n=== MODE {args.mode} BAND={band} ===\n")

    # ==================================================
    # Controllers
    # ==================================================
    asus_ap: Optional[AsusAP] = None
    if args.mode.startswith("STA_"):
        asus_ap = AsusAP(
            host=config.ASUS_AP_IP,
            port=int(getattr(config, "ASUS_AP_PORT", ASUS_SSH_PORT)),
        )

    asus_ctrl_for_ap_tx: Optional[AsusAP] = None
    if args.mode == "AP_TX":
        asus_ctrl_for_ap_tx = AsusAP(
            host=config.ASUS_AP_IP,
            port=int(getattr(config, "ASUS_AP_PORT", ASUS_SSH_PORT)),
        )

    asus_pc: Optional[AsusPC] = None
    if is_rx_mode:
        target_host = (
            config.ASUS_STA_IP if args.mode.startswith("AP_")
            else config.ASUS_AP_IP
        )
        try:
            asus_pc = AsusPC(host=target_host, port=ASUS_SSH_PORT)  # type: ignore
        except TypeError:
            asus_pc = AsusPC()

    sta = STARole()

    # ==================================================
    # Sweep BW / CH
    # ==================================================
    for bw in args.bw:
        for ch in args.ch:
            print(f"\n=== BAND={band} BW={bw} CH={ch} ===")

            # 2G guardrail
            if band == "2G" and bw != 20:
                print(f"[2G][SKIP] BW={bw} not supported (only 20MHz)")
                continue

            try:
                # -------------------------------------------------
                # Connect ASUS sessions (per BW/CH)
                # -------------------------------------------------
                if asus_ap:
                    asus_ap.connect(force=True)
                if asus_ctrl_for_ap_tx:
                    asus_ctrl_for_ap_tx.connect(force=True)

                # -------------------------------------------------
                # STA_* cleanup + WPA config
                # -------------------------------------------------
                if args.mode.startswith("STA_"):
                    cleanup_dut_ap()

                    from core.sta import update_and_upload_wpa_conf_for_band
                    update_and_upload_wpa_conf_for_band(
                        band=band,
                        local_conf_path=r"C:\Users\lindean\Desktop\Tput\wpa_supplicant.conf",
                        remote_conf_path=config.STA_WPA_CONF,
                    )

                # ==================================================
                # DUT AP bring-up (AP_TX / AP_RX)
                # ==================================================
                if mode_cfg["need_dut_ap"]:
                    if hasattr(wifi_channel, "set_ap_channel_and_bw_band"):
                        ap_info = wifi_channel.set_ap_channel_and_bw_band(
                            band=band, bw=bw, ch=ch
                        )
                    else:
                        if band != "5G":
                            raise RuntimeError("2G requires band-aware AP bring-up")
                        ap_info = wifi_channel.set_ap_channel_and_bw(bw=bw, ch=ch)

                    preamble = [
                        f"# DUT AP BAND={band} BW={bw} CH={ch}",
                        f"# Primary={ap_info.get('final_primary')} status={ap_info.get('status')}",
                    ]

                    if band == "5G":
                        wait_sta_connected(timeout=45 if bw == 80 else 20)
                        if is_rx_mode:
                            wait_sta_connected(timeout=10)
                else:
                    preamble = None

                # ==================================================
                # ASUS AP channel / BW (STA_* only)
                # ==================================================
                if mode_cfg["need_asus_ch_bw"]:
                    if band == "5G":
                        asus_ap.set_5g(channel=ch, bw=bw)
                    else:
                        _asus_set_2g_channel_best_effort(asus_ap, ch=ch)

                # ==================================================
                # Clear 5G rate override (once per BW/CH)
                # ==================================================
                if band == "5G":
                    asus_ctrl = (
                        asus_ap if args.mode.startswith("STA_")
                        else asus_ctrl_for_ap_tx
                    )
                    _clear_5g_rate_auto(mode=args.mode, asus_ctrl=asus_ctrl)

                # ==================================================
                # ================= MCS / RATE SWEEP =================
                # ==================================================

                # -----------------
                # 5G (legacy flow)
                # -----------------
                if band == "5G":
                    mcs_list = parse_mcs_list(band, bw, args.mcs)
                    first_mcs = True

                    for mcs in mcs_list:
                        print(f"\n→ MCS {mcs}")

                        # AP_RX warm-up before first lock
                        if args.mode == "AP_RX" and first_mcs:
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

                            if asus_pc:
                                _wait_tcp_port(
                                    config.ASUS_STA_IP,
                                    ASUS_SSH_PORT,
                                    timeout_sec=60,
                                )
                                asus_pc.connect(force=True)

                            first_mcs = False

                        # RX rate lock (5G only)
                        if is_rx_mode and asus_pc:
                            rate_host = (
                                config.ASUS_STA_IP if args.mode == "AP_RX"
                                else config.ASUS_AP_IP
                            )
                            _asus_pc_set_rate_with_retry(
                                asus_pc=asus_pc,
                                mcs=mcs,
                                bw=bw,
                                host=rate_host,
                                port=ASUS_RATE_PORT,
                            )

                        # REAL TEST
                        if mode_cfg["need_dut_ap"]:
                            run_iperf_ap(
                                direction=mode_cfg["direction"],
                                bw=bw,
                                channel=ch,
                                mcs=mcs,
                                duration=args.duration,
                                preamble=preamble,
                                band="5G",
                            )
                        else:
                            mode_cfg["runner"](
                                bw=bw,
                                channel=ch,
                                mcs=mcs,
                                duration=args.duration,
                                band="5G",
                            )

                # -----------------
                # 2.4G (PRODUCTION)
                # -----------------
                else:
                    RATE_SWEEP_2G_PLAN = [
                        ("11n", list(range(15, 7, -1))),
                        ("11g", [54]),
                        ("11b", [11]),
                    ]

                    for phy, rates in RATE_SWEEP_2G_PLAN:
                        for rate in rates:
                            print(f"\n→ 2G {phy} rate={rate}")

                            # RX side must clear auto first
                            if is_rx_mode:
                                if args.mode in ("AP_RX", "STA_RX"):
                                    run_mssh_once(
                                        "wl -i wlan0 2g_rate auto || true",
                                        ignore_error=True,
                                    )
                                else:
                                    asus_ap.exec(
                                        "wl -i eth6 2g_rate auto || true"
                                    )

                            # REAL TEST
                            if mode_cfg["need_dut_ap"]:
                                run_iperf_ap(
                                    direction=mode_cfg["direction"],
                                    bw=bw,
                                    channel=ch,
                                    mcs=rate,
                                    duration=args.duration,
                                    preamble=preamble,
                                    band="2G",
                                )
                            else:
                                mode_cfg["runner"](
                                    bw=bw,
                                    channel=ch,
                                    mcs=rate,
                                    duration=args.duration,
                                    band="2G",
                                )

            finally:
                if asus_ap:
                    asus_ap.close()
                if asus_ctrl_for_ap_tx:
                    asus_ctrl_for_ap_tx.close()
                if asus_pc:
                    asus_pc.close()

    print("\n✅ DONE")


if __name__ == "__main__":
    main()