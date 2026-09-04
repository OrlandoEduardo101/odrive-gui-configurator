# tabs/tuning_container.py
"""
This module groups the optional tuning and safety tools under a single top level tab,
so that adding them does not crowd the main tab bar. It owns the sub-tabs and forwards
the signals and lifecycle calls they need.
"""
from PySide6.QtWidgets import QVBoxLayout, QTabWidget, QScrollArea, QFrame
from PySide6.QtCore import QEvent

from .base_tab import BaseTab
from .tuning_tab import TuningTab
from .alignment_tab import AlignmentTab
from .safety_tab import SafetyTab
from .preset_tab import PresetTab


class TuningContainer(BaseTab):
    """Holds the Kt, alignment, safety and preset sub-tabs."""

    def __init__(self, main_window, parent=None):
        super().__init__(main_window, parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.inner_tabs = QTabWidget()
        self.kt_tab = TuningTab(main_window)
        self.alignment_tab = AlignmentTab(main_window)
        self.safety_tab = SafetyTab(main_window)
        self.preset_tab = PresetTab(main_window)
        # Each sub-tab scrolls rather than compressing. These pages are taller than the
        # window's minimum height, and without this the groups inside get crushed into
        # unreadable slivers instead of simply scrolling.
        for widget in (self.kt_tab, self.alignment_tab, self.safety_tab, self.preset_tab):
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.Shape.NoFrame)
            area.setWidget(widget)
            self.inner_tabs.addTab(area, "")
        layout.addWidget(self.inner_tabs)
        self.retranslate_ui()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate_ui()
        super().changeEvent(event)

    def retranslate_ui(self):
        """Re-translates this container's tab labels and every sub-tab."""
        self.inner_tabs.setTabText(0, self.tr("Kt Measurement"))
        self.inner_tabs.setTabText(1, self.tr("Alignment"))
        self.inner_tabs.setTabText(2, self.tr("Safety"))
        self.inner_tabs.setTabText(3, self.tr("FFB Preset"))
        for widget in (self.kt_tab, self.alignment_tab, self.safety_tab, self.preset_tab):
            widget.retranslate_ui()

    # ----------------------------------------------------- signal fan-out ---

    def update_live_current(self, iq_measured):
        self.kt_tab.update_live_current(iq_measured)

    def update_extended_telemetry(self, readings):
        self.safety_tab.update_extended_telemetry(readings)

    def reset_live_readings(self):
        self.kt_tab.reset_live_current()
        self.safety_tab.reset_live_readings()

    # ------------------------------------------------------------ BaseTab ---

    def populate_fields(self):
        """
        Loads the sub-tabs that read from the board. Each is isolated so that one
        unsupported property cannot stop the others from populating.
        """
        for widget in (self.kt_tab, self.safety_tab, self.preset_tab):
            try:
                widget.populate_fields()
            except Exception as e:
                print(f"Could not populate {type(widget).__name__}: {e}")

    def apply_config(self):
        """Each sub-tab applies its own settings; there is no combined apply."""
        pass
