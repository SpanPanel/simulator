# SPAN Panel Simulator

Simulates a SPAN electrical panel within Home Assistant for development,
testing, and energy modeling. The simulator publishes Homie v5 MQTT
topics, serves an HTTP bootstrap API, and advertises via mDNS — exactly
like real hardware. No real SPAN panel is needed.

## Setup

1. Install this app
2. Start the app — a default panel config is included
3. The `span-panel` integration will discover the simulated panel
   automatically via mDNS
4. Open the web dashboard on port **18080** to configure the panel,
   manage entities, and run energy modeling

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

## Custom configs

Place simulation config YAML files in `/config/span_simulator/` and set
the `config_file` option to the filename. The config controls panel size,
circuit names, solar/battery presence, and load profiles.

## Full documentation

For dashboard features, energy modeling workflows, panel cloning,
recorder replay, and configuration details, see the
[project README](https://github.com/SpanPanel/simulator#readme).
