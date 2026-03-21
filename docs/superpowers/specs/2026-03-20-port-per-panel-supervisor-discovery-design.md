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
- Ports assigned in panel start order, released on stop
- With `host_network: true`, no port declaration needed in add-on `config.yaml`

#### BootstrapHttpServer changes

- Constructor takes `serial: str` and `firmware: str` (single panel, not a registry)
- `GET /api/v2/status` always returns the one panel, no `?serial=` routing
- `POST /api/v2/auth/register` always registers against the one panel
- Remove `register_panel()` / `unregister_panel()` methods
- Remove `/admin/panels` endpoint (dashboard already has panel listing)
- Remove `/admin/reload` endpoint (move to dashboard app)

#### PanelAdvertiser changes

- `register_panel()` takes a `port` parameter instead of using a shared `self._http_port`
- Each mDNS entry advertises its panel-specific port in both the SRV record and `httpPort` TXT property

#### SimulatorApp changes

- `_start_panel()` creates a `BootstrapHttpServer` for the panel, starts it on the next available port from the base
- `_stop_panel()` stops and cleans up the panel's HTTP server
- Track port assignments (serial -> port mapping)
- Pass per-panel port to `PanelAdvertiser.register_panel()`

#### Config / CLI changes

| File | Change |
|------|--------|
| `config.yaml` | Add `base_http_port` option (int, default 8081) |
| `__main__.py` | Replace `--http-port` with `--base-http-port` |
| `run.sh` | Pass `--base-http-port` from add-on config |

#### Shared resources (unchanged)

- TLS certificates: single CA + broker cert, shared
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
- Tracks returned `uuid` per serial for cleanup
- `unregister_panel(serial)` — DELETEs `http://supervisor/discovery/{uuid}`
- No-ops gracefully when not in add-on mode (no SUPERVISOR_TOKEN)

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
- Calls `detect_api_version(host, port=port)` to validate
- Aborts if panel already configured (match by serial via unique ID)
- Proceeds to confirmation/auth flow (same path as zeroconf)

No changes to `async_step_zeroconf`, `async_step_user`, `SpanPanelApi`, or `span-panel-api`.

### mDNS behavior

mDNS advertising stays active in all modes:
- **Standalone mode**: Primary discovery mechanism (works correctly with port-per-panel)
- **Add-on mode**: Still advertised but may not reach HA Core. Serves the `PanelBrowser` for discovering real hardware on the LAN. Supervisor Discovery is the reliable path.

## Files affected

### Simulator (this repo)

| File | Action |
|------|--------|
| `src/span_panel_simulator/bootstrap.py` | Simplify to single-panel |
| `src/span_panel_simulator/discovery.py` | Accept per-panel port in `register_panel()` |
| `src/span_panel_simulator/supervisor_discovery.py` | New — Supervisor Discovery client |
| `src/span_panel_simulator/app.py` | Per-panel HTTP servers, port tracking, Supervisor Discovery lifecycle |
| `src/span_panel_simulator/__main__.py` | `--base-http-port` replaces `--http-port` |
| `span_panel_simulator/config.yaml` | Add `base_http_port` option |
| `span_panel_simulator/run.sh` | Pass `--base-http-port` |

### Integration (span repo)

| File | Action |
|------|--------|
| `manifest.json` | Add `"hassio"` |
| `config_flow.py` | Add `async_step_hassio()` |

## Testing

- Standalone multi-panel: start 2+ panels, verify each gets a distinct port, mDNS entries advertise correct ports
- Add-on mode: verify Supervisor discovery POSTs are made on panel start, DELETEd on stop
- Integration hassio flow: verify `async_step_hassio` receives discovery config and proceeds through auth
- Regression: real panel discovery via mDNS and manual user entry unaffected
