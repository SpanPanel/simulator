"""Dashboard application for the SPAN panel simulator.

Runs as a standalone aiohttp server on its own port (default 8080)
and provides a web UI for importing, editing, and exporting panel
configurations.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from span_panel_simulator.history import HistoryProvider

import aiohttp_jinja2
import jinja2
from aiohttp import web

from span_panel_simulator.dashboard.config_store import ConfigStore
from span_panel_simulator.dashboard.presets import init_presets
from span_panel_simulator.dashboard.routes import setup_routes


@dataclass
class DashboardContext:
    """Clean interface boundary between the dashboard and SimulatorApp."""

    config_dir: Path
    config_filter: str | None
    get_panel_configs: Callable[[], dict[Path, str]]  # path -> serial
    request_reload: Callable[[], None]
    set_config_filter: Callable[[str | None], None] = lambda _: None
    start_panel: Callable[[str], None] = lambda _: None
    stop_panel: Callable[[str], None] = lambda _: None
    restart_panel: Callable[[str], None] = lambda _: None
    get_power_summary: Callable[[], dict[str, Any] | None] = lambda: None
    set_simulation_time: Callable[[str], None] = lambda _: None
    set_time_acceleration: Callable[[float], None] = lambda _: None
    set_grid_online: Callable[[bool], None] = lambda _: None
    set_grid_islandable: Callable[[bool], None] = lambda _: None
    set_circuit_priority: Callable[[str, str], None] = lambda _id, _pri: None
    set_circuit_relay: Callable[[str, str], None] = lambda _id, _state: None
    ha_client: Any = None  # HAClient | None — optional, set when HA API is available
    history_provider: HistoryProvider | None = None


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
    app["preset_registry"] = init_presets(context.config_dir)

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
