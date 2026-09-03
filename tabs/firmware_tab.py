# tabs/firmware_tab.py
"""
This module defines the FirmwareTab class, providing a user interface for checking
ODrive firmware/hardware versions and performing firmware updates via DFU mode
using the STM32CubeProgrammer CLI tool.
"""
import sys
import os
import subprocess
import shutil
import tempfile
from PySide6.QtWidgets import (
    QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox, 
    QMessageBox, QTextEdit, QFileDialog, QWidget, QComboBox, QProgressBar
)
from PySide6.QtCore import Qt, QThread, QUrl, QCoreApplication, QEvent, Signal, QObject
from PySide6.QtGui import QFont, QDesktopServices, QTextCursor

from .base_tab import BaseTab
from .firmware_download import (FirmwareDownloadWorker, expected_asset_name,
                                AVAILABLE_VERSIONS, RECOMMENDED_VERSION)
from app_config import AppColors

def find_stm32_programmer_cli():
    """Locates the STM32_Programmer_CLI executable on Windows systems."""
    if os.name != 'nt': return None
    search_paths = [
        os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "STMicroelectronics", "STM32Cube", "STM32CubeProgrammer", "bin"),
        os.path.join(os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)"), "STMicroelectronics", "STM32Cube", "STM32CubeProgrammer", "bin")
    ]
    for path in search_paths:
        cli_path = os.path.join(path, "STM32_Programmer_CLI.exe")
        if os.path.exists(cli_path): return cli_path
    return shutil.which("STM32_Programmer_CLI.exe")

class DfuCheckWorker(QObject):
    finished = Signal(bool, str)
    def __init__(self, cli_path):
        super().__init__()
        self.cli_path = cli_path
    def run(self):
        try:
            command = f'"{self.cli_path}" -c port=usb1'
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            cli_directory = os.path.dirname(self.cli_path)
            process = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', startupinfo=startupinfo, cwd=cli_directory, timeout=5, shell=True)
            output = process.stdout + "\n" + process.stderr
            self.finished.emit(process.returncode == 0, output)
        except Exception as e:
            error_msg = QCoreApplication.translate("DfuCheckWorker", "An error occurred: {}").format(e)
            self.finished.emit(False, error_msg)

class DfuRebootWorker(QObject):
    finished = Signal(bool, str)
    def __init__(self, cli_path):
        super().__init__()
        self.cli_path = cli_path
    def run(self):
        try:
            command = f'"{self.cli_path}" -c port=usb1 --start'
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            cli_directory = os.path.dirname(self.cli_path)
            process = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', startupinfo=startupinfo, cwd=cli_directory, timeout=5, shell=True)
            output = process.stdout + "\n" + process.stderr
            self.finished.emit(process.returncode == 0, output)
        except Exception as e:
            error_msg = QCoreApplication.translate("DfuRebootWorker", "An error occurred: {}").format(e)
            self.finished.emit(False, error_msg)

