# tabs/safety_tab.py
"""
This module defines the SafetyTab, which exposes the protection settings that matter
most on a direct drive wheel: thermal limits, the control watchdog, a torque ceiling
expressed in Nm, and the bus overvoltage ramp.

Every setting here is probed on the connected board before being offered. Firmware
versions differ in which of these exist, and a field that silently writes nowhere is
worse than a field that plainly says it is unavailable.
"""
from PySide6.QtWidgets import (
    QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QDoubleSpinBox, QCheckBox, QMessageBox
)
from PySide6.QtCore import Qt, QEvent

from .base_tab import BaseTab
from app_config import AppColors, AppMessages


def resolve(root, path):
    """
    Walks a dotted property path. Returns (owner, attribute) when it exists on this
    board, or (None, None) when the firmware does not carry it.
    """
    parts = path.split('.')
    obj = root
    try:
        for part in parts[:-1]:
            obj = getattr(obj, part)
        if hasattr(obj, parts[-1]):
            return obj, parts[-1]
    except Exception:
        pass
    return None, None


class SafetyTab(BaseTab):
    """Manages the UI tab for thermal, watchdog, torque and bus protection settings."""

    def __init__(self, main_window, parent=None):
        super().__init__(main_window, parent)
        self.available = {}   # path -> bool, filled in on connect
        self._setup_ui()
        self.retranslate_ui()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate_ui()
        super().changeEvent(event)

    # ------------------------------------------------------------------ UI ---

    def _temp_spin(self, default):
        box = QDoubleSpinBox()
        box.setRange(0.0, 240.0)
        box.setDecimals(0)
        box.setValue(default)
        box.setSuffix(" °C")
        return box

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # --- Live readings -------------------------------------------------
        self.live_group = QGroupBox()
        live_layout = QHBoxLayout(self.live_group)
        self.fet_temp_label = QLabel("-")
        self.motor_temp_label = QLabel("-")
        self.label_fet_live = QLabel()
        self.label_motor_live = QLabel()
        for widget in (self.fet_temp_label, self.motor_temp_label):
            widget.setStyleSheet("font-size: 14pt; font-weight: bold;")
        live_layout.addWidget(self.label_fet_live)
        live_layout.addWidget(self.fet_temp_label)
        live_layout.addSpacing(20)
        live_layout.addWidget(self.label_motor_live)
        live_layout.addWidget(self.motor_temp_label)
        live_layout.addStretch()

        # --- Thermal limits ------------------------------------------------
        self.thermal_group = QGroupBox()
        thermal_layout = QFormLayout(self.thermal_group)
        self.thermal_help = QLabel()
        self.thermal_help.setWordWrap(True)
        self.thermal_help.setStyleSheet(f"color: {AppColors.INFO};")
        thermal_layout.addRow(self.thermal_help)

        self.fet_lower = self._temp_spin(80)
        self.fet_upper = self._temp_spin(100)
        self.motor_enabled = QCheckBox()
        self.motor_lower = self._temp_spin(80)
        self.motor_upper = self._temp_spin(100)

        self.label_fet_lower, self.label_fet_upper = QLabel(), QLabel()
        self.label_motor_lower, self.label_motor_upper = QLabel(), QLabel()
        thermal_layout.addRow(self.label_fet_lower, self.fet_lower)
        thermal_layout.addRow(self.label_fet_upper, self.fet_upper)
        thermal_layout.addRow(self.motor_enabled)
        thermal_layout.addRow(self.label_motor_lower, self.motor_lower)
        thermal_layout.addRow(self.label_motor_upper, self.motor_upper)

        # --- Torque ceiling ------------------------------------------------
        self.torque_group = QGroupBox()
        torque_layout = QFormLayout(self.torque_group)
        self.torque_help = QLabel()
        self.torque_help.setWordWrap(True)
        self.torque_help.setStyleSheet(f"color: {AppColors.INFO};")
        torque_layout.addRow(self.torque_help)
        self.torque_unlimited = QCheckBox()
        self.torque_unlimited.toggled.connect(self._on_torque_unlimited)
        self.torque_lim = QDoubleSpinBox()
        self.torque_lim.setRange(0.1, 200.0)
        self.torque_lim.setDecimals(2)
        self.torque_lim.setValue(10.0)
        self.torque_lim.setSuffix(" Nm")
        self.torque_lim.valueChanged.connect(self._update_torque_equivalent)
        self.label_torque_lim = QLabel()
        self.torque_equivalent = QLabel()
        torque_layout.addRow(self.torque_unlimited)
        torque_layout.addRow(self.label_torque_lim, self.torque_lim)
        torque_layout.addRow(self.torque_equivalent)

        # --- Watchdog ------------------------------------------------------
        self.watchdog_group = QGroupBox()
        watchdog_layout = QFormLayout(self.watchdog_group)
        self.watchdog_help = QLabel()
        self.watchdog_help.setWordWrap(True)
        self.watchdog_help.setStyleSheet(f"color: {AppColors.WARNING};")
        watchdog_layout.addRow(self.watchdog_help)
        self.watchdog_enabled = QCheckBox()
        self.watchdog_timeout = QDoubleSpinBox()
        self.watchdog_timeout.setRange(0.05, 10.0)
        self.watchdog_timeout.setDecimals(2)
        # Well above OpenFFBoard's command period, while keeping the window in which a
        # dead link can leave the wheel under torque short.
        self.watchdog_timeout.setValue(0.5)
        self.watchdog_timeout.setSuffix(" s")
        self.label_watchdog_timeout = QLabel()
        watchdog_layout.addRow(self.watchdog_enabled)
        watchdog_layout.addRow(self.label_watchdog_timeout, self.watchdog_timeout)

        # --- Bus overvoltage ramp -------------------------------------------
        self.bus_group = QGroupBox()
        bus_layout = QFormLayout(self.bus_group)
        self.bus_help = QLabel()
        self.bus_help.setWordWrap(True)
        self.bus_help.setStyleSheet(f"color: {AppColors.INFO};")
        bus_layout.addRow(self.bus_help)
        self.ramp_enabled = QCheckBox()
        self.ramp_start = QDoubleSpinBox()
        self.ramp_end = QDoubleSpinBox()
        for box in (self.ramp_start, self.ramp_end):
            box.setRange(6.0, 120.0)
            box.setDecimals(1)
            box.setSuffix(" V")
        self.ramp_start.setValue(50.0)
        self.ramp_end.setValue(56.0)
        self.label_ramp_start, self.label_ramp_end = QLabel(), QLabel()
        bus_layout.addRow(self.ramp_enabled)
        bus_layout.addRow(self.label_ramp_start, self.ramp_start)
        bus_layout.addRow(self.label_ramp_end, self.ramp_end)

        self.unavailable_label = QLabel()
        self.unavailable_label.setWordWrap(True)
        self.unavailable_label.setStyleSheet(f"color: {AppColors.WARNING};")

        self.apply_btn = QPushButton()
        self.apply_btn.clicked.connect(self.apply_config)

        layout.addWidget(self.live_group)
        layout.addWidget(self.thermal_group)
        layout.addWidget(self.torque_group)
        layout.addWidget(self.watchdog_group)
        layout.addWidget(self.bus_group)
        layout.addWidget(self.unavailable_label)
        layout.addStretch()
        layout.addWidget(self.apply_btn, 0, Qt.AlignmentFlag.AlignRight)

    def retranslate_ui(self):
        self.live_group.setTitle(self.tr("Live Temperatures"))
        self.label_fet_live.setText(self.tr("Board (FET):"))
        self.label_motor_live.setText(self.tr("Motor:"))

        self.thermal_group.setTitle(self.tr("Thermal Limits"))
        self.thermal_help.setText(self.tr(
            "Between the lower and upper limit the ODrive reduces the current gradually. "
            "Above the upper limit it faults and disarms. On a wheel that band matters: a hard "
            "cutoff mid-corner makes the wheel go limp without warning, while the ramp lets the "
            "force fade in a way you can feel coming."
        ))
        self.label_fet_lower.setText(self.tr("Board derate starts at:"))
        self.label_fet_upper.setText(self.tr("Board fault at:"))
        self.motor_enabled.setText(self.tr("Enable motor thermistor (needs an NTC wired to a GPIO)"))
        self.label_motor_lower.setText(self.tr("Motor derate starts at:"))
        self.label_motor_upper.setText(self.tr("Motor fault at:"))
        self.motor_enabled.setToolTip(self.tr(
            "The board sensor only measures the power stage. Only an NTC on the winding "
            "measures the copper that actually burns."))

        self.torque_group.setTitle(self.tr("Torque Ceiling"))
        self.torque_help.setText(self.tr(
            "A ceiling in Nm is only meaningful once the torque constant is correct, since "
            "the ODrive converts it to current by dividing by Kt."
        ))
        self.torque_unlimited.setText(self.tr("No torque limit (firmware default)"))
        self.label_torque_lim.setText(self.tr("Maximum torque:"))

        self.watchdog_group.setTitle(self.tr("Control Watchdog"))
        self.watchdog_help.setText(self.tr(
            "Disarms the axis when no command arrives within the timeout, so a dropped CAN link "
            "cannot leave the wheel holding torque against you.\n\n"
            "Safe with OpenFFBoard over CAN: ODrive firmware feeds the watchdog on every CAN "
            "message it receives, and the board sends a torque command every cycle. Pick a timeout "
            "well above that period. If you drive the ODrive some other way, confirm it sends "
            "something periodically before enabling this."
        ))
        self.watchdog_enabled.setText(self.tr("Enable watchdog"))
        self.label_watchdog_timeout.setText(self.tr("Timeout:"))

        self.bus_group.setTitle(self.tr("Bus Overvoltage Ramp"))
        self.bus_help.setText(self.tr(
            "Limits regeneration progressively as the bus voltage climbs, instead of waiting "
            "for the trip level to disarm everything. Set the end below the trip level so the "
            "ramp acts first.\n\n"
            "This cannot protect against a brake resistor that has failed open: the ODrive has "
            "no way to detect that, and the trip level stays the only backstop."
        ))
        self.ramp_enabled.setText(self.tr("Enable overvoltage ramp"))
        self.label_ramp_start.setText(self.tr("Start limiting at:"))
        self.label_ramp_end.setText(self.tr("Fully limited at:"))

        self.apply_btn.setText(self.tr("Apply Safety Settings"))
        self._update_torque_equivalent()

    def _on_torque_unlimited(self, checked):
        self.torque_lim.setEnabled(not checked)
        self._update_torque_equivalent()

    def _update_torque_equivalent(self):
        """Shows what the torque ceiling means in amps, using the configured Kt."""
        if self.torque_unlimited.isChecked():
            self.torque_equivalent.setText("")
            return
        kt = None
        if self.main_window.is_connected and self.main_window.odrv_proxy:
            try:
                kt = self.main_window.odrv_proxy.odrv.axis0.motor.config.torque_constant
            except Exception:
                kt = None
        if kt and kt > 0:
            self.torque_equivalent.setText(self.tr("At Kt = {0:.4f} Nm/A that is {1:.1f} A of phase current.")
                                           .format(kt, self.torque_lim.value() / kt))
        else:
            self.torque_equivalent.setText(self.tr("Connect to see the equivalent current."))

    # ------------------------------------------------------- live readings ---

    def update_extended_telemetry(self, readings):
        """Slot for the worker's extended telemetry dict."""
        fet = readings.get('fet_temp')
        motor = readings.get('motor_temp')
        self.fet_temp_label.setText(self.tr("{0:.1f} °C").format(fet) if fet is not None else "-")
        self.motor_temp_label.setText(self.tr("{0:.1f} °C").format(motor) if motor is not None else "-")
        self._colour_temp(self.fet_temp_label, fet, self.fet_lower.value(), self.fet_upper.value())
        self._colour_temp(self.motor_temp_label, motor, self.motor_lower.value(), self.motor_upper.value())

    @staticmethod
    def _colour_temp(label, value, lower, upper):
        """Green below the derate point, orange inside the band, red past the fault limit."""
        if value is None:
            label.setStyleSheet("font-size: 14pt; font-weight: bold;")
            return
        if value >= upper:
            colour = AppColors.ERROR
        elif value >= lower:
            colour = AppColors.WARNING
        else:
            colour = AppColors.SUCCESS
        label.setStyleSheet(f"font-size: 14pt; font-weight: bold; color: {colour};")

    def reset_live_readings(self):
        for label in (self.fet_temp_label, self.motor_temp_label):
            label.setText("-")
            label.setStyleSheet("font-size: 14pt; font-weight: bold;")

    # ------------------------------------------------------------ BaseTab ---

    FIELDS = {
        'fet_lower':       'axis0.motor.fet_thermistor.config.temp_limit_lower',
        'fet_upper':       'axis0.motor.fet_thermistor.config.temp_limit_upper',
        'motor_enabled':   'axis0.motor.motor_thermistor.config.enabled',
        'motor_lower':     'axis0.motor.motor_thermistor.config.temp_limit_lower',
        'motor_upper':     'axis0.motor.motor_thermistor.config.temp_limit_upper',
        'torque_lim':      'axis0.controller.config.torque_lim',
        'watchdog_enabled':'axis0.config.enable_watchdog',
        'watchdog_timeout':'axis0.config.watchdog_timeout',
        'ramp_enabled':    'config.enable_dc_bus_overvoltage_ramp',
        'ramp_start':      'config.dc_bus_overvoltage_ramp_start',
        'ramp_end':        'config.dc_bus_overvoltage_ramp_end',
    }

    def populate_fields(self):
        """Reads what this firmware actually offers and disables the rest."""
        odrv = self.get_odrv()
        if not odrv:
            return
        missing = []
        for name, path in self.FIELDS.items():
            widget = getattr(self, name)
            owner, attr = resolve(odrv, path)
            self.available[path] = owner is not None
            if owner is None:
                widget.setEnabled(False)
                missing.append(path)
                continue
            widget.setEnabled(True)
            value = getattr(owner, attr)
            if isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            else:
                # torque_lim defaults to infinity, which no spin box can show.
                if name == 'torque_lim':
                    unlimited = not (value == value) or value == float('inf') or value > 1e6
                    self.torque_unlimited.setChecked(bool(unlimited))
                    if not unlimited:
                        widget.setValue(float(value))
                    continue
                try:
                    widget.setValue(float(value))
                except Exception:
                    pass

        if missing:
            self.unavailable_label.setText(
                self.tr("Not available on this firmware, left disabled:\n{0}").format("\n".join(missing)))
        else:
            self.unavailable_label.clear()
        self._update_torque_equivalent()

    def apply_config(self):
        """Writes back only the settings this firmware supports."""
        odrv = self.get_odrv()
        if not odrv:
            return

        if self.ramp_enabled.isChecked() and self.ramp_end.value() <= self.ramp_start.value():
            QMessageBox.warning(self, self.tr("Input Error"), self.tr(
                "The ramp end voltage must be above the start voltage."))
            return
        if self.fet_upper.value() <= self.fet_lower.value() or self.motor_upper.value() <= self.motor_lower.value():
            QMessageBox.warning(self, self.tr("Input Error"), self.tr(
                "Each fault temperature must be above its derate temperature."))
            return
        if self.watchdog_enabled.isChecked():
            confirm = QMessageBox.question(self, self.tr("Confirm"), self.tr(
                "Enable the watchdog?\n\nIf your controller does not feed it, the axis will "
                "disarm after {0:.2f} s of normal use.").format(self.watchdog_timeout.value()),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if confirm != QMessageBox.StandardButton.Yes:
                return

        written, failed = 0, []
        for name, path in self.FIELDS.items():
            owner, attr = resolve(odrv, path)
            if owner is None:
                continue
            widget = getattr(self, name)
            try:
                if name == 'torque_lim':
                    value = float('inf') if self.torque_unlimited.isChecked() else self.torque_lim.value()
                elif isinstance(widget, QCheckBox):
                    value = widget.isChecked()
                else:
                    value = widget.value()
                setattr(owner, attr, value)
                written += 1
            except Exception as e:
                failed.append(f"{path}: {e}")

        if failed:
            QMessageBox.warning(self, self.tr("Partly Applied"), self.tr(
                "{0} settings applied, {1} failed:\n\n{2}").format(written, len(failed), "\n".join(failed)))
            return
        self.main_window.show_status_message(
            self.tr("Safety settings applied ({0} values).").format(written), AppColors.SUCCESS, 3000)
        QMessageBox.information(self, self.tr("Reminder"),
                                f"{self.tr(AppMessages.CONFIG_APPLY_SUCCESS)}\n\n{self.tr(AppMessages.SAVE_REMINDER)}")
