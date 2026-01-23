# core/dut.py
from __future__ import annotations

import os
import signal
import subprocess
import threading
from typing import Generator, Optional

from config import DUT_HOST, MSSH_BIN, MSSH_TIMEOUT_SHORT

# ------------------------------------------------------------
# mssh base command
# ------------------------------------------------------------
# NOTE:
# - Keep -tt for compatibility with current DUT environment.
# - start_new_session=True is REQUIRED so we can kill process groups safely.
MSSH_BASE = [MSSH_BIN, "-tt", DUT_HOST]


# ------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------
def _kill_process_tree(p: subprocess.Popen) -> None:
    """
    Best-effort kill for the spawned mssh process group.
    """
    try:
        if p.poll() is not None:
            return

        try:
            os.killpg(p.pid, signal.SIGTERM)
        except Exception:
            try:
                p.terminate()
            except Exception:
                pass

        try:
            p.wait(timeout=2)
            return
        except Exception:
            pass

        try:
            os.killpg(p.pid, signal.SIGKILL)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    except Exception:
        pass


# ------------------------------------------------------------
# Public APIs
# ------------------------------------------------------------
def run_mssh_once(
    cmd: str,
    *,
    timeout: int = MSSH_TIMEOUT_SHORT,
    ignore_error: bool = False,
) -> str:
    """
    Run a single command on DUT via mssh.

    Design rules:
    - NO logging here (log ownership belongs to upper layer)
    - stdout + stderr merged
    - local timeout always enforced
    """
    p = subprocess.Popen(
        MSSH_BASE + [cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
        start_new_session=True,
    )

    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(p)
        try:
            out, _ = p.communicate(timeout=2)
        except Exception:
            out = ""

        if ignore_error:
            return out or ""

        raise RuntimeError(
            f"mssh timeout ({timeout}s)\n"
            f"CMD: {cmd}\n"
            f"OUT:\n{out}"
        )

    if p.returncode != 0 and not ignore_error:
        raise RuntimeError(
            f"mssh failed (rc={p.returncode})\n"
            f"CMD: {cmd}\n"
            f"OUT:\n{out}"
        )

    return out or ""


def run_mssh_stream(
    cmd: str,
    *,
    kill_event: Optional[threading.Event] = None,
) -> Generator[str, None, None]:
    """
    Run a long-running command on DUT via mssh and stream output.

    Used mainly for iperf.
    """
    p = subprocess.Popen(
        MSSH_BASE + [cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
        start_new_session=True,
    )

    try:
        assert p.stdout is not None
        for line in p.stdout:
            if kill_event and kill_event.is_set():
                break
            yield line
    finally:
        try:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except Exception:
                    p.terminate()
        except Exception:
            pass

        try:
            p.wait(timeout=2)
        except Exception:
            _kill_process_tree(p)


def stop_all_iperf_clients() -> None:
    """
    Best-effort cleanup for iperf3 on DUT.
    Silent by design; caller decides logging.
    """
    run_mssh_once(
        "pkill -9 iperf3 || true",
        timeout=5,
        ignore_error=True,
    )
