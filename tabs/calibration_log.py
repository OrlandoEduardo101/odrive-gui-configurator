# tabs/calibration_log.py
"""
Keeps a history of what each calibration produced, so runs can be compared.

A single calibration tells you nothing about whether it went well. The same motor
calibrated three times in a row reported 0.4431, 0.4702 and 0.4944 ohm, which is one
winding roughly 30 C hotter each time rather than three different motors, and nothing
on screen said so. Each result in isolation looked perfectly reasonable.

Comparing a calibration against the previous one is a fair comparison in a way that
comparing it against a running measurement is not: both numbers come from the same
routine, measured the same way, so what is left between them is the motor changing.
"""
import json
import os
import time

from PySide6.QtCore import QStandardPaths

# Copper gains 0.393% of its resistance per degree, which turns a resistance ratio
# between two calibrations into the temperature difference between them.
COPPER_TEMPCO = 0.00393

# Enough history to see a trend, not so much that the file grows without bound.
MAX_ENTRIES = 50

# What gets recorded, and what a change in it means. The kinds differ because the
# values do: a measurement drifts with temperature and noise, a position scatters a
# little every time by nature, a setting only changes because somebody changed it, and
# an identity like cpr or direction should never move on its own.
#
#   measurement: flagged past a fraction of itself
#   position:    flagged past a number of electrical degrees, since a few cost no torque
#   setting:     recorded and shown, never alarming
#   identity:    flagged on any change at all
FIELDS = [
    ('phase_resistance', 'axis0.motor.config.phase_resistance', 'measurement', 0.05),
    ('phase_inductance', 'axis0.motor.config.phase_inductance', 'measurement', 0.10),
    ('phase_offset', 'axis0.encoder.config.phase_offset', 'position', 10.0),
    # The fraction is the sub-count half of the offset and moves every run by design, so
    # on its own it is never news; it travels with phase_offset above.
    ('phase_offset_float', 'axis0.encoder.config.phase_offset_float', 'setting', None),
    ('direction', 'axis0.encoder.config.direction', 'identity', None),
    ('calibration_current', 'axis0.motor.config.calibration_current', 'setting', None),
    ('resistance_calib_max_voltage', 'axis0.motor.config.resistance_calib_max_voltage',
     'setting', None),
    ('torque_constant', 'axis0.motor.config.torque_constant', 'setting', None),
    ('pole_pairs', 'axis0.motor.config.pole_pairs', 'identity', None),
    ('cpr', 'axis0.encoder.config.cpr', 'identity', None),
    ('vbus_voltage', 'vbus_voltage', 'setting', None),
    ('fet_temp', 'axis0.motor.fet_thermistor.temperature', 'setting', None),
]


def history_path():
    """The history file, under the per-user application data directory."""
    folder = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    if not folder:
        folder = os.path.join(os.path.expanduser("~"), ".odrive_gui_configurator")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "calibration_history.json")


def read_snapshot(odrv):
    """
    Collects the values a calibration produces, skipping whatever this board lacks.

    A field that cannot be read is left out rather than stored as zero, which would
    later read as a real measurement of a motor with no resistance.
    """
    entry = {'time': time.time()}
    for name, path, _, _ in FIELDS:
        target = odrv
        try:
            for part in path.split('.'):
                target = getattr(target, part)
            value = float(target) if not isinstance(target, bool) else target
        except Exception:
            continue
        entry[name] = value
    return entry


def load():
    """Returns the stored history, oldest first, or an empty list if there is none."""
    try:
        with open(history_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def append(entry):
    """Adds one entry, keeping the file to the newest MAX_ENTRIES."""
    entries = load()
    entries.append(entry)
    entries = entries[-MAX_ENTRIES:]
    try:
        with open(history_path(), "w", encoding="utf-8") as handle:
            json.dump(entries, handle, indent=2)
    except Exception:
        pass
    return entries


def clear():
    """
    Removes the stored history. Returns how many entries went.

    The file is deleted rather than emptied so nothing is left behind holding readings
    from a motor that may since have been rebuilt.
    """
    entries = load()
    try:
        os.remove(history_path())
    except OSError:
        pass
    return len(entries)


def temperature_difference(previous_r, current_r):
    """
    Degrees the winding differs by, from the resistance two calibrations measured.

    Both numbers come from the same routine, so the comparison is like with like and
    what remains between them is the copper being at a different temperature.
    """
    if not previous_r or not current_r:
        return None
    return (current_r / previous_r - 1.0) / COPPER_TEMPCO


def electrical_degrees(counts, entry):
    """Converts a count difference into electrical degrees for that motor."""
    cpr, pole_pairs = entry.get('cpr'), entry.get('pole_pairs')
    if not cpr or not pole_pairs:
        return None
    return counts * 360.0 / (cpr / pole_pairs)


def compare(previous, current):
    """
    Returns (name, before, after, size, is_notable) for what moved between two runs.

    `size` is a fraction for a measurement and electrical degrees for a position, which
    is why the caller is told the kind rather than being handed one number to interpret.
    Scatter in the offset is normal and reporting it every time would bury the changes
    that are not, so it has to clear a few degrees before it counts.
    """
    changes = []
    for name, _, kind, limit in FIELDS:
        if name not in previous or name not in current:
            continue
        before, after = previous[name], current[name]
        if before == after:
            continue
        if kind == 'position':
            degrees = electrical_degrees(after - before, current)
            if degrees is None:
                continue
            changes.append((name, before, after, abs(degrees), abs(degrees) >= limit))
        elif kind == 'measurement':
            fraction = abs(after - before) / abs(before) if before else 1.0
            changes.append((name, before, after, fraction, fraction >= limit))
        elif kind == 'identity':
            changes.append((name, before, after, 1.0, True))
        else:
            changes.append((name, before, after, 0.0, False))
    return changes
