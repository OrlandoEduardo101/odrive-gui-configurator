# tabs/firmware_download.py
"""
Downloads official ODrive firmware binaries matching the connected board.

The release assets are named after the hardware they are built for, as in
ODriveFirmware_v3.6-56V.elf, and the board reports those same three numbers. That
makes the correct file derivable rather than something the user has to recognise.

Getting this wrong matters: the voltage variant sets the board's voltage limits, so
firmware built for a 24V board does not belong on a 56V one. The variant is therefore
required, never guessed, and the download is refused when it cannot be read.

Only the standard library is used for HTTP, so packaging gains no new dependency.
"""
import json
import os
import ssl
import tempfile
import urllib.error
import urllib.request

from PySide6.QtCore import QObject, Signal, QCoreApplication

GITHUB_RELEASE_API = "https://api.github.com/repos/odriverobotics/ODrive/releases/tags/{tag}"

# Verified present as release tags. 0.5.6 is the last release for this hardware
# generation, and the version the OpenFFBoard ODrive guide recommends.
AVAILABLE_VERSIONS = ["fw-v0.5.6", "fw-v0.5.5", "fw-v0.5.4",
                     "fw-v0.5.3", "fw-v0.5.2", "fw-v0.5.1"]
RECOMMENDED_VERSION = "fw-v0.5.6"

DOWNLOAD_TIMEOUT_S = 30
CHUNK_SIZE = 64 * 1024


def expected_asset_name(hw_major, hw_minor, hw_variant):
    """
    Builds the release asset name for a board, or returns None when the hardware
    version is incomplete. A missing variant is not defaulted: picking a voltage on
    the user's behalf is exactly the mistake that damages a board.
    """
    if not hw_variant or not hw_major:
        return None
    return f"ODriveFirmware_v{int(hw_major)}.{int(hw_minor)}-{int(hw_variant)}V.elf"


def download_directory():
    """Cache directory for downloaded firmware, created on demand."""
    path = os.path.join(tempfile.gettempdir(), "odrive_gui_firmware")
    os.makedirs(path, exist_ok=True)
    return path


class FirmwareDownloadWorker(QObject):
    """Fetches one firmware asset from the ODrive GitHub releases."""

    progress = Signal(str, int)          # status line, percent
    result = Signal(bool, str, object)   # success, message, downloaded path
    finished = Signal()

    def __init__(self, tag, asset_name):
        super().__init__()
        self.tag = tag
        self.asset_name = asset_name
        self._is_running = True

    def stop(self):
        self._is_running = False

    @staticmethod
    def _ssl_context():
        """
        Builds a verifying SSL context, preferring certifi's bundle.

        Frozen builds and some Python installs ship without a usable CA store, which
        makes the system default fail. certifi carries its own bundle and sidesteps
        that. Verification is never turned off: this downloads a binary that gets
        flashed onto a motor controller, so an unverified transport is not acceptable.
        """
        try:
            import certifi
            return ssl.create_default_context(cafile=certifi.where())
        except Exception:
            return ssl.create_default_context()

    def _open(self, url):
        # GitHub rejects requests without a User-Agent.
        request = urllib.request.Request(url, headers={"User-Agent": "odrive-gui-configurator"})
        return urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S,
                                      context=self._ssl_context())

    def _find_asset(self):
        """Returns (download_url, size) for the wanted asset in this release."""
        with self._open(GITHUB_RELEASE_API.format(tag=self.tag)) as response:
            release = json.loads(response.read().decode("utf-8"))
        for asset in release.get("assets", []):
            if asset.get("name") == self.asset_name:
                return asset.get("browser_download_url"), asset.get("size", 0)
        return None, 0

    def run(self):
        destination = os.path.join(download_directory(), f"{self.tag}_{self.asset_name}")
        try:
            self.progress.emit(QCoreApplication.translate(
                "FirmwareDownloadWorker", "Looking up {0}...").format(self.tag), 0)
            url, expected_size = self._find_asset()
            if not url:
                self.result.emit(False, QCoreApplication.translate(
                    "FirmwareDownloadWorker",
                    "Release {0} has no file named {1}.\n\nThis firmware version may not have "
                    "been built for your board revision.").format(self.tag, self.asset_name), None)
                return

            # A previous download of the same release and board can be reused, but only
            # when its size matches: a truncated file would otherwise be flashed.
            if os.path.exists(destination) and expected_size and os.path.getsize(destination) == expected_size:
                self.progress.emit(QCoreApplication.translate(
                    "FirmwareDownloadWorker", "Already downloaded."), 100)
                self.result.emit(True, QCoreApplication.translate(
                    "FirmwareDownloadWorker", "Using the copy already downloaded:\n{0}").format(destination),
                    destination)
                return

            partial = destination + ".part"
            written = 0
            with self._open(url) as response, open(partial, "wb") as handle:
                while True:
                    if not self._is_running:
                        handle.close()
                        os.remove(partial)
                        self.result.emit(False, QCoreApplication.translate(
                            "FirmwareDownloadWorker", "Download cancelled."), None)
                        return
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    handle.write(chunk)
                    written += len(chunk)
                    percent = int(100 * written / expected_size) if expected_size else 0
                    self.progress.emit(QCoreApplication.translate(
                        "FirmwareDownloadWorker", "Downloading {0}: {1:.1f} MB").format(
                            self.asset_name, written / 1048576.0), min(percent, 99))

            if expected_size and written != expected_size:
                os.remove(partial)
                self.result.emit(False, QCoreApplication.translate(
                    "FirmwareDownloadWorker",
                    "The download is incomplete ({0} of {1} bytes). Nothing was saved.")
                    .format(written, expected_size), None)
                return

            # The right size is not the same as the right content: an error page or a
            # corrupted transfer would still get flashed. ODrive firmware is an ARM ELF,
            # so check that before handing the file to the programmer.
            with open(partial, "rb") as handle:
                if handle.read(4) != b"\x7fELF":
                    os.remove(partial)
                    self.result.emit(False, QCoreApplication.translate(
                        "FirmwareDownloadWorker",
                        "The downloaded file is not a firmware image. Nothing was saved."), None)
                    return

            # Only becomes the real filename once it is known to be complete.
            os.replace(partial, destination)
            self.progress.emit(QCoreApplication.translate(
                "FirmwareDownloadWorker", "Download complete."), 100)
            self.result.emit(True, QCoreApplication.translate(
                "FirmwareDownloadWorker", "Downloaded {0}\n\nSaved to:\n{1}").format(
                    self.asset_name, destination), destination)

        except urllib.error.HTTPError as e:
            self.result.emit(False, QCoreApplication.translate(
                "FirmwareDownloadWorker", "GitHub returned an error: {0}").format(e), None)
        except urllib.error.URLError as e:
            self.result.emit(False, QCoreApplication.translate(
                "FirmwareDownloadWorker",
                "Could not reach GitHub: {0}\n\nCheck the internet connection.").format(e.reason), None)
        except Exception as e:
            self.result.emit(False, QCoreApplication.translate(
                "FirmwareDownloadWorker", "The download failed: {0}").format(e), None)
        finally:
            self.finished.emit()
