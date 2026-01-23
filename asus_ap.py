# core/asus_ap.py
# ASUS RT-AX86U Pro / ASUSWRT-Merlin 3004.388.x
# HYBRID VERSION (20 / 40 / 80 MHz)

from __future__ import annotations
import re
import time
import paramiko
from paramiko.ssh_exception import SSHException

from config import ASUS_AP_HOST, ASUS_AP_USER, ASUS_AP_PASS


class AsusAP:
    def __init__(
        self,
        host: str | None = None,
        user: str | None = None,
        password: str | None = None,
        port: int = 65535,
        iface_5g: str = "eth7",
        last_connect_ts = 0.0,
    ):
        """
        ASUS AP controller.

        - If host/user/password are not provided,
          values will be loaded from config.py
        """
        self.host = host or ASUS_AP_HOST
        self.user = user or ASUS_AP_USER
        self.password = password or ASUS_AP_PASS
        self.port = port
        self.iface_5g = iface_5g
        self.ssh = None

        if not self.host or not self.user or not self.password:
            raise ValueError(
                "AsusAP requires host/user/password "
                "(either via args or config.py)"
            )

    # ==================================================
    # SSH helpers
    # ==================================================
    def _is_session_active(self) -> bool:
        """
        Return True if the underlying Paramiko transport is active.
        Some ASUS wireless restarts can silently kill SSH sessions;
        Paramiko client object may still exist but transport becomes inactive.
        """
        if not self.ssh:
            return False
        try:
            t = self.ssh.get_transport()
            return bool(t and t.is_active())
        except Exception:
            return False

    def connect(self, force: bool = False):
        """
        Establish SSH connection if needed.

        - If an SSH object exists but transport is inactive, reconnect.
        - If force=True, always reconnect.
        """
        if self.ssh and not force:
            # Existing handle, but ensure transport is still alive
            if self._is_session_active():
                return
            # Dead transport → reconnect
            self.close()
            time.sleep(1.0)

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
        self.ssh = ssh
        t = ssh.get_transport()
        if t:
            t.set_keepalive(10)

    def close(self):
        if self.ssh:
            try:
                self.ssh.close()
            finally:
                self.ssh = None

    # ==================================================
    # LOW-LEVEL EXEC (single source of truth)
    # ==================================================
    def exec(self, cmd: str, sleep: float = 0.3) -> str:
        """
        Execute command on ASUS AP via SSH.
        All wl / nvram / service commands MUST go through here.

        Robustness:
        - Auto-reconnect once if SSH session is not active.
        """
        # Ensure connection (and transport alive)
        self.connect()

        def _do_exec() -> str:
            stdin, stdout, stderr = self.ssh.exec_command(cmd)
            out = stdout.read().decode(errors="ignore").strip()
            err = stderr.read().decode(errors="ignore").strip()

            if out:
                print(f"[ASUS] {cmd}\n{out}")
            if err:
                print(f"[ASUS][ERR] {cmd}\n{err}")

            if sleep > 0:
                time.sleep(sleep)

            return out

        try:
            return _do_exec()

        except SSHException as e:
            # Typical when ASUS wireless/driver resets → session dies
            print(f"[ASUS][SSH] session not active, reconnecting... ({e})")
            try:
                self.close()
            except Exception:
                pass
            self.connect(force=True)
            return _do_exec()

        except OSError as e:
            # Covers socket reset/broken pipe variants
            print(f"[ASUS][SSH] socket error, reconnecting... ({e})")
            try:
                self.close()
            except Exception:
                pass
            self.connect(force=True)
            return _do_exec()

        except AttributeError as e:
            # Defensive: if self.ssh becomes None unexpectedly
            print(f"[ASUS][SSH] client missing, reconnecting... ({e})")
            try:
                self.close()
            except Exception:
                pass
            self.connect(force=True)
            return _do_exec()

    # ==================================================
    # Channel normalize
    # ==================================================
    @staticmethod
    def _normalize_ch(bw: int, ch: int) -> int:
        if bw == 40 and ch in (36, 40):
            return 36
        if bw == 80 and ch in (36, 40, 44, 48):
            return 40
        return ch

    # ==================================================
    # Public API
    # ==================================================
    def set_5g(self, channel: int, bw: int) -> dict:
        self.connect()

        if bw == 20:
            return self._set_runtime_20(channel)
        if bw in (40, 80):
            return self._set_webui(channel, bw)

        raise ValueError(f"Unsupported BW: {bw}")

    # ==================================================
    # 20 MHz (runtime only)
    # ==================================================
    def _set_runtime_20(self, ch: int) -> dict:
        print(f"\n[ASUS][RUNTIME] CH={ch} BW=20")

        self.exec(f"wl -i {self.iface_5g} down", sleep=2)
        self.exec(f"wl -i {self.iface_5g} chanspec {ch}/20", sleep=1)
        self.exec(f"wl -i {self.iface_5g} up", sleep=5)

        return self._verify(ch, 20, "RUNTIME_ONLY")

    # ==================================================
    # 40 / 80 MHz (WebUI / nvram)
    # ==================================================
    def _set_webui(self, ch: int, bw: int) -> dict:
        tch = self._normalize_ch(bw, ch)
        print(f"\n[ASUS][WEBUI] CH={ch}→{tch} BW={bw}")

        self.exec("nvram set wl1_bw=3")
        self.exec("nvram set wl1_bw_cap=7")
        self.exec(f"nvram set wl1_nbw={bw}")
        self.exec("nvram set wl1_vht_bw=1")
        self.exec("nvram set wl1_160mhz=0")
        self.exec("nvram set wl1_acs_enable=0")
        self.exec("nvram set wl1_acs_dfs=0")
        self.exec(f"nvram set wl1_channel={tch}")
        self.exec(f"nvram set wl1_chanspec={tch}/{bw}")

        self.exec("nvram commit")
        self.exec("service restart_wireless", sleep=0)
        time.sleep(10)

        return self._verify(ch, bw, "WEBUI")

    # ==================================================
    # Verify
    # ==================================================
    def _verify(self, req_ch: int, req_bw: int, mode: str) -> dict:
        out = self.exec(
            f"wl -i {self.iface_5g} status | egrep -i 'Chanspec|Primary channel'",
            sleep=0,
        )

        if "80MHz" in out:
            act_bw = 80
        elif "40MHz" in out:
            act_bw = 40
        else:
            act_bw = 20

        m = re.search(r"Primary channel:\s*(\d+)", out)
        act_ch = int(m.group(1)) if m else None

        ok = act_bw == req_bw and (act_ch == req_ch if act_ch else True)
        status = "OK" if ok else "FAIL"

        print(f"[ASUS][VERIFY] {status}")

        return {
            "requested_ch": req_ch,
            "requested_bw": req_bw,
            "actual_ch": act_ch,
            "actual_bw": act_bw,
            "mode": mode,
            "status": status,
            "raw": out,
        }

    # ==================================================
    # Rate control (AP TX)
    # ==================================================
    def set_rate_5g(
        self,
        mcs: int,
        bw: int,
        nss: int = 2,
        sgi: bool = True,
        ldpc: bool = True,
    ) -> dict:
        """
        Set fixed VHT rate on ASUS AP 5G interface.

        Example:
          wl -i eth7 5g_rate -v 8 -b 20 -s 2 --sgi --ldpc
        """
        self.connect()

        cmd = f"wl -i {self.iface_5g} 5g_rate -v {mcs} -b {bw} -s {nss}"
        if sgi:
            cmd += " --sgi"
        if ldpc:
            cmd += " --ldpc"

        print(f"[ASUS][RATE] {cmd}")
        out = self.exec(cmd)

        # optional verify
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
        # ⚠ 不在這裡強制 connect
        # 連線延遲到第一次 exec / set_rate / set_5g 時
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
