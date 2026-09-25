"""Switch platform for Stealthminer."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchDeviceClass
from homeassistant.components.climate import HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import StealthminerAPIError
from .climate import PRESET_BACKUP_ONLY
from .const import CONF_TEMPERATURE_ENTITY, DOMAIN
from .coordinator import StealthminerDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Stealthminer switches from a config entry."""
    coordinator: StealthminerDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        StealthminerATMSwitch(coordinator),
        StealthminerCurtailSwitch(coordinator),
    ]
    if entry.options.get(CONF_TEMPERATURE_ENTITY):
        entities.append(StealthminerAutoTuneSwitch(coordinator))

    async_add_entities(entities)


class StealthminerATMSwitch(CoordinatorEntity[StealthminerDataUpdateCoordinator], SwitchEntity):
    """Switch to control ATM (Auto-Tuning Mode)."""

    _attr_has_entity_name = True
    _attr_name = "ATM"
    _attr_icon = "mdi:auto-fix"

    def __init__(self, coordinator: StealthminerDataUpdateCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.api.host}_{coordinator.api.port}_atm_switch"
        self._attr_device_info = coordinator.device_info

    @property
    def is_on(self) -> bool | None:
        """Return true if ATM is enabled."""
        if not self.coordinator.data or not self.coordinator.data.get("online", False):
            return None

        atm = self.coordinator.data.get("atm", {})
        return atm.get("Enabled", False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on ATM."""
        try:
            await self.coordinator.api.set_atm(True)
            await self.coordinator.async_request_refresh()
        except StealthminerAPIError as err:
            _LOGGER.error("Error enabling ATM: %s", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off ATM."""
        try:
            await self.coordinator.api.set_atm(False)
            await self.coordinator.async_request_refresh()
        except StealthminerAPIError as err:
            _LOGGER.error("Error disabling ATM: %s", err)

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
            and self.coordinator.data.get("online", False)
        )


class StealthminerCurtailSwitch(CoordinatorEntity[StealthminerDataUpdateCoordinator], SwitchEntity):
    """Switch to control miner curtailment (sleep mode)."""

    _attr_has_entity_name = True
    _attr_name = "Sleep Mode"
    _attr_icon = "mdi:sleep"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator: StealthminerDataUpdateCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.api.host}_{coordinator.api.port}_curtail_switch"
        self._attr_device_info = coordinator.device_info

    @property
    def is_on(self) -> bool | None:
        """Return true if miner is in sleep mode (curtailed)."""
        if not self.coordinator.data or not self.coordinator.data.get("online", False):
            return None

        config = self.coordinator.data.get("config", {})
        curtail_mode = config.get("CurtailMode", "None")
        # Sleep mode is when CurtailMode is not "None"
        return curtail_mode != "None"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Put miner to sleep."""
        try:
            await self.coordinator.api.curtail_sleep()
            await self.coordinator.async_request_refresh()
        except StealthminerAPIError as err:
            _LOGGER.error("Error putting miner to sleep: %s", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Wake up miner."""
        try:
            await self.coordinator.api.curtail_wakeup()
            await self.coordinator.async_request_refresh()
        except StealthminerAPIError as err:
            _LOGGER.error("Error waking up miner: %s", err)

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data is not None
            and self.coordinator.data.get("online", False)
        )


class StealthminerAutoTuneSwitch(CoordinatorEntity[StealthminerDataUpdateCoordinator], SwitchEntity):
    """Switch that runs thermostat auto-tuning; turning it off cancels."""

    _attr_has_entity_name = True
    _attr_name = "Thermostat Auto-tune"
    _attr_icon = "mdi:tune-variant"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: StealthminerDataUpdateCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.api.host}_{coordinator.api.port}_autotune"
        self._attr_device_info = coordinator.device_info

    @property
    def is_on(self) -> bool:
        """Return true while auto-tuning is running."""
        thermostat = self.coordinator.thermostat
        return thermostat is not None and thermostat.autotune_active

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start auto-tuning."""
        if self.coordinator.thermostat is not None:
            await self.coordinator.thermostat.async_start_autotune()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Cancel auto-tuning."""
        if self.coordinator.thermostat is not None:
            await self.coordinator.thermostat.async_cancel_autotune()

    @property
    def available(self) -> bool:
        """Available while the thermostat is heating with the miner."""
        thermostat = self.coordinator.thermostat
        return (
            self.coordinator.last_update_success
            and thermostat is not None
            and thermostat.hvac_mode == HVACMode.HEAT
            and thermostat.preset_mode != PRESET_BACKUP_ONLY
        )
