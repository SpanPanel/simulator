"""Route handlers for the dashboard sub-application.

Handlers are intentionally thin: parse request, call store, render template.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import aiohttp
import aiohttp_jinja2
from aiohttp import web

from span_panel_simulator.dashboard.presets import (
    BATTERY_PRESET_LABELS,
    EVSE_PRESET_LABELS,
    PRESET_LABELS,
    PRESETS_BY_TYPE,
    match_battery_preset,
)
from span_panel_simulator.solar import compute_solar_curve
from span_panel_simulator.weather import fetch_historical_weather, get_cached_weather

if TYPE_CHECKING:
    from span_panel_simulator.dashboard import DashboardContext
    from span_panel_simulator.dashboard.config_store import ConfigStore

_LOGGER = logging.getLogger(__name__)

PRIORITIES = [
    "MUST_HAVE",
    "NICE_TO_HAVE",
    "NON_ESSENTIAL",
    "NEVER",
    "SOC_THRESHOLD",
    "OFF_GRID",
]
RELAY_BEHAVIORS = ["controllable", "non_controllable"]
ENTITY_TYPES = ["circuit", "pv", "evse", "battery"]


def _store(request: web.Request) -> ConfigStore:
    store: ConfigStore = request.app["store"]
    return store


def _ctx(request: web.Request) -> DashboardContext:
    ctx: DashboardContext = request.app["dashboard_context"]
    return ctx


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


def _dashboard_context(request: web.Request) -> dict[str, Any]:
    """Build the full-page template context."""
    store = _store(request)
    return {
        "panel_config": store.get_panel_config(),
        "sim_params": store.get_simulation_params(),
        "entities": store.list_entities(),
        "priorities": PRIORITIES,
        "relay_behaviors": RELAY_BEHAVIORS,
        "entity_types": ENTITY_TYPES,
        "preset_labels": PRESET_LABELS,
        "unmapped_tabs": store.get_unmapped_tabs(),
        "config_files": _available_configs(request),
        "panel_source": store.get_panel_source(),
        "origin_serial": store.get_origin_serial(),
    }


def _presets_for_type(entity_type: str) -> dict[str, str]:
    """Return the preset labels appropriate for the given entity type."""
    return PRESETS_BY_TYPE.get(entity_type, {})


def _entity_list_context(request: web.Request, editing_id: str | None = None) -> dict[str, Any]:
    """Build the entity-list template context.

    When *editing_id* is set, the template renders that entity in edit
    mode and all others as collapsed rows.
    """
    store = _store(request)
    ctx: dict[str, Any] = {
        "entities": store.list_entities(),
        "entity_types": ENTITY_TYPES,
        "editing_id": editing_id,
        "unmapped_tabs": store.get_unmapped_tabs(),
    }
    if editing_id is not None:
        entity = store.get_entity(editing_id)
        ctx["editing_entity"] = entity
        ctx["editing_profile"] = store.get_entity_profile(editing_id)
        ctx["priorities"] = PRIORITIES
        ctx["relay_behaviors"] = RELAY_BEHAVIORS
        ctx["preset_labels"] = _presets_for_type(entity.entity_type)
        if entity.entity_type == "battery":
            ctx["battery_preset_labels"] = BATTERY_PRESET_LABELS
            battery_profile = store.get_battery_profile(editing_id)
            ctx["battery_profile"] = battery_profile
            ctx["battery_charge_mode"] = store.get_battery_charge_mode(editing_id)
            ctx["battery_active_preset"] = match_battery_preset(battery_profile)
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
            ctx["evse_preset_labels"] = EVSE_PRESET_LABELS
    return ctx


def _profile_context(request: web.Request, entity_id: str) -> dict[str, Any]:
    """Build the profile-editor template context."""
    store = _store(request)
    entity = store.get_entity(entity_id)
    return {
        "entity": entity,
        "profile": store.get_entity_profile(entity_id),
        "preset_labels": _presets_for_type(entity.entity_type),
    }


def _battery_profile_context(request: web.Request, entity_id: str) -> dict[str, Any]:
    """Build the battery profile editor template context."""
    store = _store(request)
    entity = store.get_entity(entity_id)
    battery_profile = store.get_battery_profile(entity_id)
    return {
        "entity": entity,
        "battery_profile": battery_profile,
        "battery_preset_labels": BATTERY_PRESET_LABELS,
        "battery_charge_mode": store.get_battery_charge_mode(entity_id),
        "battery_active_preset": match_battery_preset(battery_profile),
    }


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

    # Battery profile
    app.router.add_get("/entities/{id}/battery-profile", handle_get_battery_profile)
    app.router.add_put("/entities/{id}/battery-profile", handle_put_battery_profile)
    app.router.add_post("/entities/{id}/battery-profile/preset", handle_apply_battery_preset)
    app.router.add_put("/entities/{id}/battery-charge-mode", handle_put_battery_charge_mode)

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

    # Energy projection
    app.router.add_get("/energy-projection", handle_energy_projection)

    # File operations
    app.router.add_get("/export", handle_export)
    app.router.add_post("/import", handle_import)
    app.router.add_post("/load-config", handle_load_config)
    app.router.add_post("/clone", handle_clone)
    app.router.add_post("/save-reload", handle_save_reload)

    # Panel source provenance
    app.router.add_get("/panel-source", handle_get_panel_source)
    app.router.add_post("/sync-panel-source", handle_sync_panel_source)


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
        "partials/simulation_params.html",
        request,
        {"sim_params": _store(request).get_simulation_params()},
    )


async def handle_put_sim_params(request: web.Request) -> web.Response:
    data = await request.post()
    _store(request).update_simulation_params(dict(data))
    return _render(
        "partials/simulation_params.html",
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


async def handle_put_entity(request: web.Request) -> web.Response:
    entity_id = request.match_info["id"]
    data = await request.post()
    _store(request).update_entity(entity_id, dict(data))
    # Push priority change to the running engine immediately
    if "priority" in data:
        _ctx(request).set_circuit_priority(entity_id, str(data["priority"]))
    return _render(
        "partials/entity_list.html",
        request,
        _entity_list_context(request, editing_id=entity_id),
    )


async def handle_delete_entity(request: web.Request) -> web.Response:
    entity_id = request.match_info["id"]
    _store(request).delete_entity(entity_id)
    return _render("partials/entity_list.html", request, _entity_list_context(request))


# -- Profile --


async def handle_get_profile(request: web.Request) -> web.Response:
    entity_id = request.match_info["id"]
    return _render("partials/profile_editor.html", request, _profile_context(request, entity_id))


async def handle_put_profile(request: web.Request) -> web.Response:
    entity_id = request.match_info["id"]
    data = await request.post()
    multipliers: dict[int, float] = {}
    for h in range(24):
        key = f"hour_{h}"
        if key in data:
            multipliers[h] = float(str(data[key]))
    _store(request).update_entity_profile(entity_id, multipliers)
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
    )
    return _render("partials/profile_editor.html", request, _profile_context(request, entity_id))


# -- Battery profile --


async def handle_get_battery_profile(request: web.Request) -> web.Response:
    entity_id = request.match_info["id"]
    return _render(
        "partials/battery_profile_editor.html",
        request,
        _battery_profile_context(request, entity_id),
    )


async def handle_put_battery_profile(request: web.Request) -> web.Response:
    entity_id = request.match_info["id"]
    data = await request.post()
    hour_modes: dict[int, str] = {}
    for h in range(24):
        key = f"hour_{h}"
        if key in data:
            mode = str(data[key])
            if mode in ("charge", "discharge", "idle"):
                hour_modes[h] = mode
            else:
                hour_modes[h] = "idle"
        else:
            hour_modes[h] = "idle"
    _store(request).update_battery_profile(entity_id, hour_modes)
    return _render(
        "partials/battery_profile_editor.html",
        request,
        _battery_profile_context(request, entity_id),
    )


async def handle_apply_battery_preset(request: web.Request) -> web.Response:
    entity_id = request.match_info["id"]
    data = await request.post()
    preset_name = str(data.get("preset", "custom"))
    _store(request).apply_battery_preset(entity_id, preset_name)
    return _render(
        "partials/battery_profile_editor.html",
        request,
        _battery_profile_context(request, entity_id),
    )


async def handle_put_battery_charge_mode(request: web.Request) -> web.Response:
    entity_id = request.match_info["id"]
    data = await request.post()
    mode = str(data.get("charge_mode", "custom"))
    _store(request).update_battery_charge_mode(entity_id, mode)
    return _render(
        "partials/battery_profile_editor.html",
        request,
        _battery_profile_context(request, entity_id),
    )


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
        "evse_preset_labels": EVSE_PRESET_LABELS,
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
    _store(request).update_evse_schedule(entity_id, start, duration)
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


# -- Energy projection --


async def handle_energy_projection(request: web.Request) -> web.Response:
    """Return daily energy summaries for system sizing."""
    period = request.query.get("period", "year")
    if period not in ("week", "month", "year"):
        period = "year"
    store = _store(request)
    projection = store.compute_energy_projection(period)
    return web.json_response({"period": period, "days": projection})


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
    return web.Response(status=200, headers={"HX-Redirect": "/"})


async def handle_load_config(request: web.Request) -> web.Response:
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
    # Full page redirect so HTMX replaces the entire document
    return web.Response(status=200, headers={"HX-Redirect": "/"})


async def handle_clone(request: web.Request) -> web.Response:
    data = await request.post()
    filename = str(data.get("filename", "")).strip()
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
    yaml_content = _store(request).export_yaml()
    output_path.write_text(yaml_content, encoding="utf-8")
    _LOGGER.info("Config cloned to %s", output_path)
    return web.Response(
        text=f'<div class="flash success">Cloned to {filename}</div>',
        content_type="text/html",
    )


async def handle_save_reload(request: web.Request) -> web.Response:
    store = _store(request)
    ctx = _ctx(request)

    yaml_content = store.export_yaml()

    if ctx.config_filter:
        output_path = ctx.config_dir / ctx.config_filter
    else:
        output_path = ctx.config_dir / "default_config.yaml"

    output_path.write_text(yaml_content, encoding="utf-8")
    _LOGGER.info("Config saved to %s", output_path)

    ctx.request_reload()

    return web.Response(
        text='<div class="flash success">Config saved and reload triggered.</div>',
        content_type="text/html",
    )


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
    ctx["sync_message"] = "Updated typical_power and energy seeds from source panel."
    return _render("partials/panel_source.html", request, ctx)
