# tabs/alignment_tab.py
"""
This module defines the AlignmentTab, which drives the encoder electrical offset
optimisation. It is an optional refinement on top of ODrive's own offset calibration,
not a replacement for it: the motor and encoder must already be calibrated before
this sweep can run.
"""
from PySide6.QtWidgets import (
    QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QDoubleSpinBox, QSpinBox, QProgressBar, QMessageBox, QCheckBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView
)
import math
import time

from PySide6.QtCore import Qt, QEvent, QThread, QTimer
from odrive.enums import CONTROL_MODE_POSITION_CONTROL, INPUT_MODE_TRAP_TRAJ
from odrive.enums import CONTROL_MODE_TORQUE_CONTROL, INPUT_MODE_PASSTHROUGH

from .base_tab import BaseTab
from .tuning_workers import CalibrationQualityWorker, CentringWorker, resolve
from . import calibration_log
from app_config import AppColors

# Rough per-point overhead for the idle/arm/disarm transitions around each measurement.
POINT_OVERHEAD_S = 1.0


class AlignmentTab(BaseTab):
    """Manages the UI tab for encoder offset alignment optimisation."""

    def __init__(self, main_window, parent=None):
        super().__init__(main_window, parent)
        self.align_thread = None
        self.align_worker = None
        self.centre_thread = None
        self.centre_worker = None
        self._centre_lines = []
        self.live_timer = QTimer(self)
        self.live_timer.setInterval(500)
        self.live_timer.timeout.connect(self._refresh_centre_position)
        self._setup_ui()
        self.retranslate_ui()

    def showEvent(self, event):
        """
        Keeps the live readings live while the tab is on screen.

        Position is the number you watch while lining the wheel up to set the centre, so
        a figure frozen from whenever the board connected is worse than none. Polling
        only while visible keeps it off the wire the rest of the time.
        """
        super().showEvent(event)
        self.refresh_centre_status()
        self.live_timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.live_timer.stop()

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
        # Left at a number of its own this field silently undercut the board. The motor
        # was configured for 15 A and this offered 10 A, which on a motor with strong
        # cogging is not enough for the rotor to follow the commanded angle: the encoder
        # counts then disagree with the electrical distance and the firmware refuses the
        # calibration with cpr polpairs mismatch. The board's own calibration worked at
        # the same moment, because it used the board's own current. So this now mirrors
        # the board and only departs from it when the user says so.
        self.calib_current_input.setValue(10.0)
        self._calib_current_touched = False
        self.calib_current_input.valueChanged.connect(self._mark_current_touched)
        self.calib_current_input.setSuffix(" A")

        # The ceiling the resistance measurement is allowed to push to. It belongs beside
        # the current because the two set that measurement together, and a motor that
        # cannot reach its test current within this voltage calibrates against whatever
        # it managed instead. Like the current, it mirrors the board until overridden.
        self.calib_voltage_input = QDoubleSpinBox()
        self.calib_voltage_input.setRange(0.5, 24.0)
        self.calib_voltage_input.setDecimals(1)
        self.calib_voltage_input.setValue(2.0)
        self.calib_voltage_input.setSuffix(" V")
        self._calib_voltage_touched = False
        self.calib_voltage_input.valueChanged.connect(self._mark_voltage_touched)

        for widget in (self.runs_input, self.revolutions_input, self.calib_current_input):
            widget.valueChanged.connect(self._update_estimate)

        self.label_runs = QLabel()
        self.label_revolutions = QLabel()
        self.label_calib_current = QLabel()
        self.label_calib_voltage = QLabel()
        params_layout.addRow(self.label_runs, self.runs_input)
        params_layout.addRow(self.apply_check)
        params_layout.addRow(self.keep_scan_check)
        params_layout.addRow(self.label_revolutions, self.revolutions_input)
        params_layout.addRow(self.label_calib_current, self.calib_current_input)
        params_layout.addRow(self.label_calib_voltage, self.calib_voltage_input)

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
        main_layout.addWidget(self._build_centre_group())
        main_layout.addWidget(self._build_count_group())
        main_layout.addWidget(self._build_history_group())
        main_layout.addStretch()

    def _build_count_group(self):
        """Checks whether the encoder's counts survive being turned."""
        self.count_group = QGroupBox()
        layout = QVBoxLayout(self.count_group)

        self.count_help = QLabel()
        self.count_help.setWordWrap(True)
        layout.addWidget(self.count_help)

        self.count_reading = QLabel()
        self.count_reading.setWordWrap(True)
        self.count_reading.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.count_reading)

        self.count_verdict = QLabel()
        self.count_verdict.setWordWrap(True)
        layout.addWidget(self.count_verdict)

        row = QHBoxLayout()
        self.count_mark_btn = QPushButton()
        self.count_mark_btn.clicked.connect(self.mark_count_reference)
        self.count_stop_btn = QPushButton()
        self.count_stop_btn.clicked.connect(self.stop_count_test)
        self.count_stop_btn.setEnabled(False)
        row.addWidget(self.count_mark_btn)
        row.addWidget(self.count_stop_btn)
        row.addStretch()
        layout.addLayout(row)

        # Polling rather than the telemetry signal, because this panel wants a reading
        # whether or not anything else is listening, and five a second is plenty for a
        # wheel being turned by hand.
        self._count_reference = None
        self._count_extreme = 0.0
        self.count_timer = QTimer(self)
        self.count_timer.setInterval(200)
        self.count_timer.timeout.connect(self._poll_count)
        return self.count_group

    def _build_history_group(self):
        """What past calibrations produced, so this one can be judged against them."""
        self.history_group = QGroupBox()
        layout = QVBoxLayout(self.history_group)

        self.history_help = QLabel()
        self.history_help.setWordWrap(True)
        layout.addWidget(self.history_help)

        self.history_table = QTableWidget(0, 5)
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setMaximumHeight(150)
        header = self.history_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.history_table)

        self.history_verdict = QLabel()
        self.history_verdict.setWordWrap(True)
        self.history_verdict.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.history_verdict)

        row = QHBoxLayout()
        self.record_calib_btn = QPushButton()
        self.record_calib_btn.clicked.connect(self.record_calibration)
        row.addWidget(self.record_calib_btn)
        row.addStretch()
        layout.addLayout(row)
        return self.history_group

    def _build_centre_group(self):
        """The centre reference: where zero is, and whether the axis drives there itself."""
        self.centre_group = QGroupBox()
        layout = QVBoxLayout(self.centre_group)

        self.centre_help = QLabel()
        self.centre_help.setWordWrap(True)
        layout.addWidget(self.centre_help)

        self.centre_status = QLabel()
        self.centre_status.setWordWrap(True)
        self.centre_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.centre_status)

        form = QFormLayout()
        self.move_current_input = QDoubleSpinBox()
        self.move_current_input.setRange(0.5, 30.0)
        self.move_current_input.setDecimals(1)
        self.move_current_input.setValue(6.0)
        self.move_current_input.setSuffix(" A")
        self.move_speed_input = QDoubleSpinBox()
        # Centring is not a race, and the runaway guard scales off this number, so a
        # high setting would buy a permissive guard on a wheel that cannot afford one.
        self.move_speed_input.setRange(0.1, 1.5)
        self.move_speed_input.setDecimals(2)
        self.move_speed_input.setValue(0.50)
        self.move_speed_input.setSuffix(" turns/s")
        self.label_move_current, self.label_move_speed = QLabel(), QLabel()
        form.addRow(self.label_move_current, self.move_current_input)
        form.addRow(self.label_move_speed, self.move_speed_input)
        layout.addLayout(form)

        self.centre_warning = QLabel()
        self.centre_warning.setWordWrap(True)
        self.centre_warning.setStyleSheet(f"color: {AppColors.WARNING}; font-weight: bold;")
        layout.addWidget(self.centre_warning)

        buttons = QHBoxLayout()
        self.mark_centre_btn = QPushButton()
        self.mark_centre_btn.clicked.connect(self.mark_current_as_centre)
        self.goto_centre_btn = QPushButton()
        self.goto_centre_btn.clicked.connect(self.go_to_centre)
        self.autocentre_btn = QPushButton()
        self.autocentre_btn.clicked.connect(self.enable_autocentre)
        for widget in (self.mark_centre_btn, self.goto_centre_btn, self.autocentre_btn):
            buttons.addWidget(widget)
        buttons.addStretch()
        layout.addLayout(buttons)

        self.centre_result = QLabel()
        self.centre_result.setWordWrap(True)
        self.centre_result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.centre_result)
        return self.centre_group

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
        self.count_group.setTitle(self.tr("Count Check"))
        self.count_help.setText(self.tr(
            "An incremental encoder has to keep every count it reads. Lose some and the "
            "commutation angle shifts with them, and past ninety electrical degrees the torque "
            "reverses, so the wheel goes light and pulls the way it is already turning. Mark a "
            "spot, swing the wheel about at the speed it sees in use, bring the mark back, and "
            "the reading should land on a whole number of turns."))
        self.count_mark_btn.setText(self.tr("Mark This Position"))
        self.count_mark_btn.setToolTip(self.tr(
            "Takes the wheel's position now as the mark. Put something visible on the rim to "
            "line it back up against."))
        self.count_stop_btn.setText(self.tr("Stop"))
        self.history_group.setTitle(self.tr("Calibration History"))
        self.history_help.setText(self.tr(
            "One calibration on its own tells you nothing about whether it went well. Recorded "
            "next to the previous ones, a resistance that climbed says the motor was hot, and an "
            "inductance or a direction that jumped says something went wrong. Press Record after "
            "each calibration."))
        self.history_table.setHorizontalHeaderLabels([
            self.tr("When"), self.tr("Resistance (Ω)"), self.tr("Inductance"),
            self.tr("Offset"), self.tr("Calib. current")])
        self.record_calib_btn.setText(self.tr("Record This Calibration"))
        self.record_calib_btn.setToolTip(self.tr(
            "Stores what the board holds now and compares it against the last recording."))
        self.refresh_history()
        self.centre_group.setTitle(self.tr("Centre Reference"))
        self.centre_help.setText(self.tr(
            "An incremental encoder counts from wherever it happens to be at power-on, so "
            "without a fixed mark the centre moves every time. The Z index is that mark: the "
            "axis turns until it finds it, sets the position from it, and then knows where "
            "centre is. Set it once here and the OpenFFBoard no longer needs centring by hand."))
        self.label_move_current.setText(self.tr("Current while moving:"))
        self.label_move_speed.setText(self.tr("Speed while moving:"))
        self.move_current_input.setToolTip(self.tr(
            "Enough to turn the wheel against its cogging, and no more. This is not your force "
            "feedback limit; the wheel may be moving with someone's hands on it."))
        self.move_speed_input.setToolTip(self.tr(
            "Walking pace. The move is a trapezoidal profile, so it accelerates and stops "
            "smoothly rather than snapping to centre."))
        self.centre_warning.setText(self.tr(
            "The wheel turns on its own for all three of these. Keep hands and cables clear."))
        self.mark_centre_btn.setText(self.tr("Set Current Position as Centre"))
        self.mark_centre_btn.setToolTip(self.tr(
            "Hold the wheel straight, then press. Stores how far this is from the index."))
        self.goto_centre_btn.setText(self.tr("Find Index and Centre Now"))
        self.goto_centre_btn.setToolTip(self.tr(
            "Runs the power-on sequence once, under its own current and speed limits, so you "
            "can watch it before letting it happen unattended."))
        self.autocentre_btn.setText(self.tr("Centre Automatically at Power-On"))
        self.autocentre_btn.setToolTip(self.tr(
            "Writes the startup settings so the axis does this by itself every time it powers up."))
        self.refresh_centre_status()
        self.params_group.setTitle(self.tr("Check Parameters"))
        self.label_runs.setText(self.tr("Calibrations to average:"))
        self.apply_check.setText(self.tr("Write the average to the board (off = measure only)"))
        self.apply_check.setToolTip(self.tr("Left off, this only reports how repeatable your calibration is and changes nothing.\nThe average is refused anyway when the runs disagree by more than 15 electrical degrees."))
        self.keep_scan_check.setText(self.tr("Keep the board's scan distance"))
        self.keep_scan_check.setToolTip(self.tr("Measured on a 15 pole pair hoverboard motor, the firmware default repeated more tightly than longer scans, and runs far quicker."))
        self.label_revolutions.setText(self.tr("Whole revolutions to scan:"))
        self.label_calib_current.setText(self.tr("Calibration current:"))
        self.label_calib_voltage.setText(self.tr("Calibration voltage:"))
        self.calib_voltage_input.setToolTip(self.tr(
            "The ceiling the resistance measurement may push to. Too low and the motor never reaches its test current, so it measures against whatever it managed."))
        self.runs_input.setToolTip(self.tr("More runs measure the spread better, and take proportionally longer."))
        self.revolutions_input.setToolTip(self.tr("Cogging repeats with mechanical position, so a scan covering whole revolutions lets it average out."))
        self.calib_current_input.setToolTip(self.tr(
            "Enough current for the rotor to follow the commanded angle instead of sticking in "
            "cogging detents. More is not better past that point: a high calibration current "
            "heats the winding and sags the bus while it measures, and a calibration taken at "
            "20 A has been reported worse than the same motor at 10 A."))

        self.warning_label.setText(self.tr(
            "The motor will turn on its own during each calibration. Free the shaft before starting."
        ))
        self.start_btn.setText(self.tr("Check Calibration Quality"))
        self.cancel_btn.setText(self.tr("Cancel"))
        self.restore_scan_btn.setText(self.tr("Restore Default Scan"))
        self.restore_scan_btn.setToolTip(self.tr("Puts calib_scan_distance back to the firmware default of 16*pi electrical radians."))
        self._update_estimate()




    # ----------------------------------------------------------- count integrity ---

    # Half a count of play is nothing; a tenth of a turn is a real loss. The warning
    # threshold sits where the error starts costing commutation rather than patience.
    COUNT_WARN_ELECTRICAL_DEG = 10.0

    def mark_count_reference(self):
        """Takes the wheel's position now as the mark to come back to."""
        odrv = self.get_odrv()
        if not odrv:
            return
        try:
            self._count_reference = float(odrv.axis0.encoder.pos_estimate)
        except Exception as e:
            self.count_verdict.setText(self.tr("Could not read the encoder: {0}").format(e))
            self.count_verdict.setStyleSheet(f"color: {AppColors.ERROR};")
            return
        self._count_extreme = 0.0
        self.count_mark_btn.setEnabled(False)
        self.count_stop_btn.setEnabled(True)
        self.count_timer.start()
        self._poll_count()

    def stop_count_test(self):
        self.count_timer.stop()
        self.count_mark_btn.setEnabled(True)
        self.count_stop_btn.setEnabled(False)

    def _poll_count(self):
        """
        Reports how far the wheel has gone, and how far that is from a whole turn.

        Returning to the same physical spot has to read a whole number of turns, so the
        distance to the nearest one is the counting error and needs no precision from
        whoever is turning the wheel: line the mark up by eye and the number is the
        answer. Counts are lost when the encoder is moving quickly, not while it is
        eased round by hand, so the figure only means something after the wheel has been
        swung about at the speed it sees in use.
        """
        odrv = self._quiet_odrv()
        if odrv is None or self._count_reference is None:
            self.stop_count_test()
            return
        try:
            travelled = float(odrv.axis0.encoder.pos_estimate) - self._count_reference
        except Exception:
            return
        self._count_extreme = max(self._count_extreme, abs(travelled))

        nearest = round(travelled)
        error_turns = travelled - nearest
        error_degrees = error_turns * 360.0
        electrical = abs(error_degrees) * self._pole_pairs() if self._pole_pairs() else 0.0

        self.count_reading.setText("\n".join([
            self.tr("Travelled since the mark: {0:+.4f} turns ({1:+.1f}°)").format(
                travelled, travelled * 360.0),
            self.tr("Furthest from the mark so far: {0:.2f} turns").format(self._count_extreme),
            self.tr("Distance to the nearest whole turn: {0:+.4f} turns ({1:+.2f}°, "
                    "{2:.1f}° electrical)").format(error_turns, error_degrees, electrical),
        ]))

        if self._count_extreme < 0.5:
            self.count_verdict.setText(self.tr(
                "Turn the wheel at least one full turn each way, briskly, then bring the mark "
                "back to where it started."))
            self.count_verdict.setStyleSheet("")
        elif electrical >= self.COUNT_WARN_ELECTRICAL_DEG:
            self.count_verdict.setText(self.tr(
                "If the mark is lined up, the encoder has lost about {0:.0f} counts, which is "
                "{1:.0f} electrical degrees. Past ninety the torque reverses and the wheel pulls "
                "the way it is already going.").format(
                    abs(error_turns) * self._cpr(), electrical))
            self.count_verdict.setStyleSheet(f"color: {AppColors.WARNING};")
        else:
            self.count_verdict.setText(self.tr(
                "With the mark lined up this is {0:.1f} electrical degrees out, which is within "
                "what lining it up by eye can tell apart.").format(electrical))
            self.count_verdict.setStyleSheet(f"color: {AppColors.SUCCESS};")

    def _cpr(self):
        odrv = self._quiet_odrv()
        try:
            return float(odrv.axis0.encoder.config.cpr)
        except Exception:
            return 0.0

    # ------------------------------------------------------ calibration history ---

    # How a change in each recorded value should be shown. Resistance moves with
    # temperature and is read as degrees; the rest are shown as they are.
    HISTORY_COLUMNS = ['phase_resistance', 'phase_inductance', 'phase_offset']

    def record_calibration(self, quiet=False):
        """Stores what the board holds now, and says how it differs from last time."""
        odrv = self._quiet_odrv() if quiet else self.get_odrv()
        if not odrv:
            return
        entry = calibration_log.read_snapshot(odrv)
        if 'phase_resistance' not in entry:
            if not quiet:
                QMessageBox.warning(self, self.tr("Nothing to Record"), self.tr(
                    "Could not read the motor configuration from the board."))
            return
        previous = calibration_log.load()
        calibration_log.append(entry)
        self.refresh_history(previous[-1] if previous else None, entry)

    def refresh_history(self, previous=None, current=None):
        """Fills the table from the stored history and explains the latest change."""
        entries = calibration_log.load()
        rows = entries[-6:]
        self.history_table.setRowCount(len(rows))
        for row, entry in enumerate(reversed(rows)):
            stamp = time.strftime("%d/%m %H:%M", time.localtime(entry.get('time', 0)))
            cells = [stamp]
            for name in self.HISTORY_COLUMNS:
                value = entry.get(name)
                if value is None:
                    cells.append("-")
                elif name == 'phase_inductance':
                    cells.append(f"{value * 1e6:.0f} uH")
                elif name == 'phase_resistance':
                    cells.append(f"{value:.4f}")
                else:
                    cells.append(f"{value:.0f}")
            cells.append(f"{entry.get('calibration_current', 0):.1f} A")
            for column, text in enumerate(cells):
                self.history_table.setItem(row, column, QTableWidgetItem(text))

        if not entries:
            self.history_verdict.setText(self.tr(
                "Nothing recorded yet. Press Record after a calibration to start comparing."))
            self.history_verdict.setStyleSheet("")
            return

        if previous is None or current is None:
            if len(entries) >= 2:
                previous, current = entries[-2], entries[-1]
            else:
                self.history_verdict.setText(self.tr(
                    "One calibration recorded. The next one will be compared against it."))
                self.history_verdict.setStyleSheet("")
                return

        lines, worrying = [], False
        degrees = calibration_log.temperature_difference(
            previous.get('phase_resistance'), current.get('phase_resistance'))
        if degrees is not None and abs(degrees) >= 5.0:
            # Both figures come from the same calibration routine, so what is left
            # between them is the copper being at a different temperature. That is a
            # comparison worth making; measuring resistance against a running
            # measurement instead is not, and this code does not do it.
            lines.append(self.tr(
                "The winding is about {0:.0f} C {1} than at the previous calibration.").format(
                    abs(degrees), self.tr("warmer") if degrees > 0 else self.tr("cooler")))
            if degrees > 0:
                worrying = True
                lines.append(self.tr(
                    "A warm motor calibrates to a warm resistance, and every later run inherits "
                    "it: it eats the voltage headroom and the magnets give up about {0:.1f}% of "
                    "flux, so Kt reads low by that much. Let it cool and calibrate again.")
                    .format(degrees * 0.11))

        for name, before, after, fraction, notable in calibration_log.compare(previous, current):
            if not notable:
                continue
            worrying = True
            if name == 'phase_resistance' and degrees is not None:
                continue      # already said, in degrees, which means more than a percentage
            if name == 'phase_inductance':
                lines.append(self.tr("phase_inductance moved {0:.0f}%, {1:.0f} to {2:.0f} uH.")
                             .format(fraction * 100, before * 1e6, after * 1e6))
            elif name == 'direction':
                lines.append(self.tr(
                    "encoder direction flipped, {0:.0f} to {1:.0f}. The motor will run backwards "
                    "until this is sorted out.").format(before, after))
            elif name in ('cpr', 'pole_pairs'):
                lines.append(self.tr(
                    "{0} changed from {1:.0f} to {2:.0f}. This is a setting, not a measurement, "
                    "so something rewrote it.").format(name, before, after))
            else:
                lines.append(self.tr("{0}: {1:.4g} to {2:.4g}.").format(name, before, after))

        if not lines:
            lines.append(self.tr("This calibration matches the previous one."))
        self.history_verdict.setText("\n".join(lines))
        self.history_verdict.setStyleSheet(
            f"color: {AppColors.WARNING};" if worrying else f"color: {AppColors.SUCCESS};")

    # ------------------------------------------------------------- centre / zero ---

    # What closed loop resumes into at power-on. ODrive has no startup state that moves
    # to a position (startup_homing wants a real endstop on a GPIO, which a wheel has
    # not got), but it does restore the saved control mode when it arms, so an axis
    # saved in position control with input_pos at zero drives itself to centre. The
    # OpenFFBoard then switches it to torque control over CAN when it connects, and
    # force feedback takes over from there.
    AUTOCENTRE_SETTINGS = [
        ('axis0.config.startup_encoder_index_search', True),
        ('axis0.config.startup_closed_loop_control', True),
        ('axis0.controller.config.control_mode', CONTROL_MODE_POSITION_CONTROL),
        ('axis0.controller.config.input_mode', INPUT_MODE_TRAP_TRAJ),
        ('axis0.controller.input_pos', 0.0),
        ('axis0.encoder.config.use_index', True),
        ('axis0.encoder.config.use_index_offset', True),
        ('axis0.encoder.config.pre_calibrated', True),
        ('axis0.motor.config.pre_calibrated', True),
    ]

    def _centre_reference(self, odrv):
        """
        Returns the position the axis is given when the index is found, in turns.

        With use_index_offset off the firmware sets zero there, whatever index_offset
        happens to hold, so reading the stored number in that case would measure the
        centre against a reference the board is not actually using.
        """
        try:
            if not bool(odrv.axis0.encoder.config.use_index_offset):
                return 0.0
            return float(odrv.axis0.encoder.config.index_offset)
        except Exception:
            return 0.0

    def _refresh_centre_position(self):
        """
        Updates only the position line.

        The full check walks a dozen properties, and every one is a round trip over USB
        that the telemetry worker is already competing for. Twice a second that is worth
        avoiding, and the position is the only part that moves while someone lines the
        wheel up.
        """
        odrv = self._quiet_odrv()
        if odrv is None or not self._centre_lines:
            return
        try:
            position = float(odrv.axis0.encoder.pos_estimate)
        except Exception:
            return
        lines = list(self._centre_lines)
        lines[0] = self.tr("Position now: {0:+.4f} turns ({1:+.1f}°)").format(
            position, position * 360.0)
        self._centre_lines = lines
        self.centre_status.setText("\n".join(lines))

    def refresh_centre_status(self):
        """Describes where zero is and whether the axis will centre itself at power-on."""
        odrv = self._quiet_odrv()
        if odrv is None:
            self._centre_lines = []
            self.centre_status.setText(self.tr("Connect to see the current centre."))
            self.centre_status.setStyleSheet("")
            for button in (self.mark_centre_btn, self.goto_centre_btn, self.autocentre_btn):
                button.setEnabled(False)
            return

        busy = self.align_thread is not None or self.centre_thread is not None
        for button in (self.mark_centre_btn, self.goto_centre_btn, self.autocentre_btn):
            button.setEnabled(not busy)

        lines, missing = [], []
        try:
            position = float(odrv.axis0.encoder.pos_estimate)
            lines.append(self.tr("Position now: {0:+.4f} turns ({1:+.1f}°)").format(
                position, position * 360.0))
        except Exception:
            pass
        try:
            if bool(odrv.axis0.encoder.config.use_index_offset):
                lines.append(self.tr("The index sets the position to {0:+.4f} turns.").format(
                    float(odrv.axis0.encoder.config.index_offset)))
            else:
                lines.append(self.tr("The index sets the position to zero (no offset in use)."))
        except Exception:
            pass

        for path, wanted in self.AUTOCENTRE_SETTINGS:
            if path == 'axis0.controller.input_pos':
                continue
            owner, attr = resolve(odrv, path)
            if owner is None:
                continue
            try:
                actual = getattr(owner, attr)
                matches = (bool(actual) == wanted) if isinstance(wanted, bool) else (actual == wanted)
                if not matches:
                    # Two of these are called pre_calibrated, one on the encoder and one
                    # on the motor, so the leaf alone lists the same word twice. Dropping
                    # the parts every path shares leaves what tells them apart.
                    missing.append(".".join(
                        part for part in path.split('.') if part not in ('axis0', 'config')))
            except Exception:
                continue

        # Whether the move to centre is armed decides what the button does, so that
        # turning it off is as reachable as turning it on. Only the position control
        # half is considered: the index search can stay on either way, and should,
        # since that is what makes the position mean the same thing after every boot.
        self._autocentre_armed = not missing
        if missing:
            lines.append(self.tr("It will NOT centre itself at power-on. Still to set: {0}.")
                         .format(", ".join(missing)))
            self.centre_status.setStyleSheet(f"color: {AppColors.WARNING};")
        else:
            lines.append(self.tr("It will find the index and move to centre at power-on."))
            self.centre_status.setStyleSheet(f"color: {AppColors.SUCCESS};")
        self.autocentre_btn.setText(
            self.tr("Stop Centring at Power-On") if self._autocentre_armed
            else self.tr("Centre Automatically at Power-On"))
        # Kept so the cheap poll can replace the first line without walking the rest.
        self._centre_lines = lines
        self.centre_status.setText("\n".join(lines))

    def _quiet_odrv(self):
        """The ODrive if connected, without the status-bar complaint get_odrv() makes."""
        if self.main_window.is_connected and self.main_window.odrv_proxy:
            return self.main_window.odrv_proxy.odrv
        return None

    def mark_current_as_centre(self):
        """Stores wherever the wheel is now as the position the index should report."""
        odrv = self.get_odrv()
        if not odrv:
            return
        try:
            if not bool(odrv.axis0.encoder.config.use_index):
                QMessageBox.warning(self, self.tr("Index Needed"), self.tr(
                    "encoder.config.use_index is off, so the encoder has no fixed mark and a "
                    "centre stored now would mean something different after the next power-on.\n\n"
                    "Turn it on, calibrate, then set the centre."))
                return
            if not bool(odrv.axis0.encoder.index_found):
                QMessageBox.warning(self, self.tr("Index Not Found"), self.tr(
                    "The index has not been found since power-on, so the position is not "
                    "referenced yet. Run the index search first, or use Find Index and Centre."))
                return
            position = float(odrv.axis0.encoder.pos_estimate)
            # The offset is what the index must report so that here reads zero: whatever
            # it reports today, less how far the shaft has come since.
            new_offset = self._centre_reference(odrv) - position
        except Exception as e:
            QMessageBox.critical(self, self.tr("Error"), self.tr(
                "Could not read the encoder: {0}").format(e))
            return

        if QMessageBox.question(self, self.tr("Set Centre"), self.tr(
                "Store the wheel's position right now as the centre?\n\n"
                "index_offset becomes {0:+.4f} turns. Hold the wheel straight before "
                "confirming.").format(new_offset),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                ) != QMessageBox.StandardButton.Yes:
            return

        try:
            odrv.axis0.encoder.config.index_offset = new_offset
            odrv.axis0.encoder.config.use_index_offset = True
            self.centre_result.setText(self.tr(
                "Centre stored: index_offset {0:+.4f} turns. Save the configuration to keep it.")
                .format(new_offset))
            self.centre_result.setStyleSheet(f"color: {AppColors.SUCCESS};")
        except Exception as e:
            self.centre_result.setText(self.tr("Could not write the centre: {0}").format(e))
            self.centre_result.setStyleSheet(f"color: {AppColors.ERROR};")
        self.refresh_centre_status()

    def enable_autocentre(self):
        """Arms the move to centre at power-on, or stands it down if it is already armed."""
        odrv = self.get_odrv()
        if not odrv:
            return
        if getattr(self, '_autocentre_armed', False):
            self.disable_autocentre(odrv)
            return
        if QMessageBox.question(self, self.tr("Centre At Power-On"), self.tr(
                "From now on the wheel will turn on its own at every power-on: first to find "
                "the index, then to the centre.\n\n"
                "Make sure nothing is in its way and nobody is holding it when power comes on. "
                "Continue?"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                ) != QMessageBox.StandardButton.Yes:
            return

        written, skipped = [], []
        for path, value in self.AUTOCENTRE_SETTINGS:
            owner, attr = resolve(odrv, path)
            if owner is None:
                skipped.append(path)
                continue
            try:
                setattr(owner, attr, value)
                written.append(attr)
            except Exception:
                skipped.append(path)

        message = self.tr("Set {0} settings. Save the configuration to keep them.").format(len(written))
        if skipped:
            message += "\n" + self.tr("This firmware does not carry: {0}").format(", ".join(skipped))
        message += "\n\n" + self.tr(
            "The OpenFFBoard switches the axis to torque control when it connects over CAN, so "
            "force feedback takes over once it has centred.")
        self.centre_result.setText(message)
        self.centre_result.setStyleSheet(f"color: {AppColors.SUCCESS};")
        self.refresh_centre_status()


    def disable_autocentre(self, odrv):
        """
        Stands down the move to centre, leaving the index search alone.

        Only position control is undone. The index search is what makes the position
        mean the same thing after every boot, so a wheel that is no longer to move on
        its own still wants it, and the stored centre stays valid for when it is armed
        again.
        """
        if QMessageBox.question(self, self.tr("Stop Centring"), self.tr(
                "Stop the wheel moving to centre when the drive powers up?\n\n"
                "The index search stays on, so the centre you stored is still good and "
                "position still means the same thing after every power-on."),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                ) != QMessageBox.StandardButton.Yes:
            return
        try:
            odrv.axis0.controller.config.control_mode = CONTROL_MODE_TORQUE_CONTROL
            odrv.axis0.controller.config.input_mode = INPUT_MODE_PASSTHROUGH
            odrv.axis0.controller.input_torque = 0.0
        except Exception as e:
            self.centre_result.setText(self.tr("Could not change the control mode: {0}").format(e))
            self.centre_result.setStyleSheet(f"color: {AppColors.ERROR};")
            return
        self.centre_result.setText(self.tr(
            "The wheel will no longer move to centre at power-on. It still finds the index, so "
            "the stored centre is unchanged. Save the configuration to keep this."))
        self.centre_result.setStyleSheet(f"color: {AppColors.SUCCESS};")
        self.refresh_centre_status()

    def go_to_centre(self):
        """Runs the power-on sequence now, so it can be watched before it runs unattended."""
        odrv = self.get_odrv()
        if not odrv or self.centre_thread is not None:
            return
        if QMessageBox.question(self, self.tr("Find Index and Centre"), self.tr(
                "The wheel will turn on its own: first to find the index, then to the centre.\n\n"
                "It runs at {0:.1f} A and up to {1:.2f} turns/s, not your force feedback limits, "
                "which are put back afterwards.\n\nHands off the wheel. Continue?").format(
                    self.move_current_input.value(), self.move_speed_input.value()),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                ) != QMessageBox.StandardButton.Yes:
            return

        self.progress_bar.setValue(0)
        self.centre_result.setText("")
        self.centre_worker = CentringWorker(odrv, self.move_current_input.value(),
                                            self.move_speed_input.value())
        self.centre_thread = QThread()
        self.centre_worker.moveToThread(self.centre_thread)
        self.centre_worker.progress.connect(self._on_centre_progress)
        self.centre_worker.result.connect(self._on_centre_result)
        self.centre_worker.finished.connect(self.centre_thread.quit)
        self.centre_thread.started.connect(self.centre_worker.run)
        self.centre_thread.finished.connect(self._on_centre_thread_finished)
        self.refresh_centre_status()
        self.centre_thread.start()

    def _on_centre_progress(self, message, percent):
        self.status_label.setText(message)
        self.progress_bar.setValue(percent)

    def _on_centre_result(self, success, message):
        self.centre_result.setText(message)
        self.centre_result.setStyleSheet(
            f"color: {AppColors.SUCCESS};" if success else f"color: {AppColors.ERROR};")

    def _on_centre_thread_finished(self):
        self.centre_thread = None
        self.centre_worker = None
        self.progress_bar.setValue(0)
        self.refresh_centre_status()

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
        # One extra run happens before the measured ones and is discarded, so the time
        # quoted has to include it or the estimate reads short by a calibration.
        total = (runs + 1) * per_run
        self.estimate_label.setText(
            self.tr("Estimated duration: about {0:.0f} min {1:.0f} s ({2} calibrations, "
                    "one of them a discarded warm-up)")
            .format(total // 60, total % 60, runs + 1))

        if pole_pairs and self.main_window.is_connected and self.main_window.odrv_proxy:
            try:
                self._sync_calibration_current()
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


    def _mark_current_touched(self, _value):
        """Remembers that the calibration current is the user's choice, not the board's."""
        self._calib_current_touched = True

    def _mark_voltage_touched(self, _value):
        """Same for the resistance calibration voltage."""
        self._calib_voltage_touched = True

    def _sync_calibration_current(self):
        """
        Adopts the board's calibration current until the user overrides it.

        The value that works is whatever the motor was calibrated with, and the board
        already knows it. Offering a different one by default means this check can fail
        on a motor whose own Encoder tab calibrates perfectly well.
        """
        if self._calib_current_touched:
            return
        try:
            board = float(self.main_window.odrv_proxy.odrv.axis0.motor.config.calibration_current)
        except Exception:
            return
        if board > 0 and abs(board - self.calib_current_input.value()) > 0.05:
            self.calib_current_input.blockSignals(True)
            self.calib_current_input.setValue(board)
            self.calib_current_input.blockSignals(False)

        if self._calib_voltage_touched:
            return
        try:
            volts = float(
                self.main_window.odrv_proxy.odrv.axis0.motor.config.resistance_calib_max_voltage)
        except Exception:
            return
        if volts > 0 and abs(volts - self.calib_voltage_input.value()) > 0.05:
            self.calib_voltage_input.blockSignals(True)
            self.calib_voltage_input.setValue(volts)
            self.calib_voltage_input.blockSignals(False)

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
            calibration_voltage=self.calib_voltage_input.value(),
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
        # The check leaves the board freshly calibrated, so record it without waiting to
        # be asked. Quiet, because a failed connection here should not raise a dialog on
        # top of whatever the check already reported.
        self.record_calibration(quiet=True)
        self.align_thread, self.align_worker = None, None
        self.start_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)

    # ------------------------------------------------------------ BaseTab ---

    def populate_fields(self):
        """The sweep reads what it needs when it runs; the centre panel reflects the board."""
        self.refresh_centre_status()

    def apply_config(self):
        """The sweep writes the offset itself; there is no separate apply step."""
        pass
