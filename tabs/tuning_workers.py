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
from odrive.enums import AXIS_STATE_LOCKIN_SPIN


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
    # Past this the runs are not clustering, so their mean is not a better answer than
    # any one of them and may be worse than all of them.
    MAX_SPREAD_TO_APPLY_DEG = 15.0

    def __init__(self, odrv, runs, mechanical_revolutions, calibration_current,
                 keep_scan_distance=True, apply_average=False):
        super().__init__()
        self.odrv = odrv
        self.runs = runs
        self.mechanical_revolutions = mechanical_revolutions
        self.calibration_current = calibration_current
        self.keep_scan_distance = keep_scan_distance
        self.apply_average = apply_average
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
                "Average of all {0}: offset {1}. Averaging random scatter tightens it by about "
                "{2:.1f} times.").format(self.runs, round(mean), improvement))
            lines.append("")
            lines.append(QCoreApplication.translate("CalibrationQualityWorker",
                "An offset only survives a reboot when the encoder uses the Z index with "
                "pre-calibrated enabled."))

            if to_degrees(spread) < 3.0:
                lines.append("")
                lines.append(QCoreApplication.translate("CalibrationQualityWorker",
                    "A spread this small already costs under 0.2% of torque, so there was little "
                    "to gain here. Your calibration was in good shape."))

            # The product of this routine is the averaged offset, nothing else. Leaving a
            # longer scan distance behind once made the Encoder tab's own calibration
            # button time out, because that scan no longer fitted in its 25 second limit.
            self._restore()

            # Writing the average is opt-in, and refused when the runs disagree. The
            # circular mean is not robust to an outlier: one run 144 degrees from the
            # other four once dragged the result far enough to leave the motor worse
            # commutated than the calibration it replaced. A spread this wide is not a
            # measurement to average, it is a sign that something went wrong in one of
            # the runs.
            if not self.apply_average:
                lines.append("")
                lines.append(QCoreApplication.translate("CalibrationQualityWorker",
                    "Nothing was written. This ran as a measurement only; tick the box to apply "
                    "the average."))
            elif to_degrees(spread) > self.MAX_SPREAD_TO_APPLY_DEG:
                lines.append("")
                lines.append(QCoreApplication.translate("CalibrationQualityWorker",
                    "Nothing was written: at {0:.1f} degrees of spread the runs disagree too much "
                    "to average. An average is only meaningful when the runs cluster, and one bad "
                    "run would drag it away from the good ones. Recalibrate from the Encoder tab "
                    "instead.").format(to_degrees(spread)))
            else:
                setattr(axis.encoder.config, self._offset_attr, int(round(mean)))
                lines.append("")
                lines.append(QCoreApplication.translate("CalibrationQualityWorker",
                    "Applied. Save the configuration to keep it."))
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

    # Below this the straight line is not being tested, only drawn through the points.
    MIN_USABLE_POINTS = 5

    # Everything this routine changes on the drive, in one place, so that saving and
    # restoring cannot drift apart: overspeed protection was once saved but not put
    # back, which would have left a force feedback setup faulting on fast steering.
    DRIVE_PARAMETERS = (
        ('current_lim', ('motor', 'config', 'current_lim')),
        ('control_mode', ('controller', 'config', 'control_mode')),
        ('input_mode', ('controller', 'config', 'input_mode')),
        ('vel_limit', ('controller', 'config', 'vel_limit')),
        ('vel_limit_tolerance', ('controller', 'config', 'vel_limit_tolerance')),
        ('enable_overspeed_error', ('controller', 'config', 'enable_overspeed_error')),
    )
    # Fraction of the mean speed the reading may wander during sampling before the point
    # is treated as taken while the motor was still accelerating or hunting.
    MAX_SPEED_SWING = 0.15

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
        self.runaway_speed = max(self.max_velocity * 2.0, 3.0)
        self._slipped = 0
        self.lockin_current = current_limit * 0.6

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

    def _lockin_available(self):
        try:
            return self.odrv.axis0.config.general_lockin is not None
        except Exception:
            return False

    def _measure_lockin(self, omega_electrical):
        """
        Spins the motor open loop at a commanded electrical velocity and reads the
        voltage it takes.

        run_lockin_spin drives the commutation phase from the open loop controller
        rather than the encoder, so the field turns at exactly the rate asked for. That
        removes every failure this measurement kept hitting: there is no velocity
        controller to be untuned, nothing that can run away, and a loose or badly
        calibrated encoder cannot affect the drive at all. The encoder is read only to
        confirm the rotor kept up.

        Returns (omega, volts) or None when the rotor slipped or the run was cancelled.
        """
        axis = self.odrv.axis0
        pole_pairs = int(axis.motor.config.pole_pairs)
        lockin = axis.config.general_lockin

        ramp_time = 0.5
        accel = max(omega_electrical / 1.5, 1.0)      # up to speed in about 1.5 s
        spin_up = ramp_time + omega_electrical / accel
        budget = spin_up + self.settle_s + self.sample_s + 2.0

        lockin.current = float(self.lockin_current)
        lockin.ramp_time = ramp_time
        lockin.ramp_distance = 0.0
        lockin.accel = accel
        lockin.vel = float(omega_electrical)
        # Bounded by construction: the spin ends on its own after this distance even if
        # this side stops driving it, instead of turning until something intervenes.
        lockin.finish_distance = float(omega_electrical * budget)
        lockin.finish_on_distance = True
        lockin.finish_on_vel = False
        lockin.finish_on_enc_idx = False

        axis.error = 0
        axis.motor.error = 0
        axis.requested_state = AXIS_STATE_LOCKIN_SPIN

        deadline = time.time() + spin_up + self.settle_s
        while time.time() < deadline:
            if not self._is_running:
                axis.requested_state = AXIS_STATE_IDLE
                return None
            time.sleep(0.05)

        volts, speeds = [], []
        end = time.time() + self.sample_s
        while time.time() < end:
            if not self._is_running:
                axis.requested_state = AXIS_STATE_IDLE
                return None
            if axis.error != 0:
                break
            volts.append(self._applied_voltage())
            speeds.append(abs(axis.encoder.vel_estimate))
            time.sleep(0.02)

        axis.requested_state = AXIS_STATE_IDLE
        time.sleep(0.3)

        if not volts:
            return None
        # Open loop only holds while the rotor stays in step with the field. If it
        # slipped, the voltage no longer corresponds to this speed's back-EMF.
        expected_turns = omega_electrical / (2.0 * math.pi * pole_pairs)
        measured_turns = sum(speeds) / len(speeds)
        if expected_turns > 0 and abs(measured_turns - expected_turns) / expected_turns > 0.2:
            self._slipped += 1
            return None
        return omega_electrical, sum(volts) / len(volts)

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
            # Second layer, in case the drive's own limit is not enough: nothing this
            # routine measures needs the motor past its test speed, so anything well
            # beyond it is a runaway and the torque comes off immediately.
            reached = abs(axis.encoder.vel_estimate)
            if reached > self.runaway_speed:
                axis.controller.input_vel = 0.0
                axis.requested_state = AXIS_STATE_IDLE
                raise RuntimeError(QCoreApplication.translate(
                    "BackEmfKtWorker",
                    "The motor ran away to {0:.1f} turns/s when {1:.1f} was commanded, so it was "
                    "stopped.\n\nThe velocity controller is not holding on this motor. The weight "
                    "method does not use it and is the reliable route here.").format(
                        reached, abs(velocity)))
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

        # The relation being fitted only holds in steady state. If the speed was still
        # moving through the sampling window the applied voltage includes whatever the
        # controller was doing about it, not just back-EMF, and the point is not on the
        # line. Rejecting those is what separates a measurement from a scatter plot.
        if abs(mean_turns) > 1e-3:
            swing = (max(speeds) - min(speeds)) / abs(mean_turns)
            if swing > self.MAX_SPEED_SWING:
                return None

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
        control = axis.motor.current_control
        if not (hasattr(control, 'final_v_alpha') and hasattr(control, 'final_v_beta')):
            self.result.emit(False, QCoreApplication.translate(
                "BackEmfKtWorker",
                "This firmware does not expose final_v_alpha / final_v_beta, so the applied "
                "voltage cannot be read. The weight based method is the alternative."), None)
            self.finished.emit()
            return

        for key, path in self.DRIVE_PARAMETERS:
            try:
                target = axis
                for part in path[:-1]:
                    target = getattr(target, part)
                self._saved[key] = getattr(target, path[-1])
            except Exception:
                pass

        try:
            axis.motor.config.current_lim = float(self.current_limit)
            # Force feedback setups usually disable the overspeed error, so that fast
            # steering does not fault the drive. That leaves nothing to stop a runaway
            # while this routine drives the motor, so it is turned on for the duration
            # and restored afterwards. The drive reacts in its own control loop, far
            # faster than this can poll over USB.
            axis.controller.config.vel_limit = self.max_velocity * 1.3
            try:
                axis.controller.config.vel_limit_tolerance = 1.2
                axis.controller.config.enable_overspeed_error = True
            except Exception:
                pass
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

            points, unsteady = [], 0
            use_lockin = self._lockin_available()
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
                    if use_lockin:
                        omega = direction * target * 2.0 * math.pi * pole_pairs
                        measured = self._measure_lockin(abs(omega))
                        measurement = (measured[0], measured[1], 0.0) if measured else None
                    else:
                        measurement = self._measure_speed(target * direction)
                    if measurement is not None:
                        points.append(measurement)
                    else:
                        unsteady += 1
                    done += 1

            axis.controller.input_vel = 0.0
            self._go_idle()

            usable = [(w, v) for w, v, _ in points if w > 1.0]
            # Two points always fit a line perfectly, so an R squared of 1.0000 from two
            # readings is not evidence of a good measurement, it is the absence of any
            # evidence at all. Enough points to actually test the straight line, or no
            # answer.
            if len(usable) < self.MIN_USABLE_POINTS:
                raise RuntimeError(QCoreApplication.translate(
                    "BackEmfKtWorker",
                    "Only {0} of {1} speeds gave a steady reading. The motor either stayed still "
                    "or never settled at the rest, "
                    "so there are not enough points to fit a line through.\n\nCheck 'Show Errors': "
                    "the axis is probably faulting. A fit from this few points would report a "
                    "perfect R squared while measuring nothing.").format(len(usable), len(points)))

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
                         "Fit quality R² = {0:.4f} over {1} of {2} points.").format(
                             r_squared, len(usable), len(points)),
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
                for key, path in self.DRIVE_PARAMETERS:
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
