# tabs/tuning_workers.py
"""
Workers for the optional tuning routines. These run in their own thread and are kept
separate from workers.py so that the original calibration flow stays untouched.

OffsetAlignmentWorker refines the encoder's electrical offset. ODrive's own offset
calibration pushes the rotor against cogging and stiction, which on a high-pole-count
direct drive motor can land several electrical degrees away from the true alignment.
Every degree of error splits the commanded current away from the torque-producing
axis: useful torque scales with cos(error) while the wasted portion becomes heat.

The routine therefore sweeps candidate offsets and, at each one, spins the motor at a
fixed no-load speed and records how much current that costs. The offset needing the
least current is the aligned one.
"""
import math
import time

from PySide6.QtCore import QObject, Signal, QCoreApplication

import fibre
from odrive.enums import AXIS_STATE_IDLE, AXIS_STATE_CLOSED_LOOP_CONTROL
from odrive.enums import AXIS_STATE_ENCODER_OFFSET_CALIBRATION, AXIS_STATE_ENCODER_INDEX_SEARCH
from odrive.enums import CONTROL_MODE_VELOCITY_CONTROL, INPUT_MODE_VEL_RAMP
from odrive.enums import CONTROL_MODE_TORQUE_CONTROL, INPUT_MODE_PASSTHROUGH