class FlashWorker(QObject):
    progress_updated = Signal(str)
    finished = Signal(int)
    def __init__(self, firmware_path, cli_path):
        super().__init__()
        self.firmware_path = firmware_path
        self.cli_path = cli_path
        self._is_running = True
        self.process = None
    def run(self):
        temp_file_path = None
        path_to_flash = self.firmware_path
        try:
            filename = os.path.basename(self.firmware_path)
            if ' ' in filename or '(' in filename or ')' in filename:
                self.progress_updated.emit(QCoreApplication.translate("FlashWorker", "Complex filename detected. Creating a temporary safe copy..."))
                _, extension = os.path.splitext(filename)
                fd, temp_file_path = tempfile.mkstemp(suffix=extension)
                os.close(fd)
                shutil.copy2(self.firmware_path, temp_file_path)
                path_to_flash = temp_file_path
                log_msg = QCoreApplication.translate("FlashWorker", "Secure temporary file created at: {}").format(path_to_flash)
                self.progress_updated.emit(log_msg)
            command = f'"{self.cli_path}" -c port=usb1 -d "{path_to_flash}" 0x08000000 -v --start'
            self.progress_updated.emit(QCoreApplication.translate("FlashWorker", "Starting flash process..."))
            self.progress_updated.emit(f"Command: {command}") 
            self.progress_updated.emit("-" * 50)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            cli_directory = os.path.dirname(self.cli_path)
            self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', startupinfo=startupinfo, cwd=cli_directory, shell=True)
            line_buffer = ''
            while self._is_running and self.process.poll() is None:
                char = self.process.stdout.read(1)
                if not char: break
                if char in ('\n', '\r'):
                    if line_buffer.strip():
                        if "in Progress" in line_buffer and "%" in line_buffer:
                            self.progress_updated.emit(f"PROGRESS:::{line_buffer.strip()}")
                        else:
                            self.progress_updated.emit(line_buffer.strip())
                    line_buffer = ''
                else:
                    line_buffer += char
            if self.process:
                retcode = self.process.wait()
                if not self._is_running: retcode = -1
                self.finished.emit(retcode)
            else:
                self.finished.emit(-1)
        except Exception as e:
            error_msg = QCoreApplication.translate("FlashWorker", "An unexpected error occurred: {}").format(e)
            self.progress_updated.emit(error_msg)
            self.finished.emit(-1)
        finally:
            if temp_file_path and os.path.exists(temp_file_path):
                self.progress_updated.emit(QCoreApplication.translate("FlashWorker", "Cleaning up temporary file..."))
                os.remove(temp_file_path)
    def stop(self):
        self._is_running = False
        if self.process: self.process.terminate()

