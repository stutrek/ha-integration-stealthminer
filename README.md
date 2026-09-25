# Exergy - Stealthminer

[![HACS Badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/release/exergyheat/ha-integration-stealthminer.svg)](https://github.com/exergyheat/ha-integration-stealthminer/releases)
[![License](https://img.shields.io/github/license/exergyheat/ha-integration-stealthminer.svg)](LICENSE)

Home Assistant custom integration for monitoring and controlling Bitcoin miners running LuxOS firmware.

## Features

- **Real-time Monitoring**
  - Hashrate (5s, 1m, 15m, 30m, average)
  - Power consumption and efficiency (W/TH)
  - Board and chip temperatures
  - Fan speed and RPM
  - Pool connection status
  - Share statistics (accepted, rejected, stale)

- **Controls**
  - ATM (Auto-Tuning Mode) toggle
  - Sleep mode / Wake up
  - Profile selection
  - PID thermostat that heats a room using the miner, with auto-tuning
  - Reboot and reset buttons

- **Diagnostics**
  - System status
  - LuxOS version
  - Board and chip count
  - Curtail mode status

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three dots menu in the top right
3. Select "Custom repositories"
4. Add this repository URL: `https://github.com/exergyheat/ha-integration-stealthminer`
5. Select "Integration" as the category
6. Click "Add"
7. Search for "Stealthminer" and install it
8. Restart Home Assistant

### Manual Installation

1. Download the latest release from the [releases page](https://github.com/exergyheat/ha-integration-stealthminer/releases)
2. Extract and copy the `custom_components/stealthminer` folder to your Home Assistant `config/custom_components/` directory
3. Restart Home Assistant

## Configuration

1. Go to **Settings** > **Devices & Services**
2. Click **+ Add Integration**
3. Search for "Stealthminer"
4. Enter your miner's IP address and port (default: 4028)
5. Click **Submit**

## Requirements

- Home Assistant 2024.11.0 or newer
- Miner running LuxOS firmware with HTTP API enabled (port 4028)

## Entities

### Sensors
| Entity | Description |
|--------|-------------|
| Hashrate (5s/1m/15m/30m/avg) | Mining hashrate at various intervals |
| Power | Current power consumption in watts |
| Efficiency | Power efficiency in W/TH |
| Board Temperature | Maximum board temperature |
| Fan Speed | Average fan speed percentage |
| Fan RPM | Average fan RPM |
| Accepted/Rejected/Stale Shares | Share statistics |
| Active Pool | Currently connected pool URL |
| Current Profile | Active mining profile |
| System Status | Miner operational status |

### Binary Sensors
| Entity | Description |
|--------|-------------|
| Miner Online | Connection status |
| Pool Connected | Pool connection status |
| ATM Enabled | Auto-Tuning Mode status |
| Is Mining | Whether the miner is actively mining |

### Controls
| Entity | Description |
|--------|-------------|
| ATM Switch | Enable/disable Auto-Tuning Mode |
| Sleep Mode Switch | Put miner to sleep / wake up |
| Profile Select | Choose mining profile |
| Thermostat | Heat a room to a set temperature (see below) |
| Thermostat Auto-tune Switch | Work out the thermostat gains automatically |
| Reboot Button | Reboot the miner |
| Reset Miner Button | Reset the miner application |
| Wake Up Button | Wake the miner from sleep |

## Thermostat

To use the miner as a heater, open the integration's options (**Settings** > **Devices & Services** > **Stealthminer** > **Configure**) and pick a temperature sensor. A **Thermostat** entity then appears.

- A PID controller works out how many watts of heat the room needs, and the thermostat uses the nearest preset between the **min** and **max presets** you choose. Presets are ordered by estimated wattage, scaled to the number of hash boards installed.
- It waits at least 2 minutes between preset changes.
- When the room needs less heat than the min preset gives, for longer than the **sleep/wake delay**, the miner goes to sleep without spinning the fans up: it switches to the min preset, sets the fans to a low manual speed, then sleeps. Waking runs the reverse (wake, fans back to automatic), and the thermostat holds the min preset for 2 minutes before moving up.
- **Off** puts the miner to sleep the same way.

### Backup heater
Pick a climate entity as the **backup heater** to keep the room warm when the miner can't mine (the miner is unreachable, or it's awake but not connected to its pool, e.g. during an internet outage). The switch happens on the first status update that shows the problem.

The miner and the backup never heat at the same time, since they may share a circuit: the miner is put to sleep before the backup is set to Heat at the thermostat's setpoint. While the backup heats, Home Assistant checks every 30 seconds whether it can open a connection to the miner's pool (nothing is sent), so the miner isn't woken while the internet is still down. Once the pool is reachable, the thermostat turns the backup off, wakes the miner and gives it 3 minutes to connect. If it can't, the miner sleeps again, the backup comes back on, and the next try is 10 minutes later. If the pool address isn't known, the thermostat wakes the miner to check every 10 minutes instead. If an unreachable miner comes back awake, the backup is turned off on that status update.

A miner the thermostat put to sleep on purpose doesn't report a pool connection; that never triggers the backup.

With a backup heater configured, the thermostat has three presets for choosing the heat source:

| Preset | Heat source |
|--------|-------------|
| **Auto** | The miner, with the backup taking over during outages (as above) |
| **Miner Only** | The miner; the backup is never used |
| **Backup Only** | The backup; the miner is put to sleep first |

Switching presets always turns one heat source off before turning the other on. Leave the backup heater itself alone while the thermostat is heating; changing it directly could run both at once.

### Tuning
Gains are in watts: **Kp** is watts per degree off target, **Ki** is watts added per degree-minute, and **Kd** is usually left at 0.

Turn on **Thermostat Auto-tune** while the thermostat is in Heat mode to set them automatically. It alternates between the min and max presets around the setpoint, measures 3 temperature swings (typically a few hours), then saves PI gains to the options. Run it when heat is actually needed: if the min preset alone keeps the room above the setpoint, the tune stops with an error.

## Development

```bash
pip install -r requirements_test.txt
pytest
```

## Support

If you encounter any issues, please [open an issue](https://github.com/exergyheat/ha-integration-stealthminer/issues) on GitHub.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
