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
    from collections.abc import Awaitable, Callable

    from span_panel_simulator.history import HistoryProvider

import aiohttp_jinja2
import jinja2
from aiohttp import web

from span_panel_simulator.dashboard.config_store import ConfigStore
from span_panel_simulator.dashboard.presets import init_presets
from span_panel_simulator.dashboard.routes import setup_routes


async def _noop_modeling_data(_hours: int) -> dict[str, Any] | None:
    return None


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
    get_modeling_data: Callable[[int], Awaitable[dict[str, Any] | None]] = _noop_modeling_data
    ha_client: Any = None  # HAClient | None — optional, set when HA API is available
    history_provider: HistoryProvider | None = None
    panel_browser: Any = None  # PanelBrowser | None — mDNS discovery for standalone mode


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

    app["store"] = store
    app["dashboard_context"] = context
    app["preset_registry"] = init_presets(context.config_dir)

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
