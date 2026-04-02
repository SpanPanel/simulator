"""Route handlers for the dashboard sub-application.

Handlers are intentionally thin: parse request, call store, render template.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import aiohttp
import aiohttp_jinja2
import yaml
from aiohttp import web

if TYPE_CHECKING:
    from pathlib import Path

    import multidict

    from span_panel_simulator.dashboard.context import DashboardContext

from datetime import UTC

from span_panel_simulator.dashboard.keys import (
    APP_KEY_DASHBOARD_CONTEXT,
    APP_KEY_PENDING_CLONES,
    APP_KEY_PRESET_REGISTRY,
    APP_KEY_RATE_CACHE,
    APP_KEY_STORE,
)
from span_panel_simulator.dashboard.modeling_config import resolve_modeling_config_filename
from span_panel_simulator.dashboard.presets import (
    PresetRegistry,
    is_random_days_preset,
    match_battery_preset,
)
from span_panel_simulator.ha_api.opower import (
    async_discover_opower,
    async_get_opower_cost,
)
from span_panel_simulator.rates.cost_engine import compute_costs
from span_panel_simulator.rates.openei import (
    OpenEIError,
    fetch_rate_detail,
    fetch_rate_plans,
    fetch_utilities,
)
from span_panel_simulator.solar import compute_solar_curve
from span_panel_simulator.weather import fetch_historical_weather, get_cached_weather

if TYPE_CHECKING:
    from span_panel_simulator.dashboard.config_store import ConfigStore
    from span_panel_simulator.rates.cache import RateCache

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecorderPurgeResult:
    """Outcome of best-effort HA recorder purge for a panel config."""

    status: Literal["purged", "none_found", "no_ha", "no_serial", "failed"]
    entity_count: int = 0


PRIORITIES = [
    "MUST_HAVE",
    "NICE_TO_HAVE",
    "NON_ESSENTIAL",
    "NEVER",
    "SOC_THRESHOLD",
    "OFF_GRID",
]
RELAY_BEHAVIORS = ["controllable", "non_controllable"]
ENTITY_TYPES = ["circuit", "pv", "evse"]
# Infrastructure types that should only appear once in a panel config.
_SINGLETON_TYPES = {"pv"}


def _available_entity_types(store: ConfigStore) -> list[str]:
    """Return entity types available for adding.

    Singleton types (pv) are excluded when one already exists.
    """
    existing = {e.entity_type for e in store.list_entities()}
    return [t for t in ENTITY_TYPES if t not in _SINGLETON_TYPES or t not in existing]


def _store(request: web.Request) -> ConfigStore:
    return request.app[APP_KEY_STORE]


def _rate_cache(request: web.Request) -> RateCache:
    return request.app[APP_KEY_RATE_CACHE]


def _ctx(request: web.Request) -> DashboardContext:
    return request.app[APP_KEY_DASHBOARD_CONTEXT]


def _presets(request: web.Request) -> PresetRegistry:
    return request.app[APP_KEY_PRESET_REGISTRY]


def _render(template: str, request: web.Request, context: dict[str, Any]) -> web.Response:
    """Render a Jinja2 template and return an HTML response."""
    body = aiohttp_jinja2.render_string(template, request, context)
    return web.Response(text=body, content_type="text/html")


def _available_configs(request: web.Request) -> list[str]:
    """Return sorted list of YAML config filenames in the config directory."""
    ctx = _ctx(request)
    files: list[str] = []
    for pattern in ("*.yaml", "*.yml"):
        files.extend(p.name for p in ctx.config_dir.glob(pattern))
    return sorted(set(files))


def _first_default_config(config_dir: Path) -> str | None:
    """Return the filename of the first default_* template, or None."""
    defaults = sorted(p.name for p in config_dir.glob("default_*.yaml"))
    return defaults[0] if defaults else None


def _all_panels(request: web.Request) -> list[dict[str, object]]:
    """Merge on-disk configs with running panel info.

    Returns list of {filename, serial, running, active} for every YAML on disk.
    Running panels get their serial from the engine; non-running show empty serial.
    """
    configs = _available_configs(request)
    ctx = _ctx(request)
    active_file = ctx.config_filter

    # Build lookup: filename -> serial for running panels
    running_map: dict[str, str] = {}
    for path, serial in ctx.get_panel_configs().items():
        running_map[path.name] = serial

    # Port lookup: serial -> port
    port_map = ctx.get_panel_ports()

    return [
        {
            "filename": fname,
            "serial": running_map.get(fname, ""),
            "running": fname in running_map,
            "active": fname == active_file,
            "is_default": fname.startswith("default_"),
            "port": port_map.get(running_map.get(fname, ""), 0),
        }
        for fname in configs
    ]


def _is_readonly(ctx: DashboardContext) -> bool:
    """Config is read-only when viewing a default template or no config."""
    f = ctx.config_filter
    return f is None or f.startswith("default_")


def _dashboard_context(request: web.Request) -> dict[str, Any]:
    """Build the full-page template context."""
    store = _store(request)
    ctx = _ctx(request)
    panel_source = store.get_panel_source()
    return {
        "panel_config": store.get_panel_config(),
        "sim_params": store.get_simulation_params(),
        "entities": store.list_entities(),
        "priorities": PRIORITIES,
        "relay_behaviors": RELAY_BEHAVIORS,
        "entity_types": _available_entity_types(store),
        "preset_labels": _presets(request).circuit_labels,
        "unmapped_tabs": store.get_unmapped_tabs(),
        "panel_source": panel_source,
        "origin_serial": store.get_origin_serial(),
        "ha_available": ctx.ha_client is not None,
        "clone_host": panel_source.get("host", "") if panel_source else "",
        "panels": _all_panels(request),
        "readonly": _is_readonly(ctx),
        "bess_config": store.get_bess_config(),
    }


def _presets_for_type(request: web.Request, entity_type: str) -> dict[str, str]:
    """Return the preset labels appropriate for the given entity type."""
    return _presets(request).presets_for_type(entity_type)


def _entity_list_context(request: web.Request, editing_id: str | None = None) -> dict[str, Any]:
    """Build the entity-list template context.

    When *editing_id* is set, the template renders that entity in edit
    mode and all others as collapsed rows.
    """
    store = _store(request)
    dash_ctx = _ctx(request)
    recorder_map = store.get_recorder_map()
    ctx: dict[str, Any] = {
        "entities": store.list_entities(),
        "entity_types": _available_entity_types(store),
        "editing_id": editing_id,
        "unmapped_tabs": store.get_unmapped_tabs(),
        "readonly": _is_readonly(dash_ctx),
        "restorable_templates": set(recorder_map.keys()),
    }
    if editing_id is not None:
        entity = store.get_entity(editing_id)
        ctx["editing_entity"] = entity
        ctx["editing_profile"] = store.get_entity_profile(editing_id)
        ctx["priorities"] = PRIORITIES
        ctx["relay_behaviors"] = RELAY_BEHAVIORS
        ctx["preset_labels"] = _presets_for_type(request, entity.entity_type)
        ctx["active_days"] = store.get_active_days(editing_id)
        if entity.entity_type == "pv":
            panel = store.get_panel_config()
            lat = panel.get("latitude", 37.7)
            lon = panel.get("longitude", -122.4)
            cached = get_cached_weather(lat, lon)
            if cached is not None:
                ctx["monthly_weather"] = cached.monthly_factors
                ctx["monthly_cloud"] = cached.monthly_cloud_cover
                ctx["weather_source"] = cached.source
            else:
                ctx["monthly_weather"] = None
        if entity.entity_type == "evse":
            schedule = store.get_evse_schedule(editing_id)
            ctx["evse_start"] = schedule["start"]
            ctx["evse_duration"] = schedule["duration"]
            ctx["evse_active_preset"] = schedule["preset"]
            ctx["evse_profile"] = schedule["profile"]
            ctx["evse_preset_labels"] = _presets(request).evse_labels
    return ctx


def _profile_context(request: web.Request, entity_id: str) -> dict[str, Any]:
    """Build the profile-editor template context."""
    store = _store(request)
    entity = store.get_entity(entity_id)
    return {
        "entity": entity,
        "profile": store.get_entity_profile(entity_id),
        "preset_labels": _presets_for_type(request, entity.entity_type),
        "active_days": store.get_active_days(entity_id),
    }


def _bess_card_context(
    request: web.Request,
    editing: bool = False,
    schedule: bool = False,
) -> dict[str, Any]:
    """Build the BESS card template context."""
    store = _store(request)
    ctx: dict[str, Any] = {
        "bess_config": store.get_bess_config(),
        "bess_editing": editing,
        "readonly": _is_readonly(_ctx(request)),
    }
    if schedule:
        battery_profile = store.get_battery_profile()
        ctx["bess_schedule"] = True
        ctx["battery_profile"] = battery_profile
        ctx["battery_preset_labels"] = _presets(request).battery_labels
        ctx["battery_charge_mode"] = store.get_battery_charge_mode()
        ctx["battery_active_preset"] = match_battery_preset(battery_profile)
        ctx["active_days"] = store.get_bess_active_days()
    return ctx


async def handle_get_openei_config(request: web.Request) -> web.Response:
    """GET /rates/openei-config"""
    config = _rate_cache(request).get_openei_config()
    return web.json_response({"api_url": config.api_url, "api_key": config.api_key})


async def handle_put_openei_config(request: web.Request) -> web.Response:
    """PUT /rates/openei-config"""
    body = await request.json()
    api_url = body.get("api_url", "").strip()
    api_key = body.get("api_key", "").strip()
    if not api_url or not api_key:
        return web.json_response({"error": "api_url and api_key are required"}, status=400)
    _rate_cache(request).set_openei_config(api_url, api_key)
    return web.json_response({"ok": True})


async def handle_get_utilities(request: web.Request) -> web.Response:
    """GET /rates/utilities?lat=&lon="""
    lat = request.query.get("lat")
    lon = request.query.get("lon")
    if lat is None or lon is None:
        return web.json_response({"error": "lat and lon are required"}, status=400)
    config = _rate_cache(request).get_openei_config()
    if not config.api_key:
        return web.json_response({"error": "OpenEI API key not configured"}, status=400)
    try:
        results = await fetch_utilities(float(lat), float(lon), config.api_url, config.api_key)
    except OpenEIError as e:
        return web.json_response({"error": str(e)}, status=502)
    return web.json_response(
        [{"utility_name": u.utility_name, "eia_id": u.eia_id} for u in results]
    )


async def handle_get_rate_plans(request: web.Request) -> web.Response:
    """GET /rates/plans?utility=&sector="""
    utility = request.query.get("utility")
    if not utility:
        return web.json_response({"error": "utility is required"}, status=400)
    sector = request.query.get("sector", "Residential")
    config = _rate_cache(request).get_openei_config()
    if not config.api_key:
        return web.json_response({"error": "OpenEI API key not configured"}, status=400)
    try:
        plans = await fetch_rate_plans(utility, config.api_url, config.api_key, sector)
    except OpenEIError as e:
        return web.json_response({"error": str(e)}, status=502)
    return web.json_response(
        [
            {
                "label": p.label,
                "name": p.name,
                "startdate": p.startdate,
                "enddate": p.enddate,
                "description": p.description,
            }
            for p in plans
        ]
    )


async def handle_fetch_rate(request: web.Request) -> web.Response:
    """POST /rates/fetch {label}"""
    body = await request.json()
    label = body.get("label", "").strip()
    if not label:
        return web.json_response({"error": "label is required"}, status=400)
    config = _rate_cache(request).get_openei_config()
    if not config.api_key:
        return web.json_response({"error": "OpenEI API key not configured"}, status=400)
    try:
        record = await fetch_rate_detail(label, config.api_url, config.api_key)
    except OpenEIError as e:
        return web.json_response({"error": str(e)}, status=502)
    cache = _rate_cache(request)
    cache.cache_rate(label, record)
    return web.json_response(
        {
            "label": label,
            "utility": record.get("utility", ""),
            "name": record.get("name", ""),
        }
    )


async def handle_refresh_rate(request: web.Request) -> web.Response:
    """POST /rates/refresh {label}"""
    body = await request.json()
    label = body.get("label", "").strip()
    if not label:
        return web.json_response({"error": "label is required"}, status=400)
    config = _rate_cache(request).get_openei_config()
    if not config.api_key:
        return web.json_response({"error": "OpenEI API key not configured"}, status=400)
    try:
        record = await fetch_rate_detail(label, config.api_url, config.api_key)
    except OpenEIError as e:
        return web.json_response({"error": str(e)}, status=502)
    _rate_cache(request).cache_rate(label, record)
    return web.json_response({"ok": True, "label": label})


async def handle_get_rates_cache(request: web.Request) -> web.Response:
    """GET /rates/cache"""
    return web.json_response(_rate_cache(request).list_cached_rates())


async def handle_get_current_rate(request: web.Request) -> web.Response:
    """GET /rates/current"""
    cache = _rate_cache(request)
    label = cache.get_current_rate_label()
    if label is None:
        return web.json_response({"label": None})
    entry = cache.get_cached_rate(label)
    if entry is None:
        return web.json_response({"label": label, "error": "cached record missing"})
    return web.json_response(
        {
            "label": label,
            "utility": entry.record.get("utility", ""),
            "name": entry.record.get("name", ""),
            "retrieved_at": entry.retrieved_at,
        }
    )


async def handle_put_current_rate(request: web.Request) -> web.Response:
    """PUT /rates/current {label}"""
    body = await request.json()
    label = body.get("label", "").strip()
    if not label:
        return web.json_response({"error": "label is required"}, status=400)
    _rate_cache(request).set_current_rate_label(label)
    return web.json_response({"ok": True})


async def handle_get_rate_detail(request: web.Request) -> web.Response:
    """GET /rates/detail/{label}"""
    label = request.match_info["label"]
    entry = _rate_cache(request).get_cached_rate(label)
    if entry is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(entry.record)


async def handle_get_rate_attribution(request: web.Request) -> web.Response:
    """GET /rates/attribution/{label}"""
    label = request.match_info["label"]
    entry = _rate_cache(request).get_cached_rate(label)
    if entry is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response(
        {
            "provider": entry.attribution.provider,
            "url": entry.attribution.url,
            "license": entry.attribution.license,
            "api_version": entry.attribution.api_version,
            "retrieved_at": entry.retrieved_at,
        }
    )


async def handle_get_opower_accounts(request: web.Request) -> web.Response:
    """GET /rates/opower-accounts — discover opower ELEC accounts from HA."""
    ctx = _ctx(request)
    if ctx.ha_client is None:
        return web.json_response([])
    try:
        accounts = await async_discover_opower(ctx.ha_client)
    except Exception:
        _LOGGER.exception("Failed to discover opower accounts")
        return web.json_response([])
    return web.json_response(
        [
            {
                "device_id": a.device_id,
                "utility_name": a.utility_name,
                "account_number": a.account_number,
                "cost_entity_id": a.cost_entity_id,
                "usage_entity_id": a.usage_entity_id,
            }
            for a in accounts
        ]
    )


async def handle_get_opower_account(request: web.Request) -> web.Response:
    """GET /rates/opower-account — get saved opower account."""
    account = _rate_cache(request).get_opower_account()
    if account is None:
        return web.json_response({"configured": False})
    return web.json_response({**account, "configured": True})


async def handle_put_opower_account(request: web.Request) -> web.Response:
    """PUT /rates/opower-account — save selected opower account."""
    body = await request.json()
    device_id = body.get("device_id", "").strip()
    if not device_id:
        return web.json_response({"error": "device_id is required"}, status=400)
    _rate_cache(request).set_opower_account(
        device_id=device_id,
        utility_name=body.get("utility_name", ""),
        account_number=body.get("account_number", ""),
        cost_entity_id=body.get("cost_entity_id", ""),
        usage_entity_id=body.get("usage_entity_id", ""),
    )
    return web.json_response({"ok": True})


def setup_routes(app: web.Application) -> None:
    """Register all dashboard routes."""
    # Full page
    app.router.add_get("/", handle_dashboard, name="dashboard")

    # HTMX partials
    app.router.add_get("/panel-config", handle_get_panel_config)
    app.router.add_put("/panel-config", handle_put_panel_config)
    app.router.add_get("/sim-params", handle_get_sim_params)
    app.router.add_put("/sim-params", handle_put_sim_params)
    app.router.add_get("/entities", handle_get_entities)
    app.router.add_post("/entities", handle_add_entity)
    app.router.add_post("/entities/from-tabs", handle_add_entity_from_tabs)
    app.router.add_get("/entities/{id}/edit", handle_get_entity_edit)
    app.router.add_put("/entities/{id}", handle_put_entity)
    app.router.add_delete("/entities/{id}", handle_delete_entity)
    app.router.add_get("/entities/{id}/profile", handle_get_profile)
    app.router.add_put("/entities/{id}/profile", handle_put_profile)
    app.router.add_post("/entities/{id}/profile/preset", handle_apply_preset)

    # Active days (auto-save on toggle)
    app.router.add_put("/entities/{id}/active-days", handle_put_active_days)

    # BESS (panel-level)
    app.router.add_get("/bess", handle_get_bess)
    app.router.add_get("/bess/edit", handle_get_bess_edit)
    app.router.add_put("/bess", handle_put_bess)
    app.router.add_get("/bess/schedule", handle_get_bess_schedule)
    app.router.add_put("/bess/schedule", handle_put_bess_schedule)
    app.router.add_post("/bess/schedule/preset", handle_post_bess_schedule_preset)
    app.router.add_put("/bess/charge-mode", handle_put_bess_charge_mode)
    app.router.add_put("/bess/active-days", handle_put_bess_active_days)

    # EVSE schedule
    app.router.add_get("/entities/{id}/evse-schedule", handle_get_evse_schedule)
    app.router.add_put("/entities/{id}/evse-schedule", handle_put_evse_schedule)
    app.router.add_post("/entities/{id}/evse-schedule/preset", handle_apply_evse_preset)

    # Solar curve JSON
    app.router.add_get("/solar-curve", handle_solar_curve)
    app.router.add_get("/pv-curve-data", handle_pv_curve_data)

    # Geocoding proxy
    app.router.add_get("/geocode", handle_geocode)

    # Weather data
    app.router.add_get("/fetch-weather", handle_fetch_weather)

    # Live simulation data
    app.router.add_get("/power-summary", handle_power_summary)
    app.router.add_post("/set-sim-time", handle_set_sim_time)
    app.router.add_post("/set-acceleration", handle_set_acceleration)
    app.router.add_post("/set-grid-state", handle_set_grid_state)
    app.router.add_post("/set-grid-islandable", handle_set_grid_islandable)
    app.router.add_post("/entities/{id}/relay", handle_set_relay)
    app.router.add_post("/entities/{id}/toggle-replay", handle_toggle_replay)
    app.router.add_post("/entities/{id}/restore-recorder", handle_restore_recorder)

    # Energy projection
    app.router.add_get("/energy-projection", handle_energy_projection)

    # Modeling data (Before/After comparison)
    app.router.add_get("/modeling-data", handle_modeling_data)

    # File operations
    app.router.add_get("/export", handle_export)
    app.router.add_post("/import", handle_import)
    app.router.add_post("/load-config", handle_load_config)
    app.router.add_post("/clone", handle_clone)
    app.router.add_get("/clone-check", handle_clone_check)
    app.router.add_post("/save-reload", handle_save_reload)
    app.router.add_get("/check-dirty", handle_check_dirty)

    # Panel source provenance
    app.router.add_get("/panel-source", handle_get_panel_source)
    app.router.add_post("/sync-panel-source", handle_sync_panel_source)

    # Panels inventory (polling endpoint) and lifecycle controls
    app.router.add_get("/panels-list", handle_panels_list)
    app.router.add_post("/start-panel", handle_start_panel)
    app.router.add_post("/stop-panel", handle_stop_panel)
    app.router.add_post("/restart-panel", handle_restart_panel)
    app.router.add_post("/delete-config", handle_delete_config)
    app.router.add_post("/purge-recorder", handle_purge_recorder)

    # Clone from real panel + HA profile import
    app.router.add_post("/clone-from-panel", handle_clone_from_panel)
    app.router.add_get("/clone-panel-section", handle_clone_panel_section)
    app.router.add_post("/clone-confirm", handle_clone_confirm)
    app.router.add_post("/import-ha-profiles", handle_import_ha_profiles)

    # Panel discovery (mDNS + HA manifest)
    app.router.add_get("/discovered-panels", handle_discovered_panels)

    # Rate plan management
    app.router.add_get("/rates/openei-config", handle_get_openei_config)
    app.router.add_put("/rates/openei-config", handle_put_openei_config)
    app.router.add_get("/rates/utilities", handle_get_utilities)
    app.router.add_get("/rates/plans", handle_get_rate_plans)
    app.router.add_post("/rates/fetch", handle_fetch_rate)
    app.router.add_post("/rates/refresh", handle_refresh_rate)
    app.router.add_get("/rates/cache", handle_get_rates_cache)
    app.router.add_get("/rates/current", handle_get_current_rate)
    app.router.add_put("/rates/current", handle_put_current_rate)
    app.router.add_get("/rates/detail/{label}", handle_get_rate_detail)
    app.router.add_get("/rates/attribution/{label}", handle_get_rate_attribution)

    # Opower account management
    app.router.add_get("/rates/opower-accounts", handle_get_opower_accounts)
    app.router.add_get("/rates/opower-account", handle_get_opower_account)
    app.router.add_put("/rates/opower-account", handle_put_opower_account)


# -- Full page --


async def handle_dashboard(request: web.Request) -> web.Response:
    return _render("dashboard.html", request, _dashboard_context(request))


# -- Panel config --


async def handle_get_panel_config(request: web.Request) -> web.Response:
    return _render(
        "partials/panel_config.html",
        request,
        {"panel_config": _store(request).get_panel_config()},
    )


async def handle_put_panel_config(request: web.Request) -> web.Response:
    data = await request.post()
    _store(request).update_panel_config(dict(data))
    return _render(
        "partials/panel_config.html",
        request,
        {"panel_config": _store(request).get_panel_config()},
    )


# -- Simulation params --


async def handle_get_sim_params(request: web.Request) -> web.Response:
    return _render(
        "partials/sim_config.html",
        request,
        {"sim_params": _store(request).get_simulation_params()},
    )


async def handle_put_sim_params(request: web.Request) -> web.Response:
    data = await request.post()
    _store(request).update_simulation_params(dict(data))
    return _render(
        "partials/sim_config.html",
        request,
        {"sim_params": _store(request).get_simulation_params()},
    )


# -- Entities --


async def handle_get_entities(request: web.Request) -> web.Response:
    return _render("partials/entity_list.html", request, _entity_list_context(request))


async def handle_add_entity(request: web.Request) -> web.Response:
    data = await request.post()
    entity_type = str(data.get("entity_type", "circuit"))
    _store(request).add_entity(entity_type)
    return _render("partials/entity_list.html", request, _entity_list_context(request))


async def handle_add_entity_from_tabs(request: web.Request) -> web.Response:
    data = await request.post()
    tabs_raw = data.getall("tabs")
    tabs = [int(str(t)) for t in tabs_raw if str(t).strip()]
    if not tabs:
        return _render("partials/entity_list.html", request, _entity_list_context(request))
    try:
        entity = _store(request).add_entity_from_tabs(tabs)
    except ValueError as exc:
        ctx = _entity_list_context(request)
        ctx["unmapped_error"] = str(exc)
        return _render("partials/entity_list.html", request, ctx)
    return _render(
        "partials/entity_list.html",
        request,
        _entity_list_context(request, editing_id=entity.id),
    )


async def handle_get_entity_edit(request: web.Request) -> web.Response:
    entity_id = request.match_info["id"]
    return _render(
        "partials/entity_list.html",
        request,
        _entity_list_context(request, editing_id=entity_id),
    )


def _persist_config(request: web.Request) -> None:
    """Write current in-memory config to disk and reload the running engine.

    No-op when viewing a default template (read-only).
    """
    ctx = _ctx(request)
    filename = ctx.config_filter
    if not filename or filename.startswith("default_"):
        return
    output_path = ctx.config_dir / filename
    _store(request).save_to_file(output_path)
    _LOGGER.info("Config saved to %s", output_path)
    ctx.start_panel(filename)


async def handle_put_entity(request: web.Request) -> web.Response:
    entity_id = request.match_info["id"]
    data = await request.post()
    _store(request).update_entity(entity_id, dict(data))
    # Push priority change to the running engine immediately
    if "priority" in data:
        _ctx(request).set_circuit_priority(entity_id, str(data["priority"]))
    _persist_config(request)
    return _render(
        "partials/entity_list.html",
        request,
        _entity_list_context(request),
    )


async def handle_delete_entity(request: web.Request) -> web.Response:
    entity_id = request.match_info["id"]
    _store(request).delete_entity(entity_id)
    return _render("partials/entity_list.html", request, _entity_list_context(request))


# -- Profile --


async def handle_get_profile(request: web.Request) -> web.Response:
    entity_id = request.match_info["id"]
    return _render("partials/profile_editor.html", request, _profile_context(request, entity_id))


def _parse_active_days(data: multidict.MultiDictProxy[Any]) -> list[int] | None:
    """Parse day-of-week checkboxes from form data.

    Returns ``None`` when the day-picker wasn't part of the submitted form
    (no ``days_submitted`` sentinel), so callers can distinguish "not sent"
    from "all unchecked".
    """
    if "days_submitted" not in data:
        return None
    return [d for d in range(7) if data.get(f"day_{d}")]


async def handle_put_active_days(request: web.Request) -> web.Response:
    """Save active days on toggle — returns 204 No Content."""
    entity_id = request.match_info["id"]
    data = await request.post()
    active = _parse_active_days(data)
    if active is not None:
        _store(request).update_active_days(entity_id, active)
    return web.Response(status=204)


async def handle_put_profile(request: web.Request) -> web.Response:
    entity_id = request.match_info["id"]
    data = await request.post()
    multipliers: dict[int, float] = {}
    for h in range(24):
        key = f"hour_{h}"
        if key in data:
            multipliers[h] = float(str(data[key]))
    store = _store(request)
    store.update_entity_profile(entity_id, multipliers)
    active = _parse_active_days(data)
    if active is not None:
        store.update_active_days(entity_id, active)
    return _render("partials/profile_editor.html", request, _profile_context(request, entity_id))


async def handle_apply_preset(request: web.Request) -> web.Response:
    entity_id = request.match_info["id"]
    data = await request.post()
    preset_name = str(data.get("preset", "always_on"))
    month = int(str(data.get("month", "6")))
    day = int(str(data.get("day", "21")))
    start_hour = int(str(data.get("start_hour", "0")))
    end_hour = int(str(data.get("end_hour", "24")))
    _store(request).apply_preset(
        entity_id,
        preset_name,
        month,
        day,
        start_hour=start_hour,
        end_hour=end_hour,
        random_days=is_random_days_preset(preset_name),
    )
    return _render("partials/profile_editor.html", request, _profile_context(request, entity_id))


# -- BESS (panel-level) --


async def handle_get_bess(request: web.Request) -> web.Response:
    """GET /bess — return BESS card in display mode."""
    return _render("partials/bess_card.html", request, _bess_card_context(request))


async def handle_get_bess_edit(request: web.Request) -> web.Response:
    """GET /bess/edit — return BESS card in edit mode."""
    return _render("partials/bess_card.html", request, _bess_card_context(request, editing=True))


async def handle_put_bess(request: web.Request) -> web.Response:
    """PUT /bess — save BESS settings."""
    data = await request.post()
    _store(request).update_bess_config(dict(data))
    _persist_config(request)
    return _render("partials/bess_card.html", request, _bess_card_context(request))


async def handle_get_bess_schedule(request: web.Request) -> web.Response:
    """GET /bess/schedule — return BESS card with schedule editor."""
    return _render("partials/bess_card.html", request, _bess_card_context(request, schedule=True))


async def handle_put_bess_schedule(request: web.Request) -> web.Response:
    """PUT /bess/schedule — save BESS charge/discharge schedule."""
    data = await request.post()
    hour_modes: dict[int, str] = {}
    for h in range(24):
        key = f"hour_{h}"
        mode = str(data.get(key, "idle"))
        hour_modes[h] = mode if mode in ("charge", "discharge", "idle") else "idle"
    store = _store(request)
    store.update_battery_profile(hour_modes)
    active = _parse_active_days(data)
    if active is not None:
        store.update_bess_active_days(active)
    _persist_config(request)
    return _render("partials/bess_card.html", request, _bess_card_context(request, schedule=True))


async def handle_post_bess_schedule_preset(request: web.Request) -> web.Response:
    """POST /bess/schedule/preset — apply a schedule preset."""
    data = await request.post()
    preset_name = str(data.get("preset", "custom"))
    _store(request).apply_battery_preset(preset_name)
    _persist_config(request)
    return _render("partials/bess_card.html", request, _bess_card_context(request, schedule=True))


async def handle_put_bess_charge_mode(request: web.Request) -> web.Response:
    """PUT /bess/charge-mode — change BESS charge mode."""
    data = await request.post()
    mode = str(data.get("charge_mode", "custom"))
    _store(request).update_battery_charge_mode(mode)
    _persist_config(request)
    return _render("partials/bess_card.html", request, _bess_card_context(request, schedule=True))


async def handle_put_bess_active_days(request: web.Request) -> web.Response:
    """PUT /bess/active-days — update BESS active days."""
    data = await request.post()
    active = _parse_active_days(data)
    if active is not None:
        _store(request).update_bess_active_days(active)
    _persist_config(request)
    return _render("partials/bess_card.html", request, _bess_card_context(request, schedule=True))


# -- EVSE schedule --


def _evse_schedule_context(request: web.Request, entity_id: str) -> dict[str, Any]:
    """Build the EVSE schedule editor template context."""
    store = _store(request)
    entity = store.get_entity(entity_id)
    schedule = store.get_evse_schedule(entity_id)
    return {
        "e": entity,
        "entity": entity,
        "evse_start": schedule["start"],
        "evse_duration": schedule["duration"],
        "evse_active_preset": schedule["preset"],
        "evse_profile": schedule["profile"],
        "evse_preset_labels": _presets(request).evse_labels,
        "active_days": store.get_active_days(entity_id),
    }


async def handle_get_evse_schedule(request: web.Request) -> web.Response:
    entity_id = request.match_info["id"]
    return _render(
        "partials/evse_schedule.html",
        request,
        _evse_schedule_context(request, entity_id),
    )


async def handle_put_evse_schedule(request: web.Request) -> web.Response:
    entity_id = request.match_info["id"]
    data = await request.post()
    start = int(str(data.get("charge_start", "0")))
    duration = int(str(data.get("charge_duration", "6")))
    store = _store(request)
    store.update_evse_schedule(entity_id, start, duration)
    active = _parse_active_days(data)
    if active is not None:
        store.update_active_days(entity_id, active)
    return _render(
        "partials/evse_schedule.html",
        request,
        _evse_schedule_context(request, entity_id),
    )


async def handle_apply_evse_preset(request: web.Request) -> web.Response:
    entity_id = request.match_info["id"]
    data = await request.post()
    preset = str(data.get("preset", "night"))
    _store(request).apply_evse_preset(entity_id, preset)
    return _render(
        "partials/evse_schedule.html",
        request,
        _evse_schedule_context(request, entity_id),
    )


# -- Solar curve JSON --


async def handle_solar_curve(request: web.Request) -> web.Response:
    month = int(request.query.get("month", "6"))
    day = int(request.query.get("day", "21"))
    store = _store(request)
    lat = store.get_panel_config().get("latitude", 37.7)
    curve = compute_solar_curve(month, day, latitude=lat)
    return web.json_response(curve)


async def handle_pv_curve_data(request: web.Request) -> web.Response:
    """Return hourly PV production in watts for a given month.

    Applies nameplate capacity, geographic solar curve, efficiency, and
    the monthly weather degradation factor.  Used by the PV chart in
    the entity editor.
    """
    month = int(request.query.get("month", "6"))
    nameplate = float(request.query.get("nameplate", "5000"))
    efficiency = float(request.query.get("efficiency", "0.85"))

    store = _store(request)
    panel = store.get_panel_config()
    lat = panel.get("latitude", 37.7)
    lon = panel.get("longitude", -122.4)

    # Solar curve for mid-month
    curve = compute_solar_curve(month, 15, latitude=lat)

    # Weather factor for this month
    cached = get_cached_weather(lat, lon)
    weather = cached.monthly_factors.get(month, 0.85) if cached is not None else 1.0

    # Compute hourly production in watts (negative = production)
    hourly_watts: dict[str, float] = {}
    for h in range(24):
        factor = curve.get(h, 0.0)
        watts = round(nameplate * factor * efficiency * weather, 1)
        hourly_watts[str(h)] = watts

    return web.json_response(
        {
            "month": month,
            "nameplate_w": nameplate,
            "efficiency": efficiency,
            "weather_factor": round(weather, 4),
            "latitude": lat,
            "longitude": lon,
            "hourly_watts": hourly_watts,
        }
    )


# -- Geocoding proxy --


async def handle_geocode(request: web.Request) -> web.Response:
    """Proxy geocoding requests to Photon (OpenStreetMap-based, no API key)."""
    query = request.query.get("q", "").strip()
    if len(query) < 2:
        return web.json_response([])

    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(
                "https://photon.komoot.io/api/",
                params={"q": query, "limit": "8"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp,
        ):
            if resp.status != 200:
                return web.json_response([])
            data = await resp.json()
    except Exception:
        _LOGGER.debug("Geocoding request failed for query: %s", query)
        return web.json_response([])

    results: list[dict[str, str | float]] = []
    for feature in data.get("features", []):
        props = feature.get("properties", {})
        coords = feature.get("geometry", {}).get("coordinates", [])
        if len(coords) < 2:
            continue

        parts: list[str] = []
        for key in ("name", "city", "state", "country"):
            val = props.get(key, "")
            if val and val not in parts:
                parts.append(val)
        display = ", ".join(parts) if parts else "Unknown"

        results.append(
            {
                "lat": round(coords[1], 4),
                "lon": round(coords[0], 4),
                "display": display,
            }
        )

    return web.json_response(results)


# -- Weather data --


async def handle_fetch_weather(request: web.Request) -> web.Response:
    """Fetch historical cloud cover from Open-Meteo for the panel location."""
    store = _store(request)
    panel = store.get_panel_config()
    lat = float(request.query.get("lat", panel.get("latitude", 37.7)))
    lon = float(request.query.get("lon", panel.get("longitude", -122.4)))

    cached = get_cached_weather(lat, lon)
    if cached is not None:
        return web.json_response(
            {
                "monthly_cloud_cover": cached.monthly_cloud_cover,
                "monthly_factors": cached.monthly_factors,
                "years_averaged": cached.years_averaged,
                "source": cached.source,
                "summary": cached.display_summary,
            }
        )

    try:
        data = await fetch_historical_weather(lat, lon)
    except Exception:
        _LOGGER.warning(
            "Failed to fetch weather for (%.2f, %.2f)",
            lat,
            lon,
            exc_info=True,
        )
        return web.json_response(
            {"error": "Could not fetch weather data. Using deterministic model."},
            status=502,
        )

    return web.json_response(
        {
            "monthly_cloud_cover": data.monthly_cloud_cover,
            "monthly_factors": data.monthly_factors,
            "years_averaged": data.years_averaged,
            "source": data.source,
            "summary": data.display_summary,
        }
    )


# -- Live simulation data --


async def handle_power_summary(request: web.Request) -> web.Response:
    """Return current power flows from the running simulation."""
    ctx = _ctx(request)
    summary = ctx.get_power_summary()
    if summary is None:
        return web.json_response({"error": "No running simulation"}, status=503)
    return web.json_response(summary)


async def handle_set_sim_time(request: web.Request) -> web.Response:
    """Set the simulation clock to a specific ISO datetime."""
    data = await request.json()
    iso_str = data.get("time", "")
    if not iso_str:
        raise web.HTTPBadRequest(text="Missing 'time' field")
    ctx = _ctx(request)
    ctx.set_simulation_time(iso_str)
    return web.json_response({"ok": True, "time": iso_str})


async def handle_set_acceleration(request: web.Request) -> web.Response:
    """Set the time acceleration multiplier."""
    data = await request.json()
    accel = float(data.get("acceleration", 1.0))
    ctx = _ctx(request)
    ctx.set_time_acceleration(accel)
    return web.json_response({"ok": True, "acceleration": accel})


async def handle_set_grid_state(request: web.Request) -> web.Response:
    """Toggle the utility grid connection."""
    data = await request.json()
    online = bool(data.get("online", True))
    ctx = _ctx(request)
    ctx.set_grid_online(online)
    return web.json_response({"ok": True, "online": online})


async def handle_set_grid_islandable(request: web.Request) -> web.Response:
    """Toggle whether PV can operate during grid disconnection."""
    data = await request.json()
    islandable = bool(data.get("islandable", True))
    ctx = _ctx(request)
    ctx.set_grid_islandable(islandable)
    return web.json_response({"ok": True, "islandable": islandable})


async def handle_set_relay(request: web.Request) -> web.Response:
    """Toggle a circuit relay (OPEN/CLOSED)."""
    entity_id = request.match_info["id"]
    data = await request.json()
    relay_state = str(data.get("relay_state", "CLOSED"))
    if relay_state not in ("OPEN", "CLOSED"):
        raise web.HTTPBadRequest(text="relay_state must be OPEN or CLOSED")
    ctx = _ctx(request)
    ctx.set_circuit_relay(entity_id, relay_state)
    return web.json_response({"ok": True, "relay_state": relay_state})


async def handle_toggle_replay(request: web.Request) -> web.Response:
    """Toggle a circuit between recorder replay and synthetic mode.

    When going SYN → REC, fully restores the template from the
    snapshot (re-scrapes if needed).  REC → SYN just flips the flag.
    """
    entity_id = request.match_info["id"]
    store = _store(request)
    try:
        entity = store.get_entity(entity_id)
    except KeyError:
        raise web.HTTPNotFound(text=f"Entity not found: {entity_id}") from None

    if entity.user_modified:
        # SYN → REC: full restore
        if not store.restore_recorder(entity_id):
            await _rescrape_snapshots(request)
            store.restore_recorder(entity_id)
        _persist_config(request)
    else:
        # REC → SYN: flip the flag and persist so the engine matches the UI
        store.toggle_user_modified(entity_id)
        _persist_config(request)
    return _render("partials/entity_list.html", request, _entity_list_context(request))


async def handle_restore_recorder(request: web.Request) -> web.Response:
    """Restore a single entity to its original recorder state.

    Uses the snapshot if available.  Otherwise re-scrapes the source
    panel, rebuilds a fresh config, and extracts only the one template
    needed — no other templates are touched.
    """
    entity_id = request.match_info["id"]
    store = _store(request)

    if not store.restore_recorder(entity_id):
        # No snapshot — try a targeted re-scrape
        await _rescrape_snapshots(request)
        store.restore_recorder(entity_id)

    _persist_config(request)
    return _render("partials/entity_list.html", request, _entity_list_context(request))


async def _rescrape_snapshots(request: web.Request) -> None:
    """Re-scrape source panel and store snapshots without modifying templates."""
    import copy

    from span_panel_simulator.clone import translate_scraped_panel
    from span_panel_simulator.scraper import register_with_panel, scrape_ebus

    store = _store(request)
    panel_source = store.get_panel_source()
    if not panel_source:
        return

    host = panel_source.get("host", "")
    passphrase = panel_source.get("passphrase")

    try:
        creds, ca_pem = await register_with_panel(host, passphrase)
        scraped = await scrape_ebus(creds, ca_pem)
    except Exception:
        return

    # Build a fresh config to get original template values
    fresh = translate_scraped_panel(scraped, host=host, passphrase=passphrase)
    fresh_templates = fresh.get("circuit_templates")
    if not isinstance(fresh_templates, dict):
        return

    # Build recorder_map and snapshots from the fresh config
    from span_panel_simulator.ha_api.manifest import fetch_all_manifests

    recorder_map: dict[str, str] = {}
    snapshots: dict[str, object] = {}

    ha_client = request.app.get("ha_client")
    if ha_client:
        try:
            manifests = await fetch_all_manifests(ha_client)
            origin = store.get_origin_serial()
            matched = next((m for m in manifests if m.serial == origin), None)
            if matched:
                recorder_map = {c.template: c.entity_id for c in matched.circuits}
        except Exception:
            pass

    # Store recorder_entity on fresh templates and snapshot them
    for tpl_name, tpl in fresh_templates.items():
        if isinstance(tpl, dict):
            rec = recorder_map.get(tpl_name)
            if rec:
                tpl["recorder_entity"] = rec
            snapshots[tpl_name] = copy.deepcopy(tpl)

    # Persist map and snapshots without touching current templates
    ps = store._state.setdefault("panel_source", {})
    if isinstance(ps, dict):
        if recorder_map:
            ps["recorder_map"] = recorder_map
        ps["recorder_snapshots"] = snapshots
    store._dirty = True


# -- Energy projection --


async def handle_energy_projection(request: web.Request) -> web.Response:
    """Return daily energy summaries for system sizing."""
    period = request.query.get("period", "year")
    if period not in ("week", "month", "year"):
        period = "year"
    store = _store(request)
    projection = store.compute_energy_projection(period)
    return web.json_response({"period": period, "days": projection})


# -- Modeling data --


_HORIZON_MAP: dict[str, int] = {
    "1mo": 730,
    "3mo": 2190,
    "6mo": 4380,
    "1yr": 8760,
}


async def handle_modeling_data(request: web.Request) -> web.Response:
    """Return time-series for Before/After energy comparison."""
    ctx = _ctx(request)
    horizon_key = request.query.get("horizon", "1mo")
    horizon_hours = _HORIZON_MAP.get(horizon_key, 730)

    config_file = resolve_modeling_config_filename(ctx, request.query.get("config"))
    result = await ctx.get_modeling_data(horizon_hours, config_file)
    if result is None:
        return web.json_response({"error": "No running simulation"}, status=503)
    if "error" in result:
        return web.json_response(result, status=400)

    # Attach cost data if rate cache is available
    cache = _rate_cache(request)
    proposed_label = request.query.get("proposed_rate_label")
    await _attach_costs(result, cache, proposed_label, ctx.ha_client)

    return web.json_response(result)


async def _attach_costs(
    result: dict[str, Any],
    cache: RateCache,
    proposed_rate_label: str | None,
    ha_client: Any,
) -> None:
    """Add before_costs and after_costs to a modeling result dict.

    Before cost priority:
      1. Opower actual billed cost (if HA + opower account configured)
      2. URDB calculation against recorder power arrays
    After cost: always URDB.
    """
    tz_str: str = result["time_zone"]
    ts_list: list[int] = result["timestamps"]

    # -- Before cost -----------------------------------------------------
    before_costs: dict[str, Any] | None = None

    # Try opower first
    opower_acct = cache.get_opower_account()
    if opower_acct is not None and ha_client is not None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_str)
        start_dt = datetime.fromtimestamp(ts_list[0], tz=tz)
        end_dt = datetime.fromtimestamp(ts_list[-1], tz=tz)
        start_iso = start_dt.astimezone(UTC).isoformat()
        end_iso = end_dt.astimezone(UTC).isoformat()

        try:
            opower_result = await async_get_opower_cost(
                ha_client,
                opower_acct["cost_entity_id"],
                start_iso,
                end_iso,
            )
            if opower_result is not None:
                horizon_days = (end_dt - start_dt).days or 1
                before_costs = {
                    "source": "opower",
                    "net_cost": round(opower_result.total_cost, 2),
                    "days_with_data": opower_result.days_with_data,
                    "horizon_days": horizon_days,
                }
        except Exception:
            _LOGGER.exception("Failed to fetch opower cost")

    # Fall back to URDB
    if before_costs is None:
        current_label = cache.get_current_rate_label()
        if current_label is not None:
            current_entry = cache.get_cached_rate(current_label)
            if current_entry is not None:
                costs = compute_costs(ts_list, result["site_power"], current_entry.record, tz_str)
                before_costs = {
                    "source": "urdb",
                    "import_cost": round(costs.import_cost, 2),
                    "export_credit": round(costs.export_credit, 2),
                    "fixed_charges": round(costs.fixed_charges, 2),
                    "net_cost": round(costs.net_cost, 2),
                }

    if before_costs is not None:
        result["before_costs"] = before_costs

    # -- After cost (always URDB) ----------------------------------------
    current_label = cache.get_current_rate_label()
    if current_label is None:
        return
    current_entry = cache.get_cached_rate(current_label)
    if current_entry is None:
        return

    after_record = current_entry.record
    if proposed_rate_label:
        proposed_entry = cache.get_cached_rate(proposed_rate_label)
        if proposed_entry is not None:
            after_record = proposed_entry.record
    after_costs_result = compute_costs(ts_list, result["grid_power"], after_record, tz_str)
    result["after_costs"] = {
        "source": "urdb",
        "import_cost": round(after_costs_result.import_cost, 2),
        "export_credit": round(after_costs_result.export_credit, 2),
        "fixed_charges": round(after_costs_result.fixed_charges, 2),
        "net_cost": round(after_costs_result.net_cost, 2),
    }


# -- File operations --


async def handle_export(request: web.Request) -> web.Response:
    content = _store(request).export_yaml()
    return web.Response(
        text=content,
        content_type="application/x-yaml",
        headers={"Content-Disposition": 'attachment; filename="simulator_config.yaml"'},
    )


async def handle_import(request: web.Request) -> web.Response:
    data = await request.post()
    upload = data.get("file")
    if not isinstance(upload, web.FileField):
        raise web.HTTPBadRequest(text="No file uploaded")
    raw = upload.file.read()
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        _store(request).load_from_yaml(text)
    except (ValueError, TypeError) as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc
    return web.Response(status=200, headers={"HX-Redirect": "./"})


async def handle_load_config(request: web.Request) -> web.Response:
    """Load a config into the editor without affecting running engines."""
    data = await request.post()
    filename = str(data.get("config_file", ""))
    if not filename:
        raise web.HTTPBadRequest(text="No config file selected")
    ctx = _ctx(request)
    config_path = ctx.config_dir / filename
    if not config_path.exists() or not config_path.is_file():
        raise web.HTTPBadRequest(text=f"Config file not found: {filename}")
    # Prevent path traversal
    if config_path.resolve().parent != ctx.config_dir.resolve():
        raise web.HTTPBadRequest(text="Invalid config file path")
    try:
        _store(request).load_from_file(config_path)
    except (ValueError, TypeError) as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc

    # Track which file the editor is working on.  Does NOT affect
    # running engines — use Start/Stop/Restart in the panels list.
    ctx.config_filter = filename

    # Full page redirect so HTMX replaces the entire document
    return web.Response(status=200, headers={"HX-Redirect": "./"})


async def handle_clone_check(request: web.Request) -> web.Response:
    """Return the suggested filename, auto-suffixed if it already exists."""
    filename = request.query.get("filename", "").strip()
    if not filename:
        return web.json_response({"suggested": filename})
    if not filename.endswith((".yaml", ".yml")):
        filename += ".yaml"
    ctx = _ctx(request)
    path = ctx.config_dir / filename
    if path.exists() and path.resolve().parent == ctx.config_dir.resolve():
        return web.json_response(
            {
                "suggested": _next_available_filename(ctx.config_dir, filename),
            }
        )
    return web.json_response({"suggested": filename})


async def handle_clone(request: web.Request) -> web.Response:
    data = await request.post()
    filename = str(data.get("filename", "")).strip()
    source_file = str(data.get("source_file", "")).strip()
    force = str(data.get("force", "")).strip() == "1"
    if not filename:
        return web.Response(
            text='<div class="flash error">No filename provided.</div>',
            content_type="text/html",
        )
    if not filename.endswith((".yaml", ".yml")):
        filename += ".yaml"
    ctx = _ctx(request)
    output_path = ctx.config_dir / filename
    if output_path.resolve().parent != ctx.config_dir.resolve():
        return web.Response(
            text='<div class="flash error">Invalid filename.</div>',
            content_type="text/html",
        )

    # Warn before overwriting an existing file unless the user confirmed.
    if output_path.exists() and not force:
        suggested = _next_available_filename(ctx.config_dir, filename)
        return web.json_response(
            {
                "exists": True,
                "filename": filename,
                "suggested": suggested,
            }
        )

    # When a source file is specified and differs from the active config,
    # copy directly from disk instead of exporting in-memory state.
    if source_file and source_file != ctx.config_filter:
        source_path = ctx.config_dir / source_file
        if not source_path.exists() or source_path.resolve().parent != ctx.config_dir.resolve():
            return web.Response(
                text='<div class="flash error">Source file not found.</div>',
                content_type="text/html",
            )
        yaml_content = source_path.read_text(encoding="utf-8")
    else:
        yaml_content = _store(request).export_yaml()

    output_path.write_text(yaml_content, encoding="utf-8")
    _LOGGER.info("Config cloned to %s", output_path)

    # Generate synthetic history companion DB for the cloned config.
    # The generator will derive recorder_entity mappings if needed.
    try:
        from span_panel_simulator.history_generator import SyntheticHistoryGenerator

        gen = SyntheticHistoryGenerator()
        history_db = await gen.generate(output_path)
        _LOGGER.info("Generated synthetic history: %s", history_db.name)
    except Exception:
        _LOGGER.warning(
            "Synthetic history generation failed for clone",
            exc_info=True,
        )

    # Auto-switch the editor to the newly cloned config so the entity
    # list, runtime controls, and modeling view all reflect the clone.
    _store(request).load_from_file(output_path)
    ctx.config_filter = filename

    return web.Response(status=200, headers={"HX-Redirect": "./"})


async def handle_save_reload(request: web.Request) -> web.Response:
    ctx = _ctx(request)
    filename = ctx.config_filter or "default_config.yaml"
    if filename.startswith("default_"):
        raise web.HTTPBadRequest(text="Cannot save changes to a default template. Clone it first.")

    _persist_config(request)

    return web.Response(
        text='<div class="flash success">Config saved and reload triggered.</div>',
        content_type="text/html",
    )


async def handle_check_dirty(request: web.Request) -> web.Response:
    """GET /check-dirty — return JSON dirty state for JS fetch."""
    store = _store(request)
    return web.json_response({"dirty": store.dirty})


# -- Panel source provenance --


def _panel_source_context(request: web.Request) -> dict[str, Any]:
    """Build the provenance section template context."""
    store = _store(request)
    return {
        "panel_source": store.get_panel_source(),
        "origin_serial": store.get_origin_serial(),
    }


async def handle_get_panel_source(request: web.Request) -> web.Response:
    return _render(
        "partials/panel_source.html",
        request,
        _panel_source_context(request),
    )


async def handle_sync_panel_source(request: web.Request) -> web.Response:
    """Re-scrape the source panel and overwrite typical_power + energy seeds."""
    from span_panel_simulator.clone import update_config_from_scrape
    from span_panel_simulator.scraper import ScrapeError, register_with_panel, scrape_ebus

    store = _store(request)
    panel_source = store.get_panel_source()
    if not panel_source:
        return web.Response(
            text='<div class="flash error">No panel source configured.</div>',
            content_type="text/html",
        )

    host = panel_source.get("host", "")
    passphrase = panel_source.get("passphrase")

    try:
        creds, ca_pem = await register_with_panel(host, passphrase)
        scraped = await scrape_ebus(creds, ca_pem)
    except ScrapeError as exc:
        return web.Response(
            text=f'<div class="flash error">Sync failed: [{exc.phase}] {exc}</div>',
            content_type="text/html",
        )

    update_config_from_scrape(store._state, scraped)

    ctx = _panel_source_context(request)
    ctx["sync_message"] = "Updated energy seeds from source panel."
    return _render("partials/panel_source.html", request, ctx)


# -- Clone from real panel --


def _slugify_circuit_name(name: str) -> str:
    """Slugify a circuit name to match HA's entity_id convention.

    ``"Microwave  & Oven"`` -> ``"microwave_oven"``
    ``"Lights-Outlets Bedroom"`` -> ``"lights_outlets_bedroom"``
    """
    import re

    slug = name.lower()
    # Replace any non-alphanumeric run with a single underscore
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    # Strip leading/trailing underscores
    return slug.strip("_")


async def handle_panels_list(request: web.Request) -> web.Response:
    """Return the panels list rows partial for HTMX polling."""
    return _render(
        "partials/panels_list_rows.html",
        request,
        {"panels": _all_panels(request)},
    )


async def _read_panel_filename(request: web.Request) -> tuple[str, web.Response | None]:
    """Read and validate the filename parameter from a POST request.

    Returns (filename, None) on success or ("", error_response) on failure.
    """
    data = await request.post()
    filename = str(data.get("filename", "")).strip()
    if not filename:
        return "", web.Response(
            text='<div class="flash error">No panel specified.</div>',
            content_type="text/html",
        )
    ctx = _ctx(request)
    path = ctx.config_dir / filename
    if not path.exists() or path.resolve().parent != ctx.config_dir.resolve():
        return "", web.Response(
            text='<div class="flash error">Config file not found.</div>',
            content_type="text/html",
        )
    return filename, None


async def handle_start_panel(request: web.Request) -> web.Response:
    """Start the simulation engine for a specific config file."""
    filename, err = await _read_panel_filename(request)
    if err is not None:
        return err
    ctx = _ctx(request)
    ctx.start_panel(filename)
    # Auto-switch the editor to this panel so entity list stays in sync.
    _store(request).load_from_file(ctx.config_dir / filename)
    ctx.config_filter = filename
    return web.Response(status=200, headers={"HX-Redirect": "./"})


async def handle_stop_panel(request: web.Request) -> web.Response:
    """Stop the simulation engine for a specific config file."""
    filename, err = await _read_panel_filename(request)
    if err is not None:
        return err
    ctx = _ctx(request)
    ctx.stop_panel(filename)
    _store(request).load_from_file(ctx.config_dir / filename)
    ctx.config_filter = filename
    return web.Response(status=200, headers={"HX-Redirect": "./"})


async def handle_restart_panel(request: web.Request) -> web.Response:
    """Restart the simulation engine for a specific config file."""
    filename, err = await _read_panel_filename(request)
    if err is not None:
        return err
    ctx = _ctx(request)
    ctx.restart_panel(filename)
    _store(request).load_from_file(ctx.config_dir / filename)
    ctx.config_filter = filename
    return web.Response(status=200, headers={"HX-Redirect": "./"})


async def _purge_recorder_for_config(
    ctx: DashboardContext, config_path: Path
) -> RecorderPurgeResult:
    """Purge HA recorder data for a panel config (best-effort).

    Reads the serial number from the YAML file, looks up the
    corresponding HA device, and calls ``recorder.purge_entities``
    for all entities belonging to that device.  Failures are logged
    but never prevent deletion (callers may ignore the return value).
    """
    from span_panel_simulator.ha_api.client import HAClient

    ha_client = ctx.ha_client
    if not isinstance(ha_client, HAClient):
        return RecorderPurgeResult("no_ha", 0)

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        serial: str = raw["panel_config"]["serial_number"]
    except Exception:
        _LOGGER.debug("Could not read serial from %s — skipping recorder purge", config_path)
        return RecorderPurgeResult("no_serial", 0)

    try:
        count = await ha_client.async_purge_panel_recorder_data(serial)
        if count:
            _LOGGER.info("Purged recorder data for %d entities (serial=%s)", count, serial)
        return RecorderPurgeResult("purged" if count else "none_found", count)
    except Exception:
        _LOGGER.warning("Failed to purge HA recorder data for serial %s", serial, exc_info=True)
        return RecorderPurgeResult("failed", 0)


async def handle_purge_recorder(request: web.Request) -> web.Response:
    """Purge HA recorder data for a config file without deleting it."""
    data = await request.post()
    filename = str(data.get("filename", "")).strip()
    if not filename:
        return web.Response(
            text='<div class="flash error">No filename specified.</div>',
            content_type="text/html",
        )

    ctx = _ctx(request)
    config_path = ctx.config_dir / filename

    if not config_path.exists() or config_path.resolve().parent != ctx.config_dir.resolve():
        return web.Response(
            text='<div class="flash error">Config file not found.</div>',
            content_type="text/html",
        )

    # Refuse if the panel is currently running
    running = {p.name for p in ctx.get_panel_configs()}
    if filename in running:
        return web.Response(
            text='<div class="flash error">Cannot purge while the panel is running.'
            " Stop it first.</div>",
            content_type="text/html",
        )

    result = await _purge_recorder_for_config(ctx, config_path)
    if result.status == "purged":
        n = result.entity_count
        ent_word = "entity" if n == 1 else "entities"
        msg = f"Purged recorder history for {n} {ent_word}."
        flash_cls = "flash success"
    elif result.status == "none_found":
        msg = "No recorder data was found for this simulated panel in Home Assistant."
        flash_cls = "flash info"
    elif result.status == "no_ha":
        msg = "Home Assistant is not connected; there was nothing to purge."
        flash_cls = "flash info"
    elif result.status == "no_serial":
        msg = "Could not read the panel serial from the config file."
        flash_cls = "flash error"
    else:
        msg = "Recorder purge failed. See server logs for details."
        flash_cls = "flash error"
    return web.Response(
        text=f'<div class="{flash_cls}">{msg}</div>',
        content_type="text/html",
    )


async def handle_delete_config(request: web.Request) -> web.Response:
    """Delete a config file from disk.  Refuses if the panel is running or being edited."""
    data = await request.post()
    filename = str(data.get("filename", "")).strip()
    if not filename:
        return web.Response(
            text='<div class="flash error">No filename specified.</div>',
            content_type="text/html",
        )

    # Default templates are protected — clone instead.
    if filename.startswith("default_"):
        return web.Response(
            text='<div class="flash error">Default templates cannot be deleted.'
            " Clone to create your own.</div>",
            content_type="text/html",
        )

    ctx = _ctx(request)
    config_path = ctx.config_dir / filename

    # Safety: prevent path traversal
    if not config_path.exists() or config_path.resolve().parent != ctx.config_dir.resolve():
        return web.Response(
            text='<div class="flash error">Config file not found.</div>',
            content_type="text/html",
        )

    # Refuse to delete a running panel's config
    running = {p.name for p in ctx.get_panel_configs()}
    if filename in running:
        return web.Response(
            text='<div class="flash error">Cannot delete a running panel. Stop it first.</div>',
            content_type="text/html",
        )

    # Best-effort: purge HA recorder data for this panel's entities
    # before removing the config file.
    await _purge_recorder_for_config(ctx, config_path)

    config_path.unlink()

    # Remove companion history DB if present
    history_db = config_path.with_name(config_path.stem + "_history.db")
    if history_db.exists():
        history_db.unlink()
        _LOGGER.info("Deleted history DB %s", history_db.name)

    _LOGGER.info("Deleted config %s", filename)

    # If we just deleted the active editor file, fall back to viewing
    # the first default template (read-only).
    if filename == ctx.config_filter:
        first_default = _first_default_config(ctx.config_dir)
        ctx.config_filter = first_default
        if first_default:
            _store(request).load_from_file(ctx.config_dir / first_default)
        return web.Response(
            status=200,
            headers={"HX-Redirect": "./"},
        )

    return web.Response(
        text=f'<div class="flash success">Deleted {filename}</div>',
        content_type="text/html",
        headers={"HX-Trigger": "refreshPanels"},
    )


def _clone_panel_context(request: web.Request, **extra: object) -> dict[str, Any]:
    """Build the clone panel section template context."""
    ctx = _ctx(request)
    store = _store(request)
    panel_source = store.get_panel_source()
    result: dict[str, Any] = {
        "ha_available": ctx.ha_client is not None,
        "clone_host": panel_source.get("host", "") if panel_source else "",
    }
    result.update(extra)
    return result


async def _import_profiles_for_serial(
    ha_client: Any,
    history_provider: Any,
    config_path: Path,
    origin_serial: str,
) -> int:
    """Fetch HA manifests, build profiles, and apply them to a config file.

    Uses *ha_client* for the manifest service call and *history_provider*
    for the actual statistics queries.  They may be the same object (both
    HAClient) or different backends.

    Returns the number of circuits updated (0 if nothing matched).
    Raises on errors so the caller can decide how to handle them.
    """
    import yaml

    from span_panel_simulator.ha_api.manifest import fetch_all_manifests
    from span_panel_simulator.ha_api.profile_builder import build_profiles
    from span_panel_simulator.profile_applicator import (
        apply_usage_profiles,
        store_recorder_entities,
    )

    manifests = await fetch_all_manifests(ha_client)
    if not manifests:
        return 0

    matched = next((m for m in manifests if m.serial == origin_serial), None)
    if matched is None:
        return 0

    # Store recorder_entity on ALL circuit templates (including PV/BESS)
    # so the engine can replay recorded data for any circuit.
    template_to_entity = {c.template: c.entity_id for c in matched.circuits}
    store_recorder_entities(config_path, template_to_entity)

    eligible = matched.profile_circuits()
    if not eligible:
        return 0

    # Read the panel's timezone from the config for correct hour bucketing
    panel_tz: str | None = None
    try:
        raw_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if isinstance(raw_config, dict):
            panel_cfg = raw_config.get("panel_config", {})
            if isinstance(panel_cfg, dict):
                panel_tz = panel_cfg.get("time_zone")
    except Exception:
        pass  # Fall back to UTC

    profiles = await build_profiles(
        history_provider,
        eligible,
        matched.entity_to_template(),
        time_zone=panel_tz,
    )
    if not profiles:
        return 0

    return apply_usage_profiles(config_path, profiles)


async def _finalize_clone(
    request: web.Request,
    config: dict[str, object],
    origin_serial: str,
    host: str,
    *,
    filename: str | None = None,
) -> web.Response:
    """Write a clone config, import profiles, generate history, and start the engine.

    Shared by handle_clone_from_panel and handle_clone_confirm.
    """
    from span_panel_simulator.clone import write_clone_config

    ctx = _ctx(request)

    clone_path = write_clone_config(config, ctx.config_dir, origin_serial, filename=filename)

    # Load the clone config into the dashboard editor
    store = _store(request)
    store.load_from_file(clone_path)
    ctx.config_filter = clone_path.name

    _LOGGER.info("Panel cloned from %s -> %s", host, clone_path.name)

    # Automatically import HA usage profiles for the cloned panel.
    # This must happen BEFORE history generation because it populates
    # the recorder_entity mappings the generator needs.
    profiles_imported = 0
    _LOGGER.debug(
        "Clone profile import: ha_client=%s, history_provider=%s",
        ctx.ha_client is not None,
        ctx.history_provider is not None,
    )
    if ctx.ha_client is not None and ctx.history_provider is not None:
        try:
            profiles_imported = await _import_profiles_for_serial(
                ctx.ha_client, ctx.history_provider, clone_path, origin_serial
            )
            _LOGGER.info("Imported %d profiles before history generation", profiles_imported)
        except Exception:
            _LOGGER.debug("HA profile import after clone failed", exc_info=True)

    # Check if recorder_entity mappings now exist in the written config
    import yaml as _yaml

    _check_raw = _yaml.safe_load(clone_path.read_text(encoding="utf-8"))
    _check_entities = []
    if isinstance(_check_raw, dict):
        _check_tmpls = _check_raw.get("circuit_templates", {})
        if isinstance(_check_tmpls, dict):
            for _tn, _tv in _check_tmpls.items():
                if isinstance(_tv, dict) and _tv.get("recorder_entity"):
                    _check_entities.append(f"{_tn}={_tv['recorder_entity']}")
    _LOGGER.info(
        "Pre-generate check: %d recorder_entity mappings in %s: %s",
        len(_check_entities),
        clone_path.name,
        _check_entities[:5],
    )

    # Generate synthetic history companion DB for offline replay.
    # Runs after profile import so recorder_entity mappings are present.
    try:
        from span_panel_simulator.history_generator import SyntheticHistoryGenerator

        gen = SyntheticHistoryGenerator()
        history_db = await gen.generate(clone_path)
        _LOGGER.info("Generated synthetic history: %s", history_db.name)
    except Exception:
        _LOGGER.warning(
            "Synthetic history generation failed — panel will use per-tick synthesis",
            exc_info=True,
        )

    # Start the clone engine (also triggers reload)
    ctx.start_panel(clone_path.name)

    if profiles_imported:
        # Re-read config after profile application
        store.load_from_file(clone_path)

    # Redirect to refresh the full dashboard with the new config
    return web.Response(status=200, headers={"HX-Redirect": "./"})


def _next_available_filename(config_dir: Path, filename: str) -> str:
    """Find the next available filename by appending a numeric suffix.

    Given ``foo.yaml``, tries ``foo-2.yaml``, ``foo-3.yaml``, etc.
    If the input already has a ``-N`` suffix, increments from there.
    """
    import re

    # Split off .yaml / .yml extension
    for ext in (".yaml", ".yml"):
        if filename.endswith(ext):
            stem = filename[: -len(ext)]
            break
    else:
        stem, ext = filename, ".yaml"

    # If the stem already ends with -N, start from N+1
    match = re.match(r"^(.+)-(\d+)$", stem)
    if match:
        base = match.group(1)
        start = int(match.group(2)) + 1
    else:
        base = stem
        start = 2

    idx = start
    while True:
        candidate = f"{base}-{idx}{ext}"
        if not (config_dir / candidate).exists():
            return candidate
        idx += 1


def _next_clone_filename(config_dir: Path, original_serial: str) -> str:
    """Find the next available clone filename with a numeric suffix."""
    return _next_available_filename(config_dir, f"{original_serial}-clone.yaml")


async def handle_clone_from_panel(request: web.Request) -> web.Response:
    """Scrape a real SPAN panel via eBus, create a clone config, and import HA profiles."""
    from span_panel_simulator.clone import (
        clone_config_path,
        translate_scraped_panel,
    )
    from span_panel_simulator.scraper import ScrapeError, register_with_panel, scrape_ebus

    data = await request.post()
    host = str(data.get("host", "")).strip()
    passphrase = str(data.get("passphrase", "")).strip() or None

    if not host:
        return _render(
            "partials/clone_panel.html",
            request,
            _clone_panel_context(request, clone_error="Panel IP or hostname is required."),
        )

    if not passphrase:
        return _render(
            "partials/clone_panel.html",
            request,
            _clone_panel_context(
                request,
                clone_error="Passphrase is required.",
                clone_host=host,
            ),
        )

    try:
        creds, ca_pem = await register_with_panel(host, passphrase)
        scraped = await scrape_ebus(creds, ca_pem)
    except ScrapeError as exc:
        return _render(
            "partials/clone_panel.html",
            request,
            _clone_panel_context(
                request,
                clone_error=f"Clone failed: [{exc.phase}] {exc}",
                clone_host=host,
            ),
        )

    config = translate_scraped_panel(scraped, host=host, passphrase=passphrase)

    ctx = _ctx(request)

    # Apply home location from HA if available
    if ctx.ha_client is not None:
        try:
            location = await ctx.ha_client.async_get_home_location()
            if location is not None:
                lat, lon = location
                panel_cfg = config.get("panel_config")
                if isinstance(panel_cfg, dict):
                    panel_cfg["latitude"] = lat
                    panel_cfg["longitude"] = lon

                    # Derive IANA timezone from coordinates so profile
                    # builder can bucket hour_factors in local time.
                    from timezonefinder import TimezoneFinder

                    tz_result = TimezoneFinder().timezone_at(lat=lat, lng=lon)
                    tz_name = str(tz_result) if tz_result else "America/Los_Angeles"
                    panel_cfg["time_zone"] = tz_name
                    _LOGGER.info("Applied HA home location: %.4f, %.4f → %s", lat, lon, tz_name)
        except Exception:
            _LOGGER.debug("Could not fetch HA location", exc_info=True)

    # Check if the default clone file already exists
    default_path = clone_config_path(ctx.config_dir, scraped.serial_number)
    if default_path.exists():
        # Store the pending config server-side and ask the user to confirm
        import uuid

        token = uuid.uuid4().hex
        pending = request.app[APP_KEY_PENDING_CLONES]
        pending[token] = {
            "config": config,
            "origin_serial": scraped.serial_number,
            "host": host,
        }
        return _render(
            "partials/clone_confirm.html",
            request,
            {
                "token": token,
                "existing_filename": default_path.name,
                "default_rename": _next_clone_filename(ctx.config_dir, scraped.serial_number),
            },
        )

    return await _finalize_clone(
        request,
        config,
        scraped.serial_number,
        host,
    )


async def handle_clone_panel_section(request: web.Request) -> web.Response:
    """Return the clone panel section partial (used by cancel button)."""
    return _render(
        "partials/clone_panel.html",
        request,
        _clone_panel_context(request),
    )


async def handle_clone_confirm(request: web.Request) -> web.Response:
    """Handle the user's confirmation on how to proceed with a duplicate clone."""
    data = await request.post()
    token = str(data.get("token", "")).strip()
    action = str(data.get("action", "overwrite")).strip()
    custom_name = str(data.get("custom_name", "")).strip()

    pending = request.app[APP_KEY_PENDING_CLONES]
    pending_data = pending.pop(token, None)

    if pending_data is None:
        return _render(
            "partials/clone_panel.html",
            request,
            _clone_panel_context(
                request,
                clone_error="Clone session expired. Please try again.",
            ),
        )

    raw_config = pending_data["config"]
    if not isinstance(raw_config, dict):
        return _render(
            "partials/clone_panel.html",
            request,
            _clone_panel_context(
                request,
                clone_error="Clone session corrupted. Please try again.",
            ),
        )
    config: dict[str, object] = raw_config
    origin_serial = str(pending_data["origin_serial"])
    host = str(pending_data["host"])

    filename: str | None = None
    if action == "rename" and custom_name:
        # Ensure the custom name ends with .yaml
        if not custom_name.endswith(".yaml"):
            custom_name += ".yaml"
        # Validate the filename is safe
        if "/" in custom_name or "\\" in custom_name or custom_name.startswith("."):
            return _render(
                "partials/clone_panel.html",
                request,
                _clone_panel_context(
                    request,
                    clone_error="Invalid filename. Avoid slashes and leading dots.",
                ),
            )
        filename = custom_name

    return await _finalize_clone(
        request,
        config,
        origin_serial,
        host,
        filename=filename,
    )


