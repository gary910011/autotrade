from __future__ import annotations

import os
import re
import sys
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Callable

from PyQt5 import QtCore, QtGui, QtWidgets

import config


# Accept both:
#   "→ MCS 8"
#   "→ MCS/Rate 15"
MCS_STEP_RE = re.compile(r"^→ (?:MCS|MCS/Rate) (\d+)")

# Accept both:
#   "=== BW=20 CH=36 ==="
#   "=== BAND=2G BW=20 CH=6 ==="
BW_CH_RE = re.compile(r"^=== (?:BAND=\w+\s+)?BW=(\d+)\s+CH=(\d+)")

# Accept both:
#   "=== MODE AP_TX ==="
#   "=== MODE AP_TX BAND=2G ==="
MODE_RE = re.compile(r"^=== MODE (.+) ===")


# ==================================================
# GUI Mode → actual run sequence
# ==================================================
GUI_MODE_ITEMS = [
    "AP_TX",
    "AP_RX",
    "AP_TX&RX",
    "STA_TX",
    "STA_RX",
    "STA_TX&RX",
    "ALL",
]

GUI_MODE_SEQ = {
    "AP_TX": ["AP_TX"],
    "AP_RX": ["AP_RX"],
    "AP_TX&RX": ["AP_TX", "AP_RX"],
    "STA_TX": ["STA_TX"],
    "STA_RX": ["STA_RX"],
    "STA_TX&RX": ["STA_TX", "STA_RX"],
    # ALL = STA_TX&RX + AP_TX&RX
    "ALL": ["STA_TX", "STA_RX", "AP_TX", "AP_RX"],
}


@dataclass
class RunPlan:
    mode: str
    band: str
    bw_list: List[int]
    ch_list: List[int]
    mcs_mode: str
    mcs_value: int
    duration: int

    def resolved_mode_seq(self) -> List[str]:
        return GUI_MODE_SEQ.get(self.mode, [self.mode])

    def total_steps(self) -> int:
        phases = len(self.resolved_mode_seq())

        # 2.4G auto sweep is fixed: 11n (15~8)=8 steps + 54 + 11 => 10 steps
        # If user somehow uses single (even though GUI locks it), keep sane estimate.
        if self.band == "2G":
            if self.mcs_mode != "auto":
                return phases * (len(self.bw_list) * len(self.ch_list))
            return phases * (len(self.bw_list) * len(self.ch_list) * 10)

        # 5G legacy behavior
        if self.mcs_mode != "auto":
            return phases * (len(self.bw_list) * len(self.ch_list))

        steps_one_phase = 0
        for bw in self.bw_list:
            if bw == 20:
                steps_one_phase += len(self.ch_list) * 9
            else:
                steps_one_phase += len(self.ch_list) * 10
        return phases * steps_one_phase

    def to_args_for_mode(self, mode: str) -> List[str]:
        args = ["--mode", mode, "--band", self.band]
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


