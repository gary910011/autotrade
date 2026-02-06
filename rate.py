# core/rate.py
import re
import time

from config import AP_IFACE, MSSH_TIMEOUT_RATE
from core.dut import run_mssh_once

# example: "vht mcs 7 Nss 2 Tx Exp 0 bw20 ldpc sgi fixed"
NRATE_RE = re.compile(r"vht\s+mcs\s+(\d+).*Nss\s+(\d+).*bw(\d+)", re.IGNORECASE)


def set_and_verify_mcs_ap(bw: int, mcs: int, nss: int = 2, retry: int = 3):
    print(f"[RATE] apply MCS{mcs} bw{bw} nss{nss}")

    last = ""
    for i in range(retry):
        run_mssh_once(
            f"wl -i {AP_IFACE} 5g_rate -v {mcs} -b {bw} -s {nss} --sgi --ldpc",
            timeout=MSSH_TIMEOUT_RATE,
            ignore_error=False,
        )
        out = run_mssh_once(f"wl -i {AP_IFACE} nrate", timeout=MSSH_TIMEOUT_RATE, ignore_error=False).strip()
        last = out

        m = NRATE_RE.search(out)
        if m:
            got_mcs = int(m.group(1))
            got_nss = int(m.group(2))
            got_bw = int(m.group(3))
            if got_mcs == mcs and got_nss == nss and got_bw == bw:
                print(f"✅ MCS applied: {out}")
                return

        print(f"[RATE] verify failed ({i+1}/{retry}), nrate='{out}'")
        time.sleep(0.8)

    raise RuntimeError(f"❌ MCS verify failed, last nrate: {last}")

# ============================================================
# Stable interface for main.py
# ============================================================

def apply_rate(mcs: int, bw: int, nss: int = 2):
    """
    Stable wrapper expected by main.py (AP_TX).

    This maps to existing AP-side rate control implementation.
    """
    # 直接使用你已經驗證過、正在用的實作
    return set_and_verify_mcs_ap(
        bw=bw,
        mcs=mcs,
        nss=nss,
    )
