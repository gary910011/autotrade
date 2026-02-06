# core/iperf.py
from __future__ import annotations

from typing import Optional, Dict, Literal, List, Tuple
from pathlib import Path
from datetime import datetime
import time
import socket

import config
from core.dut import (
    run_mssh_stream,
    stop_all_iperf_clients,
    run_mssh_once,
)
from core.sta import (
    sta_prepare,
    sta_prepare_tx_once,
    set_sta_rate,  # 5G STA rate lock (existing)
)
from core import wifi_channel
from core.asus_ap import AsusAP  # ✅ NEW: for 2G RX rate lock on ASUS
from utils.logger import TputLogger

StaDirection = Literal["TX", "RX"]

# ==================================================
# AP_TX warm-up settings (legacy, kept for compatibility)
# ==================================================
AP_TX_WARMUP_SEC = 5

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


def _resolve_band(band: Optional[str]) -> str:
    """
    Band resolution priority:
      1) explicit param
      2) config.CURRENT_BAND (if your main.py sets it)
      3) config.DEFAULT_BAND (optional)
      4) fallback "5G"
    """
    if band:
        return str(band).upper()
    b = getattr(config, "CURRENT_BAND", None) or getattr(config, "DEFAULT_BAND", None)
    return str(b).upper() if b else "5G"