# -- HA profile import --


async def handle_import_ha_profiles(request: web.Request) -> web.Response:
    """Import usage profiles from HA recorder via the circuit manifest service."""
    ctx = _ctx(request)
    if ctx.ha_client is None:
        return _render(
            "partials/clone_panel.html",
            request,
            _clone_panel_context(request, clone_error="HA API is not connected."),
        )

    store = _store(request)
    config_filter = ctx.config_filter
    if not config_filter:
        return _render(
            "partials/clone_panel.html",
            request,
            _clone_panel_context(
                request,
                clone_error="No config loaded — clone a panel first.",
            ),
        )

    config_path = ctx.config_dir / config_filter
    origin_serial = store.get_origin_serial()
    if not origin_serial:
        return _render(
            "partials/clone_panel.html",
            request,
            _clone_panel_context(
                request,
                clone_error="Config has no origin_serial — not a clone.",
            ),
        )

    if ctx.history_provider is None:
        return _render(
            "partials/clone_panel.html",
            request,
            _clone_panel_context(
                request,
                clone_error="No history provider available.",
            ),
        )

    try:
        updated = await _import_profiles_for_serial(
            ctx.ha_client, ctx.history_provider, config_path, origin_serial
        )
    except Exception as exc:
        _LOGGER.exception("HA profile import failed")
        return _render(
            "partials/clone_panel.html",
            request,
            _clone_panel_context(
                request,
                clone_error=f"Profile import failed: {exc}",
            ),
        )

    if not updated:
        return _render(
            "partials/clone_panel.html",
            request,
            _clone_panel_context(
                request,
                clone_error="No matching profiles found in HA recorder.",
            ),
        )

    store.load_from_file(config_path)
    ctx.request_reload()

    return _render(
        "partials/clone_panel.html",
        request,
        _clone_panel_context(
            request,
            clone_message=f"Imported profiles for {updated} circuits from HA recorder.",
        ),
    )


