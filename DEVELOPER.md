# Developer Guide

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (`brew install uv` on macOS)

## Setup

```bash
# Clone and enter the repo
git clone https://github.com/electrification-bus/simulator.git
cd simulator

# Create venv and install all dependencies (runtime + dev)
# uv reads pyproject.toml and uv.lock, creates .venv/ automatically
uv sync --group dev

# Install pre-commit hooks
uv run pre-commit install
```

That's it. The `.venv/` directory is created in the project root and
`uv.lock` pins exact versions for reproducible installs.

## Common Commands

```bash
# Run the simulator
uv run span-simulator

# Run tests
uv run pytest

# Lint + format
uv run ruff check --fix src/ tests/
uv run ruff format src/ tests/

# Type check
uv run mypy --strict src/span_panel_simulator/

# Add a new dependency
uv add <package>          # runtime
uv add --group dev <pkg>  # dev only
```

## Pre-commit Hooks

Every commit is validated by:

| Hook | What it checks |
|---|---|
| **ruff** | Lint rules (E, F, W, I, UP, B, SIM, TCH, RUF) with auto-fix |
| **ruff-format** | Consistent formatting |
| **mypy --strict** | Full strict type checking across all source files |
| **trailing-whitespace** | No trailing whitespace |
| **end-of-file-fixer** | Files end with a newline |
| **check-yaml** | Valid YAML syntax |
| **check-added-large-files** | Prevents accidental large file commits |

## Simulation Engine

### Power Calculation (per tick)

1. Apply relay and priority overrides (immediate effect)
2. Check relay state (open = 0W)
3. Apply base power from `typical_power` with `power_variation` randomness
4. Producers: geographic sine-curve solar model with weather degradation
5. Consumers: modulate by time-of-day profile / hour factors (if configured)
6. HVAC seasonal modulation (latitude-aware temperature model scales power by season)
7. Apply cycling pattern on/off state (if configured)
8. Apply battery charge/discharge schedule or solar charge mode (if configured)
9. Apply smart grid response (if configured)
10. Add noise (`noise_factor`)
11. Apply load shedding overlays (if grid offline with battery)

### Solar Model

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
  docs/images/              # Screenshots
  scripts/
    run-local.sh            # macOS native (recommended)
    entrypoint.sh           # Docker entrypoint (Linux)
  src/span_panel_simulator/
    __main__.py             # CLI and entry point
    app.py                  # Multi-panel orchestrator
    panel.py                # Single panel lifecycle
    engine.py               # Power/energy simulation
    circuit.py              # Per-circuit state and snapshot
    clock.py                # Simulation clock with acceleration
    bsee.py                 # Battery storage equipment (BESS/GFE)
    hvac.py                 # Seasonal HVAC power modulation
    solar.py                # Geographic sine-curve solar model
    weather.py              # Open-Meteo historical weather
    publisher.py            # Homie MQTT publisher (with diffing)
    bootstrap.py            # HTTP API server
    discovery.py            # mDNS advertisement
    certs.py                # TLS certificate generation
    models.py               # Snapshot dataclasses
    config_types.py         # YAML schema TypedDicts
    dashboard/              # Web dashboard (port 8080)
      routes.py             # HTMX route handlers
      config_store.py       # In-memory config state
      presets.py             # Profile and schedule presets
      defaults.py           # Entity type defaults
      solar.py              # Solar curve computation
      templates/            # Jinja2 templates
      static/               # CSS, JS (htmx, Chart.js, noUiSlider)
  .local/                   # Runtime state (gitignored)
    certs/                  # Generated TLS certificates
    mosquitto/              # Mosquitto config and passwd
    pids/                   # Process ID files
```
