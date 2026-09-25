"""PID controller and relay auto-tuner for heating with a miner.

Pure Python with no Home Assistant imports so it can be tested standalone.
All outputs are in watts of heat requested; time is in seconds (monotonic
or wall clock) and gains use minutes as their time unit.
"""
from __future__ import annotations

import math

# Ignore gaps longer than this when integrating (HA hiccup, restart, pause)
MAX_DT_MINUTES = 5.0


class PID:
    """PID controller whose output is requested heat in watts.

    The integral is stored as its contribution in watts, so changing Ki
    doesn't make the output jump. It's clamped to [0, output_max] for
    anti-windup. The derivative acts on the measurement, not the error, so
    changing the setpoint doesn't cause a spike.
    """

    def __init__(
        self,
        kp: float,
        ki: float,
        kd: float,
        output_max: float,
        integral: float = 0.0,
    ) -> None:
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.output_max = output_max
        self.integral = self._clamp(integral)
        self.p_term = 0.0
        self.d_term = 0.0
        self.output = 0.0
        self._last_time: float | None = None
        self._last_measured: float | None = None

    def _clamp(self, value: float) -> float:
        return min(max(value, 0.0), self.output_max)

    def reset_time(self) -> None:
        """Forget timing so the next update doesn't integrate across a pause."""
        self._last_time = None
        self._last_measured = None

    def update(self, setpoint: float, measured: float, now: float) -> float:
        """Return requested watts. Positive error (too cold) raises the output."""
        error = setpoint - measured
        dt = 0.0
        if self._last_time is not None:
            dt = min((now - self._last_time) / 60, MAX_DT_MINUTES)

        self.p_term = self.kp * error
        if dt > 0:
            self.integral = self._clamp(self.integral + self.ki * error * dt)
            if self._last_measured is not None:
                self.d_term = -self.kd * (measured - self._last_measured) / dt
        else:
            self.d_term = 0.0

        self._last_time = now
        self._last_measured = measured
        self.output = self.p_term + self.integral + self.d_term
        return self.output


class RelayTuner:
    """Relay (Astrom-Hagglund) auto-tuner.

    Alternates between low and high output around the setpoint, measures the
    resulting temperature oscillation, and derives PI gains using
    Tyreus-Luyben rules, which are conservative and suit slow thermal loads.
    """

    def __init__(
        self,
        setpoint: float,
        low_watts: float,
        high_watts: float,
        hysteresis: float = 0.3,
        cycles: int = 3,
        max_phase_minutes: float = 240,
    ) -> None:
        self.setpoint = setpoint
        self.low = low_watts
        self.high = high_watts
        self.hysteresis = hysteresis
        self.cycles = cycles
        self.max_phase_seconds = max_phase_minutes * 60

        self.heating: bool | None = None
        self.peaks: list[float] = []
        self.troughs: list[float] = []
        # Times the relay switched from high to low; one per full cycle
        self.switch_down_times: list[float] = []
        self._extreme: float | None = None
        self._phase_start: float | None = None

        self._start: float | None = None
        self._last_time: float | None = None
        self._energy = 0.0  # watt-seconds, for the mean output

        self.done = False
        self.error: str | None = None
        self.result: dict[str, float] | None = None

    @property
    def output(self) -> float:
        return self.high if self.heating else self.low

    @property
    def cycles_completed(self) -> int:
        return max(0, len(self.switch_down_times) - 1)

    @property
    def mean_output(self) -> float:
        """Time-weighted mean output so far: roughly the heat the room needs."""
        if self._start is None or self._last_time is None or self._last_time <= self._start:
            return (self.low + self.high) / 2
        return self._energy / (self._last_time - self._start)

    def update(self, measured: float, now: float) -> float:
        """Feed a measurement and return the relay output in watts."""
        if self._start is None:
            self._start = now
        elif self._last_time is not None and not self.done:
            self._energy += self.output * (now - self._last_time)
        self._last_time = now

        if self.done:
            return self.output

        if self.heating is None:
            self.heating = measured < self.setpoint
            self._phase_start = now

        if self.heating:
            # Heating phase: temperature bottoms out, then rises
            self._extreme = measured if self._extreme is None else min(self._extreme, measured)
            if measured > self.setpoint + self.hysteresis:
                self.troughs.append(self._extreme)
                self.switch_down_times.append(now)
                self.heating = False
                self._extreme = measured
                self._phase_start = now
        else:
            # Cooling phase: temperature peaks, then falls
            self._extreme = measured if self._extreme is None else max(self._extreme, measured)
            if measured < self.setpoint - self.hysteresis:
                self.peaks.append(self._extreme)
                self.heating = True
                self._extreme = measured
                self._phase_start = now

        if now - self._phase_start > self.max_phase_seconds:
            self.done = True
            self.error = (
                "max preset can't heat the room above the setpoint"
                if self.heating
                else "min preset keeps the room above the setpoint"
            )
            return self.output

        # The first trough and peak depend on the starting conditions; skip them
        if self.cycles_completed >= self.cycles and len(self.peaks) > self.cycles - 1:
            self._finish()

        return self.output

    def _finish(self) -> None:
        self.done = True
        peaks = self.peaks[-2:]
        troughs = self.troughs[-2:]
        times = self.switch_down_times[-3:]
        amplitude = (sum(peaks) / len(peaks) - sum(troughs) / len(troughs)) / 2
        period_minutes = (times[-1] - times[0]) / (len(times) - 1) / 60

        if amplitude <= self.hysteresis or period_minutes <= 0:
            self.error = "oscillation too small to measure"
            return

        relay = (self.high - self.low) / 2
        # Correct for the relay's hysteresis band
        ku = 4 * relay / (math.pi * math.sqrt(amplitude**2 - self.hysteresis**2))
        kp = ku / 3.2
        ti = 2.2 * period_minutes
        self.result = {
            "kp": round(kp, 2),
            "ki": round(kp / ti, 3),
            "kd": 0.0,
            "ku": round(ku, 2),
            "period_minutes": round(period_minutes, 1),
            "amplitude": round(amplitude, 2),
        }
