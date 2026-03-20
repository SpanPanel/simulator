# SPAN Panel Simulator

Simulates a SPAN electrical panel within Home Assistant for development,
testing, and energy modeling. The simulator publishes Homie v5 MQTT
topics, serves an eBus bootstrap API, and advertises via mDNS — exactly
like real hardware. No real SPAN panel is needed.

The simulator automatically connects to this HA instance for recorder
data and entity discovery — no manual URL or token configuration needed.

## Getting started

1. Install and start this app
2. The `span-panel` integration discovers the simulated panel
   automatically via mDNS — just like a real SPAN panel
3. Open the web dashboard via the **Open Web UI** button to configure
   the panel, manage entities, clone a real panel, and run energy
   modeling

A default panel configuration is included. You can also clone your
real SPAN panel's configuration directly from the dashboard.

## Dashboard

### Panel management

- **Multi-panel** — load multiple YAML configs, start/stop/restart
  individual panels
- **File operations** — import/export YAML, clone configs, save & reload
- **Panel cloning** — clone a real SPAN panel's configuration from
  the dashboard (enter the panel IP and passphrase)
- **Config persistence** — the simulator remembers the last running
  config across restarts

### Simulation controls

- **Time-of-day slider** — scrub through the day to see solar curves,
  time-of-day profiles, and battery schedules respond
- **Speed acceleration** — 1x to 360x time acceleration
- **Grid online/offline** — toggle to test backup behavior and load
  shedding
- **Islandable toggle** — controls whether PV operates during grid
  outage
- **Live power chart** — real-time grid, solar, and battery power flows

### Recorder replay

The simulator automatically replays recorded power data from this HA
instance's recorder for circuits with mapped entities. This grounds the
simulation in actual household usage patterns rather than synthetic
profiles.

Circuits with recorder data show a **REC** badge in the entity list.
Clicking the badge toggles to **SYN** (synthetic) mode, where the
simulator uses the configured power profile instead of recorded data.
Click again to switch back to recorder replay. This lets you compare
how well a synthetic profile matches your real usage, or override a
specific circuit while keeping the rest on recorded data.

### Energy modeling

The modeling view lets you answer "what if" questions about adding solar
or battery storage to your panel. Clone your real panel, then add or
modify PV and Battery entities to see the projected impact on your grid
consumption over historical data.

**Typical workflow:**

1. Clone your real SPAN panel from the dashboard
2. Circuits automatically replay actual recorded power data
3. Click **Model** on the running panel to enter the modeling view
4. The **Before** chart shows your site power as-is (loads minus any
   existing solar)
5. Add a Battery entity (or modify an existing one) — adjust capacity,
   charge/discharge schedule, and backup reserve
6. The **After** chart immediately updates to show grid power with the
   BESS applied, along with kWh savings
7. Add or resize a PV entity to see how additional solar offsets your
   consumption in the Before chart
8. Experiment with different battery sizes, charge modes, and PV
   nameplate ratings — charts auto-refresh on every save

**Modeling controls:**

- **Horizon selector** — last month, 3 months, 6 months, or 1 year
- **Range zoom** — drag the slider to zoom into any time window
- **Circuit overlays** — check individual circuits in the entity list
  to overlay their power traces on both charts
- **Toggleable legend** — show/hide Solar and Battery traces
- **Energy summary** — net kWh with import/export breakdown and savings
  percentage

### Entity management

Add, edit, and delete circuits with specialized editors per type:

- **PV** — nameplate capacity, geographic sine-curve solar model,
  monthly weather degradation from Open-Meteo historical data
- **Battery** — nameplate capacity (kWh), backup reserve %, charge mode
  (Custom / Solar Generation / Solar Excess), discharge presets,
  24-hour charge/discharge/idle schedule
- **EVSE** — charging schedule with presets (Peak Solar, Evening, Night)
  or custom start/duration, 24-hour visual timeline
- **Circuits** — typical power, 24-hour usage profile with presets,
  HVAC type selector with seasonal power modulation

PV and Battery are singleton types — only one of each can exist per
panel. Recorder-sourced entities preserve their original panel settings
(priority, relay behavior) as read-only.

### Relay control and load shedding

- Click status dots to toggle circuit relays
- Changes from the dashboard or HA integration (via MQTT) are reflected
  in both directions
- Grid offline triggers load shedding by priority:
  `OFF_GRID` circuits shed immediately, `SOC_THRESHOLD` circuits shed
  when battery SOC drops below threshold, `NEVER` circuits stay on

### Theme

System, light, or dark theme via the header selector.

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

Dashboard features, configuration reference, MQTT topics, and
simulation engine internals:

**<https://github.com/SpanPanel/simulator#readme>**
