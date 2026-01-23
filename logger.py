# utils/logger.py
from __future__ import annotations

from pathlib import Path
from datetime import datetime
from typing import Iterable, Optional


class TputLogger:
    """
    Directory layout:

      <LOG_DIR>/
        └── run_YYYYMMDD_HHMM/
            ├── BW20/
            │   └── 5G_20MHz_STA_TX_CH36_MCS8.txt
            ├── BW40/
            └── BW80/
    """

    @staticmethod
    def create_run_dir(base_dir: str) -> Path:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        run_dir = Path(base_dir) / f"run_{ts}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def __init__(
        self,
        base_dir: Path,
        band: str,
        bw_mhz: int,
        mode: str,
        direction: str,
        channel: int,
        mcs: int,
    ):
        self.base_dir = base_dir
        self.band = band
        self.bw_mhz = int(bw_mhz)
        self.mode = mode
        self.direction = direction
        self.channel = int(channel)
        self.mcs = int(mcs)

        self.bw_dir = self.base_dir / f"BW{self.bw_mhz}"
        self.filepath: Optional[Path] = None
        self.fp = None

    def create(self) -> str:
        self.bw_dir.mkdir(parents=True, exist_ok=True)

        fname = (
            f"{self.band}_{self.bw_mhz}MHz_"
            f"{self.mode}_{self.direction}_"
            f"CH{self.channel}_MCS{self.mcs}.txt"
        )

        self.filepath = self.bw_dir / fname
        self.fp = self.filepath.open("w", encoding="utf-8", newline="\n")
        return str(self.filepath)

    def write_header(self, lines: Iterable[str]) -> None:
        if not self.fp:
            return
        for ln in lines:
            self.fp.write(ln.rstrip("\n") + "\n")
        self.fp.write("\n")
        self.fp.flush()

    def write(self, line: str) -> None:
        if not self.fp:
            return
        self.fp.write(line.rstrip("\n") + "\n")
        self.fp.flush()

    def close(self) -> None:
        try:
            if self.fp:
                self.fp.flush()
                self.fp.close()
        finally:
            self.fp = None