class CalibrationQualityWorker(QObject):
    """
    Measures how repeatable ODrive's own encoder offset calibration is, and whether
    lengthening its scan makes it more repeatable.

    ODrive already does the right thing in principle: run_offset_calibration drives the
    motor open loop forwards and then backwards, averaging the encoder reading over both
    passes, which cancels a symmetric cogging bias. What it cannot do is know how far to
    scan. The default calib_scan_distance is 16*pi electrical radians, and dividing that
    by the pole pair count gives the mechanical distance actually covered:

        16*pi / (2*pi * pole_pairs) = 8 / pole_pairs mechanical revolutions

    On a 15 pole pair hoverboard motor that is little over half a turn. Cogging repeats
    with mechanical position, so half a turn samples a biased subset of it and the
    average keeps whatever bias that subset carries. Scanning a whole number of
    mechanical revolutions is what lets it cancel.

    Rather than assert that, this runs the calibration several times as configured, then
    several times over whole mechanical revolutions, and reports the spread of each. The
    spread is the evidence: a calibration that lands in the same place every time is one
    worth trusting.
    """
    progress = Signal(str, int)
    result = Signal(bool, str, object)   # success, report, suggested settings or None
    finished = Signal()

    CALIBRATION_TIMEOUT_S = 120

    def __init__(self, odrv, runs, mechanical_revolutions, calibration_current,
                 keep_scan_distance=True):
        super().__init__()
        self.odrv = odrv
        self.runs = runs
        self.mechanical_revolutions = mechanical_revolutions
        self.calibration_current = calibration_current
        self.keep_scan_distance = keep_scan_distance
        self._is_running = True
        self._saved = {}

    def stop(self):
        self._is_running = False

    # ---------------------------------------------------------------- helpers ---

    def _wait_for_state(self, state):
        """Requests a state and waits for the axis to finish it. True when it completed."""
        axis = self.odrv.axis0
        axis.error = 0
        axis.motor.error = 0
        axis.encoder.error = 0
        axis.requested_state = state
        time.sleep(0.3)
        deadline = time.time() + self.CALIBRATION_TIMEOUT_S
        while axis.current_state == state:
            if not self._is_running:
                axis.requested_state = AXIS_STATE_IDLE
                return False
            if time.time() > deadline:
                axis.requested_state = AXIS_STATE_IDLE
                raise RuntimeError(QCoreApplication.translate(
                    "CalibrationQualityWorker", "The calibration did not finish within {0} seconds.")
                    .format(self.CALIBRATION_TIMEOUT_S))
            time.sleep(0.1)
        if axis.error != 0:
            raise RuntimeError(QCoreApplication.translate(
                "CalibrationQualityWorker", "The axis reported an error during calibration: {0}")
                .format(hex(axis.error)))
        return True

    def _calibrate_once(self):
        """Runs one encoder offset calibration and returns the phase offset it produced."""
        axis = self.odrv.axis0
        if axis.encoder.config.use_index:
            if not self._wait_for_state(AXIS_STATE_ENCODER_INDEX_SEARCH):
                return None
        if not self._wait_for_state(AXIS_STATE_ENCODER_OFFSET_CALIBRATION):
            return None
        time.sleep(0.2)
        return int(getattr(axis.encoder.config, self._offset_attr))

    def _series(self, label, base_pct):
        """Runs the calibration self.runs times, returning the offsets it produced."""
        offsets = []
        for i in range(self.runs):
            if not self._is_running:
                return None
            self.progress.emit(QCoreApplication.translate(
                "CalibrationQualityWorker", "{0}: calibration {1} of {2}...")
                .format(label, i + 1, self.runs), base_pct + int(45 * i / self.runs))
            value = self._calibrate_once()
            if value is None:
                return None
            offsets.append(value)
        return offsets

    @staticmethod
    def _spread(values):
        return max(values) - min(values) if values else 0

    @staticmethod
    def _reduce(values, period):
        """
        Brings raw offsets into one electrical revolution.

        run_offset_calibration averages shadow_count_, which is the unwrapped encoder
        count, so a longer scan turns further and lands the average in an entirely
        different range. The firmware only ever uses the offset modulo one electrical
        revolution, so that is the only part worth comparing: the raw values from two
        different scan lengths are not comparable at all.
        """
        return [v % period for v in values]

    @staticmethod
    def _circular_spread(values, period):
        """Smallest arc containing every value, so a cluster straddling the wrap is not
        reported as if it spanned the whole revolution."""
        if len(values) < 2:
            return 0.0
        ordered = sorted(values)
        gaps = [(ordered[(i + 1) % len(ordered)] - ordered[i]) % period
                for i in range(len(ordered))]
        return period - max(gaps)

    @staticmethod
    def _circular_mean(values, period):
        angles = [2.0 * math.pi * v / period for v in values]
        mean = math.atan2(sum(math.sin(a) for a in angles) / len(angles),
                          sum(math.cos(a) for a in angles) / len(angles))
        return (mean * period / (2.0 * math.pi)) % period

    # -------------------------------------------------------------------- run ---

    def run(self):
        axis = self.odrv.axis0
        # Firmware 0.5.6 names it phase_offset; earlier 0.5.x called it offset.
        self._offset_attr = None
        for name in ('phase_offset', 'offset'):
            if hasattr(axis.encoder.config, name):
                self._offset_attr = name
                break
        if self._offset_attr is None:
            self.result.emit(False, QCoreApplication.translate(
                "CalibrationQualityWorker",
                "Could not find the encoder offset property on this firmware."), None)
            self.finished.emit()
            return

        try:
            cpr = int(axis.encoder.config.cpr)
            pole_pairs = int(axis.motor.config.pole_pairs)
        except Exception as e:
            self.result.emit(False, QCoreApplication.translate(
                "CalibrationQualityWorker", "Could not read the encoder or motor config: {0}").format(e), None)
            self.finished.emit()
            return
        if pole_pairs <= 0 or cpr <= 0:
            self.result.emit(False, QCoreApplication.translate(
                "CalibrationQualityWorker", "Encoder CPR and pole pairs must both be positive."), None)
            self.finished.emit()
            return

        counts_per_electrical_rev = cpr / pole_pairs
        def to_degrees(counts):
            return counts * 360.0 / counts_per_electrical_rev

        try:
            self._saved['scan_distance'] = axis.encoder.config.calib_scan_distance
            self._saved['calibration_current'] = axis.motor.config.calibration_current
        except Exception:
            pass

        try:
            current_distance = self._saved.get('scan_distance', 16.0 * math.pi)
            if not self.keep_scan_distance:
                axis.encoder.config.calib_scan_distance = float(
                    self.mechanical_revolutions * 2.0 * math.pi * pole_pairs)
                current_distance = axis.encoder.config.calib_scan_distance
            axis.motor.config.calibration_current = float(self.calibration_current)
            scanned_revolutions = current_distance / (2.0 * math.pi * pole_pairs)

            offsets = self._series(QCoreApplication.translate(
                "CalibrationQualityWorker", "Calibrating"), 0)
            if offsets is None:
                raise InterruptedError

            period = counts_per_electrical_rev
            reduced = self._reduce(offsets, period)
            spread = self._circular_spread(reduced, period)
            mean = self._circular_mean(reduced, period)

            lines = [
                QCoreApplication.translate("CalibrationQualityWorker",
                    "{0} calibrations over {1:.2f} mechanical revolutions at {2:.1f} A")
                    .format(self.runs, scanned_revolutions, self.calibration_current),
                QCoreApplication.translate("CalibrationQualityWorker",
                    "  offsets {0}").format(", ".join(str(round(v)) for v in reduced)),
                QCoreApplication.translate("CalibrationQualityWorker",
                    "  spread {0} counts = {1:.2f} electrical degrees").format(
                        round(spread), to_degrees(spread)),
                "",
                QCoreApplication.translate("CalibrationQualityWorker",
                    "That spread is how much your calibration moves from one run to the next, "
                    "which is the uncertainty you would be living with had you kept whichever "
                    "single run you happened to get."),
                "",
            ]

            # The scatter is random rather than a bias, so averaging shrinks it by the
            # square root of the number of runs. Nothing exotic: the same native
            # calibration, just not trusting any single roll of it.
            improvement = math.sqrt(self.runs)
            lines.append(QCoreApplication.translate("CalibrationQualityWorker",
                "Applied the average of all {0}: offset {1}. Averaging random scatter tightens "
                "it by about {2:.1f} times, so this is closer to the true alignment than any one "
                "run.").format(self.runs, round(mean), improvement))
            lines.append("")
            lines.append(QCoreApplication.translate("CalibrationQualityWorker",
                "Save the configuration to keep it. It only survives a reboot when the encoder "
                "uses the Z index with pre-calibrated enabled."))

            if to_degrees(spread) < 3.0:
                lines.append("")
                lines.append(QCoreApplication.translate("CalibrationQualityWorker",
                    "A spread this small already costs under 0.2% of torque, so there was little "
                    "to gain here. Your calibration was in good shape."))

            # The product of this routine is the averaged offset, nothing else. Leaving a
            # longer scan distance behind once made the Encoder tab's own calibration
            # button time out, because that scan no longer fitted in its 25 second limit.
            self._restore()
            setattr(axis.encoder.config, self._offset_attr, int(round(mean)))
            lines.append("")
            lines.append(QCoreApplication.translate("CalibrationQualityWorker",
                "Scan distance and calibration current were put back as they were."))
            self.result.emit(True, "\n".join(lines),
                             {'phase_offset': int(round(mean)), 'spread_counts': round(spread)})

        except InterruptedError:
            self._restore()
            self.result.emit(False, QCoreApplication.translate(
                "CalibrationQualityWorker", "Cancelled. The original settings were restored."), None)
        except fibre.protocol.ChannelBrokenException:
            self.result.emit(False, QCoreApplication.translate(
                "CalibrationQualityWorker", "Connection to the ODrive was lost."), None)
        except Exception as e:
            self._restore()
            self.result.emit(False, QCoreApplication.translate(
                "CalibrationQualityWorker", "The check failed: {0}\n\nThe original settings were restored.").format(e), None)
        finally:
            try:
                self.odrv.axis0.requested_state = AXIS_STATE_IDLE
            except Exception:
                pass
            self.finished.emit()

    def _restore(self):
        try:
            if 'scan_distance' in self._saved:
                self.odrv.axis0.encoder.config.calib_scan_distance = self._saved['scan_distance']
            if 'calibration_current' in self._saved:
                self.odrv.axis0.motor.config.calibration_current = self._saved['calibration_current']
        except Exception:
            pass


