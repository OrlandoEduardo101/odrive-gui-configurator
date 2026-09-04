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
from odrive.enums import CONTROL_MODE_VELOCITY_CONTROL, INPUT_MODE_VEL_RAMP


class OffsetAlignmentWorker(QObject):
    """
    Sweeps the encoder electrical offset and reports the value that spins the motor
    with the least current.
    """
    progress = Signal(str, int)              # human readable status, percent complete
    result = Signal(bool, str)               # success, report
    finished = Signal()

    def __init__(self, odrv, velocity, current_limit, coarse_points, fine_points,
                 revolutions, settle_s):
        super().__init__()
        self.odrv = odrv
        self.velocity = abs(velocity)
        self.current_limit = current_limit
        self.coarse_points = coarse_points
        self.fine_points = fine_points
        self.revolutions = revolutions
        self.settle_s = settle_s
        self._is_running = True
        self._saved = {}
        self._arm_failure = None
        self._velocities = []      # achieved speed per measured point

    def stop(self):
        """Signals the sweep to unwind at the next checkpoint."""
        self._is_running = False

    # ------------------------------------------------------- firmware probe ---

    # Where the electrical offset lives, newest naming first. Firmware 0.5.6 calls it
    # encoder.config.phase_offset; earlier 0.5.x releases called it encoder.config.offset.
    OFFSET_CANDIDATES = [
        ('encoder', 'phase_offset'),
        ('encoder', 'offset'),
        ('motor', 'phase_offset'),
    ]

    def _resolve_offset_property(self):
        """
        Finds where this firmware keeps the electrical offset.
        Returns (owner_config, attribute_name), or (None, None) when none is present.
        """
        axis = self.odrv.axis0
        for owner_name, attr in self.OFFSET_CANDIDATES:
            try:
                config = getattr(getattr(axis, owner_name), 'config')
                if hasattr(config, attr):
                    return config, attr
            except Exception:
                continue
        return None, None

    def _offset_diagnostics(self):
        """
        Lists the offset-looking properties this board actually has, so a firmware whose
        naming is not covered above reports what to add instead of just failing.
        """
        found = []
        for owner_name in ('encoder', 'motor'):
            try:
                config = getattr(getattr(self.odrv.axis0, owner_name), 'config')
                for name in dir(config):
                    if 'offset' in name.lower() and not name.startswith('_'):
                        found.append(f"{owner_name}.config.{name}")
            except Exception:
                continue
        return found

    def _read_current(self):
        """
        Returns the magnitude of the current vector. Id is included when the firmware
        exposes it, because a misaligned frame shows up partly on the d axis.
        """
        control = self.odrv.axis0.motor.current_control
        iq = control.Iq_measured
        try:
            return math.hypot(control.Id_measured, iq)
        except AttributeError:
            return abs(iq)

    # ---------------------------------------------------------- axis control ---

    def _go_idle(self):
        self.odrv.axis0.requested_state = AXIS_STATE_IDLE
        time.sleep(0.15)

    def _enter_closed_loop(self):
        """Arms closed loop velocity control. Returns True once the axis holds the state."""
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

        # Record why the drive refused, so a sweep that fails everywhere can say what
        # went wrong instead of only reporting that it did.
        try:
            self._arm_failure = (
                f"axis={hex(axis.error)} motor={hex(axis.motor.error)} "
                f"encoder={hex(axis.encoder.error)} controller={hex(axis.controller.error)}")
        except Exception:
            pass
        return False

    # -------------------------------------------------------------- sampling ---

    def _measure_at(self, offset, velocity, offset_config, offset_attr):
        """
        Applies one candidate offset and returns the mean current magnitude needed to
        spin at the requested velocity. Returns None when the sweep was cancelled.

        A candidate that cannot reach speed is not an error: a badly commutated offset
        legitimately saturates, and the high current it draws is exactly the signal
        that rules it out. Its reading is kept.
        """
        self._go_idle()
        setattr(offset_config, offset_attr, int(offset))

        if not self._enter_closed_loop():
            # Refusing to arm is itself a symptom of a hopeless offset; score it as
            # the worst possible so the sweep moves on instead of aborting.
            self._go_idle()
            return float('inf')

        axis = self.odrv.axis0
        axis.controller.input_vel = velocity

        settle_deadline = time.time() + self.settle_s
        while time.time() < settle_deadline:
            if not self._is_running:
                return None
            time.sleep(0.05)

        # Sample over a whole number of mechanical revolutions so cogging averages out.
        sample_seconds = self.revolutions / max(abs(velocity), 1e-3)
        samples, speeds = [], []
        sample_deadline = time.time() + sample_seconds
        while time.time() < sample_deadline:
            if not self._is_running:
                return None
            try:
                samples.append(self._read_current())
                speeds.append(abs(axis.encoder.vel_estimate))
            except AttributeError:
                pass
            if axis.error != 0:
                break
            time.sleep(0.02)

        axis.controller.input_vel = 0.0
        self._go_idle()

        if not samples:
            return float('inf')
        # The speed actually reached decides whether the current reading means anything:
        # a motor that never got moving draws a current unrelated to its alignment.
        if speeds:
            self._velocities.append(sum(speeds) / len(speeds))
        return sum(samples) / len(samples)

    def _measurement_summary(self, finite):
        """
        One line of evidence about what the sweep actually saw, attached to failures so
        the next attempt is informed rather than another guess.
        """
        parts = []
        if finite:
            values = list(finite.values())
            parts.append(QCoreApplication.translate(
                "OffsetAlignmentWorker", "current {0:.2f}-{1:.2f} A of {2:.1f} A allowed")
                .format(min(values), max(values), self.current_limit))
        if self._velocities:
            parts.append(QCoreApplication.translate(
                "OffsetAlignmentWorker", "speed reached {0:.2f}-{1:.2f} turns/s of {2:.2f} asked")
                .format(min(self._velocities), max(self._velocities), self.velocity))
        if not parts:
            return ""
        return "\n\n" + QCoreApplication.translate(
            "OffsetAlignmentWorker", "What the sweep saw: {0}.").format("; ".join(parts))

    def _sweep(self, offsets, velocity, offset_config, offset_attr, label, base_pct, span_pct):
        """Measures every offset in the list, reporting progress as it goes."""
        readings = {}
        for i, offset in enumerate(offsets):
            if not self._is_running:
                return None
            # Check the very first point: if the motor barely turns, every later reading
            # is noise too, and there is no reason to spend minutes collecting it.
            if self._velocities and len(self._velocities) == 1:
                reached = self._velocities[0]
                if reached < 0.3 * abs(velocity):
                    raise RuntimeError(QCoreApplication.translate(
                        "OffsetAlignmentWorker",
                        "The motor only reached {0:.2f} turns/s of the {1:.2f} asked for.\n\n"
                        "At that speed the current says nothing about alignment. Raise the sweep "
                        "current limit, or lower the test velocity, and try again.")
                        .format(reached, abs(velocity)))
            pct = base_pct + int(span_pct * i / max(len(offsets), 1))
            self.progress.emit(
                QCoreApplication.translate("OffsetAlignmentWorker", "{0}: offset {1} ({2}/{3})")
                .format(label, int(offset), i + 1, len(offsets)), pct)
            value = self._measure_at(offset, velocity, offset_config, offset_attr)
            if value is None:
                return None
            readings[int(offset)] = value
        return readings

    # ------------------------------------------------------------------- run ---

    def run(self):
        """Executes the full alignment sweep and restores the drive afterwards."""
        offset_config, offset_attr = self._resolve_offset_property()
        if offset_config is None:
            tried = ", ".join(f"{owner}.config.{attr}" for owner, attr in self.OFFSET_CANDIDATES)
            found = self._offset_diagnostics()
            message = QCoreApplication.translate(
                "OffsetAlignmentWorker",
                "Could not find the electrical offset property on this firmware.\n\nTried: {0}"
            ).format(tried)
            if found:
                message += "\n\n" + QCoreApplication.translate(
                    "OffsetAlignmentWorker",
                    "This board does have: {0}\n\nPlease report these so the routine can support "
                    "your firmware.").format(", ".join(found))
            self.result.emit(False, message)
            self.finished.emit()
            return

        axis = self.odrv.axis0
        try:
            cpr = int(axis.encoder.config.cpr)
            pole_pairs = int(axis.motor.config.pole_pairs)
        except Exception as e:
            self.result.emit(False, QCoreApplication.translate(
                "OffsetAlignmentWorker", "Could not read encoder CPR or pole pairs: {0}").format(e))
            self.finished.emit()
            return

        if pole_pairs <= 0 or cpr <= 0:
            self.result.emit(False, QCoreApplication.translate(
                "OffsetAlignmentWorker", "Encoder CPR and pole pairs must both be positive."))
            self.finished.emit()
            return

        counts_per_electrical_rev = cpr / pole_pairs
        original_offset = int(getattr(offset_config, offset_attr))

        # Everything changed here is restored in the finally block.
        try:
            self._saved['current_lim'] = axis.motor.config.current_lim
            self._saved['control_mode'] = axis.controller.config.control_mode
            self._saved['input_mode'] = axis.controller.config.input_mode
            self._saved['vel_limit'] = axis.controller.config.vel_limit
        except Exception:
            pass

        try:
            axis.motor.config.current_lim = float(self.current_limit)
            axis.controller.config.vel_limit = max(self.velocity * 2.0, 5.0)

            # Centre the window on the calibrated offset instead of starting there. The
            # window spans one electrical revolution, so its two ends are the same
            # electrical angle; with the expected optimum sitting at that seam, the two
            # directions can settle on ends that are electrically adjacent but numerically
            # a whole revolution apart.
            step = counts_per_electrical_rev / self.coarse_points
            first = original_offset - counts_per_electrical_rev / 2.0
            coarse_offsets = [round(first + i * step) % cpr
                              for i in range(self.coarse_points)]

            best_per_direction = []
            readings_by_direction = {}

            for d_index, direction in enumerate((1.0, -1.0)):
                velocity = self.velocity * direction
                label = (QCoreApplication.translate("OffsetAlignmentWorker", "Coarse sweep (forward)")
                         if direction > 0 else
                         QCoreApplication.translate("OffsetAlignmentWorker", "Coarse sweep (reverse)"))
                base = d_index * 50
                coarse = self._sweep(coarse_offsets, velocity, offset_config, offset_attr,
                                     label, base, 30)
                if coarse is None:
                    raise InterruptedError

                best_coarse = min(coarse, key=coarse.get)

                # Refine around the coarse winner, one coarse step to either side.
                fine_span = step
                fine_offsets = [
                    round(best_coarse - fine_span + (2 * fine_span) * j / max(self.fine_points - 1, 1)) % cpr
                    for j in range(self.fine_points)
                ]
                fine_label = (QCoreApplication.translate("OffsetAlignmentWorker", "Fine sweep (forward)")
                              if direction > 0 else
                              QCoreApplication.translate("OffsetAlignmentWorker", "Fine sweep (reverse)"))
                fine = self._sweep(fine_offsets, velocity, offset_config, offset_attr,
                                   fine_label, base + 30, 20)
                if fine is None:
                    raise InterruptedError

                combined = dict(coarse)
                combined.update(fine)
                readings_by_direction[direction] = combined
                best_per_direction.append(min(combined, key=combined.get))

            # Averaging the two directions cancels the encoder's velocity dependent
            # phase lag, which otherwise biases the optimum one way per direction.
            #
            # The average has to be circular. Offsets a whole electrical revolution apart
            # are the same electrical angle, so if the two directions settle on equivalent
            # offsets a revolution apart, a plain arithmetic mean lands halfway between
            # them, which is half an electrical turn from either, and meaningless.
            angles = [2.0 * math.pi * ((best - original_offset) % counts_per_electrical_rev)
                      / counts_per_electrical_rev for best in best_per_direction]
            mean_angle = math.atan2(
                sum(math.sin(a) for a in angles) / len(angles),
                sum(math.cos(a) for a in angles) / len(angles))
            best_offset = int(round(
                original_offset + counts_per_electrical_rev * mean_angle / (2.0 * math.pi))) % cpr

            all_readings = {}
            for readings in readings_by_direction.values():
                for key, value in readings.items():
                    all_readings.setdefault(key, []).append(value)
            finite = {k: sum(v) / len(v) for k, v in all_readings.items()
                      if all(math.isfinite(x) for x in v)}

            # A point scores infinite when the axis refused to arm at that offset. A few
            # are expected at badly commutated offsets, but if most of them failed there
            # is no usable minimum and picking one anyway produces a confident, wrong
            # answer. Fail instead.
            usable_ratio = len(finite) / max(len(all_readings), 1)
            if usable_ratio < 0.5:
                raise RuntimeError(QCoreApplication.translate(
                    "OffsetAlignmentWorker",
                    "Only {0} of {1} offsets could be measured; the axis would not stay in closed "
                    "loop at the rest. Without most points there is no reliable minimum.\n\n"
                    "Usually the sweep current limit is too low to turn the motor, or the axis is "
                    "faulting during the sweep. Check 'Show Errors', raise the sweep current a "
                    "little, and try again.{2}").format(
                        len(finite), len(all_readings),
                        ("\n\nLast refusal: " + self._arm_failure) if self._arm_failure else ""))

            best_current = finite.get(best_offset)
            worst_current = max(finite.values()) if finite else None
            if best_current is None:
                raise RuntimeError(QCoreApplication.translate(
                    "OffsetAlignmentWorker",
                    "The winning offset had no valid current reading, so the result cannot be "
                    "trusted. Nothing was changed."))

            delta = best_offset - original_offset
            # Express the shift as the shortest way round the electrical cycle.
            delta = (delta + counts_per_electrical_rev / 2) % counts_per_electrical_rev \
                - counts_per_electrical_rev / 2
            electrical_degrees = delta * 360.0 / counts_per_electrical_rev

            # This routine refines an existing calibration. A shift beyond a quarter turn
            # electrical is not a refinement, it is a different commutation point, and in
            # practice means the sweep measured noise. Refuse rather than apply it.
            if abs(electrical_degrees) > 90.0:
                raise RuntimeError(QCoreApplication.translate(
                    "OffsetAlignmentWorker",
                    "The sweep landed {0:.1f} electrical degrees from the calibrated offset. That "
                    "is a different commutation point, not a refinement, so it was not applied.\n\n"
                    "This usually means the measurements were noise: the motor may not have been "
                    "turning freely, or the sweep current limit was too low to move it.{1}")
                    .format(electrical_degrees, self._measurement_summary(finite)))

            # Torque scales with cos(error), so this stays within 0-100% by construction
            # once the shift is inside a quarter turn.
            torque_recovered = (1.0 - math.cos(math.radians(electrical_degrees))) * 100.0

            setattr(offset_config, offset_attr, int(best_offset))

            lines = [
                QCoreApplication.translate("OffsetAlignmentWorker", "Best alignment found: {0}").format(best_offset),
                QCoreApplication.translate("OffsetAlignmentWorker",
                    "Measured {0} of {1} offsets successfully.").format(len(finite), len(all_readings)),
                QCoreApplication.translate("OffsetAlignmentWorker",
                    "Speed held: {0:.2f} turns/s of {1:.2f} asked.").format(
                        sum(self._velocities) / len(self._velocities) if self._velocities else 0.0,
                        self.velocity),
            ]
            if best_current is not None and worst_current is not None:
                lines.append(QCoreApplication.translate(
                    "OffsetAlignmentWorker", "Current there: {0:.2f} A (worst point tested: {1:.2f} A)")
                    .format(best_current, worst_current))
            lines.append("")
            lines.append(QCoreApplication.translate(
                "OffsetAlignmentWorker", "Calibration had left {0}.").format(original_offset))
            lines.append(QCoreApplication.translate(
                "OffsetAlignmentWorker", "Difference: {0} counts = {1:.1f} electrical degrees, "
                "which was costing about {2:.1f}% of torque.")
                .format(int(delta), electrical_degrees, torque_recovered))
            lines.append("")
            # If even the best offset needed most of the allowed current, the motor was
            # current-starved and every point sat against the limit, which makes the
            # minimum meaningless. Say so rather than presenting a confident number.
            if best_current is not None and best_current > 0.5 * self.current_limit:
                lines.append("")
                lines.append(QCoreApplication.translate(
                    "OffsetAlignmentWorker",
                    "Warning: the best point still drew {0:.2f} A of the {1:.1f} A allowed. The "
                    "sweep was probably starved of current, so this result is unreliable. Raise "
                    "the sweep current limit and run it again.").format(best_current, self.current_limit))

            lines.append("")
            lines.append(QCoreApplication.translate(
                "OffsetAlignmentWorker",
                "Already applied to the running session. Use 'Save Configuration' to keep it.\n\n"
                "Note: the offset only survives a reboot when the encoder uses the Z index "
                "with pre-calibrated enabled."))

            self.result.emit(True, "\n".join(lines))

        except InterruptedError:
            setattr(offset_config, offset_attr, original_offset)
            self.result.emit(False, QCoreApplication.translate(
                "OffsetAlignmentWorker", "Sweep cancelled. The original offset was restored."))
        except fibre.protocol.ChannelBrokenException:
            self.result.emit(False, QCoreApplication.translate(
                "OffsetAlignmentWorker", "Connection to the ODrive was lost during the sweep."))
        except Exception as e:
            try:
                setattr(offset_config, offset_attr, original_offset)
            except Exception:
                pass
            self.result.emit(False, QCoreApplication.translate(
                "OffsetAlignmentWorker", "The sweep failed: {0}\n\nThe original offset was restored.").format(e))
        finally:
            # Restoring the drive matters more than reporting, so it runs unconditionally.
            try:
                self.odrv.axis0.controller.input_vel = 0.0
                self.odrv.axis0.requested_state = AXIS_STATE_IDLE
                for key, attr_path in (('current_lim', ('motor', 'config', 'current_lim')),
                                       ('control_mode', ('controller', 'config', 'control_mode')),
                                       ('input_mode', ('controller', 'config', 'input_mode')),
                                       ('vel_limit', ('controller', 'config', 'vel_limit'))):
                    if key in self._saved:
                        target = self.odrv.axis0
                        for part in attr_path[:-1]:
                            target = getattr(target, part)
                        setattr(target, attr_path[-1], self._saved[key])
            except Exception:
                pass
            self.finished.emit()


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

            # Spread the speeds over the upper half of the range: too slow and the
            # back-EMF is swamped by inverter dead time and the resistive drop.
            lowest = self.max_velocity / 3.0
            step = (self.max_velocity - lowest) / max(self.speed_count - 1, 1)
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
