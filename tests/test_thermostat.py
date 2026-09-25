"""Integration tests for the Stealthminer thermostat, against a fake miner."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.stealthminer import climate as climate_mod
from custom_components.stealthminer.const import DOMAIN

DATA = json.loads((Path(__file__).parent / "fixtures" / "miner_data.json").read_text())
SENSOR = "sensor.room"


class FakeAPI:
    """Mimics StealthminerAPI; writes change shared state and are recorded."""

    def __init__(self, host, port=8080, **_):
        self.host, self.port = host, port
        self.state = copy.deepcopy(DATA)
        self.state["config"]["Profile"] = "low"
        self.calls: list[tuple] = []
        FakeAPI.last = self

    async def get_all_data(self):
        return self.state

    async def get_limits(self):
        return self.state["limits"]

    async def test_connection(self):
        return self.state["version"]

    async def get_config(self):
        return self.state["config"]

    async def set_profile(self, name, board=None):
        self.calls.append(("set_profile", name))
        self.state["config"]["Profile"] = name

    async def set_fan_speed(self, speed):
        self.calls.append(("fan", speed))

    async def curtail_sleep(self):
        self.calls.append(("sleep",))
        self.state["config"]["CurtailMode"] = "Sleep"

    async def curtail_wakeup(self, mode="safe"):
        self.calls.append(("wake",))
        self.state["config"]["CurtailMode"] = "None"
        # Startup: not hashing yet, not ramping
        self.set_dev(mhs=0, ramping=False)

    def set_dev(self, mhs, ramping):
        for d in self.state["devs"]:
            d["MHS 5s"], d["IsRamping"] = mhs, ramping


@pytest.fixture(autouse=True)
def auto_enable(enable_custom_integrations):
    yield


@pytest.fixture
def clock(monkeypatch):
    now = SimpleNamespace(t=1_000_000.0)
    monkeypatch.setattr(climate_mod, "time", SimpleNamespace(time=lambda: now.t))
    return now


async def setup(hass: HomeAssistant, options: dict | None = None):
    hass.config.units = __import__(
        "homeassistant.util.unit_system", fromlist=["US_CUSTOMARY_SYSTEM"]
    ).US_CUSTOMARY_SYSTEM
    hass.states.async_set(SENSOR, "66", {"unit_of_measurement": "°F", "device_class": "temperature"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": "192.0.2.10", "port": 8080},
        options={"scan_interval": 30, "temperature_entity": SENSOR, "max_profile": "default", **(options or {})},
        unique_id="192.0.2.10:8080",
    )
    entry.add_to_hass(hass)
    # Leftover Power Limit entity from the old version
    er.async_get(hass).async_get_or_create(
        "number", DOMAIN, "192.0.2.10_8080_power_limit", config_entry=entry
    )
    with patch("custom_components.stealthminer.StealthminerAPI", FakeAPI):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]
    return entry, coordinator, coordinator.thermostat, FakeAPI.last


async def tick(hass, thermostat, clock, minutes=1.0):
    clock.t += minutes * 60
    await thermostat._async_tick()
    await hass.async_block_till_done()


async def test_setup_entities(hass, clock):
    entry, coord, thermostat, api = await setup(hass)
    reg = er.async_get(hass)
    assert thermostat is not None
    assert reg.async_get_entity_id("number", DOMAIN, "192.0.2.10_8080_power_limit") is None
    assert reg.async_get_entity_id("climate", DOMAIN, "192.0.2.10_8080_thermostat")
    assert reg.async_get_entity_id("switch", DOMAIN, "192.0.2.10_8080_autotune")
    # 1 board: presets scaled to a third
    presets = coord.profiles_by_watts()
    assert presets[0][0] == "low" and round(presets[0][1]) == 273
    assert thermostat._allowed_presets()[-1][0] == "default"
    assert thermostat.hvac_mode == HVACMode.OFF
    assert thermostat.current_temperature == 66.0
    assert thermostat.target_temperature == 68.0


async def test_heats_when_cold_and_respects_dwell(hass, clock):
    entry, coord, thermostat, api = await setup(hass)
    await thermostat.async_set_hvac_mode(HVACMode.HEAT)
    await hass.async_block_till_done()
    # 2° cold -> P = 200 W -> nearest option is the lowest preset (273 W), already set
    assert api.calls == []
    hass.states.async_set(SENSOR, "62")
    await tick(hass, thermostat, clock, 3)
    # 6° cold -> ~600+ W
    assert api.calls and api.calls[-1][0] == "set_profile"
    first = api.calls[-1][1]
    assert first not in ("low", "default")
    hass.states.async_set(SENSOR, "55")
    await tick(hass, thermostat, clock, 1)  # within 2-minute dwell: no change
    assert api.calls[-1] == ("set_profile", first)
    await tick(hass, thermostat, clock, 1.5)
    assert api.calls[-1][1] != first
    assert thermostat.hvac_action == HVACAction.HEATING


async def test_sleep_and_wake_sequences(hass, clock):
    entry, coord, thermostat, api = await setup(hass)
    await thermostat.async_set_hvac_mode(HVACMode.HEAT)
    api.state["config"]["Profile"] = "170MHz"
    hass.states.async_set(SENSOR, "75")  # far too warm -> output < 0
    await tick(hass, thermostat, clock, 0.5)
    assert ("sleep",) not in api.calls  # sleep delay (1 min) not reached yet
    await tick(hass, thermostat, clock, 1.1)
    assert api.calls[-3:] == [("set_profile", "low"), ("fan", 20), ("sleep",)]
    assert thermostat.hvac_action == HVACAction.IDLE
    assert thermostat._state == "sleeping"

    api.calls.clear()
    hass.states.async_set(SENSOR, "60")  # cold again
    await tick(hass, thermostat, clock, 1)
    assert api.calls == []  # wake delay
    await tick(hass, thermostat, clock, 1.1)
    assert api.calls == [("wake",)]
    assert thermostat._state == "waking"
    assert thermostat._cancel_wait_poll is not None  # polling faster while waiting
    # Fans stay manual (and the preset lowest) until hashing and done ramping
    await tick(hass, thermostat, clock, 0.25)  # not hashing yet
    assert api.calls == [("wake",)]
    api.set_dev(mhs=3e6, ramping=True)
    await tick(hass, thermostat, clock, 0.25)
    assert api.calls == [("wake",)]
    api.set_dev(mhs=6e6, ramping=False)
    await tick(hass, thermostat, clock, 0.25)
    assert api.calls == [("wake",), ("fan", -1)]
    assert thermostat._state == "running"
    await tick(hass, thermostat, clock, 1)
    assert api.calls[-1][0] == "set_profile"


async def test_failed_step_is_retried(hass, clock):
    from custom_components.stealthminer.api import StealthminerAPIError

    entry, coord, thermostat, api = await setup(hass)
    await thermostat.async_set_hvac_mode(HVACMode.HEAT)
    real = api.set_fan_speed
    fails = {"n": 1}

    async def flaky(speed):
        if fails["n"]:
            fails["n"] -= 1
            raise StealthminerAPIError("busy")
        await real(speed)

    api.set_fan_speed = flaky
    await thermostat.async_set_hvac_mode(HVACMode.OFF)
    await hass.async_block_till_done()
    assert api.calls == [("set_profile", "low")]
    await tick(hass, thermostat, clock, 1)
    assert api.calls == [("set_profile", "low"), ("fan", 20), ("sleep",)]


async def test_off_sleeps_and_restores(hass, clock):
    entry, coord, thermostat, api = await setup(hass)
    await thermostat.async_set_hvac_mode(HVACMode.HEAT)
    await thermostat.async_set_temperature(temperature=70)
    await thermostat.async_set_hvac_mode(HVACMode.OFF)
    await hass.async_block_till_done()
    assert api.calls[-3:] == [("set_profile", "low"), ("fan", 20), ("sleep",)]
    state = hass.states.get("climate.antminer_thermostat")
    assert state.state == "off" and state.attributes["temperature"] == 70


async def test_options_flow(hass, clock):
    entry, coord, thermostat, api = await setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    keys = [str(k) for k in result["data_schema"].schema]
    for key in ("temperature_entity", "min_profile", "max_profile", "kp", "ki", "kd", "sleep_delay", "sleep_fan_speed"):
        assert key in keys, key
    with patch("custom_components.stealthminer.StealthminerAPI", FakeAPI):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"scan_interval": 30, "temperature_entity": SENSOR, "min_profile": "145MHz",
             "max_profile": "220MHz", "kp": 50, "ki": 1, "kd": 0, "sleep_delay": 2, "sleep_fan_speed": 25},
        )
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    new = hass.data[DOMAIN][entry.entry_id].thermostat
    assert [p for p, _ in new._allowed_presets()] == ["145MHz", "170MHz", "195MHz", "220MHz"]
    assert new._pid.kp == 50


async def test_autotune_switch(hass, clock):
    entry, coord, thermostat, api = await setup(hass)
    switch = "switch.antminer_thermostat_auto_tune"
    assert hass.states.get(switch).state == "unavailable"  # thermostat is off
    await thermostat.async_set_hvac_mode(HVACMode.HEAT)
    await hass.async_block_till_done()
    await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(switch).state == "on"
    assert api.calls[-1] == ("set_profile", "default")  # 66° < 68° - 0.3: max preset
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(switch).state == "off"
    assert "cancelled" in hass.states.get("climate.antminer_thermostat").attributes["autotune_status"]


async def test_autotune_completes_and_saves_gains(hass, clock):
    entry, coord, thermostat, api = await setup(hass)
    await thermostat.async_set_hvac_mode(HVACMode.HEAT)
    await thermostat.async_start_autotune()
    # Drive a synthetic oscillation: rise while heating, fall while cooling
    temp = 66.0
    with patch("custom_components.stealthminer.StealthminerAPI", FakeAPI):
        for _ in range(2000):
            t = hass.data[DOMAIN][entry.entry_id].thermostat
            if t is None or not t.autotune_active:
                break
            temp += 0.05 if api.state["config"]["Profile"] == "default" else -0.05
            hass.states.async_set(SENSOR, f"{temp:.2f}")
            await tick(hass, t, clock, 1)
        await hass.async_block_till_done()
    assert entry.options["kp"] != 100.0, entry.options
    assert entry.options["kd"] == 0.0
    new = hass.data[DOMAIN][entry.entry_id].thermostat
    assert new._pid.kp == entry.options["kp"]
    assert new._pid.integral > 0  # seeded from the tune's mean output


async def test_fans_auto_fallback_if_ramp_never_ends(hass, clock):
    entry, coord, thermostat, api = await setup(hass)
    await thermostat.async_set_hvac_mode(HVACMode.HEAT)
    hass.states.async_set(SENSOR, "75")
    await tick(hass, thermostat, clock, 0.5)
    await tick(hass, thermostat, clock, 1.1)
    hass.states.async_set(SENSOR, "60")
    await tick(hass, thermostat, clock, 1)
    await tick(hass, thermostat, clock, 1.1)
    assert api.calls[-1] == ("wake",)
    api.set_dev(mhs=3e6, ramping=True)
    await tick(hass, thermostat, clock, 4)
    assert api.calls[-1] == ("wake",)
    await tick(hass, thermostat, clock, 1.1)  # > 5 minutes since waking
    assert api.calls[-1] == ("fan", -1)
