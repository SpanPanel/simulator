# Port-per-Panel Bootstrap + Supervisor Discovery

**Date**: 2026-03-20
**Status**: Approved
**Scope**: Simulator (this repo) + span_panel HA integration

## Problem

When the SPAN panel simulator runs as a Home Assistant add-on, mDNS advertisements never reach HA Core's zeroconf listener. Both run in Docker containers with `host_network: true`, but multicast from a co-resident process does not reliably trigger HA's singleton zeroconf instance. The span_panel integration cannot discover simulated panels in add-on mode.

A secondary problem: the simulator multiplexes multiple panels behind a single HTTP port, differentiated by serial query params. Real panels are always one-per-IP, so the integration assumes one panel per endpoint. This mismatch is unique to the simulator.

## Constraints

- Zero changes to existing `async_step_zeroconf` or `async_step_user` flows in the integration
- Real panel discovery via mDNS must be completely unaffected
- The integration already has full port support: `CONF_HTTP_PORT` in user schema, `httpPort` read from mDNS TXT records, port threaded through to `detect_api_version`, auth, and FQDN registration
- `span-panel-api` package's `SpanPanelClient` already accepts a `port` parameter

## Solution

Two coordinated changes:

1. **Port-per-panel bootstrap** (simulator): Each simulated panel gets its own `BootstrapHttpServer` on a dedicated port, matching the real hardware contract of one panel per endpoint.
2. **Supervisor Discovery** (simulator + integration): In add-on mode, the simulator uses the HA Supervisor Discovery API to notify the integration about available panels, bypassing mDNS entirely.

## Design

### 1. Simulator: Port-per-Panel Bootstrap

**Current state**: One `BootstrapHttpServer` instance on a single port. Multiple panels registered in a dict, routed by `?serial=` query param.

**New state**: Each panel gets its own `BootstrapHttpServer` instance on its own port. The server is simplified to single-panel (no registry, no serial routing).

#### Port allocation

- New config option `base_http_port` (default: 8081)
- First panel gets `base_http_port`, second gets `base_http_port + 1`, etc.
- Ports assigned sequentially from the base. When a panel stops, its port is released. New panels always take the lowest available port from the base to keep allocation dense and predictable.
- If a port bind fails (already in use), log a warning and skip to the next port. This handles conflicts with other services on the host.
- With `host_network: true`, no port declaration needed in add-on `config.yaml`

#### BootstrapHttpServer changes

Constructor signature becomes:

```python
def __init__(
    self,
    serial: str,
    firmware: str,
    certs: CertificateBundle,
    schema: HomieSchemaRegistry,
    *,
    broker_username: str = DEFAULT_BROKER_USERNAME,
    broker_password: str = DEFAULT_BROKER_PASSWORD,
    broker_host: str = "localhost",
    host: str = "0.0.0.0",
    port: int = 443,
) -> None:
```

- Single panel identity (serial + firmware) replaces the registry dict
- Shared resources (`certs`, `schema`, broker credentials) passed per-instance — same objects, just injected at construction
- `GET /api/v2/status` always returns the one panel, no `?serial=` routing
- `POST /api/v2/auth/register` always registers against the one panel
- Remove `register_panel()` / `unregister_panel()` methods
- Remove `/admin/panels` endpoint (dashboard already has panel listing via `DashboardContext.get_panel_configs`)
- Remove `/admin/reload` endpoint and `reload_callback` parameter — reload is already handled by `DashboardContext.request_reload` and the dashboard's own HTTP endpoint

#### PanelAdvertiser changes

- Remove `http_port` from constructor — there is no single shared port
- `register_panel()` takes a `port` parameter: `register_panel(serial, firmware, *, model, port)`
- Each mDNS entry advertises its panel-specific port in both the SRV record and `httpPort` TXT property

#### SimulatorApp changes

- Remove `self._advertise_http_port` — this single-port concept no longer applies with port-per-panel. Each panel's advertised port matches its actual HTTP port.
- `_start_panel()` creates a `BootstrapHttpServer` for the panel, starts it on the next available port from the base. Track the server and port on the `PanelInstance` or in a `serial -> (server, port)` map.
- `_stop_panel()` stops the panel's HTTP server and releases the port
- Pass per-panel port to `PanelAdvertiser.register_panel()`

#### Config / CLI changes

| File | Change |
|------|--------|
| `config.yaml` | Add `base_http_port` option (int, default 8081) |
| `__main__.py` | Add `--base-http-port`. Keep `--http-port` as a deprecated alias that maps to the same value, for backward compatibility with existing scripts and compose files. |
| `run.sh` | Pass `--base-http-port` from add-on config |
| `const.py` | Rename `HTTPS_PORT` to `DEFAULT_BASE_HTTP_PORT` (value stays 8081) |

#### Shared resources (unchanged)

