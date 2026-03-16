# Developer Guide

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (`brew install uv` on macOS)

## Setup

```bash
# Clone and enter the repo
git clone https://github.com/SpanPanel/simulator.git
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

## Simulation Engine Internals

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

### Energy Accumulation

Energy integrates over time in watt-hours:

```
delta_energy = power_watts * delta_seconds / 3600
```

Consumed and produced energy are tracked separately per circuit.

### Diffing

Only changed property values are republished each tick. Unchanged values
are not retransmitted.

## Home Assistant Add-on (App)

### Directory naming matters

The `span_panel_simulator/` directory **must** match the `slug` field in
`config.yaml`. The HA Supervisor uses the directory name to identify the
add-on — renaming it will break discovery. If you need to change the slug,
update both the directory name and the `slug` field together.

### Build pipeline

The GitHub Actions workflow (`.github/workflows/build-addon.yaml`) builds
the Docker image from the **repo root** as the build context (not from the
add-on subdirectory). This is necessary because the Dockerfile needs access
to `pyproject.toml`, `src/`, and `mosquitto/` which live at the repo root.

The HA Supervisor would normally build from the add-on subdirectory, which
can't reach parent files — that's why we use the `image:` field to pull
pre-built images instead.

The workflow:
- Triggers on pushes to `main` that touch source, config, or workflow files
- Builds per-architecture images (amd64, aarch64) using the appropriate
  HA base image
- Pushes to `ghcr.io/SpanPanel/simulator/{arch}:{version}`

### Local testing

There are three ways to run the simulator locally, depending on what
you're testing:

**1. Native (recommended for development)**

Runs directly on the host with full mDNS visibility. Best for iterating
on simulator code and testing integration discovery.

```bash
./scripts/run-local.sh
```

**2. Docker container**

Builds and runs the same image that CI pushes to GHCR. Useful for
verifying the container works before pushing. HTTP, MQTTS, and the
dashboard are all reachable via the mapped ports. mDNS auto-discovery
won't work on macOS because all container runtimes (Colima, Docker
Desktop, OrbStack) run inside a Linux VM with NAT networking — the
multicast packets never reach the host LAN. The `span-panel`
integration can still connect by manual configuration instead of
zeroconf discovery. On Linux, Docker runs natively and mDNS works
with host networking.

```bash
# Build (use aarch64 base on Apple Silicon, amd64 on Intel/Linux)
docker build -f span_panel_simulator/Dockerfile \
  --build-arg BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.13-alpine3.21 \
  -t span-panel-simulator:local .

# Run
mkdir -p .local/addon-test
cat > .local/addon-test/options.json <<'EOF'
{
  "config_file": "span_simulator/default_config.yaml",
  "tick_interval": 1.0,
  "log_level": "INFO",
  "advertise_address": "",
  "dashboard_enabled": true
}
EOF

docker run --rm \
  -p 18883:18883 -p 8081:8081 -p 18080:18080 \
  -v $(pwd)/configs:/config/span_simulator \
  -v $(pwd)/.local/addon-test:/data \
  span-panel-simulator:local
```

**3. HA add-on**

Requires a Home Assistant instance running HA OS or a supervised install
(the Supervisor manages add-on containers). Add the repo URL as a custom
repository and install from the Add-on Store. This is the only way to
test the full add-on lifecycle (options UI, Supervisor image pull,
`/data/options.json` injection).

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
  repository.json            # HA add-on repository metadata
  configs/                   # Panel YAML configurations
  span_panel_simulator/      # HA add-on (dir name must match slug)
    config.yaml              # Add-on metadata, image ref, options schema
    build.yaml               # Per-architecture base images
    Dockerfile               # Used by CI (build context is repo root)
    run.sh                   # Container entry point
    DOCS.md                  # User-facing add-on documentation
    translations/en.yaml     # Option labels for HA UI
  .github/workflows/
    build-addon.yaml         # CI: build and push images to GHCR
  docs/images/               # Screenshots
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
