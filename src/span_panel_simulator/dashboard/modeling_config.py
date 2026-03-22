"""Which panel YAML file modeling API calls should target."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from span_panel_simulator.dashboard import DashboardContext


def resolve_modeling_config_filename(
    ctx: DashboardContext,
    query_value: str | None,
) -> str | None:
    """Resolve YAML basename: valid ``?config=`` query, else editor active file."""
    if query_value:
        raw = query_value.strip()
        path = ctx.config_dir / raw
        if (
            raw
            and "/" not in raw
            and "\\" not in raw
            and raw not in (".", "..")
            and path.is_file()
            and path.suffix.lower() in (".yaml", ".yml")
            and path.resolve().parent == ctx.config_dir.resolve()
        ):
            return raw
    return ctx.config_filter
