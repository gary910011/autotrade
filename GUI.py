from __future__ import annotations

import re
import sys
import subprocess
from dataclasses import dataclass
from typing import List

from PyQt5 import QtCore, QtWidgets

import config


MCS_STEP_RE = re.compile(r"^→ MCS (\d+)")
BW_CH_RE = re.compile(r"^=== BW=(\d+) CH=(\d+)")
MODE_RE = re.compile(r"^=== MODE (.+) ===")


@dataclass
class RunPlan:
    mode: str
    bw_list: List[int]
    ch_list: List[int]
    mcs_mode: str
    mcs_value: int
    duration: int

    def total_steps(self) -> int:
        if self.mcs_mode != "auto":
            return len(self.bw_list) * len(self.ch_list)

        steps = 0
        for bw in self.bw_list:
            if bw == 20:
                steps += len(self.ch_list) * 9
            else:
                steps += len(self.ch_list) * 10
        return steps

    def to_args(self) -> List[str]:
        args = ["--mode", self.mode]
        if self.bw_list:
            args.append("--bw")
            args.extend([str(bw) for bw in self.bw_list])
        if self.ch_list:
            args.append("--ch")
            args.extend([str(ch) for ch in self.ch_list])
        if self.mcs_mode == "auto":
            args.extend(["--mcs", "auto"])
        else:
            args.extend(["--mcs", str(self.mcs_value)])
        args.extend(["--duration", str(self.duration)])
        return args