class FirmwareTab(BaseTab):
    def __init__(self, main_window, parent=None):
        super().__init__(main_window, parent)
        self.flash_thread, self.flash_worker = None, None
        self.download_thread, self.download_worker = None, None
        self.dfu_check_thread, self.dfu_check_worker = None, None
        self.dfu_reboot_thread, self.dfu_reboot_worker = None, None
        self.stm32_cli_path = find_stm32_programmer_cli()
        self.selected_firmware_path = None
        self._is_updating_progress = False
        self._is_dfu_found = False
        self._setup_ui()
        self.retranslate_ui()

    def changeEvent(self, event):
        if event.type() == QEvent.Type.LanguageChange:
            self.retranslate_ui()
        super().changeEvent(event)

    def _setup_ui(self):
        main_layout = QHBoxLayout(self)
        left_panel_widget = QWidget()
        left_panel_layout = QVBoxLayout(left_panel_widget)
        left_panel_layout.setContentsMargins(0, 0, 10, 0)
        self.info_group = QGroupBox()
        info_layout = QFormLayout(self.info_group)
        self.label_fw = QLabel()
        self.fw_version_label = QLabel("-")
        self.label_hw = QLabel()
        self.hw_version_label = QLabel("-")
        self.label_serial = QLabel()
        self.serial_number_label = QLabel("-")
        info_value_style = "color: #2ECC71;"
        self.fw_version_label.setStyleSheet(info_value_style)
        self.hw_version_label.setStyleSheet(info_value_style)
        self.serial_number_label.setStyleSheet(info_value_style)
        info_layout.addRow(self.label_fw, self.fw_version_label)
        info_layout.addRow(self.label_hw, self.hw_version_label)
        info_layout.addRow(self.label_serial, self.serial_number_label)
        left_panel_layout.addWidget(self.info_group)
        self.dfu_actions_group = QGroupBox()
        dfu_actions_layout = QVBoxLayout(self.dfu_actions_group)
        step1_2_layout = QHBoxLayout()
        self.enter_dfu_btn = QPushButton(); self.enter_dfu_btn.clicked.connect(self.enter_dfu_mode)
        self.check_dfu_btn = QPushButton(); self.check_dfu_btn.clicked.connect(self.check_for_dfu_device)
        step1_2_layout.addWidget(self.enter_dfu_btn); step1_2_layout.addWidget(self.check_dfu_btn)
        dfu_actions_layout.addLayout(step1_2_layout)
        self.dfu_status_label = QLabel(); self.dfu_status_label.setStyleSheet("font-style: italic;")
        self.reboot_dfu_btn = QPushButton()
        self.reboot_dfu_btn.clicked.connect(self.reboot_from_dfu); self.reboot_dfu_btn.setEnabled(False)
        dfu_actions_layout.addWidget(self.dfu_status_label); dfu_actions_layout.addWidget(self.reboot_dfu_btn)
        left_panel_layout.addWidget(self.dfu_actions_group)
        self.prereq_group = QGroupBox()
        prereq_layout = QVBoxLayout(self.prereq_group)
        self.stm32_prog_status_label = QLabel()
        self.stm32_prog_status_label.setTextFormat(Qt.TextFormat.RichText)
        if self.stm32_cli_path:
            self.stm32_prog_status_label.setToolTip(f"Location: {self.stm32_cli_path}")
            self.check_dfu_btn.setEnabled(True)
        else:
            self.check_dfu_btn.setEnabled(False)
        prereq_layout.addWidget(self.stm32_prog_status_label)
        left_panel_layout.addWidget(self.prereq_group)
        self.download_group = QGroupBox()
        download_layout = QVBoxLayout(self.download_group)
        self.download_help = QLabel(); self.download_help.setWordWrap(True)
        self.download_help.setStyleSheet(f"color: {AppColors.INFO};")
        download_layout.addWidget(self.download_help)
        version_row = QHBoxLayout()
        self.label_fw_version_pick = QLabel()
        self.fw_version_combo = QComboBox()
        for tag in AVAILABLE_VERSIONS: self.fw_version_combo.addItem(tag)
        self.fw_version_combo.setCurrentText(RECOMMENDED_VERSION)
        version_row.addWidget(self.label_fw_version_pick); version_row.addWidget(self.fw_version_combo)
        self.download_btn = QPushButton(); self.download_btn.clicked.connect(self.start_firmware_download)
        self.download_btn.setEnabled(False)
        version_row.addWidget(self.download_btn)
        download_layout.addLayout(version_row)
        self.detected_board_label = QLabel(); self.detected_board_label.setWordWrap(True)
        download_layout.addWidget(self.detected_board_label)
        self.download_progress = QProgressBar(); self.download_progress.setRange(0, 100)
        download_layout.addWidget(self.download_progress)
        left_panel_layout.addWidget(self.download_group)
        self.controls_group = QGroupBox()
        controls_layout = QVBoxLayout(self.controls_group)
        action_buttons_layout = QHBoxLayout()
        self.select_fw_btn = QPushButton(); self.select_fw_btn.clicked.connect(self.select_firmware_file)
        self.install_fw_btn = QPushButton(); self.install_fw_btn.clicked.connect(self.start_firmware_flash); self.install_fw_btn.setEnabled(False)
        self.cancel_flash_btn = QPushButton(); self.cancel_flash_btn.clicked.connect(self.cancel_flash); self.cancel_flash_btn.setVisible(False)
        action_buttons_layout.addWidget(self.select_fw_btn); action_buttons_layout.addWidget(self.install_fw_btn); action_buttons_layout.addWidget(self.cancel_flash_btn)
        controls_layout.addLayout(action_buttons_layout)
        self.selected_file_label = QLabel(); self.selected_file_label.setStyleSheet("font-style: italic;")
        controls_layout.addWidget(self.selected_file_label)
        left_panel_layout.addWidget(self.controls_group)
        left_panel_layout.addStretch()
        self.log_group = QGroupBox()
        log_layout = QVBoxLayout(self.log_group)
        self.flash_status_display = QTextEdit()
        self.flash_status_display.setReadOnly(True)
        self.flash_status_display.setFont(QFont("Consolas", 9))
        log_layout.addWidget(self.flash_status_display)
        main_layout.addWidget(left_panel_widget, 2)
        main_layout.addWidget(self.log_group, 3)
        self.on_connection_status_changed(False)
        self.select_fw_btn.setEnabled(False)

    def retranslate_ui(self):
        self.info_group.setTitle(self.tr("Device Information"))
        self.label_fw.setText(self.tr("Firmware Version:"))
        self.label_hw.setText(self.tr("Hardware Version:"))
        self.label_serial.setText(self.tr("Serial Number:"))
        self.download_group.setTitle(self.tr("Automatic Firmware Download"))
        self.download_help.setText(self.tr(
            "Downloads the official firmware built for this exact board from the ODrive "
            "releases. Do this while still connected normally: in DFU mode the board no longer "
            "reports its hardware version, so the right file cannot be identified.\n\n"
            "The OpenFFBoard ODrive guide recommends fw-v0.5.6, the last release for this "
            "hardware generation."))
        self.label_fw_version_pick.setText(self.tr("Version:"))
        self.download_btn.setText(self.tr("Download"))
        self.download_btn.setToolTip(self.tr("Fetches the release file matching this board's hardware version."))
        self._update_detected_board()
        self.dfu_actions_group.setTitle(self.tr("Update Steps"))
        self.enter_dfu_btn.setText(self.tr("1. DFU Mode"))
        self.check_dfu_btn.setText(self.tr("2. Check DFU"))
        self.dfu_status_label.setText(self.tr("Check status: Waiting..."))
        self.reboot_dfu_btn.setText(self.tr("Exit DFU Mode (Reboot)"))
        self.reboot_dfu_btn.setToolTip(self.tr("Sends a command for the ODrive to exit DFU mode and start the main firmware."))
        self.prereq_group.setTitle(self.tr("Prerequisites (Windows)"))
        if self.stm32_cli_path:
            self.stm32_prog_status_label.setText(self.tr("✅ <b>STM32CubeProgrammer</b> found."))
        else:
            self.stm32_prog_status_label.setText(self.tr("❌ <b>STM32CubeProgrammer not found.</b>"))
        self.controls_group.setTitle(self.tr("3. Firmware Flashing"))
        self.select_fw_btn.setText(self.tr("Select File..."))
        self.install_fw_btn.setText(self.tr("Flash Firmware"))
        self.cancel_flash_btn.setText(self.tr("Cancel Flash"))
        self.selected_file_label.setText(self.tr("No file selected."))
        self.log_group.setTitle(self.tr("Flashing Log"))
        self.flash_status_display.setPlaceholderText(self.tr("The flashing status will appear here."))

    def update_flash_status(self, message):
        if message.startswith("PROGRESS:::"):
            progress_line = message.split(":::", 1)[1]
            cursor = self.flash_status_display.textCursor()
            if self._is_updating_progress:
                cursor.movePosition(QTextCursor.End); cursor.select(QTextCursor.BlockUnderCursor)
                cursor.removeSelectedText(); cursor.insertBlock()
            self.flash_status_display.insertPlainText(progress_line)
            self._is_updating_progress = True
        else:
            self._is_updating_progress = False; self.flash_status_display.append(message)
        self.flash_status_display.verticalScrollBar().setValue(self.flash_status_display.verticalScrollBar().maximum())

    def on_flash_finished(self, return_code):
        self._is_updating_progress = False
        self.flash_status_display.append("-" * 50)
        if return_code == 0:
            self.flash_status_display.append(self.tr("PROCESS COMPLETED SUCCESSFULLY!"))
            QMessageBox.information(self, self.tr("Success"), self.tr("Firmware flashed successfully!"))
        elif return_code == -1: self.flash_status_display.append(self.tr("PROCESS CANCELED."))
        else:
            fail_msg = self.tr("PROCESS FAILED (code: {0}).").format(return_code)
            self.flash_status_display.append(fail_msg)
            QMessageBox.critical(self, self.tr("Failure"), self.tr("The firmware flash failed."))
        if self.flash_thread: self.flash_thread.quit(); self.flash_thread.wait()
        self.flash_thread = None; self.flash_worker = None
        self.install_fw_btn.setVisible(True); self.cancel_flash_btn.setVisible(False)
        self.on_connection_status_changed(self.main_window.is_connected)
        self.set_dfu_buttons_enabled(True); self.reset_firmware_selection_state()
        self.select_fw_btn.setEnabled(False); self.reboot_dfu_btn.setEnabled(False)
        self.dfu_status_label.setText(self.tr("Check status: Waiting...")); self.dfu_status_label.setStyleSheet("font-style: italic;")
        
    def reset_device_info_labels(self):
        self.fw_version_label.setText("-"); self.hw_version_label.setText("-"); self.serial_number_label.setText("-")

    def reset_firmware_selection_state(self):
        self.selected_firmware_path = None; self.install_fw_btn.setEnabled(False)
        self.selected_file_label.setText(self.tr("No file selected.")); self.selected_file_label.setStyleSheet("font-style: italic;")

    def on_dfu_check_finished(self, success, output):
        self.set_dfu_buttons_enabled(True); self.reset_firmware_selection_state()
        self._is_dfu_found = success
        if success:
            self.dfu_status_label.setText(self.tr("Check status: DFU device found!")); self.dfu_status_label.setStyleSheet(f"font-style: normal; color: {AppColors.SUCCESS};")
            self.select_fw_btn.setEnabled(True); self.reboot_dfu_btn.setEnabled(True)
            dfu_sn = "N/A"
            for line in output.splitlines():
                if line.strip().startswith("SN"):
                    parts = line.split(':');
                    if len(parts) > 1: dfu_sn = parts[1].strip()
                    break
            self.fw_version_label.setText(self.tr("DFU Mode")); self.hw_version_label.setText(self.tr("DFU Mode")); self.serial_number_label.setText(dfu_sn)
        else:
            self.dfu_status_label.setText(self.tr("Check status: DFU device not found.")); self.dfu_status_label.setStyleSheet(f"font-style: normal; color: {AppColors.ERROR};")
            self.select_fw_btn.setEnabled(False); self.reboot_dfu_btn.setEnabled(False); self.reset_device_info_labels()

    def select_firmware_file(self):
        file_name, _ = QFileDialog.getOpenFileName(self, self.tr("Select Firmware File"), "", self.tr("Firmware Files (*.elf *.hex *.bin);;All Files (*)"))
        if file_name:
            if file_name.lower().endswith(('.elf', '.hex', '.bin')):
                self.selected_firmware_path = file_name
                label_text = self.tr("<b>Ready to flash:</b> {0}").format(os.path.basename(file_name))
                self.selected_file_label.setText(label_text); self.selected_file_label.setStyleSheet(f"color: {AppColors.SUCCESS}; font-style: normal;")
                self.install_fw_btn.setEnabled(True)
            else:
                QMessageBox.warning(self, self.tr("Invalid File"), self.tr("Please select a valid firmware file (.elf, .hex, or .bin).")); self.reset_firmware_selection_state()

    def start_firmware_flash(self):
        if not self.selected_firmware_path or (self.flash_thread and self.flash_thread.isRunning()): return
        self.set_dfu_buttons_enabled(False); self.enter_dfu_btn.setEnabled(False); self.select_fw_btn.setEnabled(False)
        self.install_fw_btn.setVisible(False); self.cancel_flash_btn.setVisible(True); self.cancel_flash_btn.setEnabled(True); self.cancel_flash_btn.setText(self.tr("Cancel Flash"))
        log_msg = self.tr("Selected file: {0}").format(os.path.basename(self.selected_firmware_path))
        self.flash_status_display.clear(); self.flash_status_display.append(log_msg)
        self.flash_thread = QThread(); self.flash_worker = FlashWorker(self.selected_firmware_path, self.stm32_cli_path)
        self.flash_worker.moveToThread(self.flash_thread); self.flash_thread.started.connect(self.flash_worker.run)
        self.flash_worker.finished.connect(self.on_flash_finished); self.flash_worker.progress_updated.connect(self.update_flash_status)
        self.flash_thread.start()

    def open_download_page(self): QDesktopServices.openUrl(QUrl("https://www.st.com/en/development-tools/stm32cubeprog.html"))
    
    def check_for_dfu_device(self):
        self.dfu_status_label.setText(self.tr("Check status: Searching...")); self.dfu_status_label.setStyleSheet("font-style: italic; color: white;")
        self.set_dfu_buttons_enabled(False)
        self.dfu_check_thread = QThread(); self.dfu_check_worker = DfuCheckWorker(self.stm32_cli_path)
        self.dfu_check_worker.moveToThread(self.dfu_check_thread); self.dfu_check_thread.started.connect(self.dfu_check_worker.run)
        self.dfu_check_worker.finished.connect(self.on_dfu_check_finished); self.dfu_check_worker.finished.connect(self.dfu_check_thread.quit)
        self.dfu_check_worker.finished.connect(self.dfu_check_worker.deleteLater); self.dfu_check_thread.finished.connect(self.dfu_check_thread.deleteLater)
        self.dfu_check_thread.start()

    def set_dfu_buttons_enabled(self, enabled):
        self.check_dfu_btn.setEnabled(enabled)
        self.reboot_dfu_btn.setEnabled(enabled and self._is_dfu_found); self.select_fw_btn.setEnabled(enabled and self._is_dfu_found)

    def reboot_from_dfu(self):
        self.dfu_status_label.setText(self.tr("Check status: Rebooting...")); self.set_dfu_buttons_enabled(False)
        self.dfu_reboot_thread = QThread(); self.dfu_reboot_worker = DfuRebootWorker(self.stm32_cli_path)
        self.dfu_reboot_worker.moveToThread(self.dfu_reboot_thread); self.dfu_reboot_thread.started.connect(self.dfu_reboot_worker.run)
        self.dfu_reboot_worker.finished.connect(self.on_dfu_reboot_finished); self.dfu_reboot_worker.finished.connect(self.dfu_reboot_thread.quit)
        self.dfu_reboot_worker.finished.connect(self.dfu_reboot_worker.deleteLater); self.dfu_reboot_thread.finished.connect(self.dfu_reboot_thread.deleteLater)
        self.dfu_reboot_thread.start()

    def on_dfu_reboot_finished(self, success, output):
        self.set_dfu_buttons_enabled(True); self.reboot_dfu_btn.setEnabled(False); self.select_fw_btn.setEnabled(False)
        self.dfu_status_label.setText(self.tr("Check status: Waiting...")); self.dfu_status_label.setStyleSheet("font-style: italic;")
        self.reset_device_info_labels()
        if success: QMessageBox.information(self, self.tr("Success"), self.tr("Reboot command sent.\nThe device will now exit DFU mode."))
        else: QMessageBox.critical(self, self.tr("Failure"), self.tr("Could not reboot the device."))

    def cancel_flash(self):
        if self.flash_worker: self.flash_worker.stop(); self.cancel_flash_btn.setEnabled(False); self.cancel_flash_btn.setText(self.tr("Canceling..."))
    
    def on_connection_status_changed(self, is_connected):
        self.enter_dfu_btn.setEnabled(is_connected)
        if not is_connected: self.reset_device_info_labels()

    def populate_fields(self): self.read_firmware_info(); self._update_detected_board()
    
    def read_firmware_info(self):
        odrv = self.get_odrv()
        if not odrv: return
        try:
            fw_ver = f"{odrv.fw_version_major}.{odrv.fw_version_minor}.{odrv.fw_version_revision}"
            hw_ver = f"{odrv.hw_version_major}.{odrv.hw_version_minor}"
            serial_hex = format(odrv.serial_number, 'X').upper()
            self.fw_version_label.setText(fw_ver); self.hw_version_label.setText(hw_ver); self.serial_number_label.setText(serial_hex)
        except Exception: pass
    
    def enter_dfu_mode(self):
        odrv = self.get_odrv();
        if not odrv: return
        reply = QMessageBox.warning(self, self.tr("Confirm DFU Mode"), self.tr("This will reboot the ODrive into DFU mode.\nThe application will lose connection."), QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            try: odrv.enter_dfu_mode()
            except Exception: pass
            finally: self.main_window.show_status_message(self.tr("DFU command sent."), AppColors.WARNING, 5000)


    # --------------------------------------------- automatic firmware download ---

    def _update_detected_board(self):
        """
        Shows which firmware file this board needs. The hardware version is only
        readable over the normal protocol, so this has to happen before DFU, which is
        also why downloading is offered as the step before entering DFU.
        """
        if not self.main_window.is_connected or not self.main_window.odrv_proxy:
            self.detected_board_label.setText(self.tr("Connect to the ODrive to detect which firmware it needs."))
            self.detected_board_label.setStyleSheet("font-style: italic;")
            self.download_btn.setEnabled(False)
            return
        try:
            odrv = self.main_window.odrv_proxy.odrv
            major, minor = odrv.hw_version_major, odrv.hw_version_minor
            variant = getattr(odrv, 'hw_version_variant', None)
        except Exception:
            self.detected_board_label.setText(self.tr("Could not read the hardware version."))
            self.detected_board_label.setStyleSheet(f"color: {AppColors.ERROR};")
            self.download_btn.setEnabled(False)
            return

        asset = expected_asset_name(major, minor, variant)
        if not asset:
            # Never guess the voltage variant: the wrong one sets the wrong voltage
            # limits on the board.
            self.detected_board_label.setText(self.tr(
                "This board did not report its voltage variant, so the right firmware cannot be "
                "identified. Select the file manually."))
            self.detected_board_label.setStyleSheet(f"color: {AppColors.WARNING};")
            self.download_btn.setEnabled(False)
            return

        self.detected_board_label.setText(self.tr("Detected ODrive v{0}.{1} {2}V, needs <b>{3}</b>").format(
            major, minor, variant, asset))
        self.detected_board_label.setStyleSheet(f"color: {AppColors.SUCCESS};")
        self.detected_board_label.setTextFormat(Qt.TextFormat.RichText)
        self.download_btn.setEnabled(self.download_thread is None)

    def start_firmware_download(self):
        """Fetches the release asset matching this board, before DFU is entered."""
        if self.download_thread is not None:
            return
        odrv = self.get_odrv()
        if not odrv:
            return
        try:
            asset = expected_asset_name(odrv.hw_version_major, odrv.hw_version_minor,
                                        getattr(odrv, 'hw_version_variant', None))
        except Exception:
            asset = None
        if not asset:
            QMessageBox.warning(self, self.tr("Unknown Board"), self.tr(
                "The hardware version could not be read, so the matching firmware cannot be chosen."))
            return

        self.download_btn.setEnabled(False)
        self.download_progress.setValue(0)
        self.download_thread = QThread()
        self.download_worker = FirmwareDownloadWorker(self.fw_version_combo.currentText(), asset)
        self.download_worker.moveToThread(self.download_thread)
        self.download_thread.started.connect(self.download_worker.run)
        self.download_worker.progress.connect(self._on_download_progress)
        self.download_worker.result.connect(self._on_download_result)
        self.download_worker.finished.connect(self.download_thread.quit)
        self.download_worker.finished.connect(self.download_worker.deleteLater)
        self.download_thread.finished.connect(self.download_thread.deleteLater)
        self.download_thread.finished.connect(self._on_download_thread_finished)
        self.download_thread.start()

    def _on_download_progress(self, message, percent):
        self.download_progress.setValue(percent)
        self.flash_status_display.append(message)

    def _on_download_result(self, success, message, path):
        if not success:
            QMessageBox.warning(self, self.tr("Download Failed"), message)
            return
        # Hand the file to the existing flash flow, exactly as picking it by hand does.
        self.selected_firmware_path = path
        self.selected_file_label.setText(self.tr("<b>Ready to flash:</b> {0}").format(os.path.basename(path)))
        self.selected_file_label.setStyleSheet(f"color: {AppColors.SUCCESS}; font-style: normal;")
        self.install_fw_btn.setEnabled(self._is_dfu_found)
        self.flash_status_display.append(message)
        QMessageBox.information(self, self.tr("Download Complete"), self.tr(
            "{0}\n\nNow put the ODrive in DFU mode, check for it, and install.").format(message))

    def _on_download_thread_finished(self):
        self.download_thread, self.download_worker = None, None
        self._update_detected_board()

    def show_missing_prereq_error(self):
        msg_box = QMessageBox(self)
        msg_box.setTextFormat(Qt.TextFormat.RichText)
        msg_box.setIcon(QMessageBox.Icon.Critical)
        msg_box.setWindowTitle(self.tr("Prerequisite Missing"))
        msg_box.setText(self.tr("<b>STM32CubeProgrammer</b> was not found on your system."))
        msg_box.setInformativeText(self.tr("It is required to flash the firmware. Would you like to go to the official download page now?"))
        download_button = msg_box.addButton(self.tr("Go to Download Page"), QMessageBox.ButtonRole.ActionRole); msg_box.addButton(QMessageBox.StandardButton.Cancel)
        msg_box.exec();
        if msg_box.clickedButton() == download_button: self.open_download_page()

    def apply_config(self):
        pass
