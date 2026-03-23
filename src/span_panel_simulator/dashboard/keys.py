"""AppKey instances for dashboard application state.

Using web.AppKey instead of string keys avoids NotAppKeyWarning and
improves type safety. See: https://docs.aiohttp.org/en/stable/web_advanced.html#application-s-config
"""

from __future__ import annotations

from aiohttp import web

from span_panel_simulator.dashboard.config_store import ConfigStore
from span_panel_simulator.dashboard.presets import PresetRegistry

# DashboardContext defined in __init__.py; avoid circular import by using object.
# _store, _ctx, _presets in routes.py provide typed access.
APP_KEY_STORE = web.AppKey("store", ConfigStore)
APP_KEY_DASHBOARD_CONTEXT = web.AppKey("dashboard_context", object)
APP_KEY_PRESET_REGISTRY = web.AppKey("preset_registry", PresetRegistry)
