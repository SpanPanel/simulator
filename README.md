# SPAN Panel Simulator

> **Work in Progress** — This project is under active development and may be
> relocated to a different repository or organization. APIs, configuration
> formats, and architectural decisions are subject to change. Do not depend
> on this repository URL as a stable reference.

A standalone eBus simulator that mimics real SPAN panel behavior: mDNS
discovery, bootstrap HTTP API, TLS certificate provisioning, and Homie v5
MQTT publishing.

Includes a web dashboard for real-time configuration, grid simulation,
and energy modeling.

![Dashboard overview — grid offline with load shedding, live power chart, entity list with relay status](docs/images/dashboard1.png)

![PV editor — solar production curve with geographic modeling and historical weather degradation](docs/images/dashboard2.png)

![PV editor — BESS Charge and Discharge Profile](docs/images/dashboard_battery.png)

## Quick Start (macOS)

```bash
# Prerequisites
brew install mosquitto uv

# Run
./scripts/run-local.sh

# Run with debug logging
./scripts/run-local.sh --debug

# Stop
./scripts/run-local.sh --stop

# Restart (stop + start)
./scripts/run-local.sh --restart

# Status
./scripts/run-local.sh --status
```

The script automatically:

- Creates a Python virtual environment via `uv` and installs the package
- Generates TLS certificates (with the host LAN IP in the SAN)
- Starts Mosquitto with MQTTS on port 18883
- Starts the simulator with HTTP on port 8081 and mDNS advertising
- Detects your LAN IP from `en0`/`en1`

No `sudo` required.

## Dashboard

The simulator runs a web dashboard on port 18080 (`http://localhost:18080`).

### Features

- **Panel config** — serial number, tab count, main breaker size, geographic
  location with geocoding search, SOC shed threshold
- **Simulation controls** — time-of-day slider, speed acceleration (1x-360x),
  grid online/offline toggle, islandable toggle
- **Live power chart** — real-time grid, solar, and battery power flows
- **Energy projection** — modeling view with weekly/monthly/annual energy
  estimates based on configured circuits, PV, and battery
- **Entity management** — add, edit, delete circuits with per-type editors:
  - **PV** — nameplate capacity, geographic sine-curve solar model, monthly
    weather degradation from Open-Meteo historical data
  - **Battery** — nameplate capacity (kWh), backup reserve %, charge mode
    (Custom / Solar Generation / Solar Excess), discharge presets, 24-hour
    charge/discharge/idle schedule
  - **EVSE** — charging schedule with presets (Peak Solar, Evening, Night)
    or custom start/duration, 24-hour visual timeline
  - **Circuits** — typical power, 24-hour usage profile with presets,
    HVAC type selector for circuits with cycling patterns (seasonal
    power modulation based on latitude and system type)
- **Grid simulation** — toggle grid online/offline to test backup behavior:
  - With battery: BESS becomes dominant power source, load shedding activates
  - Without battery: panel goes offline (all circuits dead)
  - Islandable toggle controls whether PV operates during grid outage
- **Load shedding** — per-circuit shed priority matching the Homie v2 schema:
  - `OFF_GRID` circuits shed immediately when grid disconnects
  - `SOC_THRESHOLD` circuits shed when battery SOC drops below threshold
  - `NEVER` circuits stay on as long as battery has power
  - User relay overrides take precedence over shedding
- **Relay control** — click status dots to toggle circuit relays; changes
  from the dashboard or the HA integration (via MQTT) are reflected in both