def _create_logger(
    *,
    role: str,
    direction: str,
    band: str,
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
        band=band,
        bw_mhz=bw,
        mode=role,
        direction=direction,
        channel=channel,
        mcs=mcs,
    )
    logger.create()

    header = [
        f"# MODE={role}_{direction}",
        f"# BAND={band}",
        f"# BW={bw}MHz CH={channel} MCS/Rate={mcs} NSS={nss}",
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
# STA (TX / RX) -- keep existing behavior, add band-aware TX/RX rate lock
# ==================================================
_STA_TX_READY = False


def _sta_is_connected(iface: str) -> bool:
    out = run_mssh_once(f"iw {iface} link || true", ignore_error=True)
    return "Connected" in out


def _sta_soft_reconnect(iface: str, ip: str) -> bool:
    if _sta_is_connected(iface):
        return True

    print("[STA_RX] soft reconnect: wpa_cli reconnect")
    run_mssh_once(f"wpa_cli -i {iface} reconnect || true", ignore_error=True)
    time.sleep(2.0)

    if _sta_is_connected(iface):
        run_mssh_once(f"ifconfig {iface} {ip} || true", ignore_error=True)
        return True

    return False


def _sta_hard_recover(iface: str, ip: str, wpa_conf: str) -> bool:
    print("[STA_RX][RECOVERY] hard recover wlan0 + wpa_supplicant")

    run_mssh_once("killall wpa_supplicant || true", ignore_error=True)
    run_mssh_once("pkill -9 wpa_supplicant || true", ignore_error=True)

    run_mssh_once(f"ifconfig {iface} down || true", ignore_error=True)
    time.sleep(1.0)
    run_mssh_once(f"ifconfig {iface} up || true", ignore_error=True)
    time.sleep(1.0)

    run_mssh_once(
        f"wpa_supplicant -B -c {wpa_conf} -i {iface} || true",
        ignore_error=True,
    )

    deadline = time.time() + 30.0
    while time.time() < deadline:
        if _sta_is_connected(iface):
            run_mssh_once(f"ifconfig {iface} {ip} || true", ignore_error=True)
            print("[STA_RX][RECOVERY] connected")
            return True
        time.sleep(1.0)

    print("[STA_RX][RECOVERY] still not connected")
    return False


# ==================================================
# 2.4G rate lock helpers
#   - DUT side: wl -i wlan0 2g_rate ...
#   - ASUS side (RX): wl -i eth6 2g_rate ... + nrate/rate verify
# ==================================================
def _dut_rate_iface_for_2g() -> str:
    # 你要求 2.4G rate lock 要用 wlan0（可用 config.DUT_2G_RATE_IFACE 覆寫）
    return getattr(config, "DUT_2G_RATE_IFACE", "wlan0")


def _set_rate_2g_from_value(value: int) -> Tuple[str, int]:
    """
    Infer 2.4G rate mode by numeric value (FIXED):
      - 11b CCK : 1 / 2 / 5.5 / 11  (你目前用 11；5.5 以整數 5 表示時不支援)
      - 11g OFDM: 6~54 (你目前用 54)
      - 11n HT  : MCS 0~15

    IMPORTANT: 11 (Mbps) 必須判成 11b，不可落到 11n。
    """
    # 11b (CCK) – 必須最先判斷
    if value in (1, 2, 11):
        return ("11b", value)

    # 11g (OFDM) – 你目前只用 54
    if value == 54:
        return ("11g", value)

    # 11n HT
    if 0 <= value <= 15:
        return ("11n", value)

    raise ValueError(f"Unsupported 2G MCS/Rate value: {value}")


def _wl_2g_rate_cmd(*, iface: str, value: int, bw: int = 20) -> Tuple[str, str, int]:
    """
    Build wl 2g_rate command.
    Returns: (cmd, mode, value)
    """
    mode, v = _set_rate_2g_from_value(value)

    if mode == "11n":
        cmd = f"wl -i {iface} 2g_rate -h {v} -b {bw} --sgi --ldpc"
    elif mode == "11g":
        cmd = f"wl -i {iface} 2g_rate -r {v} -b {bw}"
    elif mode == "11b":
        cmd = f"wl -i {iface} 2g_rate -r {v} -b {bw}"
    else:
        raise ValueError(mode)

    return cmd, mode, v


def _set_rate_2g_dut(*, role: str, direction: str, value: int, bw: int = 20, timeout_sec: int = 10) -> bool:
    """
    Standardized 2G rate lock on DUT.
    """
    iface = _dut_rate_iface_for_2g()
    cmd, mode, v = _wl_2g_rate_cmd(iface=iface, value=value, bw=bw)
    print(f"[{role}_{direction}][RATE][2G] robust lock: mode={mode} value={v} iface={iface}")

    max_retry = 2
    for attempt in range(1, max_retry + 1):
        try:
            run_mssh_once(cmd, timeout=timeout_sec)
            return True
        except Exception as e:
            print(f"[{role}_{direction}][RATE][2G][WARN] attempt {attempt} failed: {e}")
            time.sleep(1.0)

    print(f"[{role}_{direction}][RATE][2G][FAIL] rate lock failed, continue without override")
    return False


def _wait_tcp_port(host: str, port: int, timeout_sec: int = 30, interval_sec: float = 1.0) -> bool:
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

    print(f"[ASUS][WAIT] TCP {host}:{port} not reachable after {timeout_sec}s (last_err={last_err})")
    return False


def _asus_2g_iface() -> str:
    # 你指定 2.4G 在 ASUS 上是 eth6（可用 config.ASUS_AP_IFACE_2G 覆寫）
    return getattr(config, "ASUS_AP_IFACE_2G", "eth6")


def _asus_ssh_port() -> int:
    # 你環境 ASUS SSH/control port = 65535
    return int(getattr(config, "ASUS_AP_PORT", 65535))


def _asus_host_for_2g_rx(role: str) -> str:
    """
    Determine which ASUS management IP to use for 2G RX rate lock.

    - role="AP"  : AP_RX mode => ASUS is STA side => use config.ASUS_STA_IP
    - role="STA" : STA_RX mode => ASUS is AP side => use config.ASUS_AP_IP
    """
    if role == "AP":
        return str(getattr(config, "ASUS_STA_IP", getattr(config, "ASUS_AP_IP", "192.168.50.1")))
    if role == "STA":
        return str(getattr(config, "ASUS_AP_IP", "192.168.50.1"))
    raise ValueError(f"Unknown role for ASUS host mapping: {role}")


def _asus_set_rx_rate_2g(*, role: str, value: int, bw: int = 20) -> None:
    """
    2G RX rate lock on ASUS (per your spec):
      wl -i eth6 2g_rate -h <mcs> -b 20 --sgi --ldpc
      wl -i eth6 2g_rate -r 54 -b 20
      wl -i eth6 2g_rate -r 11 -b 20
      wl -i eth6 nrate
      wl -i eth6 rate
    """
    host = _asus_host_for_2g_rx(role)
    port = _asus_ssh_port()
    iface = _asus_2g_iface()

    # wait port
    _wait_tcp_port(host, port, timeout_sec=30, interval_sec=1.0)

    asus = AsusAP(host=host, port=port)
    asus.connect(force=True)

    try:
        cmd, mode, v = _wl_2g_rate_cmd(iface=iface, value=value, bw=bw)
        print(f"[{role}_RX][RATE][2G][ASUS] set: mode={mode} value={v} iface={iface} host={host}:{port}")
        asus.exec(cmd, sleep=0.0)

        # verify (your request)
        try:
            out1 = asus.exec(f"wl -i {iface} nrate || true", sleep=0.0)
            out2 = asus.exec(f"wl -i {iface} rate || true", sleep=0.0)
            if out1:
                print(f"[{role}_RX][RATE][2G][ASUS][VERIFY] nrate: {out1.strip()}")
            if out2:
                print(f"[{role}_RX][RATE][2G][ASUS][VERIFY] rate : {out2.strip()}")
        except Exception as e:
            print(f"[{role}_RX][RATE][2G][ASUS][WARN] verify failed: {e}")

    finally:
        try:
            asus.close()
        except Exception:
            pass


def _set_sta_rate_2g(value: int) -> None:
    # STA_TX uses this (legacy naming kept), implemented via standardized DUT helper
    _set_rate_2g_dut(
        role="STA",
        direction="TX",
        value=value,
        bw=20,
        timeout_sec=getattr(config, "MSSH_TIMEOUT_RATE", 15),
    )


def run_iperf_sta(
    *,
    direction: StaDirection,
    bw: int,
    channel: int,
    mcs: int,
    duration: int,
    nss: int = 2,
    band: str = "5G",
    preamble: Optional[List[str]] = None,
) -> Dict:
    global _STA_TX_READY

    band = _resolve_band(band)
    print(f"[STA_{direction}] band={band} BW={bw} CH={channel} MCS/Rate={mcs}")

    # ==================================================
    # STA prepare
    # ==================================================
    if direction == "TX":
        if not _STA_TX_READY:
            print("🔌 [STA_TX] prepare (once per BW/CH)")
            sta_info = sta_prepare_tx_once(
                iface=config.STA_IFACE,
                ip=config.STA_IP,
                wpa_conf=config.STA_WPA_CONF,
            )
            if not sta_info.get("connected"):
                print("⚠️ [STA_TX] link not reported connected, continue")
            _STA_TX_READY = True
        else:
            print("♻️ [STA_TX] reuse existing STA link")
    else:  # RX
        try:
            sta_info = sta_prepare(
                iface=config.STA_IFACE,
                ip=config.STA_IP,
                wpa_conf=config.STA_WPA_CONF,
                retry=3,
                link_timeout_sec=30.0,
                poll_sec=0.5,
            )
            connected = bool(sta_info.get("connected"))
        except Exception as e:
            print(f"⚠️ [STA_RX] sta_prepare failed: {e}")
            connected = False

        if not connected:
            if not _sta_soft_reconnect(config.STA_IFACE, config.STA_IP):
                ok = _sta_hard_recover(
                    config.STA_IFACE,
                    config.STA_IP,
                    config.STA_WPA_CONF,
                )
                if not ok:
                    print("❌ [STA_RX] cannot recover link → SKIP")
                    return {"ok": False, "skipped": True, "reason": "STA link down"}

    # ==================================================
    # Rate lock (band-aware) — A mode: every sweep do it
    # ==================================================
    if band == "5G":
        if direction == "TX":
            set_sta_rate(mcs=mcs, bw=bw, nss=nss)
        else:
            # 5G STA_RX: keep existing design (main.py ASUS-PC handles RX rate lock)
            pass

    elif band == "2G":
        if bw != 20:
            print(f"[STA_{direction}][2G][WARN] BW={bw} not supported yet; force bw=20 for rate cmd")
        if direction == "TX":
            _set_rate_2g_dut(role="STA", direction="TX", value=mcs, bw=20)
        else:
            # ✅ STA_RX 2G: set ASUS RX rate on eth6 (your spec)
            _asus_set_rx_rate_2g(role="STA", value=mcs, bw=20)

    else:
        raise ValueError(f"Unsupported band: {band}")

    # ==================================================
    # iPerf target
    # ==================================================
    if direction == "TX":
        server_ip = config.IPERF_SERVER_STA_TX
        server_port = config.IPERF_PORT_STA_TX
        reverse = ""
    else:
        server_ip = config.IPERF_SERVER_STA_RX
        server_port = config.IPERF_PORT_STA_RX
        reverse = "-R"

    cmd = f"iperf3 --forceflush -c {server_ip} -p {server_port} -i 1 -t {duration} {reverse}".strip()

    stop_all_iperf_clients()

    logger = _create_logger(
        role="STA",
        direction=direction,
        band=band,
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


def run_iperf_sta_tx(bw: int, channel: int, mcs: int, duration: int, *, band: str = "5G") -> Dict:
    return run_iperf_sta(direction="TX", bw=bw, channel=channel, mcs=mcs, duration=duration, band=band)


def run_iperf_sta_rx(bw: int, channel: int, mcs: int, duration: int, *, band: str = "5G") -> Dict:
    return run_iperf_sta(direction="RX", bw=bw, channel=channel, mcs=mcs, duration=duration, band=band)


# ==================================================
# AP (TX / RX)
# ==================================================
_AP_WARMED: set[tuple[str, int, int, str]] = set()  # (band, bw, channel, direction)


def _ap_dataplane_barrier(server_ip: str, iface: str = "wlan1") -> None:
    """
    Best-effort barrier:
    - drop neighbor entry (forces ARP refresh)
    - ping twice (wakes dataplane)
    Never raises.
    """
    try:
        run_mssh_once(f"ip neigh del {server_ip} dev {iface} || true", ignore_error=True)
    except Exception:
        pass

    try:
        run_mssh_once(f"ping -c 1 -W 1 {server_ip} >/dev/null 2>&1 || true", ignore_error=True)
        run_mssh_once(f"ping -c 1 -W 1 {server_ip} >/dev/null 2>&1 || true", ignore_error=True)
    except Exception:
        pass


def _looks_like_connect_fail(line: str) -> bool:
    low = line.lower()
    return (
        "unable to connect" in low
        or "no route to host" in low
        or "network is unreachable" in low
    )


def _dut_ap_restart_hostapd(
    *,
    iface: str,
    ip: str,
    runtime_conf: str,
    wait_sec: float = 5.0,
) -> None:
    print("[AP_TX][RECOVERY] restart DUT hostapd (runtime conf)")

    # 🔒 runtime conf 不存在就直接跳過（避免炸流程）
    chk = run_mssh_once(
        f"test -f {runtime_conf} && echo OK || echo MISS",
        ignore_error=True,
    ).strip()

    if chk != "OK":
        print(f"[AP_TX][RECOVERY][SKIP] runtime conf missing: {runtime_conf}")
        return

    run_mssh_once("killall hostapd || true", ignore_error=True)
    run_mssh_once("pkill -9 hostapd || true", ignore_error=True)
    run_mssh_once("rm -rf /var/run/hostapd || true", ignore_error=True)

    run_mssh_once(
        f"hostapd -B -i {iface} {runtime_conf}",
        ignore_error=False,
    )
    run_mssh_once(f"ifconfig {iface} {ip} up || true", ignore_error=True)

    print(f"[AP_TX][RECOVERY] wait {wait_sec:.0f}s for re-association")
    time.sleep(wait_sec)


def _ap_tx_rate_lock_robust_5g(
    *,
    iface: str,
    mcs: int,
    bw: int,
    nss: int,
    timeout_sec: int = 10,
) -> bool:
    """
    5G AP_TX rate lock: wl 5g_rate -v <mcs> -b <bw> -s <nss> --sgi --ldpc
    """
    print(f"[AP_TX][RATE][5G] robust lock: MCS={mcs} BW={bw} NSS={nss} iface={iface}")

    time.sleep(1.0)

    run_mssh_once(
        f"wl -i {iface} 5g_rate auto || true",
        ignore_error=True,
        timeout=timeout_sec,
    )
    time.sleep(0.5)

    max_retry = 2
    for attempt in range(1, max_retry + 1):
        try:
            run_mssh_once(
                f"wl -i {iface} 5g_rate -v {mcs} -b {bw} -s {nss} --sgi --ldpc",
                timeout=timeout_sec,
            )
            return True
        except Exception as e:
            print(f"[AP_TX][RATE][5G][WARN] attempt {attempt} failed: {e}")
            time.sleep(1.0)

    print("[AP_TX][RATE][5G][FAIL] rate lock failed, continue without override")
    return False


def run_iperf_ap(
    *,
    direction: StaDirection,
    bw: int,
    channel: int,
    mcs: int,
    duration: int,
    nss: int = 2,
    band: str = "5G",
    preamble: Optional[List[str]] = None,
) -> Dict:
    band = _resolve_band(band)
    print(f"[AP_{direction}] band={band} BW={bw} CH={channel} MCS/Rate={mcs}")

    server_ip = config.IPERF_SERVER_AP_TX
    server_port = config.IPERF_PORT_AP_TX
    reverse = "-R" if direction == "RX" else ""

    real_cmd = (
        f"iperf3 --forceflush "
        f"-c {server_ip} -p {server_port} -i 1 -t {duration} {reverse}"
    ).strip()

    # ==================================================
    # Rate lock (band-aware) — A mode: every sweep do it
    # ==================================================
    if band == "5G":
        if direction == "TX":
            _ap_tx_rate_lock_robust_5g(
                iface=wifi_channel.AP_IFACE,
                mcs=mcs,
                bw=bw,
                nss=nss,
            )
        else:
            # 5G AP_RX: keep existing design (main.py ASUS-PC handles RX rate lock)
            pass

    elif band == "2G":
        if direction == "TX":
            # DUT TX → ASUS RX
            _clear_2g_rate_auto_rx(rx_side="ASUS")
            _set_rate_2g_dut(role="AP", direction="TX", value=mcs, bw=20)
        else:
            # ASUS TX → DUT RX
            _clear_2g_rate_auto_rx(rx_side="DUT")
            _asus_set_rx_rate_2g(role="AP", value=mcs, bw=20)

    else:
        raise ValueError(f"Unsupported band: {band}")

    time.sleep(0.5)

    # ==================================================
    # Warm-up once per (band,bw,ch,direction)
    # ==================================================
    warmup_key = (band, bw, channel, direction)
    if warmup_key not in _AP_WARMED:
        warmup_cmd = (
            f"iperf3 --forceflush "
            f"-c {server_ip} -p {server_port} -i 1 "
            f"-t {AP_TX_WARMUP_SEC} {reverse}"
        ).strip()

        print(
            f"[AP_{direction}][WARMUP] {AP_TX_WARMUP_SEC}s traffic stabilization "
            f"(band={band} BW={bw} CH={channel})"
        )

        stop_all_iperf_clients()
        _ap_dataplane_barrier(server_ip, iface=wifi_channel.AP_IFACE)

        for _ in run_mssh_stream(warmup_cmd):
            pass

        stop_all_iperf_clients()
        _AP_WARMED.add(warmup_key)
        print(f"[AP_{direction}][WARMUP] done")

    # ==================================================
    # REAL TEST: barrier + retry + (AP_TX only) hostapd recovery
    # ==================================================
    stop_all_iperf_clients()

    logger = _create_logger(
        role="AP",
        direction=direction,
        band=band,
        bw=bw,
        channel=channel,
        mcs=mcs,
        nss=nss,
        server=f"{server_ip}:{server_port}",
        duration=duration,
        preamble=preamble,
    )

    iface = wifi_channel.AP_IFACE
    runtime_conf = wifi_channel.RUNTIME_CONF
    dut_ap_ip = getattr(
        config,
        "DUT_AP_IP",
        getattr(config, "AP_IP", "192.168.50.100"),
    )

    outer_barrier_tries = 3
    ap_recover_tries = 2 if direction == "TX" else 0

    try:
        for outer_try in range(1, outer_barrier_tries + 1):
            if outer_try > 1:
                print(f"[AP_{direction}][RETRY] barrier attempt {outer_try}/{outer_barrier_tries}")

            _ap_dataplane_barrier(server_ip, iface=iface)

            for recover_try in range(0, ap_recover_tries + 1):
                if recover_try > 0:
                    print(f"[AP_TX][RETRY] after hostapd recovery {recover_try}/{ap_recover_tries}")

                stop_all_iperf_clients()

                saw_output = False
                saw_connect_fail = False

                for line in run_mssh_stream(real_cmd):
                    line = line.rstrip()
                    if not line:
                        continue
                    saw_output = True
                    print(f"[iPerf] {line}")
                    logger.write(line)
                    if _looks_like_connect_fail(line):
                        saw_connect_fail = True

                if saw_output and not saw_connect_fail:
                    return {"ok": True}

                if direction != "TX":
                    break

                if recover_try < ap_recover_tries:
                    logger.write("# RECOVERY=restart_hostapd (runtime conf)")
                    _dut_ap_restart_hostapd(
                        iface=iface,
                        ip=dut_ap_ip,
                        runtime_conf=runtime_conf,
                        wait_sec=5.0,
                    )
                    _ap_dataplane_barrier(server_ip, iface=iface)

            if outer_try < outer_barrier_tries:
                time.sleep(2.0)

        logger.write("# RESULT=FAIL (iperf connect failed after retries)")
        return {"ok": False, "error": "iperf connect failed after retries"}

    finally:
        logger.close()


def run_iperf_ap_tx(bw: int, channel: int, mcs: int, duration: int, *, band: str = "5G") -> Dict:
    return run_iperf_ap(direction="TX", bw=bw, channel=channel, mcs=mcs, duration=duration, band=band)


def run_iperf_ap_rx(bw: int, channel: int, mcs: int, duration: int, *, band: str = "5G") -> Dict:
    return run_iperf_ap(direction="RX", bw=bw, channel=channel, mcs=mcs, duration=duration, band=band)


# ==================================================
# Optional: public helper if other modules want 2G rate lock
# ==================================================
def set_sta_rate_2g(mode: str, value: int) -> None:
    """
    Public API (backward-compatible style):
      mode: "11n" | "11g" | "11b"
    """
    iface = _dut_rate_iface_for_2g()
    if mode == "11n":
        cmd = f"wl -i {iface} 2g_rate -h {value} -b 20 --sgi --ldpc"
    elif mode == "11g":
        cmd = f"wl -i {iface} 2g_rate -r {value} -b 20"
    elif mode == "11b":
        cmd = f"wl -i {iface} 2g_rate -r {value} -b 20"
    else:
        raise ValueError(mode)

    run_mssh_once(cmd, timeout=getattr(config, "MSSH_TIMEOUT_RATE", 15))

def _clear_2g_rate_auto_rx(*, rx_side: str) -> None:
    """
    rx_side:
      - "DUT"  -> wl -i wlan0 2g_rate auto
      - "ASUS" -> wl -i eth6  2g_rate auto
    """
    if rx_side == "DUT":
        iface = _dut_rate_iface_for_2g()
        run_mssh_once(f"wl -i {iface} 2g_rate auto || true", ignore_error=True)
        print(f"[RATE][AUTO][2G] DUT RX clear auto ({iface})")
    elif rx_side == "ASUS":
        host = _asus_host_for_2g_rx(role="AP")  # role 不影響 auto
        port = _asus_ssh_port()
        iface = _asus_2g_iface()
        _wait_tcp_port(host, port)
        asus = AsusAP(host=host, port=port)
        asus.connect(force=True)
        try:
            asus.exec(f"wl -i {iface} 2g_rate auto || true", sleep=0.0)
            print(f"[RATE][AUTO][2G] ASUS RX clear auto ({iface})")
        finally:
            asus.close()