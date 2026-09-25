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
from homeassistant.exceptions import HomeAssistantError
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
    CONF_BACKUP_CLIMATE,
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
from .pool_check import pool_address, pool_reachable

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
# While the backup heater runs, how often HA checks whether the pool is reachable
POOL_CHECK_INTERVAL_SECONDS = 30
# After a failed switch back (pool reachable but the miner couldn't connect),
# or when the pool address isn't known, how long until the miner is woken to try again
BACKUP_PROBE_INTERVAL_SECONDS = 10 * 60
# How long a woken miner gets to connect to its pool during a check
BACKUP_PROBE_TIMEOUT_SECONDS = 3 * 60
# After an ordinary wake, the pool connects before hashing starts (~25 s);
# don't count a missing pool as an outage until this long after waking
WAKE_POOL_GRACE_SECONDS = 60
AUTOTUNE_SENSOR_TIMEOUT_SECONDS = 10 * 60

STATE_RUNNING = "running"
STATE_SLEEPING = "sleeping"
STATE_GOING_TO_SLEEP = "going_to_sleep"
STATE_WAKING = "waking"
STATE_SWITCHING_TO_BACKUP = "switching_to_backup"
STATE_BACKUP = "backup_heating"
STATE_CHECKING_POOL = "checking_pool"

# Backup heater phases
BACKUP_SWITCHING = "switching"  # putting the miner to sleep, then backup on
BACKUP_ACTIVE = "active"  # backup heating, miner asleep or unreachable
BACKUP_MANUAL = "manual"  # backup heating because Backup Only was chosen

