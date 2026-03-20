# SPAN Panel Simulator

Simulates a SPAN electrical panel within Home Assistant for development,
testing, and energy modeling. The simulator publishes Homie v5 MQTT
topics, serves an HTTP bootstrap API, and advertises via mDNS — exactly
like real hardware. No real SPAN panel is needed.

## Getting started

1. Install and start this app
2. The `span-panel` integration will discover the simulated panel
   automatically via mDNS — just like a real SPAN panel
3. Open the simulator's web dashboard from the sidebar (or via the
   app's **Open Web UI** button) to configure the panel, manage
   entities, clone a real panel, and run energy modeling

A default panel configuration is included. You can also clone your
real SPAN panel's configuration directly from the dashboard.

## What you can do

- **Clone your real panel** — enter your panel's IP and passphrase in
  the dashboard to import its full circuit layout
- **Add virtual PV or Battery** — model what solar or battery storage
  would look like on your panel using actual recorded usage data
- **Energy modeling** — Before/After comparison charts showing grid
  consumption impact of adding BESS, with kWh savings over historical
  data
- **Recorder replay** — circuits replay actual power data from the HA
  recorder for realistic simulation
- **Grid simulation** — toggle grid online/offline to test backup
  behavior and load shedding

## Configuration options

| Option | Default | Description |
|--------|---------|-------------|
| `config_file` | `span_simulator/default_config.yaml` | Simulation config (relative to `/config`) |
| `tick_interval` | `1.0` | Seconds between simulation updates |
| `log_level` | `INFO` | Logging verbosity |
| `advertise_address` | (auto-detected) | IP to advertise via mDNS (leave blank for auto) |
| `dashboard_enabled` | `true` | Enable the web dashboard |

## Custom configs

Place simulation config YAML files in `/config/span_simulator/` and
set the `config_file` option to the filename. The config controls panel
size, circuit names, solar/battery presence, and load profiles. Or
simply clone your real panel from the dashboard — no manual config
needed.

## Full documentation

Dashboard features, energy modeling workflows, panel cloning, recorder
replay, and configuration reference:

**<https://github.com/SpanPanel/simulator#readme>**