- TLS certificates: single CA + broker cert, shared across all per-panel servers
- MQTT broker: single instance on port 18883
- Dashboard: single instance on port 18080

### 2. Supervisor Discovery

#### Simulator side

New `supervisor_discovery.py` module with `SupervisorDiscovery` class:

- Detects add-on mode via `SUPERVISOR_TOKEN` env var
- `register_panel(serial, host, port)` — POSTs to `http://supervisor/discovery`:
  ```json
  {
    "service": "span_panel",
    "config": {
      "host": "<advertise_address>",
      "port": 8081,
      "serial": "sim-001"
    }
  }
  ```
  The `"service"` value must match the integration's domain (`span_panel`) exactly — this is the coupling that routes discovery to the correct integration.
- The `host` value comes from the same `advertise_address` used for mDNS. In add-on mode, `run.sh` auto-detects this from the default gateway interface. With `host_network: true` on both the add-on and HA Core, this address is directly reachable.
- Tracks returned `uuid` per serial for cleanup
- `unregister_panel(serial)` — DELETEs `http://supervisor/discovery/{uuid}`
- **Error handling**: Supervisor API failures (401, 400, network errors) are logged as warnings and swallowed — panel startup must not be blocked by discovery registration failure. Same defensive pattern as `_load_recorder_data`. DELETE failures during cleanup are also logged and swallowed.
- No-ops gracefully when not in add-on mode (no SUPERVISOR_TOKEN)
- On startup, clean up any stale discovery entries from a prior run (the Supervisor may retain entries across add-on restarts)

Lifecycle integration in `SimulatorApp`:
- Instantiate `SupervisorDiscovery` alongside `PanelAdvertiser`
- In `_start_panel()`, after mDNS registration, call `supervisor_discovery.register_panel()`
- In `_stop_panel()`, call `supervisor_discovery.unregister_panel()`
- On shutdown, unregister all remaining panels

#### Integration side

**`manifest.json`**: Add `"hassio"` key to enable Supervisor discovery.

**`config_flow.py`**: New `async_step_hassio(self, discovery_info)`:
- Extracts `host`, `port`, `serial` from `discovery_info.config`
- Sets `self._http_port = port`
- Calls `detect_api_version(host, port=port)` to validate panel is reachable and v2
- Aborts if panel already configured (match by serial via unique ID)
- Proceeds to `async_step_confirm_discovery()` then auth — same confirmation and v2 passphrase/proximity flow that zeroconf uses. The user sees a discovery notification and confirms, then authenticates normally. No steps are skipped.

No changes to `async_step_zeroconf`, `async_step_user`, `SpanPanelApi`, or `span-panel-api`.

### mDNS behavior

mDNS advertising stays active in all modes:
- **Standalone mode**: Primary discovery mechanism (works correctly with port-per-panel)
- **Add-on mode**: Still advertised but may not reach HA Core. Serves the `PanelBrowser` for discovering real hardware on the LAN. Supervisor Discovery is the reliable path.

## Files affected

### Simulator (`src/span_panel_simulator/`)

| File | Action |
|------|--------|
| `bootstrap.py` | Simplify to single-panel, updated constructor |
| `discovery.py` | Accept per-panel `port` in `register_panel()`, remove `http_port` from constructor |
| `supervisor_discovery.py` | New — Supervisor Discovery client |
| `app.py` | Per-panel HTTP servers, port tracking, remove `advertise_http_port`, Supervisor Discovery lifecycle |
| `__main__.py` | Add `--base-http-port`, keep `--http-port` as deprecated alias |
| `const.py` | Rename `HTTPS_PORT` to `DEFAULT_BASE_HTTP_PORT` |

### Simulator (add-on metadata, `span_panel_simulator/`)

| File | Action |
|------|--------|
| `config.yaml` | Add `base_http_port` option |
| `run.sh` | Pass `--base-http-port` |

### Integration (span repo)

| File | Action |
|------|--------|
| `manifest.json` | Add `"hassio"` |
| `config_flow.py` | Add `async_step_hassio()` |

## Testing

### Happy path
- Standalone multi-panel: start 2+ panels, verify each gets a distinct port, mDNS entries advertise correct ports
- Add-on mode: verify Supervisor discovery POSTs are made on panel start, DELETEd on stop
- Integration hassio flow: verify `async_step_hassio` receives discovery config and proceeds through confirmation and auth
- Regression: real panel discovery via mDNS and manual user entry unaffected

### Edge cases
- Port conflict: bind `base_http_port` to another service before starting simulator, verify it skips to next port
- Supervisor API failure: mock 401/network error on POST /discovery, verify panel still starts with warning logged
- Panel restart: stop and re-start a panel, verify port is reused and discovery entry is re-registered
- Add-on restart: verify stale discovery entries from prior run are cleaned up on startup
- Rapid start/stop: start and stop multiple panels quickly, verify no port leaks or dangling discovery entries