# -- Panel discovery (HA manifest + mDNS) --


async def handle_discovered_panels(request: web.Request) -> web.Response:
    """Return panels discovered via HA manifest and/or mDNS.

    In add-on mode (HA available) the HA manifest service provides
    panels with circuit counts.  In standalone mode, mDNS discovery
    finds ``_span._tcp`` panels on the LAN.  Both sources are merged;
    HA entries take precedence when a serial appears in both.
    """
    ctx = _ctx(request)
    panels: dict[str, dict[str, object]] = {}

    # mDNS-discovered panels (standalone and add-on)
    if ctx.panel_browser is not None:
        for p in ctx.panel_browser.panels:
            panels[p.serial] = {
                "serial": p.serial,
                "host": p.host,
                "circuits": 0,
                "source": "mdns",
            }

    # HA manifest panels (add-on mode) — override mDNS entries
    if ctx.ha_client is not None:
        try:
            from span_panel_simulator.ha_api.manifest import fetch_all_manifests

            manifests = await fetch_all_manifests(ctx.ha_client)
            for m in manifests:
                if m.host:
                    panels[m.serial] = {
                        "serial": m.serial,
                        "host": m.host,
                        "circuits": len(m.circuits),
                        "source": "ha",
                    }
        except Exception:
            _LOGGER.debug("Failed to fetch panel manifests from HA", exc_info=True)

    return web.json_response(list(panels.values()))