class BackEmfKtWorker(QObject):
    """
    Measures the torque constant from the motor's own back-EMF, with no external load.

    Spinning with no load, the voltage the drive has to apply is dominated by the
    back-EMF, so the applied voltage magnitude rises linearly with speed:

        |V| = lambda * omega_electrical + (R * Iq + cross-coupling)

    Sampling several speeds and fitting a line puts lambda in the slope and leaves the
    resistive drop and the d-axis cross-coupling in the intercept, which is what makes
    this robust without knowing either of them precisely.

    Kt then follows the firmware's own convention. ODrive's back-EMF feedforward reads
    (motor.cpp, fw-v0.5.6):

        vq += phase_vel * (2/3) * (torque_constant / pole_pairs)

    so torque_constant = lambda * 1.5 * pole_pairs. Using the firmware's own relation
    means the measured value is consistent with how the firmware consumes it.
    """
    progress = Signal(str, int)
    result = Signal(bool, str, object)     # success, report, measured Kt (or None)
    finished = Signal()

    def __init__(self, odrv, max_velocity, speed_count, current_limit, settle_s, sample_s):
        super().__init__()
        self.odrv = odrv
        self.max_velocity = abs(max_velocity)
        self.speed_count = speed_count
        self.current_limit = current_limit
        self.settle_s = settle_s
        self.sample_s = sample_s
        self._is_running = True
        self._saved = {}
        self._speed_capped = None

    def stop(self):
        self._is_running = False

    def _applied_voltage(self):
        """
        Magnitude of the applied voltage vector. Using alpha/beta keeps this independent
        of the rotor angle, which cannot be sampled synchronously over USB anyway.
        """
        control = self.odrv.axis0.motor.current_control
        return math.hypot(control.final_v_alpha, control.final_v_beta)

    def _enter_closed_loop(self):
        axis = self.odrv.axis0
        axis.error = 0
        axis.motor.error = 0
        axis.encoder.error = 0
        axis.controller.error = 0
        axis.controller.config.control_mode = CONTROL_MODE_VELOCITY_CONTROL
        axis.controller.config.input_mode = INPUT_MODE_VEL_RAMP
        axis.controller.input_vel = 0.0
        axis.requested_state = AXIS_STATE_CLOSED_LOOP_CONTROL
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if not self._is_running:
                return False
            if axis.current_state == AXIS_STATE_CLOSED_LOOP_CONTROL:
                return True
            time.sleep(0.05)
        return False

    def _measure_speed(self, velocity):
        """Returns (mean electrical omega, mean |V|, mean Iq) at one commanded speed."""
        axis = self.odrv.axis0
        axis.controller.input_vel = velocity

        settle_deadline = time.time() + self.settle_s
        while time.time() < settle_deadline:
            if not self._is_running:
                return None
            time.sleep(0.05)

        # Always take at least one sample, so a short sampling window reports a real
        # reading rather than looking like a cancellation.
        volts, speeds, currents = [], [], []
        sample_deadline = time.time() + self.sample_s
        while True:
            if not self._is_running:
                return None
            if axis.error != 0:
                break
            volts.append(self._applied_voltage())
            speeds.append(axis.encoder.vel_estimate)
            currents.append(axis.motor.current_control.Iq_measured)
            if time.time() >= sample_deadline:
                break
            time.sleep(0.02)

        if not volts:
            return None
        pole_pairs = int(axis.motor.config.pole_pairs)
        mean_turns = sum(speeds) / len(speeds)
        omega_electrical = abs(mean_turns) * 2.0 * math.pi * pole_pairs
        return omega_electrical, sum(volts) / len(volts), abs(sum(currents) / len(currents))

    @staticmethod
    def _linear_fit(xs, ys):
        n = len(xs)
        mean_x, mean_y = sum(xs) / n, sum(ys) / n
        sxx = sum((x - mean_x) ** 2 for x in xs)
        if sxx == 0:
            return None
        slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / sxx
        intercept = mean_y - slope * mean_x
        ss_tot = sum((y - mean_y) ** 2 for y in ys)
        ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
        return slope, intercept, (1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0)

    def run(self):
        axis = self.odrv.axis0
        try:
            pole_pairs = int(axis.motor.config.pole_pairs)
        except Exception as e:
            self.result.emit(False, QCoreApplication.translate(
                "BackEmfKtWorker", "Could not read pole pairs: {0}").format(e), None)
            self.finished.emit()
            return
        if pole_pairs <= 0:
            self.result.emit(False, QCoreApplication.translate(
                "BackEmfKtWorker", "Pole pairs must be greater than zero."), None)
            self.finished.emit()
            return

        axis = self.odrv.axis0
        try:
            self._applied_voltage()
        except AttributeError:
            self.result.emit(False, QCoreApplication.translate(
                "BackEmfKtWorker",
                "This firmware does not expose final_v_alpha / final_v_beta, so the applied "
                "voltage cannot be read. The weight based method is the alternative."), None)
            self.finished.emit()
            return

        for key, path in (('current_lim', ('motor', 'config', 'current_lim')),
                          ('control_mode', ('controller', 'config', 'control_mode')),
                          ('input_mode', ('controller', 'config', 'input_mode')),
                          ('vel_limit', ('controller', 'config', 'vel_limit'))):
            try:
                target = axis
                for part in path[:-1]:
                    target = getattr(target, part)
                self._saved[key] = getattr(target, path[-1])
            except Exception:
                pass

        try:
            axis.motor.config.current_lim = float(self.current_limit)
            axis.controller.config.vel_limit = max(self.max_velocity * 2.0, 5.0)
            if not self._enter_closed_loop():
                raise RuntimeError(QCoreApplication.translate(
                    "BackEmfKtWorker", "The axis would not enter closed loop control."))

            # Cap the top speed at what the bus can still drive against. Back-EMF grows
            # with speed until it meets the available voltage, and past that the drive
            # saturates and the voltage stops rising linearly, which is exactly the
            # straight line this measurement fits. On a 24 V bus a 15 pole pair motor
            # reaches that ceiling around 7 turns/s.
            usable_velocity = self.max_velocity
            try:
                configured_kt = float(axis.motor.config.torque_constant)
                flux_estimate = configured_kt / (1.5 * pole_pairs)
                # Two thirds of the bus is what the modulator can put on the dq vector,
                # and 70% of that leaves room for the resistive drop and control margin.
                available_volts = 0.7 * (2.0 / 3.0) * float(self.odrv.vbus_voltage)
                if flux_estimate > 0:
                    ceiling = available_volts / flux_estimate / (2.0 * math.pi * pole_pairs)
                    if ceiling < usable_velocity:
                        usable_velocity = max(ceiling, 1.0)
                        self._speed_capped = (self.max_velocity, usable_velocity)
            except Exception:
                pass

            # Spread the speeds over the upper half of the range: too slow and the
            # back-EMF is swamped by inverter dead time and the resistive drop.
            lowest = usable_velocity / 3.0
            step = (usable_velocity - lowest) / max(self.speed_count - 1, 1)
            targets = [lowest + i * step for i in range(self.speed_count)]

            points = []
            runs = [(1.0, QCoreApplication.translate("BackEmfKtWorker", "forward")),
                    (-1.0, QCoreApplication.translate("BackEmfKtWorker", "reverse"))]
            total = len(targets) * len(runs)
            done = 0
            for direction, label in runs:
                for target in targets:
                    if not self._is_running:
                        raise InterruptedError
                    self.progress.emit(QCoreApplication.translate(
                        "BackEmfKtWorker", "Measuring {0:.1f} turns/s ({1})").format(target, label),
                        int(100 * done / total))
                    measurement = self._measure_speed(target * direction)
                    if measurement is None:
                        raise InterruptedError
                    points.append(measurement)
                    done += 1

            axis.controller.input_vel = 0.0
            self._go_idle()

            usable = [(w, v) for w, v, _ in points if w > 1.0]
            if len(usable) < 2:
                raise RuntimeError(QCoreApplication.translate(
                    "BackEmfKtWorker", "The motor did not reach a usable speed."))

            fit = self._linear_fit([w for w, _ in usable], [v for _, v in usable])
            if fit is None:
                raise RuntimeError(QCoreApplication.translate(
                    "BackEmfKtWorker", "All points landed at the same speed."))
            flux_linkage, intercept, r_squared = fit
            if flux_linkage <= 0:
                raise RuntimeError(QCoreApplication.translate(
                    "BackEmfKtWorker", "The fitted slope is negative, which is not physical."))

            kt = 1.5 * pole_pairs * flux_linkage
            original_limit = self._saved.get('current_lim')

            lines = [QCoreApplication.translate("BackEmfKtWorker", "Kt = {0:.4f} Nm/A").format(kt),
                     QCoreApplication.translate("BackEmfKtWorker",
                         "Fit quality R² = {0:.4f} over {1} points.").format(r_squared, len(usable)),
                     ""]
            if original_limit:
                lines.append(QCoreApplication.translate("BackEmfKtWorker",
                    "At your current limit of {0:.1f} A that is {1:.1f} Nm of peak torque.")
                    .format(original_limit, kt * original_limit))
                lines.append("")
            lines.append(QCoreApplication.translate("BackEmfKtWorker",
                "Flux linkage {0:.5f} Vs/rad, offset absorbed {1:.2f} V.").format(flux_linkage, intercept))
            if self._speed_capped:
                asked, used = self._speed_capped
                lines.append("")
                lines.append(QCoreApplication.translate("BackEmfKtWorker",
                    "Top speed was capped from {0:.1f} to {1:.1f} turns/s: above that the "
                    "back-EMF meets the voltage your bus can supply, the drive saturates, and the "
                    "readings stop following a line.").format(asked, used))
            if r_squared < 0.98:
                lines.append("")
                lines.append(QCoreApplication.translate("BackEmfKtWorker",
                    "R² below 0.98 means the points are not on a line. Treat this as unreliable, "
                    "and check that the shaft really was free."))
            self.result.emit(True, "\n".join(lines), kt)

        except InterruptedError:
            self.result.emit(False, QCoreApplication.translate(
                "BackEmfKtWorker", "Measurement cancelled."), None)
        except fibre.protocol.ChannelBrokenException:
            self.result.emit(False, QCoreApplication.translate(
                "BackEmfKtWorker", "Connection to the ODrive was lost."), None)
        except Exception as e:
            self.result.emit(False, QCoreApplication.translate(
                "BackEmfKtWorker", "The measurement failed: {0}").format(e), None)
        finally:
            try:
                self.odrv.axis0.controller.input_vel = 0.0
                self.odrv.axis0.requested_state = AXIS_STATE_IDLE
                for key, path in (('current_lim', ('motor', 'config', 'current_lim')),
                                  ('control_mode', ('controller', 'config', 'control_mode')),
                                  ('input_mode', ('controller', 'config', 'input_mode')),
                                  ('vel_limit', ('controller', 'config', 'vel_limit'))):
                    if key in self._saved:
                        target = self.odrv.axis0
                        for part in path[:-1]:
                            target = getattr(target, part)
                        setattr(target, path[-1], self._saved[key])
            except Exception:
                pass
            self.finished.emit()

    def _go_idle(self):
        self.odrv.axis0.requested_state = AXIS_STATE_IDLE
        time.sleep(0.15)
