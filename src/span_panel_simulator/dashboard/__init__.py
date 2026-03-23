"""Dashboard application for the SPAN panel simulator.

Runs as a standalone aiohttp server on its own port (default 8080)
and provides a web UI for importing, editing, and exporting panel
configurations.
"""

from __future__ import annotations

from pathlib import Path

import aiohttp_jinja2
import jinja2
from aiohttp import web

from span_panel_simulator.dashboard.config_store import ConfigStore
from span_panel_simulator.dashboard.context import DashboardContext
from span_panel_simulator.dashboard.keys import (
    APP_KEY_DASHBOARD_CONTEXT,
    APP_KEY_PRESET_REGISTRY,
    APP_KEY_STORE,
)
from span_panel_simulator.dashboard.presets import init_presets
from span_panel_simulator.dashboard.routes import setup_routes

__all__ = ["DashboardContext", "create_dashboard_app"]


def create_dashboard_app(context: DashboardContext) -> web.Application:
    """Create and return the dashboard aiohttp application."""
    app = web.Application()

    store = ConfigStore()

    # Load the active config into the editor/viewer.
    if context.config_filter:
        config_path = context.config_dir / context.config_filter
        if config_path.exists():
            store.load_from_file(config_path)
    else:
        # No active config — show first default template (read-only).
        defaults = sorted(context.config_dir.glob("default_*.yaml"))
        if defaults:
            context.config_filter = defaults[0].name
            store.load_from_file(defaults[0])

    app[APP_KEY_STORE] = store
    app[APP_KEY_DASHBOARD_CONTEXT] = context
    app[APP_KEY_PRESET_REGISTRY] = init_presets(context.config_dir)

    template_dir = Path(__file__).parent / "templates"
    env = aiohttp_jinja2.setup(
        app,
        loader=jinja2.FileSystemLoader(str(template_dir)),
    )
    env.globals["static_url"] = "static"

    static_dir = Path(__file__).parent / "static"
    app.router.add_static("/static", static_dir, name="static")

    setup_routes(app)

    return app