class RunWorker(QtCore.QObject):
    log_line = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal(int)

    def __init__(self, args: List[str]):
        super().__init__()
        self.args = args
        self.process = QtCore.QProcess()
        self.process.readyReadStandardOutput.connect(self._on_stdout)
        self.process.readyReadStandardError.connect(self._on_stderr)
        self.process.finished.connect(self._on_finished)

    def start(self):
        cmd = sys.executable
        full_args = ["-u", "main.py", *self.args]

        env = QtCore.QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUTF8", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        self.process.setProcessEnvironment(env)

        self.log_line.emit(f"[GUI] Running: {cmd} {' '.join(full_args)}")
        self.process.start(cmd, full_args)

    def stop(self):
        # 1) Stop Windows-side process
        if self.process.state() != QtCore.QProcess.NotRunning:
            self.log_line.emit("[GUI] Force stopping test...")
            self.process.kill()

        # 2) Fire-and-forget Linux cleanup
        try:
            subprocess.Popen(
                [sys.executable, "stop_cleanup.py"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=os.path.dirname(os.path.abspath(__file__)),
            )
        except Exception as e:
            self.log_line.emit(f"[GUI][WARN] cleanup launch failed: {e}")

    def _on_stdout(self):
        data = self.process.readAllStandardOutput()
        text = bytes(data).decode("utf-8", errors="replace")
        for line in text.splitlines():
            self.log_line.emit(line)

    def _on_stderr(self):
        data = self.process.readAllStandardError()
        text = bytes(data).decode("utf-8", errors="replace")
        for line in text.splitlines():
            self.log_line.emit(f"[STDERR] {line}")

    def _on_finished(self, exit_code, _status):
        self.finished.emit(exit_code)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Wi-Fi Tput Automation GUI")
        self.resize(1000, 700)

        self.worker: Optional[RunWorker] = None
        self.plan: Optional[RunPlan] = None

        self.completed_steps = 0
        self.total_steps = 0

        # phase control for composite mode
        self._phase_seq: List[str] = []
        self._phase_idx: int = 0

        # ENV state (AUTO only)
        self.env_preparing: bool = False
        self.env_process: Optional[QtCore.QProcess] = None
        self._current_env_target: Optional[str] = None  # None / "ap" / "sta"

        self._build_ui()
        self._apply_style()
        self._refresh_start_enabled()

    # ==================================================
    # UI
    # ==================================================
    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)

        main_layout = QtWidgets.QHBoxLayout(central)

        settings_panel = QtWidgets.QGroupBox("Test Settings")
        settings_layout = QtWidgets.QVBoxLayout(settings_panel)

        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItems(GUI_MODE_ITEMS)
        settings_layout.addWidget(self._labeled("Mode", self.mode_combo))

        # -------- Band selector --------
        self.band_combo = QtWidgets.QComboBox()
        self.band_combo.addItems(["5G", "2G"])
        settings_layout.addWidget(self._labeled("Band", self.band_combo))

        # Hint label (STA SSID)
        self.band_hint_label = QtWidgets.QLabel("")
        self.band_hint_label.setStyleSheet("color: #555555; font-size: 11px;")
        settings_layout.addWidget(self.band_hint_label)

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
        report_layout.addWidget(self._labeled_with_button("Excel Template", self.excel_path_edit, self.excel_path_button))

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

        # Band hooks (must be after widgets exist)
        self.band_combo.currentTextChanged.connect(self._update_band_hint)
        self.band_combo.currentTextChanged.connect(self._apply_band_constraints)
        self._update_band_hint(self.band_combo.currentText())
        self._apply_band_constraints(self.band_combo.currentText())

    def _make_checkbox_group(self, title: str, values: List[int]) -> QtWidgets.QGroupBox:
        group = QtWidgets.QGroupBox(title)
        layout = QtWidgets.QVBoxLayout(group)
        for value in values:
            checkbox = QtWidgets.QCheckBox(str(value))
            checkbox.setChecked(True)
            layout.addWidget(checkbox)
        return group

    def _labeled_with_button(self, label: str, widget: QtWidgets.QWidget, button: QtWidgets.QPushButton) -> QtWidgets.QWidget:
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

    # ==================================================
    # Band constraints (NEW)
    # ==================================================
    def _apply_band_constraints(self, band: str) -> None:
        is_2g = (band == "2G")

        # BW: 2G fixed 20
        for cb in self.bw_checks.findChildren(QtWidgets.QCheckBox):
            if is_2g:
                cb.setChecked(cb.text() == "20")
                cb.setEnabled(False)
            else:
                cb.setEnabled(True)

        # CH: 2G fixed 6
        for cb in self.ch_checks.findChildren(QtWidgets.QCheckBox):
            if is_2g:
                cb.setChecked(cb.text() == "6")
                cb.setEnabled(False)
            else:
                cb.setEnabled(True)

        # MCS: 2G force auto
        if is_2g:
            self.mcs_auto_radio.setChecked(True)
            self.mcs_auto_radio.setEnabled(False)
            self.mcs_single_radio.setEnabled(False)
            self.mcs_spin.setEnabled(False)
        else:
            self.mcs_auto_radio.setEnabled(True)
            self.mcs_single_radio.setEnabled(True)
            self.mcs_spin.setEnabled(self.mcs_single_radio.isChecked())

    def _update_band_hint(self, band: str) -> None:
        if band == "2G":
            self.band_hint_label.setText('STA SSID → "Garmin-1234" (2.4 GHz) | 2G auto: MCS15~8, 54M, 11M')
        else:
            self.band_hint_label.setText('STA SSID → "Garmin-5678" (5 GHz)')

    # ==================================================
    # ENV helpers (AUTO only)
    # ==================================================
    def _refresh_start_enabled(self) -> None:
        can_start = (self.worker is None) and (not self.env_preparing)
        self.start_button.setEnabled(bool(can_start))

    def _set_env_buttons_enabled(self, enabled: bool) -> None:
        # Manual Prepare UI removed → no-op
        return

    def _env_target_for_phase_mode(self, phase_mode: str) -> str:
        # STA_* tests require ASUS as AP -> restore target "ap"
        if phase_mode.startswith("STA_"):
            return "ap"
        # AP_* tests require ASUS as STA -> restore target "sta"
        return "sta"

    def _prepare_env_internal(self, target: str, on_success: Optional[Callable[[], None]] = None) -> None:
        if self.worker:
            self.append_log("[ENV][WARN] Cannot prepare environment while test is running.")
            return
        if self.env_preparing:
            self.append_log("[ENV][WARN] Environment preparation already running.")
            return

        cfg = "/jffs/ap.cfg" if target == "ap" else "/jffs/sta.cfg"

        self.env_preparing = True
        self._refresh_start_enabled()
        self._set_env_buttons_enabled(False)

        human = "ASUS as AP (for STA_* tests)" if target == "ap" else "ASUS as STA (for AP_* tests)"
        self.append_log(f"[ENV] Preparing ({human}) ...")
        self.append_log(f"[ENV] cmd: {sys.executable} -u core/restore_asus_cfg.py --target {target} --cfg {cfg}")

        proc = QtCore.QProcess(self)
        self.env_process = proc

        proc.setProgram(sys.executable)
        band = self.band_combo.currentText()  # "5G" or "2G"
        proc.setArguments(["-u", "core/restore_asus_cfg.py", "--target", target, "--cfg", cfg, "--band", band])

        env = QtCore.QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUTF8", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        proc.setProcessEnvironment(env)

        def _drain_stdout():
            data = proc.readAllStandardOutput()
            text = bytes(data).decode("utf-8", errors="replace")
            for line in text.splitlines():
                self.append_log(line)

        def _drain_stderr():
            data = proc.readAllStandardError()
            text = bytes(data).decode("utf-8", errors="replace")
            for line in text.splitlines():
                self.append_log(f"[ENV][STDERR] {line}")

        proc.readyReadStandardOutput.connect(_drain_stdout)
        proc.readyReadStandardError.connect(_drain_stderr)

        def _finished(code, _status):
            self.env_preparing = False
            self._set_env_buttons_enabled(True)
            self._refresh_start_enabled()

            if code == 0:
                self._current_env_target = target
                self.append_log(f"✅ ENV READY ({target})")
                if on_success:
                    on_success()
            else:
                self.append_log(f"❌ ENV PREP FAILED (code={code})")
                if self.stop_button.isEnabled():
                    self.append_log("[GUI] Abort remaining phases due to ENV prep failure.")
                    self.worker = None
                    self.stop_button.setEnabled(False)
                    self._refresh_start_enabled()

            proc.deleteLater()
            self.env_process = None

        proc.finished.connect(_finished)
        proc.start()

    # ==================================================
    # Composite mode runner (AUTO-ENV integrated)
    # ==================================================
    def start_run(self) -> None:
        if self.worker or self.env_preparing:
            return

        band = self.band_combo.currentText()

        bw_list = self._get_checked_values(self.bw_checks)
        ch_list = self._get_checked_values(self.ch_checks)

        # Hard guardrail for 2G (even if UI got modified later)
        if band == "2G":
            bw_list = [20]
            ch_list = [6]

        if not bw_list or not ch_list:
            QtWidgets.QMessageBox.warning(self, "Invalid Selection", "Please select at least one BW and channel.")
            return

        # 2G forces auto in UI; keep safe here too
        if band == "2G":
            mcs_mode = "auto"
        else:
            mcs_mode = "auto" if self.mcs_auto_radio.isChecked() else "single"

        plan = RunPlan(
            mode=self.mode_combo.currentText(),
            band=band,
            bw_list=bw_list,
            ch_list=ch_list,
            mcs_mode="auto" if mcs_mode == "auto" else "single",
            mcs_value=self.mcs_spin.value(),
            duration=self.duration_spin.value(),
        )
        self.plan = plan

        self._phase_seq = plan.resolved_mode_seq()
        self._phase_idx = 0

        self.completed_steps = 0
        self.total_steps = plan.total_steps()

        self.progress_bar.setMaximum(self.total_steps or 1)
        self.progress_bar.setValue(0)
        self.step_label.setText(f"Step: 0/{self.total_steps}")
        self.mode_label.setText(self._format_mode_label())
        self.bw_ch_label.setText("BW/CH: -")
        self.log_view.clear()

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self._set_env_buttons_enabled(False)

        self._start_phase_with_auto_env()

    def _format_mode_label(self) -> str:
        if not self._phase_seq:
            return "Mode: -"
        if len(self._phase_seq) == 1:
            return f"Mode: {self._phase_seq[0]}"
        return f"Mode: {self._phase_seq[0]} → {self._phase_seq[-1]} ({self._phase_idx+1}/{len(self._phase_seq)})"

    def _start_phase_with_auto_env(self) -> None:
        if not self.plan:
            return

        if self._phase_idx >= len(self._phase_seq):
            self.append_log("✅ DONE (all phases)")
            self.worker = None
            self.stop_button.setEnabled(False)
            self._set_env_buttons_enabled(True)
            self._refresh_start_enabled()
            return

        phase_mode = self._phase_seq[self._phase_idx]
        needed_target = self._env_target_for_phase_mode(phase_mode)

        self.mode_label.setText(self._format_mode_label())
        self.append_log(f"[GUI] ===== Phase {self._phase_idx+1}/{len(self._phase_seq)}: {phase_mode} =====")

        def _launch_worker():
            if not self.plan or not self.stop_button.isEnabled():
                return
            self._start_phase_worker(phase_mode)

        if self._current_env_target != needed_target:
            self.append_log(f"[GUI] AUTO ENV: {self._current_env_target} -> {needed_target}")
            self._prepare_env_internal(target=needed_target, on_success=_launch_worker)
        else:
            _launch_worker()

    def _start_phase_worker(self, phase_mode: str) -> None:
        if not self.plan:
            return

        args = self.plan.to_args_for_mode(phase_mode)
        self.worker = RunWorker(args)
        self.worker.log_line.connect(self.append_log)
        self.worker.finished.connect(self.finish_run)
        self.worker.start()

    def stop_run(self) -> None:
        # Stop test phase
        if self.worker:
            try:
                self.worker.stop()
            except Exception:
                pass
            self.append_log("[GUI] Stop requested.")
            self.worker = None

        # Stop env tool if running
        if self.env_process and self.env_process.state() != QtCore.QProcess.NotRunning:
            try:
                self.env_process.kill()
                self.append_log("[GUI] ENV tool killed.")
            except Exception:
                pass
            self.env_process = None
            self.env_preparing = False

        # Linux cleanup (fire-and-forget)
        try:
            subprocess.Popen(
                [sys.executable, "stop_cleanup.py"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=os.path.dirname(os.path.abspath(__file__)),
            )
            self.append_log("[GUI] Cleanup launched (DUT iperf will be killed).")
        except Exception as exc:
            self.append_log(f"[GUI][WARN] Failed to launch cleanup: {exc}")

        self.stop_button.setEnabled(False)
        self._set_env_buttons_enabled(True)
        self._refresh_start_enabled()

    # ==================================================
    # Logging / progress parse
    # ==================================================
    def append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)

        bw_match = BW_CH_RE.match(line)
        if bw_match:
            self.bw_ch_label.setText(f"BW/CH: {bw_match.group(1)} / {bw_match.group(2)}")

        if MCS_STEP_RE.match(line):
            self.completed_steps += 1
            self.progress_bar.setValue(self.completed_steps)
            self.step_label.setText(f"Step: {self.completed_steps}/{self.total_steps}")

        # MODE_RE currently not used for UI label; kept for compatibility if you extend

    def finish_run(self, code: int) -> None:
        self.append_log(f"[GUI] Phase finished with code {code}.")
        self.worker = None

        if code != 0:
            self.append_log("[GUI] Abort remaining phases due to non-zero exit.")
            self.stop_button.setEnabled(False)
            self._set_env_buttons_enabled(True)
            self._refresh_start_enabled()
            return

        self._phase_idx += 1
        self._start_phase_with_auto_env()

    def closeEvent(self, event: QtCore.QEvent) -> None:
        if self.worker:
            try:
                self.worker.stop()
            except Exception:
                pass
            self.worker = None

        if self.env_process and self.env_process.state() != QtCore.QProcess.NotRunning:
            try:
                self.env_process.kill()
            except Exception:
                pass
            self.env_process = None

        event.accept()

    # ==================================================
    # Misc UI actions
    # ==================================================
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
            "utils/excel.py",
            "--log-dir",
            log_dir,
            "--excel-path",
            excel_path,
        ]
        self.append_log(f"[GUI] Generating Excel: {' '.join(cmd)}")

        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                env=env,
            )
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
