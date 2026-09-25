"""Climate platform: a PID thermostat that heats a room with the miner."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import StealthminerAPIError
from .const import (
    CONF_KD,
    CONF_KI,
    CONF_KP,
    CONF_MAX_PROFILE,
    CONF_MIN_PROFILE,
    CONF_SLEEP_DELAY,
    CONF_SLEEP_FAN_SPEED,
    CONF_TEMPERATURE_ENTITY,
    DEFAULT_KD,
    DEFAULT_KI,
    DEFAULT_KP,
    DEFAULT_MAX_PROFILE,
    DEFAULT_SLEEP_DELAY,
    DEFAULT_SLEEP_FAN_SPEED,
    DOMAIN,
    FAN_SPEED_AUTO,
)
from .coordinator import StealthminerDataUpdateCoordinator
from .pid import PID, RelayTuner

_LOGGER = logging.getLogger(__name__)

CONTROL_INTERVAL = timedelta(seconds=60)
# Minimum time on a preset before changing again
MIN_DWELL_SECONDS = 120
# Control ticks are 60 s apart; don't let a tick that lands a hair early add a minute
DWELL_SLACK_SECONDS = 5
# While the miner ramps up after waking, automatic fan control holds the fans
# at 75-100%. Keep them on manual until it's hashing and done ramping.
FAN_AUTO_FALLBACK_SECONDS = 300
# How often to check while waiting for the ramp (instead of the 60 s tick)
WAIT_POLL_SECONDS = 15
AUTOTUNE_TIMEOUT_SECONDS = 12 * 3600
AUTOTUNE_SENSOR_TIMEOUT_SECONDS = 10 * 60

STATE_RUNNING = "running"
STATE_SLEEPING = "sleeping"
STATE_GOING_TO_SLEEP = "going_to_sleep"
STATE_WAKING = "waking"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the thermostat once a temperature sensor is configured."""
    coordinator: StealthminerDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    if not entry.options.get(CONF_TEMPERATURE_ENTITY):
        coordinator.thermostat = None
        return

    thermostat = StealthminerThermostat(coordinator, entry)
    coordinator.thermostat = thermostat
    async_add_entities([thermostat])


