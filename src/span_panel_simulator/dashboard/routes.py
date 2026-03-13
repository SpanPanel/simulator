"""Route handlers for the dashboard sub-application.

Handlers are intentionally thin: parse request, call store, render template.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import aiohttp_jinja2
from aiohttp import web

from span_panel_simulator.dashboard.presets import PRESET_LABELS, PRESETS_BY_TYPE
from span_panel_simulator.dashboard.solar import compute_solar_curve

if TYPE_CHECKING:
    from span_panel_simulator.dashboard import DashboardContext
    from span_panel_simulator.dashboard.config_store import ConfigStore

_LOGGER = logging.getLogger(__name__)

PRIORITIES = [
    "MUST_HAVE", "NICE_TO_HAVE", "NON_ESSENTIAL", "NEVER",
    "SOC_THRESHOLD", "OFF_GRID",
]
RELAY_BEHAVIORS = ["controllable", "non_controllable"]
ENTITY_TYPES = ["circuit", "pv", "evse", "battery"]


def _store(request: web.Request) -> ConfigStore:
    return request.app["store"]


def _ctx(request: web.Request) -> DashboardContext:
    return request.app["dashboard_context"]


def _render(template: str, request: web.Request, context: dict[str, Any]) -> web.Response:
    """Render a Jinja2 template and return an HTML response."""
    body = aiohttp_jinja2.render_string(template, request, context)
    return web.Response(text=body, content_type="text/html")


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
    }


def _presets_for_type(entity_type: str) -> dict[str, str]:
    """Return the preset labels appropriate for the given entity type."""
    return PRESETS_BY_TYPE.get(entity_type, {})


def _entity_list_context(
    request: web.Request, editing_id: str | None = None
) -> dict[str, Any]:
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

    # Solar curve JSON
    app.router.add_get("/solar-curve", handle_solar_curve)

    # File operations
    app.router.add_get("/export", handle_export)
    app.router.add_post("/import", handle_import)
    app.router.add_post("/save-reload", handle_save_reload)


# -- Full page --

async def handle_dashboard(request: web.Request) -> web.Response:
    return _render("dashboard.html", request, _dashboard_context(request))


# -- Panel config --

async def handle_get_panel_config(request: web.Request) -> web.Response:
    return _render(
        "partials/panel_config.html", request,
        {"panel_config": _store(request).get_panel_config()},
    )


async def handle_put_panel_config(request: web.Request) -> web.Response:
    data = await request.post()
    _store(request).update_panel_config(dict(data))
    return _render(
        "partials/panel_config.html", request,
        {"panel_config": _store(request).get_panel_config()},
    )


# -- Simulation params --

async def handle_get_sim_params(request: web.Request) -> web.Response:
    return _render(
        "partials/simulation_params.html", request,
        {"sim_params": _store(request).get_simulation_params()},
    )


async def handle_put_sim_params(request: web.Request) -> web.Response:
    data = await request.post()
    _store(request).update_simulation_params(dict(data))
    return _render(
        "partials/simulation_params.html", request,
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
        "partials/entity_list.html", request,
        _entity_list_context(request, editing_id=entity.id),
    )


async def handle_get_entity_edit(request: web.Request) -> web.Response:
    entity_id = request.match_info["id"]
    return _render(
        "partials/entity_list.html", request,
        _entity_list_context(request, editing_id=entity_id),
    )


async def handle_put_entity(request: web.Request) -> web.Response:
    entity_id = request.match_info["id"]
    data = await request.post()
    _store(request).update_entity(entity_id, dict(data))
    return _render(
        "partials/entity_list.html", request,
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
        entity_id, preset_name, month, day,
        start_hour=start_hour, end_hour=end_hour,
    )
    return _render("partials/profile_editor.html", request, _profile_context(request, entity_id))


# -- Solar curve JSON --

async def handle_solar_curve(request: web.Request) -> web.Response:
    month = int(request.query.get("month", "6"))
    day = int(request.query.get("day", "21"))
    curve = compute_solar_curve(month, day)
    return web.json_response(curve)


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
    return _render("dashboard.html", request, _dashboard_context(request))


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
