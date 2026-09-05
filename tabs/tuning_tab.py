# tabs/tuning_tab.py
"""
This module defines the TuningTab class, which measures the motor's torque constant
(Kt, in Nm/A) from a known static load rather than estimating it from the motor's
Kv rating.

Both supported setups reduce to the same physics:

    torque = mass * g * radius * cos(angle)

and differ only in how the load hangs off the shaft. With the torque known, Kt comes
from the phase current the motor needs to hold that load:

    Kt = torque / Iq

Capturing several points with different masses and fitting a line through them is
preferred over a single point: the slope of torque against Iq is Kt, while the
intercept absorbs the arm's own weight, static friction and cogging, none of which
a single-point division can separate out.
"""
import math

from PySide6.QtWidgets import (
    QLabel, QLineEdit, QPushButton, QVBoxLayout, QHBoxLayout, QFormLayout,
    QGroupBox, QComboBox, QDoubleSpinBox, QSpinBox, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QAbstractItemView, QProgressBar
)
from PySide6.QtGui import QDoubleValidator
from PySide6.QtCore import Qt, QEvent, QThread

from .base_tab import BaseTab
from .tuning_workers import BackEmfKtWorker
from app_config import AppColors, AppMessages

GRAVITY = 9.80665

# Number of telemetry samples averaged when capturing a point. The worker emits
# telemetry every AppConstants.TELEMETRY_INTERVAL_S (0.1 s), so this is about one
# second of averaging, which settles the noise on an instantaneous Iq reading.
CURRENT_SAMPLE_WINDOW = 10

METHOD_LEVER_SCALE = 0
METHOD_HANGING_WEIGHT = 1


