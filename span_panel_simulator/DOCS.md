# SPAN Panel Simulator Add-on

Simulates a SPAN electrical panel within Home Assistant for development and testing. The simulator publishes Homie v5 MQTT topics, serves an HTTP bootstrap API, and advertises via mDNS — exactly like real hardware.

## How it works

The `span-panel` HA integration discovers the simulated panel through mDNS, connects to its MQTT broker, and creates entities with live-updating power values. No real SPAN hardware is needed.

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `config_file` | `span_simulator/default_config.yaml` | Path to simulation config (relative to `/config`) |
| `tick_interval` | `1.0` | Seconds between simulation updates |
| `log_level` | `INFO` | Logging verbosity |
| `advertise_address` | (auto-detected) | IP address to advertise via mDNS |
| `dashboard_enabled` | `true` | Enable the web dashboard |

## Port mappings

| Port | Service |
|------|---------|
| 18883 | MQTTS broker |
| 8081 | HTTP bootstrap API |
| 18080 | Web dashboard |

## Setup

1. Install this add-on
2. Place a simulation config YAML in `/config/span_simulator/`
3. Start the add-on
4. The `span-panel` integration will discover the simulated panel automatically

## Custom configs

Copy the default config from the simulator repository to `/config/span_simulator/default_config.yaml` and modify as needed. The config controls panel size, circuit names, solar/battery presence, and load profiles.
