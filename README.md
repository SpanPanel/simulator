# SPAN Panel Simulator

A standalone eBus simulator that mimics real SPAN panel behavior: mDNS
discovery, bootstrap HTTP API, TLS certificate provisioning, and Homie v5
MQTT publishing. Designed for integration testing with Home Assistant and
the `span-panel` custom component.

## Quick Start (macOS)

```bash
# Prerequisites
brew install mosquitto

# Run
./scripts/run-local.sh

# Stop
./scripts/run-local.sh --stop

# Status
./scripts/run-local.sh --status
```

The script automatically:

- Creates a Python virtual environment and installs the package
- Generates TLS certificates (with the host LAN IP in the SAN)
- Starts Mosquitto with MQTTS on port 8883
- Starts the simulator with HTTP on port 80 and mDNS advertising
- Detects your LAN IP from `en0`/`en1`

No `sudo` required.

## Running with Docker (Linux only)

```bash
docker compose up --build
```

Container-based approaches on macOS do not work for this simulator.
Both Colima and Apple's native `container` runtime use VM-based
networking that prevents containers from obtaining real LAN IPs.
mDNS advertisement and port 80 binding require direct LAN access,
which only native execution (`run-local.sh`) or Linux Docker with
`macvlan` networking can provide.

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
| `HTTP_PORT` | `80` | Bootstrap HTTP server port |
| `BROKER_PORT` | `8883` | MQTTS broker port |
| `BROKER_HOST` | `localhost` | MQTT broker hostname |
| `BROKER_USERNAME` | `span` | MQTT credentials |
| `BROKER_PASSWORD` | `sim-password` | MQTT credentials |
| `CERT_DIR` | `/tmp/span-sim-certs` | TLS certificate directory |
| `ADVERTISE_ADDRESS` | auto-detected | IP to advertise via mDNS |
| `ADVERTISE_HTTP_PORT` | same as `HTTP_PORT` | Port advertised via mDNS |

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

circuits:
  - id: "kitchen_outlets"
    name: "Kitchen Outlets"
    tabs: [1, 2]
    energy_profile:
      mode: "consumer"
      power_range: [0.0, 1800.0]
      typical_power: 150.0
      power_variation: 0.3
    relay_behavior: "controllable"
    priority: "NEVER"
```

### Full Schema

```yaml
panel_config:
  serial_number: str        # Unique panel serial (e.g., "SPAN-SIM-001")
  total_tabs: int           # Breaker tab count (8, 32, 64)
  main_size: int            # Main breaker amps (100, 150, 200)

circuit_templates:          # Reusable template definitions
  template_name:
    energy_profile:
      mode: str             # "consumer" | "producer" | "bidirectional"
      power_range: [min, max]   # Watts (negative = production)
      typical_power: float      # Base power in watts
      power_variation: float    # Fraction (0.1 = +/-10%)
      efficiency: float         # 0.0-1.0 (optional)
    relay_behavior: str     # "controllable" | "non_controllable"
    priority: str           # "NEVER" | "SOC_THRESHOLD" | "OFF_GRID"
    device_type: str        # "circuit" | "evse" | "pv" (default: "circuit")

    # Optional behavioral modules
    cycling_pattern:
      on_duration: int      # Seconds on
      off_duration: int     # Seconds off

    time_of_day_profile:
      enabled: bool
      peak_hours: [int]           # Hours 0-23
      peak_multiplier: float
      off_peak_multiplier: float
      hourly_multipliers:         # Per-hour override
        6: 0.1
        12: 1.0
        18: 0.8

    smart_behavior:
      responds_to_grid: bool
      max_power_reduction: float  # 0.0-1.0

    battery_behavior:
      enabled: bool
      charge_power: float
      discharge_power: float
      idle_power: float
      charge_hours: [int]
      discharge_hours: [int]

circuits:
  - id: str                 # Unique identifier
    name: str               # Human-readable name
    template: str           # References a circuit_templates key
    tabs: [int]             # Tab positions ([1] = 120V, [1, 3] = 240V)
    overrides:              # Override any template field
      typical_power: 500.0

unmapped_tabs: [int]        # Tab numbers with no circuit assigned

simulation_params:
  update_interval: int          # Seconds between snapshots (default: 5)
  time_acceleration: float      # 1.0 = real-time, 2.0 = double speed
  noise_factor: float           # Random noise fraction (0.02 = +/-2%)
  enable_realistic_behaviors: bool
```

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
| `default_config.yaml` | `SPAN-SIM-40T-001` | 40 | 26 | Default: 2 SPAN Drives, battery, solar, full residential |
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
curl -X POST http://192.168.7.26/admin/reload

# List panels
curl http://192.168.7.26/admin/panels
```

## MQTT Topics

The simulator publishes Homie v5 messages on the eBus topic namespace:

```
ebus/5/{serial}/{node}/{property}
```

### Nodes

| Node | Description |
|---|---|
| `core` | Panel state: door, relay, voltages, grid status |
| `upstream-lugs` | Grid-facing: power, currents, energy |
| `downstream-lugs` | Load-facing: feedthrough power, currents |
| `{circuit-uuid}` | Per-circuit: relay, power, energy, priority |
| `bess-0` | Battery (if configured) |
| `pv-0` | Solar inverter (if configured) |
| `evse-0` | EV charger (if configured) |
| `power-flows` | Aggregated power flow sensors |

### Settable Properties

Control circuits by publishing to `/set` topics:

```bash
# Open a circuit relay
mosquitto_pub -t "ebus/5/SPAN-TEST-001/{uuid}/relay/set" -m "OPEN"

# Change shed priority
mosquitto_pub -t "ebus/5/SPAN-TEST-001/{uuid}/shed-priority/set" -m "OFF_GRID"
```

## Simulation Engine

### Power Calculation (per tick)

1. Check relay state (open = 0W)
2. Apply base power from `typical_power` with `power_variation` randomness
3. Modulate by time-of-day profile (if configured)
4. Apply cycling pattern on/off state (if configured)
5. Apply smart grid response (if configured)
6. Add noise (`noise_factor`)

### Energy Accumulation

Energy integrates over time in watt-hours:

```
delta_energy = power_watts * delta_seconds / 3600
```

Consumed and produced energy are tracked separately per circuit.

### Diffing

Only changed property values are republished each tick. Unchanged values
are not retransmitted.

## TLS Certificates

Certificates are generated automatically on first run and cached in the
cert directory. The server certificate SAN includes:

- `span-simulator` (hostname)
- `localhost`
- `127.0.0.1`
- The `ADVERTISE_ADDRESS` IP (if set)

If the host IP changes, certificates are automatically regenerated on the
next startup. Delete `.local/certs/` to force regeneration.

## Directory Layout

```
simulator/
  configs/                  # Panel YAML configurations
  scripts/
    run-local.sh            # macOS native (recommended)
    entrypoint.sh           # Docker entrypoint (Linux)
  src/span_panel_simulator/
    __main__.py             # CLI and entry point
    app.py                  # Multi-panel orchestrator
    panel.py                # Single panel lifecycle
    engine.py               # Power/energy simulation
    publisher.py            # Homie MQTT publisher (with diffing)
    bootstrap.py            # HTTP API server
    discovery.py            # mDNS advertisement
    certs.py                # TLS certificate generation
    models.py               # Snapshot and circuit dataclasses
  .local/                   # Runtime state (gitignored)
    certs/                  # Generated TLS certificates
    mosquitto/              # Mosquitto config and passwd
    pids/                   # Process ID files
```
