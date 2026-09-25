"""Tests for the PID controller and relay auto-tuner, including a simulated room."""
from custom_components.stealthminer.pid import PID, RelayTuner

# Estimated watts per preset for a single-board S19j Pro, lowest to default
PRESETS = [273, 318, 343, 369, 402, 437, 474, 514, 555, 599, 645, 691, 743, 795, 850, 927, 964, 1003]


class Room:
    """First-order room: heat loss proportional to the indoor-outdoor difference.

    Steps one minute at a time, with the heater's effect delayed a few minutes.
    """

    def __init__(self, temp, outside, ua=15.0, tau_min=180, delay_min=5):
        self.temp, self.outside, self.ua = temp, outside, ua
        self.capacity = ua * tau_min
        self.delayed = [0.0] * delay_min

    def step(self, watts):
        self.delayed.append(watts)
        heat = self.delayed.pop(0)
        self.temp += (heat - self.ua * (self.temp - self.outside)) / self.capacity
        return self.temp


def nearest(watts):
    return min([0.0, *PRESETS], key=lambda w: abs(w - watts))


def closed_loop(kp, ki, outside, minutes=24 * 60, setpoint=68.0, start=62.0, dwell=2):
    room = Room(start, outside)
    pid = PID(kp, ki, 0, PRESETS[-1])
    watts, last_change, temps = 0.0, -dwell, []
    for minute in range(minutes):
        temp = room.step(watts)
        want = nearest(pid.update(setpoint, temp, minute * 60))
        if want != watts and minute - last_change >= dwell:
            watts, last_change = want, minute
        temps.append(temp)
    return temps


def tune(outside):
    room = Room(66, outside)
    tuner = RelayTuner(68, PRESETS[0], PRESETS[-1])
    watts, minute = 0.0, 0
    while not tuner.done and minute < 24 * 60:
        watts = tuner.update(room.step(watts), minute * 60)
        minute += 1
    return tuner


def test_proportional_only_on_first_update():
    assert PID(100, 2, 0, 1000).update(68, 66, 0) == 200


def test_integral_accumulates_per_minute():
    pid = PID(100, 2, 0, 1000)
    pid.update(68, 66, 0)
    pid.update(68, 66, 60)
    assert abs(pid.integral - 4) < 1e-9  # 2 W/deg-min * 2 deg * 1 min


def test_integral_is_clamped():
    high = PID(0, 1000, 0, 500)
    high.update(68, 60, 0)
    high.update(68, 60, 3600)
    assert high.integral == 500
    low = PID(0, 1000, 0, 500)
    low.update(68, 80, 0)
    low.update(68, 80, 60)
    assert low.integral == 0


def test_no_derivative_kick_on_setpoint_change():
    pid = PID(0, 0, 50, 500)
    pid.update(68, 66, 0)
    assert pid.update(68, 66, 60) == 0
    assert pid.update(75, 66, 120) == 0


def test_tuned_gains_hold_temperature_in_cold_weather():
    tuner = tune(outside=10)
    assert tuner.result is not None, tuner.error
    for outside in (10, 30):
        tail = closed_loop(tuner.result["kp"], tuner.result["ki"], outside)[-6 * 60 :]
        assert max(tail) - 68 < 0.3 and 68 - min(tail) < 0.3, outside


def test_tune_fails_clearly_when_min_preset_overheats():
    tuner = tune(outside=50)
    assert tuner.result is None
    assert tuner.error == "min preset keeps the room above the setpoint"
