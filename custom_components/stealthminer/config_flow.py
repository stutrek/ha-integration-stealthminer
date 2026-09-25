"""Config flow for Exergy - Stealthminer integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import StealthminerAPI, StealthminerAPIError, StealthminerConnectionError
from .const import (
    DOMAIN,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    CONF_SCAN_INTERVAL,
    CONF_TEMPERATURE_ENTITY,
    CONF_MIN_PROFILE,
    CONF_MAX_PROFILE,
    CONF_KP,
    CONF_KI,
    CONF_KD,
    CONF_SLEEP_DELAY,
    CONF_SLEEP_FAN_SPEED,
    CONF_BACKUP_CLIMATE,
    DEFAULT_MAX_PROFILE,
    DEFAULT_KP,
    DEFAULT_KI,
    DEFAULT_KD,
    DEFAULT_SLEEP_DELAY,
    DEFAULT_SLEEP_FAN_SPEED,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
    }
)


class StealthminerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Exergy - Stealthminer."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._host: str | None = None
        self._port: int = DEFAULT_PORT
        self._miner_info: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._host = user_input[CONF_HOST]
            self._port = user_input.get(CONF_PORT, DEFAULT_PORT)

            # Test connection
            session = async_get_clientsession(self.hass)
            api = StealthminerAPI(
                host=self._host,
                port=self._port,
                session=session,
            )

            try:
                version_info = await api.test_connection()
                config_info = await api.get_config()

                self._miner_info = {
                    "model": version_info.get("Type", "Stealthminer"),
                    "hostname": config_info.get("Hostname", self._host),
                    "version": version_info.get("LUXminer", ""),
                }

            except StealthminerConnectionError:
                errors["base"] = "cannot_connect"
            except StealthminerAPIError:
                errors["base"] = "api_error"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

            if not errors:
                # Check if already configured
                await self.async_set_unique_id(f"{self._host}:{self._port}")
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=self._miner_info.get("hostname", self._host),
                    data={
                        CONF_HOST: self._host,
                        CONF_PORT: self._port,
                    },
                    options={
                        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> StealthminerOptionsFlowHandler:
        """Get the options flow for this handler."""
        return StealthminerOptionsFlowHandler()


def _gain_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(min=0, step="any", mode=selector.NumberSelectorMode.BOX)
    )


class StealthminerOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Stealthminer options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        profiles = [name for name, _ in coordinator.profiles_by_watts()] if coordinator else []

        schema: dict[Any, Any] = {
            vol.Optional(
                CONF_SCAN_INTERVAL,
                default=options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): vol.All(vol.Coerce(int), vol.Range(min=5, max=300)),
            vol.Optional(
                CONF_TEMPERATURE_ENTITY,
                description={"suggested_value": options.get(CONF_TEMPERATURE_ENTITY)},
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
            ),
        }

        if profiles:
            profile_selector = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=profiles, mode=selector.SelectSelectorMode.DROPDOWN
                )
            )
            min_default = options.get(CONF_MIN_PROFILE)
            if min_default not in profiles:
                min_default = profiles[0]
            max_default = options.get(CONF_MAX_PROFILE, DEFAULT_MAX_PROFILE)
            if max_default not in profiles:
                max_default = profiles[-1]
            schema[vol.Optional(CONF_MIN_PROFILE, default=min_default)] = profile_selector
            schema[vol.Optional(CONF_MAX_PROFILE, default=max_default)] = profile_selector

        schema.update(
            {
                vol.Optional(CONF_KP, default=options.get(CONF_KP, DEFAULT_KP)): _gain_selector(),
                vol.Optional(CONF_KI, default=options.get(CONF_KI, DEFAULT_KI)): _gain_selector(),
                vol.Optional(CONF_KD, default=options.get(CONF_KD, DEFAULT_KD)): _gain_selector(),
                vol.Optional(
                    CONF_SLEEP_DELAY,
                    default=options.get(CONF_SLEEP_DELAY, DEFAULT_SLEEP_DELAY),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=60, step=1, unit_of_measurement="min",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    CONF_SLEEP_FAN_SPEED,
                    default=options.get(CONF_SLEEP_FAN_SPEED, DEFAULT_SLEEP_FAN_SPEED),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=10, max=100, step=5, unit_of_measurement="%",
                        mode=selector.NumberSelectorMode.SLIDER,
                    )
                ),
                vol.Optional(
                    CONF_BACKUP_CLIMATE,
                    description={"suggested_value": options.get(CONF_BACKUP_CLIMATE)},
                ): selector.EntitySelector(selector.EntitySelectorConfig(domain="climate")),
            }
        )

        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema))
