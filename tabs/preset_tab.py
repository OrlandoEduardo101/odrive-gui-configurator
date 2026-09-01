# tabs/preset_tab.py
"""
This module defines the PresetTab, which collects the settings a force feedback wheel
controller typically needs into one reviewable list.

It deliberately shows current value against proposed value and lets each row be applied
or skipped, rather than writing a fixed profile in one shot. These are the conventional
ODrive settings for torque-commanded force feedback; they are not verified against any
particular controller's documentation, so the user reviews before anything is written.
"""
from PySide6.QtWidgets import (
    QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QMessageBox,
    QSpinBox, QComboBox, QDoubleSpinBox
)
from PySide6.QtCore import Qt, QEvent

from .base_tab import BaseTab
from .safety_tab import resolve
from app_config import AppColors, AppMessages

# Firmware constants, named here so the table can explain them in words.
CONTROL_MODE_TORQUE = 1
INPUT_MODE_PASSTHROUGH = 1


class PresetTab(BaseTab):
    """Manages the UI tab that proposes a force feedback oriented configuration."""

    def __init__(self, main_window, parent=None):
        super().__init__(main_window, parent)
        self.rows = []      # list of (path, proposed_value, description_key)
        self._setup_ui()
        self.retranslate_ui()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate_ui()
        super().changeEvent(event)

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        self.intro_label = QLabel()
        self.intro_label.setWordWrap(True)
        self.intro_label.setStyleSheet(f"color: {AppColors.WARNING};")
        layout.addWidget(self.intro_label)

        self.options_group = QGroupBox()
        options_layout = QFormLayout(self.options_group)
        self.node_id = QSpinBox()
        self.node_id.setRange(0, 63)
        self.node_id.setValue(0)
        self.baud_rate = QComboBox()
        for rate in ("125000", "250000", "500000", "1000000"):
            self.baud_rate.addItem(rate)
        self.baud_rate.setCurrentText("500000")
        self.vel_limit = QDoubleSpinBox()
        self.vel_limit.setRange(1.0, 500.0)
        self.vel_limit.setDecimals(0)
        self.vel_limit.setValue(50.0)
        self.vel_limit.setSuffix(" turns/s")
        self.label_node_id, self.label_baud, self.label_vel = QLabel(), QLabel(), QLabel()
        options_layout.addRow(self.label_node_id, self.node_id)
        options_layout.addRow(self.label_baud, self.baud_rate)
        options_layout.addRow(self.label_vel, self.vel_limit)
        for widget in (self.node_id, self.baud_rate, self.vel_limit):
            (widget.valueChanged if hasattr(widget, 'valueChanged') else widget.currentTextChanged).connect(self.refresh)
        layout.addWidget(self.options_group)

        self.table = QTableWidget(0, 4)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)

        buttons = QHBoxLayout()
        self.refresh_btn = QPushButton()
        self.refresh_btn.clicked.connect(self.refresh)
        self.apply_btn = QPushButton()
        self.apply_btn.clicked.connect(self.apply_config)
        buttons.addWidget(self.refresh_btn)
        buttons.addStretch()
        buttons.addWidget(self.apply_btn)
        layout.addLayout(buttons)

    def retranslate_ui(self):
        self.intro_label.setText(self.tr(
            "Settings for driving this ODrive from OpenFFBoard over CAN, taken from its ODrive "
            "guide and its CAN driver source. The guide recommends firmware 0.5.6.\n\n"
            "OpenFFBoard scales its output as torque = (output / 32767) x maxtorque and sends "
            "that in Nm, which the ODrive divides by the torque constant to get current. So the "
            "torque constant has to be right, or the wheel draws current that does not match the "
            "force being asked for.\n\n"
            "Rows already matching are shown in green and will not be written."
        ))
        self.options_group.setTitle(self.tr("Preset Options"))
        self.label_node_id.setText(self.tr("CAN Node ID:"))
        self.label_baud.setText(self.tr("CAN Baud Rate:"))
        self.label_vel.setText(self.tr("Velocity limit:"))
        self.table.setHorizontalHeaderLabels([
            self.tr("Setting"), self.tr("Current"), self.tr("Proposed"), self.tr("Why")])
        self.refresh_btn.setText(self.tr("Re-read from ODrive"))
        self.apply_btn.setText(self.tr("Apply Proposed Changes"))
        self.refresh()

    # ------------------------------------------------------------- content ---

    def _proposals(self):
        """Builds the proposed settings list. Returns (path, value, why) tuples."""
        return [
            ('axis0.controller.config.control_mode', CONTROL_MODE_TORQUE,
             self.tr("The controller commands torque, not position or velocity.")),
            ('axis0.controller.config.input_mode', INPUT_MODE_PASSTHROUGH,
             self.tr("Pass the torque command straight through, with no ramping in between.")),
            ('axis0.controller.config.enable_torque_mode_vel_limit', False,
             self.tr("Otherwise the wheel fights you as it speeds up, which reads as fading force.")),
            ('axis0.controller.config.vel_limit', float(self.vel_limit.value()),
             self.tr("High enough that fast steering never reaches it.")),
            ('axis0.controller.config.enable_overspeed_error', False,
             self.tr("A fast flick of the wheel should not fault the drive mid-use.")),
            ('axis0.config.startup_closed_loop_control', True,
             self.tr("Critical: if OpenFFBoard finds the axis idle it runs a full calibration, "
                     "which overwrites your alignment offset. Arming on boot prevents that.")),
            ('axis0.encoder.config.use_index', True,
             self.tr("Gives a repeatable zero and lets the calibration be stored.")),
            ('axis0.encoder.config.pre_calibrated', True,
             self.tr("Required for arming on boot without recalibrating the encoder.")),
            ('axis0.motor.config.pre_calibrated', True,
             self.tr("Required for arming on boot without recalibrating the motor.")),
            ('axis0.config.can.node_id', int(self.node_id.value()),
             self.tr("Must match the node ID the controller is configured to talk to.")),
            ('can.config.baud_rate', int(self.baud_rate.currentText()),
             self.tr("Must match the controller's CAN bit rate exactly.")),
        ]

    @staticmethod
    def _same(current, proposed):
        if isinstance(proposed, bool) or isinstance(current, bool):
            return bool(current) == bool(proposed)
        try:
            return abs(float(current) - float(proposed)) < 1e-6
        except (TypeError, ValueError):
            return current == proposed

    @staticmethod
    def _display(value):
        if isinstance(value, bool):
            return "True" if value else "False"
        if isinstance(value, float):
            return f"{value:g}"
        return str(value)

    def refresh(self):
        """Re-reads the board and rebuilds the comparison table."""
        self.rows = []
        odrv = None
        if self.main_window.is_connected and self.main_window.odrv_proxy:
            odrv = self.main_window.odrv_proxy.odrv

        proposals = self._proposals()
        self.table.setRowCount(len(proposals))
        for row, (path, proposed, why) in enumerate(proposals):
            current, available = None, False
            if odrv is not None:
                owner, attr = resolve(odrv, path)
                if owner is not None:
                    available = True
                    try:
                        current = getattr(owner, attr)
                    except Exception:
                        available = False

            if not available:
                current_text = self.tr("unavailable") if odrv is not None else self.tr("not connected")
                matches = False
            else:
                current_text = self._display(current)
                matches = self._same(current, proposed)
                if not matches:
                    self.rows.append((path, proposed))

            items = [QTableWidgetItem(path.split('.')[-1]),
                     QTableWidgetItem(current_text),
                     QTableWidgetItem(self._display(proposed)),
                     QTableWidgetItem(why)]
            for item in items:
                if matches:
                    item.setForeground(Qt.GlobalColor.darkGreen)
                elif not available:
                    item.setForeground(Qt.GlobalColor.gray)
            for col, item in enumerate(items):
                self.table.setItem(row, col, item)

        self.apply_btn.setEnabled(bool(self.rows))
        self.apply_btn.setText(
            self.tr("Apply {0} Changes").format(len(self.rows)) if self.rows
            else self.tr("Nothing to Change"))

    # ------------------------------------------------------------ BaseTab ---

    def populate_fields(self):
        self.refresh()

    def apply_config(self):
        """Writes only the rows that differ, after showing exactly what will change."""
        odrv = self.get_odrv()
        if not odrv or not self.rows:
            return

        summary = "\n".join(f"  {p.split('.')[-1]} -> {self._display(v)}" for p, v in self.rows)
        confirm = QMessageBox.question(self, self.tr("Confirm"), self.tr(
            "Write these {0} settings?\n\n{1}").format(len(self.rows), summary),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if confirm != QMessageBox.StandardButton.Yes:
            return

        written, failed = 0, []
        for path, value in self.rows:
            owner, attr = resolve(odrv, path)
            if owner is None:
                failed.append(path)
                continue
            try:
                setattr(owner, attr, value)
                written += 1
            except Exception as e:
                failed.append(f"{path}: {e}")

        self.refresh()
        if failed:
            QMessageBox.warning(self, self.tr("Partly Applied"), self.tr(
                "{0} settings applied, {1} failed:\n\n{2}").format(written, len(failed), "\n".join(failed)))
            return
        self.main_window.show_status_message(
            self.tr("Preset applied ({0} values).").format(written), AppColors.SUCCESS, 3000)
        QMessageBox.information(self, self.tr("Reminder"),
                                f"{self.tr(AppMessages.CONFIG_APPLY_SUCCESS)}\n\n{self.tr(AppMessages.SAVE_REMINDER)}")
