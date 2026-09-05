# tabs/alignment_tab.py
"""
This module defines the AlignmentTab, which drives the encoder electrical offset
optimisation. It is an optional refinement on top of ODrive's own offset calibration,
not a replacement for it: the motor and encoder must already be calibrated before
this sweep can run.
"""
from PySide6.QtWidgets import (
    QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QDoubleSpinBox, QSpinBox, QProgressBar, QMessageBox, QCheckBox
)
import math

from PySide6.QtCore import Qt, QEvent, QThread

from .base_tab import BaseTab
from .tuning_workers import CalibrationQualityWorker
from app_config import AppColors

# Rough per-point overhead for the idle/arm/disarm transitions around each measurement.
POINT_OVERHEAD_S = 1.0


class AlignmentTab(BaseTab):
    """Manages the UI tab for encoder offset alignment optimisation."""

    def __init__(self, main_window, parent=None):
        super().__init__(main_window, parent)
        self.align_thread = None
        self.align_worker = None
        self._setup_ui()
        self.retranslate_ui()

    def changeEvent(self, event):
        """Catches language change events to re-translate the UI."""
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate_ui()
        super().changeEvent(event)

    # ------------------------------------------------------------------ UI ---

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)

        self.explain_label = QLabel()
        self.explain_label.setWordWrap(True)
        self.explain_label.setStyleSheet(f"color: {AppColors.INFO};")
        main_layout.addWidget(self.explain_label)

        self.params_group = QGroupBox()
        params_layout = QFormLayout(self.params_group)

        self.runs_input = QSpinBox()
        self.runs_input.setRange(2, 10)
        self.runs_input.setValue(3)

        # Whole mechanical revolutions, so cogging averages out instead of leaving a
        # bias in whatever fraction of a turn the scan happened to cover.
        self.revolutions_input = QDoubleSpinBox()
        self.revolutions_input.setRange(1.0, 10.0)
        self.revolutions_input.setDecimals(0)
        self.revolutions_input.setValue(2.0)

        self.apply_check = QCheckBox()
        self.apply_check.setChecked(False)

        self.keep_scan_check = QCheckBox()
        self.keep_scan_check.setChecked(True)
        self.keep_scan_check.toggled.connect(self._on_keep_scan_toggled)
        self.revolutions_input.setEnabled(False)

        self.calib_current_input = QDoubleSpinBox()
        self.calib_current_input.setRange(1.0, 60.0)
        self.calib_current_input.setDecimals(1)
        self.calib_current_input.setValue(10.0)
        self.calib_current_input.setSuffix(" A")

        for widget in (self.runs_input, self.revolutions_input, self.calib_current_input):
            widget.valueChanged.connect(self._update_estimate)

        self.label_runs = QLabel()
        self.label_revolutions = QLabel()
        self.label_calib_current = QLabel()
        params_layout.addRow(self.label_runs, self.runs_input)
        params_layout.addRow(self.apply_check)
        params_layout.addRow(self.keep_scan_check)
        params_layout.addRow(self.label_revolutions, self.revolutions_input)
        params_layout.addRow(self.label_calib_current, self.calib_current_input)

        self.current_scan_label = QLabel()
        self.current_scan_label.setWordWrap(True)
        params_layout.addRow(self.current_scan_label)

        self.estimate_label = QLabel()
        params_layout.addRow(self.estimate_label)

        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet(f"color: {AppColors.WARNING}; font-weight: bold;")

        controls_row = QHBoxLayout()
        self.start_btn = QPushButton()
        self.start_btn.clicked.connect(self.start_alignment)
        self.cancel_btn = QPushButton()
        self.cancel_btn.clicked.connect(self.cancel_alignment)
        self.cancel_btn.setEnabled(False)
        self.restore_scan_btn = QPushButton()
        self.restore_scan_btn.clicked.connect(self.restore_default_scan)
        controls_row.addWidget(self.start_btn)
        controls_row.addWidget(self.cancel_btn)
        controls_row.addStretch()
        controls_row.addWidget(self.restore_scan_btn)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.status_label = QLabel()

        main_layout.addWidget(self.params_group)
        main_layout.addWidget(self.warning_label)
        main_layout.addLayout(controls_row)
        main_layout.addWidget(self.progress_bar)
        main_layout.addWidget(self.status_label)
        main_layout.addStretch()

    def retranslate_ui(self):
        """Updates all translatable texts in this tab."""
        self.explain_label.setText(self.tr(
            "ODrive's encoder calibration lands in a slightly different place every time it runs, "
            "and whichever single run you happened to get is the one you keep.\n\n"
            "This runs it several times, shows how far apart the results fall, and applies their "
            "average. The scatter is random rather than a bias, so averaging shrinks it by roughly "
            "the square root of the number of runs. It uses the native calibration exactly as it "
            "is; it just does not trust any single roll of it."
        ))
        self.params_group.setTitle(self.tr("Check Parameters"))
        self.label_runs.setText(self.tr("Calibrations to average:"))
        self.apply_check.setText(self.tr("Write the average to the board (off = measure only)"))
        self.apply_check.setToolTip(self.tr("Left off, this only reports how repeatable your calibration is and changes nothing.\nThe average is refused anyway when the runs disagree by more than 15 electrical degrees."))
        self.keep_scan_check.setText(self.tr("Keep the board's scan distance"))
        self.keep_scan_check.setToolTip(self.tr("Measured on a 15 pole pair hoverboard motor, the firmware default repeated more tightly than longer scans, and runs far quicker."))
        self.label_revolutions.setText(self.tr("Whole revolutions to scan:"))
        self.label_calib_current.setText(self.tr("Calibration current:"))
        self.runs_input.setToolTip(self.tr("More runs measure the spread better, and take proportionally longer."))
        self.revolutions_input.setToolTip(self.tr("Cogging repeats with mechanical position, so a scan covering whole revolutions lets it average out."))
        self.calib_current_input.setToolTip(self.tr("Higher current makes the rotor follow the commanded angle instead of sticking in cogging detents."))

        self.warning_label.setText(self.tr(
            "The motor will turn on its own during each calibration. Free the shaft before starting."
        ))
        self.start_btn.setText(self.tr("Check Calibration Quality"))
        self.cancel_btn.setText(self.tr("Cancel"))
        self.restore_scan_btn.setText(self.tr("Restore Default Scan"))
        self.restore_scan_btn.setToolTip(self.tr("Puts calib_scan_distance back to the firmware default of 16*pi electrical radians."))
        self._update_estimate()

    def _update_estimate(self):
        """
        Shows how long the check will take, and what the board's current scan distance
        works out to in mechanical revolutions, which is the number that matters.
        """
        # The scan runs forward then back at calib_scan_omega, 4*pi electrical rad/s by
        # default, plus the fixed overhead of arming and settling around each run.
        revolutions = self.revolutions_input.value()
        runs = self.runs_input.value()
        pole_pairs = self._pole_pairs()
        if self.keep_scan_check.isChecked():
            per_run = 8
        else:
            per_run = (2 * revolutions * pole_pairs * 2 * math.pi) / (4 * math.pi) + 4
        total = runs * per_run
        self.estimate_label.setText(
            self.tr("Estimated duration: about {0:.0f} min {1:.0f} s ({2} calibrations)")
            .format(total // 60, total % 60, runs))

        if pole_pairs and self.main_window.is_connected and self.main_window.odrv_proxy:
            try:
                distance = self.main_window.odrv_proxy.odrv.axis0.encoder.config.calib_scan_distance
                current_revs = distance / (2 * math.pi * pole_pairs)
                text = self.tr("The board currently scans {0:.2f} mechanical revolutions ({1} pole pairs).").format(
                    current_revs, pole_pairs)
                # The Encoder tab's own calibration gives up after 25 s, and the scan
                # runs out and back at calib_scan_omega, 4*pi electrical rad/s.
                scan_seconds = distance / (2 * math.pi)
                if scan_seconds > 20:
                    text += " " + self.tr(
                        "That takes about {0:.0f} s, which is past the 25 s limit on the Encoder "
                        "tab's own calibration button. Reduce it or that button will time out."
                    ).format(scan_seconds)
                    self.current_scan_label.setStyleSheet(f"color: {AppColors.ERROR};")
                else:
                    self.current_scan_label.setStyleSheet(f"color: {AppColors.SUCCESS};")
                self.current_scan_label.setText(text)
                return
            except Exception:
                pass
        self.current_scan_label.setText(self.tr("Connect to see what the board currently scans."))
        self.current_scan_label.setStyleSheet("font-style: italic;")

    def restore_default_scan(self):
        """
        Puts calib_scan_distance back to the firmware default.

        An earlier version of this tab left a longer scan behind, which made the Encoder
        tab's own calibration time out, since that scan no longer fitted in its 25 second
        limit. A board carrying that value needs it written back, and asking the user to
        type it into the terminal is a poor way to fix damage this branch caused.
        """
        odrv = self.get_odrv()
        if not odrv:
            return
        default_distance = 16.0 * math.pi
        try:
            previous = odrv.axis0.encoder.config.calib_scan_distance
            odrv.axis0.encoder.config.calib_scan_distance = default_distance
        except Exception as e:
            QMessageBox.critical(self, self.tr("Error"), self.tr(
                "Could not write the scan distance.\n\nDetails: {0}").format(e))
            return
        self._update_estimate()
        QMessageBox.information(self, self.tr("Scan Distance Restored"), self.tr(
            "Scan distance set from {0:.1f} to {1:.1f} electrical radians, which scans in about "
            "8 seconds.\n\nSave the configuration to keep it, or it returns on the next reboot."
        ).format(previous, default_distance))

    def _on_keep_scan_toggled(self, checked):
        self.revolutions_input.setEnabled(not checked)
        self._update_estimate()

    def _pole_pairs(self):
        if not self.main_window.is_connected or not self.main_window.odrv_proxy:
            return 0
        try:
            return int(self.main_window.odrv_proxy.odrv.axis0.motor.config.pole_pairs)
        except Exception:
            return 0

    # ------------------------------------------------------------- routine ---

    def _preflight(self, odrv):
        """
        Checks the prerequisites the sweep depends on. Returns True when it is safe to
        proceed. The index warning is advisory: the sweep still works without it, but
        the result cannot survive a reboot.
        """
        try:
            if not odrv.axis0.motor.is_calibrated:
                QMessageBox.warning(self, self.tr("Action Required"), self.tr(
                    "The motor is not calibrated.\n\nRun the motor calibration first."))
                return False
            if not odrv.axis0.encoder.is_ready:
                QMessageBox.warning(self, self.tr("Action Required"), self.tr(
                    "The encoder is not ready.\n\nRun the encoder calibration first."))
                return False
            if not odrv.axis0.encoder.config.use_index:
                proceed = QMessageBox.question(self, self.tr("Index Not Enabled"), self.tr(
                    "This encoder is not set to use the Z index.\n\nWithout it each calibration "
                    "starts from a different reference, so the spread will look worse than it "
                    "really is.\n\nRun the check anyway?"),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if proceed != QMessageBox.StandardButton.Yes:
                    return False
        except Exception as e:
            QMessageBox.critical(self, self.tr("Error"), self.tr(
                "Could not read the axis state.\n\nDetails: {0}").format(e))
            return False
        return True

    def start_alignment(self):
        """Validates prerequisites and launches the sweep in a background thread."""
        odrv = self.get_odrv()
        if not odrv:
            return
        if self.align_thread is not None:
            return
        if not self._preflight(odrv):
            return

        confirm = QMessageBox.question(self, self.tr("Confirm"), self.tr(
            "The motor will run its calibration several times, turning on its own.\n\n"
            "Is the shaft free and clear?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress_bar.setValue(0)

        self.align_thread = QThread()
        self.align_worker = CalibrationQualityWorker(
            odrv,
            runs=self.runs_input.value(),
            mechanical_revolutions=self.revolutions_input.value(),
            calibration_current=self.calib_current_input.value(),
            keep_scan_distance=self.keep_scan_check.isChecked(),
            apply_average=self.apply_check.isChecked(),
        )
        self.align_worker.moveToThread(self.align_thread)

        self.align_thread.started.connect(self.align_worker.run)
        self.align_worker.progress.connect(self._on_progress)
        self.align_worker.result.connect(self._on_result)
        self.align_worker.finished.connect(self.align_thread.quit)
        self.align_worker.finished.connect(self.align_worker.deleteLater)
        self.align_thread.finished.connect(self.align_thread.deleteLater)
        self.align_thread.finished.connect(self._on_thread_finished)
        self.align_thread.start()

    def cancel_alignment(self):
        """Asks the worker to unwind; it restores the original offset on its way out."""
        if self.align_worker:
            self.align_worker.stop()
            self.cancel_btn.setEnabled(False)
            self.status_label.setText(self.tr("Cancelling, restoring the drive..."))

    def _on_progress(self, message, percent):
        self.status_label.setText(message)
        self.progress_bar.setValue(percent)

    def _on_result(self, success, message, suggested):
        if success:
            self.progress_bar.setValue(100)
            self.status_label.setText(self.tr("Check complete."))
            QMessageBox.information(self, self.tr("Check Complete"), message)
        else:
            self.status_label.setText(self.tr("Check did not complete."))
            QMessageBox.warning(self, self.tr("Check Failed"), message)

    def _on_thread_finished(self):
        self.align_thread, self.align_worker = None, None
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)

    # ------------------------------------------------------------ BaseTab ---

    def populate_fields(self):
        """Nothing to load: the sweep reads what it needs when it runs."""
        pass

    def apply_config(self):
        """The sweep writes the offset itself; there is no separate apply step."""
        pass