class RunWorker(QtCore.QThread):
    log_line = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal(int)

    def __init__(self, args: List[str], parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self.args = args
        self.process: subprocess.Popen[str] | None = None
        self._stop_requested = False

    def run(self) -> None:
        cmd = [sys.executable, "-u", "main.py", *self.args]
        self.log_line.emit(f"[GUI] Running: {' '.join(cmd)}")
        self.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if not self.process.stdout:
            self.finished.emit(1)
            return

        for line in self.process.stdout:
            if self._stop_requested:
                break
            self.log_line.emit(line.rstrip("\n"))

        if self._stop_requested and self.process:
            self.process.terminate()

        if self.process:
            self.process.wait()
            self.finished.emit(self.process.returncode or 0)

    def stop(self) -> None:
        self._stop_requested = True
        if self.process:
            self.process.terminate()


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Wi-Fi Tput Automation GUI")
        self.resize(1000, 700)

        self.worker: RunWorker | None = None
        self.plan: RunPlan | None = None
        self.completed_steps = 0
        self.total_steps = 0

        self._build_ui()

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        main_layout = QtWidgets.QHBoxLayout(central)

        settings_panel = QtWidgets.QGroupBox("Test Settings")
        settings_layout = QtWidgets.QVBoxLayout(settings_panel)

        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItems(["AP_TX", "AP_RX", "STA_TX", "STA_RX"])
        settings_layout.addWidget(self._labeled("Mode", self.mode_combo))

        self.bw_checks = self._make_checkbox_group("Bandwidth (MHz)", [20, 40, 80])
        settings_layout.addWidget(self.bw_checks)

        self.ch_checks = self._make_checkbox_group("Channel", [36, 149])
        settings_layout.addWidget(self.ch_checks)

        mcs_group = QtWidgets.QGroupBox("MCS")
        mcs_layout = QtWidgets.QVBoxLayout(mcs_group)
        self.mcs_auto_radio = QtWidgets.QRadioButton("Auto")
        self.mcs_auto_radio.setChecked(True)
        self.mcs_single_radio = QtWidgets.QRadioButton("Single")
        self.mcs_spin = QtWidgets.QSpinBox()
        self.mcs_spin.setRange(0, 9)
        self.mcs_spin.setEnabled(False)
        self.mcs_single_radio.toggled.connect(self.mcs_spin.setEnabled)
        mcs_layout.addWidget(self.mcs_auto_radio)
        single_row = QtWidgets.QHBoxLayout()
        single_row.addWidget(self.mcs_single_radio)
        single_row.addWidget(self.mcs_spin)
        mcs_layout.addLayout(single_row)
        settings_layout.addWidget(mcs_group)

        self.duration_spin = QtWidgets.QSpinBox()
        self.duration_spin.setRange(1, 3600)
        self.duration_spin.setValue(config.IPERF_DURATION)
        settings_layout.addWidget(self._labeled("Duration (sec)", self.duration_spin))

        self.start_button = QtWidgets.QPushButton("Start")
        self.stop_button = QtWidgets.QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.start_button.clicked.connect(self.start_run)
        self.stop_button.clicked.connect(self.stop_run)

        button_row = QtWidgets.QHBoxLayout()
        button_row.addWidget(self.start_button)
        button_row.addWidget(self.stop_button)
        settings_layout.addLayout(button_row)

        settings_layout.addStretch()

        right_panel = QtWidgets.QVBoxLayout()
        status_group = QtWidgets.QGroupBox("Progress")
        status_layout = QtWidgets.QVBoxLayout(status_group)

        self.mode_label = QtWidgets.QLabel("Mode: -")
        self.bw_ch_label = QtWidgets.QLabel("BW/CH: -")
        self.step_label = QtWidgets.QLabel("Step: 0/0")
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setValue(0)

        status_layout.addWidget(self.mode_label)
        status_layout.addWidget(self.bw_ch_label)
        status_layout.addWidget(self.step_label)
        status_layout.addWidget(self.progress_bar)

        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)

        right_panel.addWidget(status_group)
        right_panel.addWidget(QtWidgets.QLabel("Live Log"))
        right_panel.addWidget(self.log_view, 1)

        main_layout.addWidget(settings_panel, 0)
        main_layout.addLayout(right_panel, 1)

    def _make_checkbox_group(self, title: str, values: List[int]) -> QtWidgets.QGroupBox:
        group = QtWidgets.QGroupBox(title)
        layout = QtWidgets.QVBoxLayout(group)
        for value in values:
            checkbox = QtWidgets.QCheckBox(str(value))
            checkbox.setChecked(True)
            layout.addWidget(checkbox)
        return group

    def _labeled(self, label: str, widget: QtWidgets.QWidget) -> QtWidgets.QWidget:
        container = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QtWidgets.QLabel(label))
        layout.addStretch()
        layout.addWidget(widget)
        return container

    def _get_checked_values(self, group: QtWidgets.QGroupBox) -> List[int]:
        values = []
        for child in group.findChildren(QtWidgets.QCheckBox):
            if child.isChecked():
                values.append(int(child.text()))
        return values

    def start_run(self) -> None:
        if self.worker:
            return

        bw_list = self._get_checked_values(self.bw_checks)
        ch_list = self._get_checked_values(self.ch_checks)
        if not bw_list or not ch_list:
            QtWidgets.QMessageBox.warning(self, "Invalid Selection", "Please select at least one BW and channel.")
            return

        mcs_mode = "auto" if self.mcs_auto_radio.isChecked() else "single"
        plan = RunPlan(
            mode=self.mode_combo.currentText(),
            bw_list=bw_list,
            ch_list=ch_list,
            mcs_mode="auto" if mcs_mode == "auto" else "single",
            mcs_value=self.mcs_spin.value(),
            duration=self.duration_spin.value(),
        )
        self.plan = plan
        self.completed_steps = 0
        self.total_steps = plan.total_steps()

        self.progress_bar.setMaximum(self.total_steps or 1)
        self.progress_bar.setValue(0)
        self.step_label.setText(f"Step: 0/{self.total_steps}")
        self.mode_label.setText(f"Mode: {plan.mode}")
        self.bw_ch_label.setText("BW/CH: -")
        self.log_view.clear()

        args = plan.to_args()
        self.worker = RunWorker(args)
        self.worker.log_line.connect(self.append_log)
        self.worker.finished.connect(self.finish_run)
        self.worker.start()

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)

    def stop_run(self) -> None:
        if self.worker:
            self.worker.stop()
            self.append_log("[GUI] Stop requested.")

    def append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)

        mode_match = MODE_RE.match(line)
        if mode_match:
            self.mode_label.setText(f"Mode: {mode_match.group(1)}")

        bw_match = BW_CH_RE.match(line)
        if bw_match:
            self.bw_ch_label.setText(f"BW/CH: {bw_match.group(1)} / {bw_match.group(2)}")

        if MCS_STEP_RE.match(line):
            self.completed_steps += 1
            self.progress_bar.setValue(self.completed_steps)
            self.step_label.setText(f"Step: {self.completed_steps}/{self.total_steps}")

    def finish_run(self, code: int) -> None:
        self.append_log(f"[GUI] Process finished with code {code}.")
        self.worker = None
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