- **Dark mode** — system, light, or dark theme with localStorage persistence
- **File operations** — import/export YAML, load configs, clone, save & reload
- **Panel cloning** — clone a real SPAN panel's configuration via the HA
  integration (see [Panel Cloning](#panel-cloning) below)

### Theme

A theme selector in the header supports three modes:

| Mode | Behavior |
|---|---|
| **System** | Follows OS light/dark preference |
| **Light** | Forces light theme |
| **Dark** | Forces dark theme |

## Home Assistant Add-on

The simulator can run as an HA add-on (app) so users with the `span-panel`
integration can spin up a simulated panel directly in their HA environment.

1. Go to **Settings > Add-ons > Add-on Store** > three-dot menu >
   **Repositories**
2. Add `https://github.com/SpanPanel/simulator`
3. Install **SPAN Panel Simulator** from the store
4. Configure options (config file, tick interval, log level) and start

The add-on runs the simulator in a container with its own Mosquitto broker.
The `span-panel` integration discovers it via mDNS just like a real panel.

## Running with Docker (Linux only)

```bash
docker compose up --build
```

Container-based approaches on macOS do not work for this simulator.
Both Colima and Apple's native `container` runtime use VM-based
networking that prevents containers from obtaining real LAN IPs.
mDNS advertisement requires direct LAN access, which only native
execution (`run-local.sh`) or Linux Docker with `macvlan` networking
can provide.

## Environment Variables

All variables can also be passed as CLI arguments (use `--help` to see the
full list).

| Variable | Default | Description |
|---|---|---|
| `CONFIG_DIR` | `./configs` | Directory containing panel YAML configs |
| `CONFIG_NAME` | `default_config.yaml` | Specific config file to load (omit to use default) |
| `TICK_INTERVAL` | `1.0` | Seconds between simulation ticks |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `FIRMWARE_VERSION` | `spanos2/sim/01` | Reported firmware version |
| `HTTP_PORT` | `8081` | Bootstrap HTTP server port |
| `BROKER_PORT` | `18883` | MQTTS broker port |
| `BROKER_HOST` | `localhost` | MQTT broker hostname |
| `BROKER_USERNAME` | `span` | MQTT credentials |
| `BROKER_PASSWORD` | `sim-password` | MQTT credentials |
| `CERT_DIR` | `/tmp/span-sim-certs` | TLS certificate directory |
| `ADVERTISE_ADDRESS` | auto-detected | IP to advertise via mDNS |
| `ADVERTISE_HTTP_PORT` | same as `HTTP_PORT` | Port advertised via mDNS |
| `DASHBOARD_PORT` | `18080` | Dashboard web UI port |

## Panel Configuration

Each YAML file in the config directory defines one simulated panel. The
simulator scans the directory at startup and can hot-reload via the admin
API.

### Minimal Example

```yaml
panel_config:
  serial_number: "SPAN-TEST-001"
  total_tabs: 8
  main_size: 100

circuit_templates:
  kitchen:
    energy_profile:
      mode: "consumer"
      power_range: [0.0, 1800.0]
      typical_power: 150.0
      power_variation: 0.3
    relay_behavior: "controllable"
    priority: "NEVER"

circuits:
  - id: "kitchen_outlets"
    name: "Kitchen Outlets"
    template: "kitchen"
    tabs: [1, 3]

unmapped_tabs: [2, 4, 5, 6, 7, 8]

simulation_params:
  update_interval: 5
```

### Full Schema

```yaml
panel_config:
  serial_number: str        # Unique panel serial (e.g., "SPAN-SIM-001")
  total_tabs: int           # Breaker tab count (8, 32, 64)
  main_size: int            # Main breaker amps (100, 150, 200)
  latitude: float           # Degrees north (default: 37.7)
  longitude: float          # Degrees east (default: -122.4)
  time_zone: str            # IANA timezone (default: resolved from lat/lon)
  soc_shed_threshold: float # SOC % for SOC_THRESHOLD shedding (default: 20)

circuit_templates:          # Reusable template definitions
  template_name:
    energy_profile:
      mode: str             # "consumer" | "producer" | "bidirectional"
      power_range: [min, max]   # Watts (negative = production)
      typical_power: float      # Base power in watts
      power_variation: float    # Fraction (0.1 = +/-10%)
      efficiency: float         # 0.0-1.0 (optional, PV/battery)
      nameplate_capacity_w: float        # PV nameplate rating in watts
      initial_consumed_energy_wh: float  # Seed consumed energy (from clone)
      initial_produced_energy_wh: float  # Seed produced energy (from clone)
    relay_behavior: str     # "controllable" | "non_controllable"
    priority: str           # "NEVER" | "SOC_THRESHOLD" | "OFF_GRID"
    device_type: str        # "circuit" | "evse" | "pv" (default: "circuit")
    breaker_rating: int     # Amps (derived from power_range if not set)

    # Optional behavioral modules
    cycling_pattern:
      on_duration: int      # Seconds on (explicit mode)
      off_duration: int     # Seconds off (explicit mode)
      duty_cycle: float     # 0.0-1.0 — fraction of cycle spent on (from HA stats)
      period: int           # Total cycle length in seconds (default: 2700)
    hvac_type: str          # "central_ac" | "heat_pump" | "heat_pump_aux"
    monthly_factors:        # Month (1-12) -> multiplier (1.0 = peak month)
      1: 0.6                # Takes precedence over hvac_type seasonal model
      7: 1.0

    time_of_day_profile:
      enabled: bool
      peak_hours: [int]           # Hours 0-23
      hour_factors:               # Per-hour multiplier (0.0-1.0)
        0: 1.0
        6: 0.0
        18: 1.0
      hourly_multipliers:         # Alternate per-hour override
        6: 0.1
        12: 1.0

    smart_behavior:
      responds_to_grid: bool
      max_power_reduction: float  # 0.0-1.0

    battery_behavior:
      enabled: bool
      charge_mode: str            # "custom" | "solar-gen" | "solar-excess"
      nameplate_capacity_kwh: float  # Total battery capacity (default: 13.5)
      backup_reserve_pct: float      # Normal discharge floor % (default: 20)
      charge_efficiency: float       # 0.0-1.0 (default: 0.95)
      discharge_efficiency: float    # 0.0-1.0 (default: 0.95)
      charge_power: float
      discharge_power: float
      idle_power: float
      max_charge_power: float        # Used by solar-gen/solar-excess modes
      max_discharge_power: float
      charge_hours: [int]
      discharge_hours: [int]
      idle_hours: [int]

circuits:
  - id: str                 # Unique identifier
    name: str               # Human-readable name
    template: str           # References a circuit_templates key
    tabs: [int]             # Tab positions ([1] = 120V, [1, 3] = 240V)
    breaker_rating: int     # Per-circuit override (optional)
    overrides:              # Override any template field
      typical_power: 500.0

unmapped_tabs: [int]        # Tab numbers with no circuit assigned

simulation_params:
  update_interval: int          # Seconds between snapshots (default: 5)
  time_acceleration: float      # 1.0 = real-time, 2.0 = double speed
  noise_factor: float           # Random noise fraction (0.02 = +/-2%)
  enable_realistic_behaviors: bool

# Clone provenance (written by the clone pipeline)
panel_source:
  origin_serial: str        # Real panel's serial (immutable)
  host: str                 # IP or hostname of source panel
  passphrase: str | null    # Proximity code (null for door-bypass)
  last_synced: str          # ISO 8601 timestamp
```

### Shed Priority

Circuit shed priority controls backup behavior when the grid disconnects,
matching the Homie v2 schema (`shed-priority` property):

| Priority | Behavior |
|---|---|
| `NEVER` | Never shed — stays on as long as battery has power |
| `OFF_GRID` | Shed immediately when dominant power source leaves GRID |
| `SOC_THRESHOLD` | Shed when battery SOC drops below `soc_shed_threshold` |

The `soc_shed_threshold` in `panel_config` (default 20%) sets the SOC
percentage that triggers shedding for `SOC_THRESHOLD` circuits.

User relay overrides (from dashboard or MQTT) take precedence over
shedding — if a user closes a shed relay, shedding will not reopen it.

### Config Selection

By default, the simulator loads `default_config.yaml`. To use a different
config:

```bash
# Via environment variable
CONFIG_NAME=simple_test_config.yaml ./scripts/run-local.sh

# Via CLI argument
span-simulator --config simple_test_config.yaml
```

When no `--config` is specified and no `default_config.yaml` exists, all
YAML files in the config directory are loaded (one panel per file).

### Included Configs

| File | Serial | Tabs | Circuits | Description |
|---|---|---|---|---|
| `default_config.yaml` | `SPAN-SIM-40T-001` | 40 | 31 | Default: 2 SPAN Drives, battery, solar, full residential |
| `simple_test_config.yaml` | `SPAN-TEST-001` | 8 | 4 | Minimal test: lights, outlets, HVAC, solar |
| `simulation_config_32_circuit.yaml` | `SPAN-32-SIM-001` | 32 | 29 | Full residential with cycling, time-of-day, solar curves |

## Multi-Panel Limitations

The simulator can load multiple configs, but each panel shares the same
host IP and HTTP server. Since a real SPAN panel has its own IP, the
integration's discovery flow deduplicates panels that resolve to the same
address.

For true multi-panel simulation, assign separate IPs to the host:

```bash
# macOS — add an alias IP
sudo ifconfig en0 alias 192.168.7.27 255.255.255.0

# Run one simulator per IP
ADVERTISE_ADDRESS=192.168.7.26 CONFIG_DIR=./configs/panel1 ./scripts/run-local.sh
ADVERTISE_ADDRESS=192.168.7.27 CONFIG_DIR=./configs/panel2 ./scripts/run-local.sh
```

## HTTP API

### Bootstrap Endpoints (eBus v2)

These endpoints match the real SPAN panel's API exactly.

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/v2/status` | Panel identity (`serialNumber`, `firmwareVersion`) |
| `POST` | `/api/v2/auth/register` | Returns MQTT credentials and broker details |
| `GET` | `/api/v2/certificate/ca` | Self-signed CA certificate (PEM) |
| `GET` | `/api/v2/homie/schema` | Homie v5 property schema |

Query `/api/v2/status?serial=XXX` to target a specific panel when multiple
are loaded.

The `/register` endpoint accepts any `hopPassphrase` value.

### Admin Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/admin/reload` | Hot-reload configs (add/remove/update panels) |
| `GET` | `/admin/panels` | List all running panels |

```bash
# Reload after editing configs
curl -X POST http://192.168.7.26:8081/admin/reload

# List panels
curl http://192.168.7.26:8081/admin/panels
```

## MQTT Topics

The simulator publishes Homie v5 messages on the eBus topic namespace:

```
ebus/5/{serial}/{node}/{property}
```

### Nodes

| Node | Description |
|---|---|
| `core` | Panel state: door, relay, voltages, grid status, dominant power source |
| `upstream-lugs` | Grid-facing: power, currents, energy |
| `downstream-lugs` | Load-facing: feedthrough power, currents |
| `{circuit-uuid}` | Per-circuit: relay, power, energy, shed-priority |
| `bess-0` | Battery: SOC, grid-state, capacity |
| `pv-0` | Solar inverter: nameplate capacity |
| `evse-0` | EV charger: status, lock state, advertised current |
| `power-flows` | Aggregated: PV, battery, grid, site power |

### Settable Properties

Control circuits by publishing to `/set` topics:

```bash
# Open a circuit relay
mosquitto_pub -t "ebus/5/SPAN-TEST-001/{uuid}/relay/set" -m "OPEN"

# Change shed priority
mosquitto_pub -t "ebus/5/SPAN-TEST-001/{uuid}/shed-priority/set" -m "OFF_GRID"

# Change dominant power source (triggers load shedding)
mosquitto_pub -t "ebus/5/SPAN-TEST-001/core/dominant-power-source/set" -m "BATTERY"
```

Relay and priority changes made via MQTT are reflected in the dashboard
in real time.

## Panel Cloning

The simulator can clone a real SPAN panel's configuration over its eBus.
The HA SPAN integration triggers cloning via the Socket.IO channel
(`/v1/panel` namespace, `clone_panel` event), providing the target panel's
address and passphrase. The simulator handles the rest: authenticating,
scraping MQTT topics, translating to a simulator config, and hot-reloading.

### How it works

1. The HA integration discovers the simulator via mDNS and connects over Socket.IO
2. Integration sends a `clone_panel` event with the panel host, passphrase, and HA location
3. Simulator authenticates with the panel (`/api/v2/auth/register`, `/api/v2/certificate/ca`)
4. Simulator connects to the panel's MQTTS broker and collects all retained eBus topics
5. Simulator translates the `$description` and property values into a YAML config
6. Config is written to `{config_dir}/{serial}-clone.yaml`, location/timezone applied, and the simulator reloads

### Socket.IO contract

All events use the `/v1/panel` namespace. On connect, the server emits a
`protocol` event with `{"version": "1.0"}`.

#### `set_location`

Push HA's location to a running panel. Updates lat/lon/timezone in the
config and triggers a reload.

```json
// client sends
{"serial": "sim-TEST-001", "latitude": 37.78, "longitude": -121.96}
// server acks
{"status": "ok", "time_zone": "America/Los_Angeles"}
```

#### `clone_panel`

Clone a real panel's eBus into a simulator config.

```json
// client sends
{"host": "192.168.1.100", "passphrase": "panel-passphrase",
 "latitude": 37.78, "longitude": -121.96}
// server acks
{"status": "ok", "serial": "nj-2316-XXXX",
 "clone_serial": "sim-nj-2316-XXXX-clone",
 "filename": "nj-2316-XXXX-clone.yaml", "circuits": 16,
 "has_bess": true, "has_pv": true, "has_evse": false,
 "time_zone": "America/Los_Angeles"}
// server emits (after async reload completes)
clone_ready {}
// error ack
{"status": "error", "phase": "connecting",
 "message": "MQTTS connection refused: bad credentials"}
```

The ack returns immediately after the config is written. The server
then emits `clone_ready` to the same SID once the async reload
completes and the clone panel is registered. Clients that intend to
send `apply_usage_profiles` should wait for `clone_ready` rather than
sending immediately after the ack.

#### `apply_usage_profiles`

Merge HA-derived per-circuit usage profiles into a clone config.
Typically sent after `clone_ready`.

```json
// client sends
{"clone_serial": "sim-nj-2316-XXXX-clone",
 "profiles": {
   "clone_2": {
     "typical_power": 145.3,
     "power_variation": 0.45,
     "hour_factors": {"0": 0.15, "8": 0.65, "14": 1.0},
     "duty_cycle": 0.4,
     "monthly_factors": {"1": 0.6, "7": 1.0}
   }
 }}
// server acks
{"status": "ok", "templates_updated": 1}
```

All profile fields are optional per circuit. The simulator merges only
the fields present, preserving topology values (breaker_rating,
relay_behavior, priority, mode, power_range) untouched. String dict
keys from JSON are converted to int keys for YAML compatibility.
`typical_power` and `power_variation` are skipped for producer and
bidirectional circuits whose power is hardware-driven.

### What gets cloned

- Panel identity (`sim-{serial}-clone`), main breaker rating, panel size
- All circuits: name, tab position, breaker rating, relay behavior, priority
- Energy profile mode inferred from device feeds (PV -> producer, BESS -> bidirectional, EVSE -> bidirectional)
- Energy accumulators seeded from the panel's imported/exported energy values
- Battery behavior with sensible schedule defaults
- PV nameplate capacity and production profile
- EVSE night-charging time-of-day profile
- Source panel credentials stored in `panel_source` for on-demand refresh

### Usage profile import

The HA SPAN integration can derive per-circuit usage profiles from the
HA recorder's long-term statistics and deliver them to the simulator
via `apply_usage_profiles`. This replaces the clone's point-in-time
power readings with patterns grounded in actual household behavior:

- **typical_power** -- mean of hourly means over 30 days
- **power_variation** -- coefficient of variation (stddev/mean)
- **hour_factors** -- 24-hour shape normalized to peak = 1.0
- **duty_cycle** -- mean/max ratio (skipped if >= 0.8)
- **monthly_factors** -- 12-month seasonality (requires 3+ months of data)

The integration builds profiles before connecting, sends `clone_panel`,
waits for `clone_ready`, then sends `apply_usage_profiles` on the same
Socket.IO session.

The cloned config is a faithful starting point. Behavioral tuning
(profiles, cycling patterns, smart behavior) can be adjusted via the
dashboard after cloning.

## Simulation Models

### Solar

PV circuits use a geographic sine-based model instead of hourly multipliers:

- Sunrise/sunset computed from latitude, longitude, and date
- Solar elevation angle determines instantaneous production factor
- Daily weather degradation from Open-Meteo historical cloud cover data
- Falls back to deterministic seed-based weather when no API data available

### HVAC Seasonal Modulation

Circuits with `hvac_type` set automatically adjust power draw by season
using a latitude-aware sinusoidal temperature model:

| HVAC Type | Summer | Winter | Why |
|---|---|---|---|
| `central_ac` | Full compressor (~100%) | Blower fan only (~15%) | Gas furnace handles heating |
| `heat_pump` | Full compressor (~100%) | COP reduces draw (~45%) | Heat pump efficiency in cold |
| `heat_pump_aux` | Full compressor (~100%) | Aux strips exceed cooling (~140%) | Resistive backup below ~35F |

The seasonal factor scales the base power before cycling is applied, so
the on/off duty cycle remains unchanged while the power magnitude varies.

### Battery (BSEE)

The Battery Storage Energy Equipment tracks real state-of-energy by
integrating power over time:

- **Charging**: `SOE += power * dt * charge_efficiency`
- **Discharging**: `SOE -= power * dt / discharge_efficiency`
- **Backup reserve**: Normal discharge stops at `backup_reserve_pct`
  (default 20%); only grid-disconnect emergencies drain to the 5% hard floor
- **Charge modes**: Custom (hour-based schedule), Solar Generation (tracks
  PV curve), Solar Excess (surplus after loads)

### Load Shedding

When the grid goes offline (dominant power source changes from GRID):

1. `OFF_GRID` priority circuits: relay opened immediately
2. `SOC_THRESHOLD` priority circuits: relay opened when SOC < threshold
3. `NEVER` priority circuits: remain on
4. Battery covers the load deficit (consumption minus PV production)
5. PV continues operating if panel is islandable, otherwise zeroed

User relay overrides take precedence -- closing a shed relay keeps it on.

## Development

See [DEVELOPER.md](DEVELOPER.md) for setup, pre-commit hooks, simulation
engine internals, and directory layout.