class StealthminerThermostat(
    CoordinatorEntity[StealthminerDataUpdateCoordinator], ClimateEntity, RestoreEntity
):
    """Thermostat that picks a mining preset with a PID controller.

    The controller asks for watts of heat; the nearest preset between the
    configured min and max is used. When the nearest option is "no heat" for
    long enough, the miner goes to sleep gently: lowest preset, fans on a low
    manual speed, then sleep. Waking runs the reverse.
    """

    _attr_has_entity_name = True
    _attr_name = "Thermostat"
    _attr_icon = "mdi:thermostat"
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_target_temperature_step = 0.5
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(
        self, coordinator: StealthminerDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the thermostat."""
        super().__init__(coordinator)
        self._entry = entry
        options = entry.options
        self._attr_unique_id = f"{coordinator.api.host}_{coordinator.api.port}_thermostat"
        self._attr_device_info = coordinator.device_info

        features = ClimateEntityFeature.TARGET_TEMPERATURE
        for name in ("TURN_ON", "TURN_OFF"):
            features |= getattr(ClimateEntityFeature, name, 0)
        self._attr_supported_features = features

        unit = coordinator.hass.config.units.temperature_unit
        self._attr_temperature_unit = unit
        if unit == UnitOfTemperature.FAHRENHEIT:
            self._attr_min_temp, self._attr_max_temp, self._default_target = 40.0, 90.0, 68.0
        else:
            self._attr_min_temp, self._attr_max_temp, self._default_target = 5.0, 32.0, 20.0

        self._sensor_entity: str = options[CONF_TEMPERATURE_ENTITY]
        self._min_profile: str | None = options.get(CONF_MIN_PROFILE)
        self._max_profile: str = options.get(CONF_MAX_PROFILE, DEFAULT_MAX_PROFILE)
        self._sleep_delay = options.get(CONF_SLEEP_DELAY, DEFAULT_SLEEP_DELAY) * 60
        self._sleep_fan_speed = int(options.get(CONF_SLEEP_FAN_SPEED, DEFAULT_SLEEP_FAN_SPEED))

        self._hvac_mode = HVACMode.OFF
        self._target_temperature: float | None = None
        self._pid = PID(
            options.get(CONF_KP, DEFAULT_KP),
            options.get(CONF_KI, DEFAULT_KI),
            options.get(CONF_KD, DEFAULT_KD),
            output_max=0.0,  # set from the presets on each tick
        )

        self._state = STATE_RUNNING
        self._wanted: str | None = None  # preset name, or None for sleep
        self._last_change = 0.0
        self._want_sleep_since: float | None = None
        self._want_wake_since: float | None = None
        # (name, step, gate): a step with a gate waits until the gate returns True
        self._sequence: list[
            tuple[str, Callable[[], Awaitable[Any]], Callable[[], bool] | None]
        ] = []
        self._woke_at = 0.0
        self._cancel_wait_poll: Callable[[], None] | None = None
        self._sequence_done_state: str | None = None
        self._lock = asyncio.Lock()

        self._tuner: RelayTuner | None = None
        self._tune_started = 0.0
        self._tune_sensor_lost_since: float | None = None
        self._tune_status: str | None = None

    # ---- Home Assistant lifecycle ----

    async def async_added_to_hass(self) -> None:
        """Restore state and start the control loop."""
        await super().async_added_to_hass()

        if (last := await self.async_get_last_state()) is not None:
            if last.state in (HVACMode.HEAT, HVACMode.OFF):
                self._hvac_mode = HVACMode(last.state)
            if (temp := last.attributes.get(ATTR_TEMPERATURE)) is not None:
                self._target_temperature = float(temp)
            if (integral := last.attributes.get("pid_integral")) is not None:
                self._pid.integral = float(integral)
        if self._target_temperature is None:
            self._target_temperature = self._default_target

        self._state = STATE_SLEEPING if self.coordinator.is_sleeping else STATE_RUNNING

        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_tick, CONTROL_INTERVAL)
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self._sensor_entity], self._async_sensor_changed
            )
        )

    async def async_will_remove_from_hass(self) -> None:
        """Drop the coordinator's reference to this entity."""
        if self._cancel_wait_poll is not None:
            self._cancel_wait_poll()
            self._cancel_wait_poll = None
        if self.coordinator.thermostat is self:
            self.coordinator.thermostat = None
        await super().async_will_remove_from_hass()

    @callback
    def _async_sensor_changed(self, event: Event) -> None:
        self.async_write_ha_state()

    # ---- Climate properties ----

    @property
    def available(self) -> bool:
        """The thermostat stays usable while the miner is briefly offline."""
        return self.coordinator.last_update_success

    @property
    def hvac_mode(self) -> HVACMode:
        return self._hvac_mode

    @property
    def hvac_action(self) -> HVACAction:
        if self._hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if self._state == STATE_SLEEPING or self.coordinator.is_sleeping:
            return HVACAction.IDLE
        return HVACAction.HEATING

    @property
    def current_temperature(self) -> float | None:
        return self._read_sensor()

    @property
    def target_temperature(self) -> float | None:
        return self._target_temperature

    @property
    def autotune_active(self) -> bool:
        return self._tuner is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "controller_state": self._state,
            "requested_watts": round(self._pid.output),
            "target_preset": self._wanted or "sleep",
            "pid_p": round(self._pid.p_term, 1),
            "pid_i": round(self._pid.integral, 1),
            "pid_d": round(self._pid.d_term, 1),
            "pid_integral": self._pid.integral,
            "reported_watts": self.coordinator.data.get("power", {}).get("Watts")
            if self.coordinator.data
            else None,
        }
        next_change = self._last_change + MIN_DWELL_SECONDS - time.time()
        if next_change > 0:
            attrs["next_preset_change_in_s"] = int(next_change)
        if self._tuner is not None:
            attrs["autotune_cycles"] = f"{self._tuner.cycles_completed}/{self._tuner.cycles}"
            attrs["autotune_peaks"] = [round(p, 2) for p in self._tuner.peaks]
            attrs["autotune_troughs"] = [round(t, 2) for t in self._tuner.troughs]
        if self._tune_status:
            attrs["autotune_status"] = self._tune_status
        return attrs

    # ---- Commands ----

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is not None:
            self._target_temperature = float(temp)
            self.async_write_ha_state()
            await self._async_tick()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == self._hvac_mode:
            return
        self._hvac_mode = hvac_mode
        self._want_sleep_since = self._want_wake_since = None
        if hvac_mode == HVACMode.OFF:
            self._cancel_autotune("cancelled: thermostat turned off")
            if not self.coordinator.is_sleeping:
                self._start_sleep_sequence()
        else:
            # Heat: the controller decides whether to wake the miner
            self._pid.reset_time()
        self.async_write_ha_state()
        await self._async_tick()

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_start_autotune(self) -> None:
        """Start relay auto-tuning between the min and max presets."""
        if self._hvac_mode != HVACMode.HEAT:
            self._tune_status = "failed: thermostat must be in heat mode"
            self._notify()
            return
        presets = self._allowed_presets()
        if len(presets) < 2 or self._target_temperature is None:
            self._tune_status = "failed: need at least two presets between min and max"
            self._notify()
            return
        self._tuner = RelayTuner(self._target_temperature, presets[0][1], presets[-1][1])
        self._tune_started = time.time()
        self._tune_sensor_lost_since = None
        self._tune_status = "running"
        _LOGGER.info("Auto-tune started around %s", self._target_temperature)
        self._notify()
        await self._async_tick()

    async def async_cancel_autotune(self) -> None:
        self._cancel_autotune("cancelled")
        self._notify()

    # ---- Control loop ----

    def _read_sensor(self) -> float | None:
        state = self.hass.states.get(self._sensor_entity)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    def _allowed_presets(self) -> list[tuple[str, float]]:
        """Presets between the configured min and max, lowest watts first."""
        presets = self.coordinator.profiles_by_watts()
        names = [name for name, _ in presets]
        lo = names.index(self._min_profile) if self._min_profile in names else 0
        hi = names.index(self._max_profile) if self._max_profile in names else len(names) - 1
        if lo > hi:
            lo, hi = hi, lo
        return presets[lo : hi + 1]

    def _notify(self) -> None:
        """Update this entity and the ones that mirror it (auto-tune switch)."""
        self.async_write_ha_state()
        self.coordinator.async_update_listeners()

    def _cancel_autotune(self, status: str) -> None:
        if self._tuner is not None:
            _LOGGER.info("Auto-tune %s", status)
            self._tuner = None
            self._tune_status = status

    async def _async_tick(self, _now: Any = None) -> None:
        if self._lock.locked():
            return
        async with self._lock:
            try:
                await self._control()
            finally:
                self.async_write_ha_state()

    async def _control(self) -> None:
        now = time.time()

        if self._sequence:
            await self._run_sequence()
            return

        if self._hvac_mode != HVACMode.HEAT:
            return

        if self._tuner is not None and now - self._tune_started > AUTOTUNE_TIMEOUT_SECONDS:
            self._cancel_autotune("failed: timed out after 12 h")
            self._notify()

        measured = self._read_sensor()
        if measured is None:
            if self._tuner is not None:
                self._tune_sensor_lost_since = self._tune_sensor_lost_since or now
                if now - self._tune_sensor_lost_since > AUTOTUNE_SENSOR_TIMEOUT_SECONDS:
                    self._cancel_autotune("failed: temperature sensor unavailable")
                    self._notify()
            return
        self._tune_sensor_lost_since = None

        if not self.coordinator.data or not self.coordinator.data.get("online"):
            return

        presets = self._allowed_presets()
        if not presets:
            return
        self._pid.output_max = presets[-1][1]

        if self._tuner is not None:
            watts = self._tuner.update(measured, now)
            self._pid.output = watts
            wanted = min(presets, key=lambda p: abs(p[1] - watts))[0]
            if self._tuner.done:
                await self._finish_autotune()
        else:
            watts = self._pid.update(self._target_temperature, measured, now)
            options: list[tuple[str | None, float]] = [(None, 0.0), *presets]
            wanted = min(options, key=lambda p: abs(p[1] - watts))[0]

        self._wanted = wanted
        sleeping = self.coordinator.is_sleeping

        if wanted is None:
            self._want_wake_since = None
            if not sleeping:
                self._want_sleep_since = self._want_sleep_since or now
                if now - self._want_sleep_since >= self._sleep_delay:
                    self._start_sleep_sequence()
                    await self._run_sequence()
            return

        self._want_sleep_since = None
        if sleeping:
            self._want_wake_since = self._want_wake_since or now
            # Auto-tune wakes immediately; it needs the heat
            if self._tuner is not None or now - self._want_wake_since >= self._sleep_delay:
                self._start_wake_sequence()
                await self._run_sequence()
            return
        self._want_wake_since = None
        self._state = STATE_RUNNING

        current = self.coordinator.data.get("config", {}).get("Profile")
        if wanted != current and now - self._last_change >= MIN_DWELL_SECONDS - DWELL_SLACK_SECONDS:
            _LOGGER.info(
                "Thermostat: %.1f° -> %d W requested, preset %s -> %s",
                measured,
                watts,
                current,
                wanted,
            )
            try:
                await self.coordinator.api.set_profile(wanted)
                self._last_change = now
                await self.coordinator.async_request_refresh()
            except StealthminerAPIError as err:
                _LOGGER.error("Thermostat: error setting preset %s: %s", wanted, err)

    async def _finish_autotune(self) -> None:
        tuner = self._tuner
        self._tuner = None
        if tuner is None:
            return
        if tuner.result is None:
            self._tune_status = f"failed: {tuner.error}"
            _LOGGER.warning("Auto-tune %s", self._tune_status)
            self._notify()
            return

        result = tuner.result
        self._tune_status = (
            f"done: kp={result['kp']} ki={result['ki']} "
            f"(period {result['period_minutes']} min, swing ±{result['amplitude']}°)"
        )
        _LOGGER.info("Auto-tune %s", self._tune_status)
        # Seed the integral with the average heat the room needed during the
        # tune; saved in the state so it survives the reload below
        self._pid.integral = min(tuner.mean_output, self._pid.output_max)
        self._pid.reset_time()
        self._notify()
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={
                **self._entry.options,
                CONF_KP: result["kp"],
                CONF_KI: result["ki"],
                CONF_KD: result["kd"],
            },
        )

    # ---- Sleep / wake sequences ----

    def _start_sleep_sequence(self) -> None:
        presets = self._allowed_presets()
        api = self.coordinator.api
        steps: list[
            tuple[str, Callable[[], Awaitable[Any]], Callable[[], bool] | None]
        ] = []
        if presets:
            lowest = presets[0][0]
            steps.append((f"set preset {lowest}", lambda: api.set_profile(lowest), None))
        steps += [
            (
                f"fans to {self._sleep_fan_speed}%",
                lambda: api.set_fan_speed(self._sleep_fan_speed),
                None,
            ),
            ("sleep", api.curtail_sleep, None),
        ]
        self._sequence = steps
        self._sequence_done_state = STATE_SLEEPING
        self._state = STATE_GOING_TO_SLEEP
        self._want_sleep_since = None

    def _start_wake_sequence(self) -> None:
        api = self.coordinator.api
        # Fans stay on the low manual speed while the boards warm up
        async def wake() -> None:
            await api.curtail_wakeup()
            self._woke_at = time.time()

        self._sequence = [
            ("wake", wake, None),
            ("fans to auto", lambda: api.set_fan_speed(FAN_SPEED_AUTO), self._ramp_done),
        ]
        self._sequence_done_state = STATE_RUNNING
        self._state = STATE_WAKING
        self._want_wake_since = None

    async def _run_sequence(self) -> None:
        """Run the remaining steps; a failed step is retried next tick."""
        while self._sequence:
            name, step, gate = self._sequence[0]
            if gate is not None:
                await self.coordinator.async_refresh()
                if not gate():
                    self._schedule_wait_poll()
                    return
            try:
                await step()
            except StealthminerAPIError as err:
                _LOGGER.error("Thermostat: %s failed, will retry: %s", name, err)
                return
            _LOGGER.info("Thermostat: %s", name)
            self._sequence.pop(0)

        if self._sequence_done_state is not None:
            self._state = self._sequence_done_state
            self._sequence_done_state = None
        await self.coordinator.async_request_refresh()

    def _ramp_done(self) -> bool:
        """True once the miner is hashing and has finished ramping after a wake.

        Before the ramp starts (about 25 s) the miner isn't hashing yet, and
        automatic fans would spin up then too, so hashing is required.
        """
        elapsed = time.time() - self._woke_at
        if elapsed > FAN_AUTO_FALLBACK_SECONDS:
            _LOGGER.warning("Thermostat: miner still ramping after %d s", elapsed)
            return True
        devs = (self.coordinator.data or {}).get("devs") or []
        hashing = any((d.get("MHS 5s") or 0) > 0 for d in devs)
        ramping = any(d.get("IsRamping") for d in devs)
        return hashing and not ramping

    def _schedule_wait_poll(self) -> None:
        """Check again soon, rather than waiting for the 60 s tick."""
        if self._cancel_wait_poll is not None:
            return

        @callback
        def _poll(_now: Any) -> None:
            self._cancel_wait_poll = None
            self.hass.async_create_task(self._async_tick())

        self._cancel_wait_poll = async_call_later(self.hass, WAIT_POLL_SECONDS, _poll)
