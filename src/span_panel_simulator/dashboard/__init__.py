"""Dashboard application for the SPAN panel simulator.

Runs as a standalone aiohttp server on its own port (default 8080)
and provides a web UI for importing, editing, and exporting panel
configurations.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import aiohttp_jinja2
import jinja2
from aiohttp import web

from span_panel_simulator.dashboard.config_store import ConfigStore
from span_panel_simulator.dashboard.routes import setup_routes


@dataclass
class DashboardContext:
    """Clean interface boundary between the dashboard and SimulatorApp."""

    config_dir: Path
    config_filter: str | None
    get_panel_configs: Callable[[], dict[Path, str]]  # path -> serial
    request_reload: Callable[[], None]


def create_dashboard_app(context: DashboardContext) -> web.Application:
    """Create and return the dashboard aiohttp application."""
    app = web.Application()

    store = ConfigStore()

    # Try loading the current config
    if context.config_filter:
        config_path = context.config_dir / context.config_filter
        if config_path.exists():
            store.load_from_file(config_path)
    else:
        default_path = context.config_dir / "default_config.yaml"
        if default_path.exists():
            store.load_from_file(default_path)

    app["store"] = store
    app["dashboard_context"] = context

    template_dir = Path(__file__).parent / "templates"
    env = aiohttp_jinja2.setup(
        app,
        loader=jinja2.FileSystemLoader(str(template_dir)),
    )
    env.globals["static_url"] = "/static"

    static_dir = Path(__file__).parent / "static"
    app.router.add_static("/static", static_dir, name="static")

    setup_routes(app)

    return app
