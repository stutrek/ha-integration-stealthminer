"""Constants for the Exergy - Stealthminer integration."""
from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "stealthminer"

# Configuration
CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_TEMPERATURE_ENTITY: Final = "temperature_entity"
CONF_MIN_PROFILE: Final = "min_profile"
CONF_MAX_PROFILE: Final = "max_profile"
CONF_KP: Final = "kp"
CONF_KI: Final = "ki"
CONF_KD: Final = "kd"
CONF_SLEEP_DELAY: Final = "sleep_delay"
CONF_SLEEP_FAN_SPEED: Final = "sleep_fan_speed"
CONF_BACKUP_CLIMATE: Final = "backup_climate"

# Defaults
DEFAULT_PORT: Final = 8080
DEFAULT_SCAN_INTERVAL: Final = 5
# Profile list, ATM/fan settings and limits only change on a write, which forces a refresh
SLOW_DATA_INTERVAL: Final = timedelta(seconds=60)
DEFAULT_TIMEOUT: Final = 10
DEFAULT_MAX_PROFILE: Final = "default"
DEFAULT_KP: Final = 100.0  # W per degree
DEFAULT_KI: Final = 2.0  # W per degree-minute
DEFAULT_KD: Final = 0.0  # W per degree/minute
DEFAULT_SLEEP_DELAY: Final = 1  # minutes
DEFAULT_SLEEP_FAN_SPEED: Final = 20  # percent
FAN_SPEED_AUTO: Final = -1

# Profile wattages from the API are for a standard 3-board machine
STANDARD_BOARD_COUNT: Final = 3

# API Commands
CMD_VERSION: Final = "version"
CMD_SUMMARY: Final = "summary"
CMD_POWER: Final = "power"
CMD_TEMPS: Final = "temps"
CMD_FANS: Final = "fans"
CMD_POOLS: Final = "pools"
CMD_PROFILES: Final = "profiles"
CMD_ATM: Final = "atm"
CMD_CONFIG: Final = "config"
CMD_DEVS: Final = "devs"
CMD_DEVDETAILS: Final = "devdetails"
CMD_TEMPCTRL: Final = "tempctrl"
CMD_SESSION: Final = "session"
CMD_LOGON: Final = "logon"
CMD_LOGOFF: Final = "logoff"
CMD_ATMSET: Final = "atmset"
CMD_CURTAIL: Final = "curtail"
CMD_PROFILESET: Final = "profileset"
CMD_REBOOTDEVICE: Final = "rebootdevice"
CMD_RESETMINER: Final = "resetminer"
CMD_POWERTARGETSET: Final = "powertargetset"
CMD_LIMITS: Final = "limits"
CMD_FANSET: Final = "fanset"

# Units
UNIT_TERAHASH: Final = "TH/s"
UNIT_GIGAHASH: Final = "GH/s"
UNIT_WATTS_PER_TERAHASH: Final = "W/TH"
UNIT_RPM: Final = "RPM"

# Platforms
PLATFORMS: Final = [
    "sensor",
    "binary_sensor",
    "switch",
    "button",
    "select",
    "climate",
]
