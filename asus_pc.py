# core/asus_pc.py
"""
ASUS PC-side controller

用途：
- 從 PC 直接 SSH 進 ASUS（Media Bridge / STA）
- 不經 DUT、不用 mssh
- 專門用於 AP_RX / STA_RX 的 rate control

設計原則：
- control plane（PC → ASUS）與 data plane（DUT ↔ ASUS）完全分離
- 不包含任何 AP / nvram / restart_wireless 行為
"""

from __future__ import annotations
import time
import paramiko
from paramiko.ssh_exception import SSHException

from config import (
    ASUS_STA_IP,
    ASUS_AP_USER,
    ASUS_AP_PASS,
    ASUS_AP_PORT,
)


class AsusPC:
    def __init__(
        self,
        host: str | None = None,
        user: str | None = None,
        password: str | None = None,
        port: int | None = None,
        iface_5g: str = "eth7",
    ):
        self.host = host or ASUS_STA_IP
        self.user = user or ASUS_AP_USER
        self.password = password or ASUS_AP_PASS
        self.port = port or ASUS_AP_PORT
        self.iface_5g = iface_5g

        self.ssh: paramiko.SSHClient | None = None

        if not self.host or not self.user or not self.password:
            raise ValueError("AsusPC requires host/user/password")

    # ==================================================
    # SSH lifecycle (PC → ASUS)
    # ==================================================
    def _is_alive(self) -> bool:
        if not self.ssh:
            return False
        try:
            t = self.ssh.get_transport()
            return bool(t and t.is_active())
        except Exception:
            return False

    def connect(self, force: bool = False):
        if self.ssh and not force and self._is_alive():
            return

        self.close()
        time.sleep(0.5)

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            hostname=self.host,
            port=self.port,
            username=self.user,
            password=self.password,
            timeout=10,
            look_for_keys=False,
            allow_agent=False,
        )

        t = ssh.get_transport()
        if t:
            t.set_keepalive(10)

        self.ssh = ssh
        print(f"[ASUS-PC] connected to {self.host}:{self.port}")

    def close(self):
        if self.ssh:
            try:
                self.ssh.close()
            finally:
                self.ssh = None
                print("[ASUS-PC] disconnected")

    # ==================================================
    # Exec helper
    # ==================================================
    def exec(self, cmd: str, sleep: float = 0.2) -> str:
        self.connect()

        def _do() -> str:
            stdin, stdout, stderr = self.ssh.exec_command(cmd)
            out = stdout.read().decode(errors="ignore").strip()
            err = stderr.read().decode(errors="ignore").strip()

            if out:
                print(f"[ASUS-PC] {cmd}\n{out}")
            if err:
                print(f"[ASUS-PC][ERR] {cmd}\n{err}")

            if sleep > 0:
                time.sleep(sleep)
            return out

        try:
            return _do()

        except (SSHException, OSError) as e:
            print(f"[ASUS-PC][SSH] reconnect due to error: {e}")
            self.connect(force=True)
            return _do()

    # ==================================================
    # STA status helpers
    # ==================================================
    def wait_sta_associated(self, timeout: int = 20):
        print("[ASUS-PC] wait STA associated...")
        for sec in range(timeout):
            out = self.exec(f"wl -i {self.iface_5g} status || true", sleep=0)
            if "Associated" in out:
                print(f"[ASUS-PC] STA associated (after {sec+1}s)")
                return True
            time.sleep(1)
        raise RuntimeError("ASUS STA not associated")

    # ==================================================
    # RX rate control (PC-side)
    # ==================================================
    def set_rx_rate_5g(
        self,
        mcs: int,
        bw: int,
        nss: int = 2,
        sgi: bool = True,
        ldpc: bool = True,
    ) -> dict:
        """
        RX rate lock for ASUS (STA role)

        Example:
          wl -i eth7 5g_rate -v 8 -b 20 -s 2 --sgi --ldpc
        """
        cmd = f"wl -i {self.iface_5g} 5g_rate -v {mcs} -b {bw} -s {nss}"
        if sgi:
            cmd += " --sgi"
        if ldpc:
            cmd += " --ldpc"

        print(f"[ASUS-PC][RATE] {cmd}")
        out = self.exec(cmd)

        # Optional debug info
        nrate = self.exec(f"wl -i {self.iface_5g} nrate || true", sleep=0)
        rate = self.exec(f"wl -i {self.iface_5g} rate || true", sleep=0)

        return {
            "status": "OK",
            "mcs": mcs,
            "bw": bw,
            "nss": nss,
            "sgi": sgi,
            "ldpc": ldpc,
            "raw": out.strip(),
            "nrate": nrate.strip(),
            "rate": rate.strip(),
        }

    # ==================================================
    # Context manager
    # ==================================================
    def __enter__(self):
        # 不在這裡自動 connect，讓 main 明確控制時機
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