# Presets (HA's HVAC modes are a fixed list, so the heat sources are presets)
PRESET_AUTO = "Auto"  # miner, with the backup taking over during outages
PRESET_MINER_ONLY = "Miner Only"
PRESET_BACKUP_ONLY = "Backup Only"
BACKUP_PROBING = "probing"  # backup off, miner woken, waiting for the pool


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
        self._attr_preset_modes = None
        if options.get(CONF_BACKUP_CLIMATE):
            features |= ClimateEntityFeature.PRESET_MODE
            self._attr_preset_modes = [PRESET_AUTO, PRESET_MINER_ONLY, PRESET_BACKUP_ONLY]
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
        self._backup_entity: str | None = options.get(CONF_BACKUP_CLIMATE) or None

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

        self._backup_phase: str | None = None
        self._preset = PRESET_AUTO
        # Phase to enter once a switch to the backup has finished
        self._switch_target = BACKUP_ACTIVE
        self._next_probe = 0.0
        self._probe_started = 0.0
        # Pool addresses from the miner's config, kept for checks while it sleeps
        self._pool_addresses: list[tuple[str, int]] = []

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
            if last.attributes.get("preset_mode") in (self.preset_modes or []):
                self._preset = last.attributes["preset_mode"]
            if self._backup_entity and last.attributes.get("backup_phase"):
                if self._preset == PRESET_BACKUP_ONLY:
                    self._backup_phase = BACKUP_MANUAL
                else:
                    # The backup may still be heating; carry on and check the miner soon
                    self._backup_phase = BACKUP_ACTIVE
                    self._next_probe = time.time()
        if self._target_temperature is None:
            self._target_temperature = self._default_target

        self._update_pool_addresses()
        if self._backup_phase in (BACKUP_ACTIVE, BACKUP_MANUAL):
            self._state = STATE_BACKUP
        else:
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

    @callback
    def _handle_coordinator_update(self) -> None:
        """React to each miner status update rather than waiting for the 60 s tick.

        Switches to the backup as soon as an update shows the pool or miner
        gone, and turns the backup off as soon as a miner we lost comes back
        awake (it may already be mining).
        """
        data = self.coordinator.data or {}
        self._update_pool_addresses()
        if self._backup_entity and self._hvac_mode == HVACMode.HEAT and self._preset == PRESET_AUTO:
            miner_ok = data.get("online") and data.get("pool_connected")
            if (self._backup_phase is None and not miner_ok) or (
                self._backup_phase == BACKUP_ACTIVE
                and data.get("online")
                and not self.coordinator.is_sleeping
            ):
                self.hass.async_create_task(self._async_tick())
        super()._handle_coordinator_update()

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
        if (
            self._state in (STATE_SLEEPING, STATE_BACKUP, STATE_SWITCHING_TO_BACKUP)
            or self.coordinator.is_sleeping
        ):
            return HVACAction.IDLE
        return HVACAction.HEATING

    @property
    def current_temperature(self) -> float | None:
        return self._read_sensor()

    @property
    def target_temperature(self) -> float | None:
        return self._target_temperature

    @property
    def preset_mode(self) -> str | None:
        return self._preset if self._backup_entity else None

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
        if self._backup_entity:
            attrs["backup_heater"] = self._backup_entity
            attrs["backup_phase"] = self._backup_phase
            if self._backup_phase == BACKUP_ACTIVE:
                attrs["next_miner_check_in_s"] = max(0, int(self._next_probe - time.time()))
        return attrs

    # ---- Commands ----

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is not None:
            self._target_temperature = float(temp)
            self.async_write_ha_state()
            if self._backup_phase in (BACKUP_ACTIVE, BACKUP_MANUAL):
                try:
                    await self._backup_on()
                except HomeAssistantError as err:
                    _LOGGER.error("Thermostat: couldn't update backup heater: %s", err)
            await self._async_tick()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == self._hvac_mode:
            return
        self._hvac_mode = hvac_mode
        self._want_sleep_since = self._want_wake_since = None
        if hvac_mode == HVACMode.OFF:
            self._cancel_autotune("cancelled: thermostat turned off")
            steps = []
            if not self.coordinator.is_sleeping and self._miner_online():
                steps = self._sleep_steps()
            if self._backup_phase is not None:
                steps.append(("backup heater off", self._backup_off, None))
                self._backup_phase = None
            if steps:
                self._sequence = steps
                self._sequence_done_state = STATE_SLEEPING
                self._state = STATE_GOING_TO_SLEEP
        else:
            # Heat: the controller decides whether to wake the miner
            self._pid.reset_time()
            if self._preset == PRESET_BACKUP_ONLY:
                await self._switch_to_backup(time.time(), BACKUP_MANUAL)
        self.async_write_ha_state()
        await self._async_tick()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Choose the heat source. Switches always go miner off -> backup on, or reverse."""
        if preset_mode == self._preset:
            return
        self._preset = preset_mode
        if self._hvac_mode == HVACMode.HEAT:
            now = time.time()
            if preset_mode == PRESET_BACKUP_ONLY:
                await self._switch_to_backup(now, BACKUP_MANUAL)
            elif self._backup_phase is not None and (
                preset_mode == PRESET_MINER_ONLY or self._backup_phase == BACKUP_MANUAL
            ):
                # Backup off; the controller wakes the miner when heat is needed
                _LOGGER.info("Thermostat: %s: turning the backup heater off", preset_mode)
                self._sequence = [("backup heater off", self._backup_off, None)]
                self._sequence_done_state = STATE_SLEEPING
                self._state = STATE_SLEEPING
                self._backup_phase = None
                self._pid.reset_time()
        self._notify()
        await self._async_tick()

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_start_autotune(self) -> None:
        """Start relay auto-tuning between the min and max presets."""
        if self._hvac_mode != HVACMode.HEAT or self._preset == PRESET_BACKUP_ONLY:
            self._tune_status = "failed: thermostat must be heating with the miner"
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

        if self._hvac_mode == HVACMode.HEAT and self._backup_entity:
            if await self._backup_control(now):
                return

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
        self._sequence = self._sleep_steps()
        self._sequence_done_state = STATE_SLEEPING
        self._state = STATE_GOING_TO_SLEEP
        self._want_sleep_since = None

    def _sleep_steps(
        self,
    ) -> list[tuple[str, Callable[[], Awaitable[Any]], Callable[[], bool] | None]]:
        """Lowest preset, fans to a low manual speed, then sleep."""
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
        return steps

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
            except (StealthminerAPIError, HomeAssistantError) as err:
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

    def _schedule_wait_poll(self, delay: float = WAIT_POLL_SECONDS) -> None:
        """Check again soon, rather than waiting for the 60 s tick."""
        if self._cancel_wait_poll is not None:
            return

        @callback
        def _poll(_now: Any) -> None:
            self._cancel_wait_poll = None
            self.hass.async_create_task(self._async_tick())

        self._cancel_wait_poll = async_call_later(self.hass, delay, _poll)

    # ---- Backup heater ----

    def _update_pool_addresses(self) -> None:
        pools = (self.coordinator.data or {}).get("pools") or []
        addresses = [addr for p in pools if (addr := pool_address(p.get("URL") or ""))]
        if addresses:
            self._pool_addresses = addresses

    def _miner_online(self) -> bool:
        return bool((self.coordinator.data or {}).get("online"))

    async def _backup_control(self, now: float) -> bool:
        """Switch to and from the backup heater. Returns True if it handled this tick.

        The miner and the backup may share a circuit, so they never heat at
        the same time: the miner sleeps before the backup turns on, and the
        backup turns off before the miner wakes.
        """
        online = self._miner_online()

        if self._preset == PRESET_MINER_ONLY:
            return False
        if self._preset == PRESET_BACKUP_ONLY and self._backup_phase not in (
            BACKUP_SWITCHING,
            BACKUP_MANUAL,
        ):
            await self._switch_to_backup(now, BACKUP_MANUAL)
            return True
        if self._backup_phase == BACKUP_MANUAL:
            return True

        if self._backup_phase == BACKUP_SWITCHING:
            if self._sequence and not online and self._sequence[0][0] != "backup heater on":
                # Lost the miner mid-way; it can't be put to sleep, so skip to the backup
                self._sequence = [s for s in self._sequence if s[0] == "backup heater on"]
            await self._run_sequence()
            if not self._sequence:
                self._backup_phase = self._switch_target
                if self._backup_phase == BACKUP_ACTIVE:
                    self._schedule_wait_poll(max(1.0, self._next_probe - now))
            return True

        if self._backup_phase == BACKUP_ACTIVE:
            if online and not self.coordinator.is_sleeping:
                # A miner we couldn't reach is back and may be mining: backup off first
                _LOGGER.info("Thermostat: miner is back; turning the backup heater off")
                self._start_probe(now, wake=False)
                await self._run_sequence()
            elif online and now >= self._next_probe:
                if not self._pool_addresses:
                    _LOGGER.info("Thermostat: pool address unknown; waking the miner to check")
                    self._start_probe(now, wake=True)
                    await self._run_sequence()
                elif await pool_reachable(self._pool_addresses):
                    _LOGGER.info("Thermostat: pool is reachable again; switching back to the miner")
                    self._start_probe(now, wake=True)
                    await self._run_sequence()
                else:
                    self._next_probe = now + POOL_CHECK_INTERVAL_SECONDS
            if self._backup_phase == BACKUP_ACTIVE:
                self._schedule_wait_poll(max(1.0, self._next_probe - now))
            return True

        if self._backup_phase == BACKUP_PROBING:
            if self._sequence:
                await self._run_sequence()
                if self._sequence:
                    return True
            await self.coordinator.async_refresh()
            if self._miner_online() and (self.coordinator.data or {}).get("pool_connected"):
                _LOGGER.info("Thermostat: miner is mining again; back to normal control")
                self._backup_phase = None
                self._pid.reset_time()
                # Fans stay manual until the ramp is over, as after any wake
                self._sequence = [
                    ("fans to auto", lambda: self.coordinator.api.set_fan_speed(FAN_SPEED_AUTO), self._ramp_done),
                ]
                self._sequence_done_state = STATE_RUNNING
                self._state = STATE_WAKING
                await self._run_sequence()
            elif now - self._probe_started > BACKUP_PROBE_TIMEOUT_SECONDS:
                _LOGGER.info("Thermostat: miner still can't reach its pool")
                # Don't bounce straight back if HA can reach the pool but the miner can't
                await self._start_backup(now, retry_in=BACKUP_PROBE_INTERVAL_SECONDS)
            else:
                self._schedule_wait_poll()
            return True

        # Normal control: is the miner able to mine?
        pool_ok = (self.coordinator.data or {}).get("pool_connected")
        intentionally_asleep = self._state in (STATE_SLEEPING, STATE_GOING_TO_SLEEP)
        just_woke = (
            self._state == STATE_WAKING and now - self._woke_at < WAKE_POOL_GRACE_SECONDS
        )
        if online and (pool_ok or intentionally_asleep or just_woke):
            return False
        _LOGGER.warning(
            "Thermostat: miner %s; switching to the backup heater",
            "unreachable" if not online else "not connected to its pool",
        )
        await self._start_backup(now)
        return True

    async def _start_backup(self, now: float, retry_in: float | None = None) -> None:
        """Outage: switch to the backup and watch for the pool coming back."""
        if retry_in is None:
            retry_in = (
                POOL_CHECK_INTERVAL_SECONDS if self._pool_addresses else BACKUP_PROBE_INTERVAL_SECONDS
            )
        self._next_probe = now + retry_in
        await self._switch_to_backup(now, BACKUP_ACTIVE)
        if self._backup_phase == BACKUP_ACTIVE:
            self._schedule_wait_poll(retry_in)

    async def _switch_to_backup(self, now: float, target: str) -> None:
        """Miner to sleep (if it's awake and reachable), then backup on."""
        self._cancel_autotune("cancelled: switched to the backup heater")
        steps = []
        if self._miner_online() and not self.coordinator.is_sleeping:
            steps = self._sleep_steps()
        if self._backup_phase not in (BACKUP_ACTIVE, BACKUP_MANUAL):
            steps.append(("backup heater on", self._backup_on, None))
        self._sequence = steps
        self._sequence_done_state = STATE_BACKUP
        self._state = STATE_SWITCHING_TO_BACKUP
        self._backup_phase = BACKUP_SWITCHING
        self._switch_target = target
        self._want_sleep_since = self._want_wake_since = None
        await self._run_sequence()
        if not self._sequence:
            self._backup_phase = target

    def _start_probe(self, now: float, wake: bool) -> None:
        async def wake_miner() -> None:
            await self.coordinator.api.curtail_wakeup()
            self._woke_at = time.time()

        steps: list[tuple[str, Callable[[], Awaitable[Any]], Callable[[], bool] | None]] = [
            ("backup heater off", self._backup_off, None)
        ]
        if wake:
            steps.append(("wake", wake_miner, None))
        else:
            self._woke_at = time.time()
        self._sequence = steps
        self._sequence_done_state = STATE_CHECKING_POOL
        self._state = STATE_CHECKING_POOL
        self._backup_phase = BACKUP_PROBING
        self._probe_started = now

    async def _backup_on(self) -> None:
        entity = self._backup_entity
        await self.hass.services.async_call(
            "climate", "set_hvac_mode", {"entity_id": entity, "hvac_mode": HVACMode.HEAT}, blocking=True
        )
        await self.hass.services.async_call(
            "climate",
            "set_temperature",
            {"entity_id": entity, ATTR_TEMPERATURE: self._target_temperature},
            blocking=True,
        )

    async def _backup_off(self) -> None:
        await self.hass.services.async_call(
            "climate", "set_hvac_mode", {"entity_id": self._backup_entity, "hvac_mode": HVACMode.OFF}, blocking=True
        )
