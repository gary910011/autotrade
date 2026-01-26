from __future__ import annotations

import re
import sys
import subprocess
from dataclasses import dataclass
from typing import List

from PyQt5 import QtCore, QtGui, QtWidgets

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

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
            )
            if not self.process.stdout:
                self.finished.emit(1)
                return

            for raw_line in self.process.stdout:
                if self._stop_requested:
                    break
                line = self._decode_line(raw_line)
                if line:
                    self.log_line.emit(line.rstrip("\n"))
        except Exception as exc:
            self.log_line.emit(f"[GUI][ERROR] Runner crashed: {exc}")
            self.finished.emit(1)
            return
        finally:
            if self._stop_requested and self.process:
                self._terminate_process()

        if self.process:
            self.process.wait()
            self.finished.emit(self.process.returncode or 0)

    def stop(self) -> None:
        self._stop_requested = True
        if self.process:
            self._terminate_process()

    def _terminate_process(self) -> None:
        if not self.process:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()

    @staticmethod
    def _decode_line(raw_line: bytes) -> str:
        if isinstance(raw_line, str):
            return raw_line
        utf8_text = raw_line.decode("utf-8", errors="replace")
        if "\ufffd" not in utf8_text:
            return utf8_text
        cp950_text = raw_line.decode("cp950", errors="replace")
        if cp950_text.count("\ufffd") < utf8_text.count("\ufffd"):
            return cp950_text
        return utf8_text


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
        self._apply_style()

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

        report_group = QtWidgets.QGroupBox("Report")
        report_layout = QtWidgets.QVBoxLayout(report_group)

        self.log_dir_edit = QtWidgets.QLineEdit(config.LOG_DIR)
        self.log_dir_button = QtWidgets.QPushButton("Browse")
        self.log_dir_button.clicked.connect(self.select_log_dir)
        report_layout.addWidget(self._labeled_with_button("Log Folder", self.log_dir_edit, self.log_dir_button))

        self.excel_path_edit = QtWidgets.QLineEdit(config.EXCEL_PATH)
        self.excel_path_button = QtWidgets.QPushButton("Browse")
        self.excel_path_button.clicked.connect(self.select_excel_path)
        report_layout.addWidget(
            self._labeled_with_button("Excel Template", self.excel_path_edit, self.excel_path_button)
        )

        self.excel_button = QtWidgets.QPushButton("Generate Excel")
        self.excel_button.clicked.connect(self.generate_excel)
        report_layout.addWidget(self.excel_button)

        settings_layout.addWidget(report_group)

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

    def _labeled_with_button(
        self,
        label: str,
        widget: QtWidgets.QWidget,
        button: QtWidgets.QPushButton,
    ) -> QtWidgets.QWidget:
        container = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QtWidgets.QLabel(label))
        layout.addWidget(widget, 1)
        layout.addWidget(button)
        return container

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
            self._stop_iperf_on_dut()

    def _stop_iperf_on_dut(self) -> None:
        try:
            subprocess.run(
                [sys.executable, "-c", "from dut import stop_all_iperf_clients; stop_all_iperf_clients()"],
                check=False,
            )
            self.append_log("[GUI] Sent iperf stop request to DUT.")
        except Exception as exc:
            self.append_log(f"[GUI][WARN] Failed to stop iperf on DUT: {exc}")

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

    def closeEvent(self, event: QtCore.QEvent) -> None:
        if self.worker:
            self.append_log("[GUI] Closing: stopping runner...")
            self.worker.stop()
            self._stop_iperf_on_dut()
            self.worker.wait(2000)
        event.accept()

    def select_log_dir(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Log Folder", self.log_dir_edit.text())
        if path:
            self.log_dir_edit.setText(path)

    def select_excel_path(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select Excel Template",
            self.excel_path_edit.text(),
            "Excel Files (*.xlsx);;All Files (*)",
        )
        if path:
            self.excel_path_edit.setText(path)

    def generate_excel(self) -> None:
        log_dir = self.log_dir_edit.text().strip()
        excel_path = self.excel_path_edit.text().strip()
        if not log_dir or not excel_path:
            QtWidgets.QMessageBox.warning(self, "Missing Path", "Please select log folder and Excel template.")
            return

        cmd = [
            sys.executable,
            "-u",
            "excel.py",
            "--log-dir",
            log_dir,
            "--excel-path",
            excel_path,
        ]
        self.append_log(f"[GUI] Generating Excel: {' '.join(cmd)}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.stdout:
                self.append_log(result.stdout.strip())
            if result.stderr:
                self.append_log(result.stderr.strip())
        except Exception as exc:
            self.append_log(f"[GUI][ERROR] Excel generation failed: {exc}")

    def _apply_style(self) -> None:
        font = QtGui.QFont("Calibri", 10)
        self.setFont(font)
        self.setStyleSheet(
            """
            QMainWindow {
                background: #f5f6f8;
            }
            QWidget {
                font-family: "Calibri", "MingLiU";
            }
            QGroupBox {
                font-weight: 600;
                border: 1px solid #d0d4da;
                border-radius: 8px;
                margin-top: 12px;
                padding: 8px;
                background: #ffffff;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
                color: #2b2f36;
            }
            QLabel {
                color: #2b2f36;
            }
            QPushButton {
                background: #2f6fed;
                color: white;
                padding: 6px 12px;
                border-radius: 6px;
            }
            QPushButton:disabled {
                background: #a0a6b1;
            }
            QLineEdit, QSpinBox, QComboBox {
                background: #ffffff;
                padding: 4px 6px;
                border: 1px solid #cfd4db;
                border-radius: 6px;
            }
            QPlainTextEdit {
                border: 1px solid #cfd4db;
                border-radius: 6px;
                background: #ffffff;
            }
            QProgressBar {
                border: 1px solid #cfd4db;
                border-radius: 6px;
                text-align: center;
                background: #ffffff;
            }
            QProgressBar::chunk {
                background-color: #2f6fed;
                border-radius: 6px;
            }
            """
        )


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