class TuningTab(BaseTab):
    """
    Manages the UI tab for measuring the motor torque constant from a known load.
    """

    def __init__(self, main_window, parent=None):
        """Initializes the TuningTab, setting up UI and measurement state."""
        super().__init__(main_window, parent)
        self.points = []          # list of (mass_kg, iq_a) captured by the user
        self.current_samples = [] # rolling window of recent Iq readings
        self.live_current = None
        self.measured_kt = None
        self.backemf_kt = None
        self.auto_thread = None
        self.auto_worker = None
        self._setup_ui()
        self.retranslate_ui()

    def changeEvent(self, event):
        """Catches language change events to re-translate the UI."""
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate_ui()
        super().changeEvent(event)

    def showEvent(self, event):
        """Refreshes the currently configured Kt whenever the tab becomes visible."""
        super().showEvent(event)
        self._refresh_configured_kt()

    # ------------------------------------------------------------------ UI ---

    def _setup_ui(self):
        """Constructs the user interface widgets and layouts for this tab."""
        main_layout = QVBoxLayout(self)

        # --- Setup: which rig is being used, and its geometry ---
        self.setup_group = QGroupBox()
        setup_layout = QFormLayout(self.setup_group)

        self.method_combo = QComboBox()
        self.method_combo.addItem("", METHOD_LEVER_SCALE)
        self.method_combo.addItem("", METHOD_HANGING_WEIGHT)
        self.method_combo.currentIndexChanged.connect(self._on_method_changed)

        positive_validator = QDoubleValidator(0.0, 100000.0, 2, self)
        self.radius_input = QLineEdit()
        self.radius_input.setValidator(positive_validator)
        self.radius_input.textChanged.connect(self._recompute)

        self.angle_input = QDoubleSpinBox()
        self.angle_input.setRange(0.0, 89.0)
        self.angle_input.setDecimals(1)
        self.angle_input.setValue(0.0)
        self.angle_input.setSuffix(" °")
        self.angle_input.valueChanged.connect(self._recompute)

        self.label_method = QLabel()
        self.label_radius = QLabel()
        self.label_angle = QLabel()
        setup_layout.addRow(self.label_method, self.method_combo)
        setup_layout.addRow(self.label_radius, self.radius_input)
        setup_layout.addRow(self.label_angle, self.angle_input)

        self.help_label = QLabel()
        self.help_label.setWordWrap(True)
        self.help_label.setStyleSheet(f"color: {AppColors.INFO};")
        setup_layout.addRow(self.help_label)

        # --- Capture: live current plus the table of captured points ---
        self.capture_group = QGroupBox()
        capture_layout = QVBoxLayout(self.capture_group)

        live_row = QHBoxLayout()
        self.label_live_current = QLabel()
        self.live_current_label = QLabel("-")
        self.live_current_label.setStyleSheet("font-size: 16pt; font-weight: bold;")
        live_row.addWidget(self.label_live_current)
        live_row.addWidget(self.live_current_label)
        live_row.addStretch()
        capture_layout.addLayout(live_row)

        entry_row = QHBoxLayout()
        self.label_mass = QLabel()
        self.mass_input = QLineEdit()
        self.mass_input.setValidator(positive_validator)
        self.capture_btn = QPushButton()
        self.capture_btn.clicked.connect(self._capture_point)
        self.remove_btn = QPushButton()
        self.remove_btn.clicked.connect(self._remove_selected_point)
        self.clear_btn = QPushButton()
        self.clear_btn.clicked.connect(self._clear_points)
        entry_row.addWidget(self.label_mass)
        entry_row.addWidget(self.mass_input)
        entry_row.addWidget(self.capture_btn)
        entry_row.addWidget(self.remove_btn)
        entry_row.addWidget(self.clear_btn)
        capture_layout.addLayout(entry_row)

        self.points_table = QTableWidget(0, 3)
        self.points_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.points_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.points_table.verticalHeader().setVisible(False)
        self.points_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.points_table.setMaximumHeight(140)
        capture_layout.addWidget(self.points_table)

        # --- Result ---
        self.result_group = QGroupBox()
        result_layout = QVBoxLayout(self.result_group)
        self.result_label = QLabel()
        self.result_label.setWordWrap(True)
        self.quality_label = QLabel()
        self.quality_label.setWordWrap(True)
        self.configured_label = QLabel()
        result_layout.addWidget(self.result_label)
        result_layout.addWidget(self.quality_label)
        result_layout.addWidget(self.configured_label)

        self.apply_btn = QPushButton()
        self.apply_btn.clicked.connect(self.apply_config)
        self.apply_btn.setEnabled(False)
        result_layout.addWidget(self.apply_btn, 0, Qt.AlignmentFlag.AlignRight)

        # --- Automatic method: back-EMF, no external load needed ---
        self.auto_group = QGroupBox()
        auto_layout = QVBoxLayout(self.auto_group)
        self.auto_help = QLabel()
        self.auto_help.setWordWrap(True)
        self.auto_help.setStyleSheet(f"color: {AppColors.INFO};")
        auto_layout.addWidget(self.auto_help)

        auto_form = QFormLayout()
        self.auto_max_vel = QDoubleSpinBox()
        self.auto_max_vel.setRange(2.0, 30.0)
        self.auto_max_vel.setDecimals(1)
        self.auto_max_vel.setValue(10.0)
        self.auto_max_vel.setSuffix(" turns/s")
        self.auto_points = QSpinBox()
        self.auto_points.setRange(3, 12)
        self.auto_points.setValue(5)
        self.auto_current = QDoubleSpinBox()
        self.auto_current.setRange(1.0, 60.0)
        self.auto_current.setDecimals(1)
        self.auto_current.setValue(10.0)
        self.auto_current.setSuffix(" A")
        self.label_auto_vel, self.label_auto_points, self.label_auto_current = QLabel(), QLabel(), QLabel()
        auto_form.addRow(self.label_auto_vel, self.auto_max_vel)
        auto_form.addRow(self.label_auto_points, self.auto_points)
        auto_form.addRow(self.label_auto_current, self.auto_current)
        auto_layout.addLayout(auto_form)

        auto_buttons = QHBoxLayout()
        self.auto_start_btn = QPushButton()
        self.auto_start_btn.clicked.connect(self.start_backemf)
        self.auto_cancel_btn = QPushButton()
        self.auto_cancel_btn.clicked.connect(self.cancel_backemf)
        self.auto_cancel_btn.setEnabled(False)
        self.auto_apply_btn = QPushButton()
        self.auto_apply_btn.clicked.connect(self.apply_backemf)
        self.auto_apply_btn.setEnabled(False)
        auto_buttons.addWidget(self.auto_start_btn)
        auto_buttons.addWidget(self.auto_cancel_btn)
        auto_buttons.addStretch()
        auto_buttons.addWidget(self.auto_apply_btn)
        auto_layout.addLayout(auto_buttons)

        self.auto_progress = QProgressBar()
        self.auto_progress.setRange(0, 100)
        self.auto_result_label = QLabel()
        self.auto_result_label.setWordWrap(True)
        self.compare_label = QLabel()
        self.compare_label.setWordWrap(True)
        auto_layout.addWidget(self.auto_progress)
        auto_layout.addWidget(self.auto_result_label)
        auto_layout.addWidget(self.compare_label)

        main_layout.addWidget(self.setup_group)
        main_layout.addWidget(self.capture_group)
        main_layout.addWidget(self.result_group)
        main_layout.addWidget(self.auto_group)
        main_layout.addStretch()

    def retranslate_ui(self):
        """Updates all translatable texts in this tab."""
        self.setup_group.setTitle(self.tr("Measurement Setup"))
        self.label_method.setText(self.tr("Method:"))
        self.label_radius.setText(self.tr("Radius / Arm length (mm):"))
        self.label_angle.setText(self.tr("Arm angle from horizontal:"))
        self.method_combo.setItemText(METHOD_LEVER_SCALE, self.tr("Rigid arm on a scale"))
        self.method_combo.setItemText(METHOD_HANGING_WEIGHT, self.tr("Weight hanging at a known radius"))
        self.radius_input.setToolTip(self.tr("Distance from the shaft centre to where the force is applied, in millimetres."))
        self.angle_input.setToolTip(self.tr("0° means the arm is horizontal, which gives the full torque.\nAny tilt reduces the effective lever by cos(angle)."))

        self.capture_group.setTitle(self.tr("Capture Points"))
        self.label_live_current.setText(self.tr("Measured current (Iq):"))
        self.label_mass.setText(self.tr("Mass (kg):"))
        self.capture_btn.setText(self.tr("Capture Point"))
        self.remove_btn.setText(self.tr("Remove Selected"))
        self.clear_btn.setText(self.tr("Clear All"))
        self.capture_btn.setToolTip(self.tr("Records the typed mass together with the averaged current reading."))
        self.mass_input.setToolTip(self.tr("Total mass hanging on the arm for this point."))
        self.points_table.setHorizontalHeaderLabels([
            self.tr("Mass (kg)"), self.tr("Torque (Nm)"), self.tr("Iq (A)")
        ])

        self.result_group.setTitle(self.tr("Result"))
        self.apply_btn.setText(self.tr("Apply Kt to ODrive"))
        self.apply_btn.setToolTip(self.tr("Writes the measured value to axis0.motor.config.torque_constant."))

        self.auto_group.setTitle(self.tr("Automatic Method (back-EMF)"))
        self.auto_help.setText(self.tr(
            "Spins the motor with no load and reads the voltage it has to apply. That voltage "
            "rises with speed in proportion to the flux linkage, which gives Kt without any "
            "weights. Free the shaft first: the motor will spin on its own, both ways.\n\n"
            "Typically within about 5% of the weight method, which stays the more accurate of "
            "the two. Running both and comparing is the point."
        ))
        self.label_auto_vel.setText(self.tr("Top speed:"))
        self.label_auto_points.setText(self.tr("Speeds sampled:"))
        self.label_auto_current.setText(self.tr("Current limit during test:"))
        self.auto_start_btn.setText(self.tr("Measure Automatically"))
        self.auto_cancel_btn.setText(self.tr("Cancel"))
        self.auto_apply_btn.setText(self.tr("Apply This Kt"))

        self._on_method_changed()
        self._recompute()
        self._refresh_configured_kt()

    def _on_method_changed(self):
        """Shows rig-specific instructions for the selected measurement method."""
        if self.method_combo.currentData() == METHOD_LEVER_SCALE:
            self.help_label.setText(self.tr(
                "Clamp a rigid arm to the shaft and rest its far end on a kitchen scale. "
                "The radius is the distance from the shaft centre to the contact point on the scale. "
                "Keep the arm horizontal, and subtract nothing for the arm's own weight: "
                "capturing two or more points cancels it automatically."
            ))
        else:
            self.help_label.setText(self.tr(
                "Hang the weight from a known radius on the wheel, with the hanging point level "
                "with the shaft (the 3 or 9 o'clock position). Best accuracy comes from running the "
                "cord tangentially over the rim, since then the torque does not depend on the angle at all."
            ))
        self._recompute()

    # --------------------------------------------------------- measurement ---

    def _axis_is_armed(self):
        """
        Iq_measured holds its last value while the axis is disarmed, so a stale reading
        from the previous run looks like a settled measurement. Only an armed axis is
        actually reporting current.
        """
        if not self.main_window.is_connected or not self.main_window.odrv_proxy:
            return False
        try:
            return self.main_window.odrv_proxy.odrv.axis0.current_state == 8
        except Exception:
            return False

    def update_live_current(self, iq_measured):
        """
        Slot fed by the main window's telemetry signal. Keeps a short rolling window
        of readings so that a captured point is an average rather than one noisy sample.

        The window is deliberately not treated as ready until it is full, and it is
        emptied after every capture. Otherwise a capture taken right after changing the
        load would average in readings from the previous mass and quietly skew the fit.
        """
        if not self._axis_is_armed():
            self.current_samples.clear()
            self.live_current = None
            self.live_current_label.setText(self.tr("- (axis not in closed loop)"))
            self.live_current_label.setStyleSheet("font-size: 16pt; font-weight: bold;")
            return

        self.current_samples.append(iq_measured)
        if len(self.current_samples) > CURRENT_SAMPLE_WINDOW:
            self.current_samples.pop(0)

        filling = len(self.current_samples) < CURRENT_SAMPLE_WINDOW
        mean = sum(self.current_samples) / len(self.current_samples)
        self.live_current = None if filling else mean

        if filling:
            self.live_current_label.setText(self.tr("{0:.3f} A  (settling {1}/{2})").format(
                mean, len(self.current_samples), CURRENT_SAMPLE_WINDOW))
            self.live_current_label.setStyleSheet(f"font-size: 16pt; font-weight: bold; color: {AppColors.WARNING};")
            return

        # A window that is still moving means the load has not settled yet.
        spread = max(self.current_samples) - min(self.current_samples)
        unsettled = spread > max(0.05, abs(mean) * 0.05)
        if unsettled:
            self.live_current_label.setText(self.tr("{0:.3f} A  (still moving)").format(mean))
            self.live_current_label.setStyleSheet(f"font-size: 16pt; font-weight: bold; color: {AppColors.WARNING};")
        else:
            self.live_current_label.setText(self.tr("{0:.3f} A").format(mean))
            self.live_current_label.setStyleSheet(f"font-size: 16pt; font-weight: bold; color: {AppColors.SUCCESS};")

    def reset_live_current(self):
        """Empties the averaging window, on disconnect and after every capture."""
        self.current_samples.clear()
        self.live_current = None
        self.live_current_label.setText("-")
        self.live_current_label.setStyleSheet("font-size: 16pt; font-weight: bold;")

    def _lever_arm_m(self):
        """Returns the effective lever arm in metres, or None if the geometry is incomplete."""
        try:
            radius_mm = float(self.radius_input.text().replace(',', '.'))
        except ValueError:
            return None
        if radius_mm <= 0:
            return None
        return (radius_mm / 1000.0) * math.cos(math.radians(self.angle_input.value()))

    def _torque_for(self, mass_kg):
        """Converts a hanging mass into the torque it applies about the shaft."""
        lever = self._lever_arm_m()
        if lever is None:
            return None
        return mass_kg * GRAVITY * lever

    def _capture_point(self):
        """Records the typed mass alongside the averaged live current reading."""
        if self._lever_arm_m() is None:
            QMessageBox.warning(self, self.tr("Input Error"),
                                self.tr("Enter a valid radius before capturing points."))
            return
        try:
            mass = float(self.mass_input.text().replace(',', '.'))
        except ValueError:
            QMessageBox.warning(self, self.tr("Input Error"),
                                self.tr("Enter the mass for this point, in kilograms."))
            return
        if mass < 0:
            QMessageBox.warning(self, self.tr("Input Error"), self.tr("The mass cannot be negative."))
            return
        if self.live_current is None:
            QMessageBox.warning(self, self.tr("No Reading"),
                                self.tr("No settled current reading yet.\n\nConnect to the ODrive, put Axis 0 in closed loop so it holds the load, and wait for the reading to fill up."))
            return

        self.points.append((mass, self.live_current))
        self.mass_input.clear()
        # Start the averaging window over, so the next point cannot inherit this load.
        self.reset_live_current()
        self._refresh_table()
        self._recompute()

    def _remove_selected_point(self):
        """Drops the selected row, so a bad capture does not force starting over."""
        rows = sorted({index.row() for index in self.points_table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        for row in rows:
            if 0 <= row < len(self.points):
                self.points.pop(row)
        self._refresh_table()
        self._recompute()

    def _clear_points(self):
        """Discards every captured point."""
        self.points.clear()
        self._refresh_table()
        self._recompute()

    def _refresh_table(self):
        """Redraws the table from the captured points."""
        self.points_table.setRowCount(len(self.points))
        for row, (mass, iq) in enumerate(self.points):
            torque = self._torque_for(mass)
            values = [
                f"{mass:.3f}",
                f"{torque:.3f}" if torque is not None else "-",
                f"{iq:.3f}",
            ]
            for col, text in enumerate(values):
                self.points_table.setItem(row, col, QTableWidgetItem(text))

    # ------------------------------------------------------------- results ---

    @staticmethod
    def _linear_fit(xs, ys):
        """
        Least-squares fit of ys against xs, returning (slope, intercept, r_squared).
        Returns None when the points are degenerate (all the same x).
        """
        n = len(xs)
        mean_x, mean_y = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mean_x) ** 2 for x in xs)
        if sxx == 0:
            return None
        slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / sxx
        intercept = mean_y - slope * mean_x
        ss_tot = sum((y - mean_y) ** 2 for y in ys)
        ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
        r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
        return slope, intercept, r_squared

    def _recompute(self):
        """Recalculates Kt from the captured points and updates the result display."""
        self.measured_kt = None
        self._refresh_table()

        if self._lever_arm_m() is None:
            self.result_label.setText(self.tr("Enter the radius to start."))
            self.quality_label.clear()
            self.apply_btn.setEnabled(False)
            return

        usable = [(self._torque_for(m), iq) for m, iq in self.points]
        usable = [(t, iq) for t, iq in usable if t is not None]

        if not usable:
            self.result_label.setText(self.tr("Capture at least two points with different masses."))
            self.quality_label.clear()
            self.apply_btn.setEnabled(False)
            return

        if len(usable) == 1:
            torque, iq = usable[0]
            if abs(iq) < 1e-6:
                self.result_label.setText(self.tr("The measured current is zero. Is the motor holding the load?"))
                self.quality_label.clear()
                self.apply_btn.setEnabled(False)
                return
            self.measured_kt = torque / iq
            self.result_label.setText(self.tr("Kt (single point): {0:.4f} Nm/A").format(self.measured_kt))
            self.quality_label.setText(self.tr(
                "Single-point result. It still includes the arm's own weight and friction. "
                "Capture another point with a different mass for a reliable value."
            ))
            self.quality_label.setStyleSheet(f"color: {AppColors.WARNING};")
            self.apply_btn.setEnabled(True)
            return

        fit = self._linear_fit([iq for _, iq in usable], [t for t, _ in usable])
        if fit is None:
            self.result_label.setText(self.tr("Every point has the same current. Vary the mass between captures."))
            self.quality_label.clear()
            self.apply_btn.setEnabled(False)
            return

        slope, intercept, r_squared = fit
        self.measured_kt = slope
        self.result_label.setText(self.tr("Kt (slope of {0} points): {1:.4f} Nm/A").format(len(usable), slope))

        if slope <= 0:
            self.quality_label.setText(self.tr(
                "The slope came out negative, which is not physically valid. "
                "Check that the current rises as you add mass, and that every point used the same direction."
            ))
            self.quality_label.setStyleSheet(f"color: {AppColors.ERROR};")
            self.apply_btn.setEnabled(False)
            return

        quality = self.tr("Fit quality R² = {0:.4f}. Offset absorbed: {1:.3f} Nm.").format(r_squared, intercept)
        if r_squared < 0.98:
            quality += "\n" + self.tr("R² below 0.98 means the points do not lie on a line. Treat this value as unreliable.")
            self.quality_label.setStyleSheet(f"color: {AppColors.WARNING};")
        else:
            self.quality_label.setStyleSheet(f"color: {AppColors.SUCCESS};")
        self.quality_label.setText(quality)
        self.apply_btn.setEnabled(True)
        self._update_comparison()

    def _refresh_configured_kt(self):
        """Shows the value currently stored on the ODrive, for comparison."""
        if not self.main_window.is_connected or not self.main_window.odrv_proxy:
            self.configured_label.setText(self.tr("Currently on the ODrive: not connected."))
            return
        try:
            configured = self.main_window.odrv_proxy.odrv.axis0.motor.config.torque_constant
            self.configured_label.setText(self.tr("Currently on the ODrive: {0:.4f} Nm/A").format(configured))
        except Exception:
            self.configured_label.setText(self.tr("Currently on the ODrive: could not read."))

    # ------------------------------------------------- automatic (back-EMF) ---

    def start_backemf(self):
        """Runs the no-load back-EMF measurement in a background thread."""
        odrv = self.get_odrv()
        if not odrv or self.auto_thread is not None:
            return
        try:
            if not odrv.axis0.motor.is_calibrated or not odrv.axis0.encoder.is_ready:
                QMessageBox.warning(self, self.tr("Action Required"), self.tr(
                    "Calibrate the motor and encoder before measuring."))
                return
        except Exception as e:
            QMessageBox.critical(self, self.tr("Error"), self.tr(
                "Could not read the axis state.\n\nDetails: {0}").format(e))
            return

        confirm = QMessageBox.question(self, self.tr("Confirm"), self.tr(
            "The motor will spin on its own, in both directions.\n\nIs the shaft free and clear?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self.auto_start_btn.setEnabled(False)
        self.auto_cancel_btn.setEnabled(True)
        self.auto_progress.setValue(0)

        self.auto_thread = QThread()
        self.auto_worker = BackEmfKtWorker(
            odrv,
            max_velocity=self.auto_max_vel.value(),
            speed_count=self.auto_points.value(),
            current_limit=self.auto_current.value(),
            settle_s=1.5,
            sample_s=1.0,
        )
        self.auto_worker.moveToThread(self.auto_thread)
        self.auto_thread.started.connect(self.auto_worker.run)
        self.auto_worker.progress.connect(self._on_auto_progress)
        self.auto_worker.result.connect(self._on_auto_result)
        self.auto_worker.finished.connect(self.auto_thread.quit)
        self.auto_worker.finished.connect(self.auto_worker.deleteLater)
        self.auto_thread.finished.connect(self.auto_thread.deleteLater)
        self.auto_thread.finished.connect(self._on_auto_thread_finished)
        self.auto_thread.start()

    def cancel_backemf(self):
        if self.auto_worker:
            self.auto_worker.stop()
            self.auto_cancel_btn.setEnabled(False)

    def _on_auto_progress(self, message, percent):
        self.auto_result_label.setText(message)
        self.auto_progress.setValue(percent)

    def _on_auto_result(self, success, message, kt):
        self.backemf_kt = kt if success else None
        self.auto_result_label.setText(message)
        self.auto_result_label.setStyleSheet(
            f"color: {AppColors.SUCCESS if success else AppColors.ERROR};")
        self.auto_apply_btn.setEnabled(bool(kt))
        if success:
            self.auto_progress.setValue(100)
        self._update_comparison()

    def _on_auto_thread_finished(self):
        self.auto_thread, self.auto_worker = None, None
        self.auto_start_btn.setEnabled(True)
        self.auto_cancel_btn.setEnabled(False)
        self._refresh_configured_kt()

    def _update_comparison(self):
        """
        Cross-checks the two methods. Agreement is the whole point of having both: the
        weight measurement is the more trustworthy one, so it is what the automatic
        result is judged against.
        """
        if not self.backemf_kt or not self.measured_kt:
            self.compare_label.clear()
            return
        difference = abs(self.backemf_kt - self.measured_kt) / self.measured_kt * 100.0
        text = self.tr("Weight method: {0:.4f} · Back-EMF: {1:.4f} · Difference: {2:.1f}%").format(
            self.measured_kt, self.backemf_kt, difference)
        if difference <= 5.0:
            text += "\n" + self.tr("The two agree. Either value is trustworthy.")
            colour = AppColors.SUCCESS
        elif difference <= 15.0:
            text += "\n" + self.tr("Some disagreement. Prefer the weight result and re-run the automatic one.")
            colour = AppColors.WARNING
        else:
            text += "\n" + self.tr("They disagree badly. Check the pole pairs, and that the shaft was free.")
            colour = AppColors.ERROR
        self.compare_label.setText(text)
        self.compare_label.setStyleSheet(f"color: {colour};")

    def apply_backemf(self):
        """Writes the automatically measured value to the ODrive."""
        if not self.backemf_kt:
            return
        self._write_torque_constant(self.backemf_kt)

    # ------------------------------------------------------------ BaseTab ---

    def populate_fields(self):
        """Refreshes the configured Kt reading. Captured points are left untouched."""
        self._refresh_configured_kt()

    def apply_config(self):
        """Writes the torque constant from the weight based measurement."""
        self._write_torque_constant(self.measured_kt)

    def _write_torque_constant(self, kt):
        """Writes a measured torque constant to the ODrive after confirmation."""
        odrv = self.get_odrv()
        if not odrv:
            return
        if not kt or kt <= 0:
            return

        confirm = QMessageBox.question(
            self, self.tr("Confirm"),
            self.tr("Write Kt = {0:.4f} Nm/A to axis0.motor.config.torque_constant?\n\n"
                    "A lower value makes the ODrive command more current for the same requested "
                    "torque, which heats the motor.").format(kt),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        try:
            odrv.axis0.motor.config.torque_constant = float(kt)
            self.main_window.show_status_message(
                self.tr("Torque constant applied."), AppColors.SUCCESS, 3000)
            self._refresh_configured_kt()
            QMessageBox.information(self, self.tr("Reminder"),
                                    f"{self.tr(AppMessages.CONFIG_APPLY_SUCCESS)}\n\n{self.tr(AppMessages.SAVE_REMINDER)}")
        except Exception as e:
            error_message = self.tr("Error applying torque constant: {}").format(e)
            self.main_window.show_status_message(error_message, AppColors.ERROR, 5000)
            QMessageBox.critical(self, self.tr("Error"), error_message)
