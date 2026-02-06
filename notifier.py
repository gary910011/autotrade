import os
import time

STATE_DIR = r"C:\Users\lindean\Desktop\Tput\state"
STATE_FILE = os.path.join(STATE_DIR, "current_state.txt")


def emit_state(state: str, bw: int = None, ch: int = None, mcs: int = None):
    os.makedirs(STATE_DIR, exist_ok=True)

    if state == "READY":
        msg = f"READY,{bw},{ch},{mcs}"
        print(f"[STATE] READY BW={bw} CH={ch} MCS={mcs}")
    else:
        msg = state
        print(f"[STATE] {state}")

    with open(STATE_FILE, "w") as f:
        f.write(msg + "\n")

    # small fs sync guard
    time.sleep(0.2)
