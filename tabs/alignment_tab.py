# tabs/alignment_tab.py
"""
This module defines the AlignmentTab, which drives the encoder electrical offset
optimisation. It is an optional refinement on top of ODrive's own offset calibration,
not a replacement for it: the motor and encoder must already be calibrated before
this sweep can run.
"""
from PySide6.QtWidgets import (
    QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QDoubleSpinBox, QSpinBox, QProgressBar, QMessageBox
)
from PySide6.QtCore import Qt, QEvent, QThread

from .base_tab import BaseTab
from .tuning_workers import OffsetAlignmentWorker
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

        self.velocity_input = QDoubleSpinBox()
        self.velocity_input.setRange(0.5, 20.0)
        self.velocity_input.setDecimals(1)
        self.velocity_input.setValue(3.0)
        self.velocity_input.setSuffix(" turns/s")

        self.current_input = QDoubleSpinBox()
        self.current_input.setRange(1.0, 60.0)
        self.current_input.setDecimals(1)
        # Only has to spin an unloaded motor at the test speed. Higher buys nothing and
        # heats the motor at the badly commutated offsets; the worker warns if it is low.
        self.current_input.setValue(5.0)
        self.current_input.setSuffix(" A")

        # 32 coarse and 21 fine points land within a fraction of an electrical degree
        # of the true optimum; coarser grids leave over a degree of residual error.
        self.coarse_input = QSpinBox()
        self.coarse_input.setRange(8, 64)
        self.coarse_input.setValue(32)

        self.fine_input = QSpinBox()
        self.fine_input.setRange(3, 31)
        self.fine_input.setValue(21)

        self.revolutions_input = QSpinBox()
        self.revolutions_input.setRange(1, 10)
        self.revolutions_input.setValue(2)

        self.settle_input = QDoubleSpinBox()
        self.settle_input.setRange(0.2, 5.0)
        self.settle_input.setDecimals(1)
        self.settle_input.setValue(1.0)
        self.settle_input.setSuffix(" s")

        for widget in (self.velocity_input, self.current_input, self.coarse_input,
                       self.fine_input, self.revolutions_input, self.settle_input):
            widget.valueChanged.connect(self._update_estimate)

        self.label_velocity = QLabel()
        self.label_current = QLabel()
        self.label_coarse = QLabel()
        self.label_fine = QLabel()
        self.label_revolutions = QLabel()
        self.label_settle = QLabel()
        params_layout.addRow(self.label_velocity, self.velocity_input)
        params_layout.addRow(self.label_current, self.current_input)
        params_layout.addRow(self.label_coarse, self.coarse_input)
        params_layout.addRow(self.label_fine, self.fine_input)
        params_layout.addRow(self.label_revolutions, self.revolutions_input)
        params_layout.addRow(self.label_settle, self.settle_input)

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
        controls_row.addWidget(self.start_btn)
        controls_row.addWidget(self.cancel_btn)
        controls_row.addStretch()

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
            "ODrive's offset calibration pushes the rotor against cogging and friction, so on a "
            "direct drive motor it can settle a few electrical degrees off. That error wastes "
            "current as heat without producing torque. This sweep spins the motor with no load at "
            "many candidate offsets and keeps the one that produces the most torque per amp."
        ))
        self.params_group.setTitle(self.tr("Sweep Parameters"))
        self.label_velocity.setText(self.tr("Test velocity (unused):"))
        self.label_current.setText(self.tr("Current limit during sweep:"))
        self.label_coarse.setText(self.tr("Coarse points:"))
        self.label_fine.setText(self.tr("Fine points:"))
        self.label_revolutions.setText(self.tr("Revolutions sampled per point:"))
        self.label_settle.setText(self.tr("Settle time per point:"))

        self.velocity_input.setToolTip(self.tr("Speed the motor spins at while each offset is scored."))
        self.current_input.setToolTip(self.tr("Temporarily replaces the motor current limit during the sweep.\n\nOnly needs to be high enough to spin the motor at the test speed when well aligned. Raising it further adds heat at the badly commutated offsets without improving the result."))
        self.coarse_input.setToolTip(self.tr("How many offsets are tested across one full electrical revolution."))
        self.fine_input.setToolTip(self.tr("Extra points tested around the coarse winner."))
        self.revolutions_input.setToolTip(self.tr("Sampling over whole mechanical revolutions averages cogging out."))
        self.settle_input.setToolTip(self.tr("Time to let the speed stabilise before sampling starts."))

        self.warning_label.setText(self.tr(
            "The motor will spin on its own, in both directions, including at badly commutated "
            "offsets where it jerks and draws current. Free the shaft and remove anything attached "
            "to the wheel before starting."
        ))
        self.start_btn.setText(self.tr("Start Alignment Sweep"))
        self.cancel_btn.setText(self.tr("Cancel"))
        self._update_estimate()

    def _update_estimate(self):
        """Shows roughly how long the sweep will take, so the duration is no surprise."""
        points = (self.coarse_input.value() + self.fine_input.value()) * 2
        per_point = (self.settle_input.value()
                     + self.revolutions_input.value() / max(self.velocity_input.value(), 0.1)
                     + POINT_OVERHEAD_S)
        total = points * per_point
        self.estimate_label.setText(
            self.tr("Estimated duration: about {0:.0f} min {1:.0f} s ({2} measurements)")
            .format(total // 60, total % 60, points))

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
                    "This encoder is not set to use the Z index.\n\nWithout it the offset is "
                    "recalculated on every boot, so the value found here cannot be saved "
                    "permanently.\n\nRun the sweep anyway?"),
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
            "The motor is about to spin unattended in both directions for several minutes.\n\n"
            "Is the shaft free and clear?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self.start_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress_bar.setValue(0)

        self.align_thread = QThread()
        self.align_worker = OffsetAlignmentWorker(
            odrv,
            velocity=self.velocity_input.value(),
            current_limit=self.current_input.value(),
            coarse_points=self.coarse_input.value(),
            fine_points=self.fine_input.value(),
            revolutions=self.revolutions_input.value(),
            settle_s=self.settle_input.value(),
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

    def _on_result(self, success, message):
        if success:
            self.progress_bar.setValue(100)
            self.status_label.setText(self.tr("Alignment complete."))
            QMessageBox.information(self, self.tr("Alignment Complete"), message)
        else:
            self.status_label.setText(self.tr("Alignment did not complete."))
            QMessageBox.warning(self, self.tr("Alignment Failed"), message)

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
