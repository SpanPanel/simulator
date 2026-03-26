"""Synthetic history generator — builds companion SQLite databases.

Given a panel config YAML, generates a year of synthetic power statistics
matching HA's recorder schema.  The output SQLite file can be read by
``SqliteHistoryProvider`` and fed to ``RecorderDataSource`` for replay.

Time windows:
  - ``[anchor - 1 year, anchor - 10 days]``: hourly rows in ``statistics``
  - ``[anchor - 10 days, anchor]``: 5-minute rows in ``statistics_short_term``

Uses the same modulation infrastructure as the live simulation engine:
solar curves, weather degradation, HVAC seasonal model, time-of-day
profiles, cycling patterns, and monthly factors.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from span_panel_simulator.hvac import hvac_seasonal_factor
from span_panel_simulator.solar import daily_weather_factor, solar_production_factor
from span_panel_simulator.sqlite_history import SCHEMA_SQL
from span_panel_simulator.weather import fetch_historical_weather, get_cached_weather

_LOGGER = logging.getLogger(__name__)

_SECONDS_PER_HOUR = 3600
_SECONDS_PER_5MIN = 300
_DAYS_SHORT_TERM = 10
_DAYS_TOTAL = 365


def _deterministic_noise(panel_serial: str, circuit_id: str, start_ts: float) -> float:
    """Deterministic per-row noise in [-1, 1], seeded from identity + timestamp."""
    raw = f"{panel_serial}:{circuit_id}:{start_ts}".encode()
    h = int(hashlib.sha256(raw).hexdigest()[:8], 16)
    return (h % 20000 - 10000) / 10000.0


def _resolve_timezone(config: dict[str, object]) -> ZoneInfo:
    """Resolve panel timezone from config, matching engine logic."""
    panel = config.get("panel_config", {})
    if not isinstance(panel, dict):
        return ZoneInfo("America/Los_Angeles")

    explicit = panel.get("time_zone")
    if isinstance(explicit, str) and explicit:
        try:
            return ZoneInfo(explicit)
        except (KeyError, ValueError):
            pass

    lat = panel.get("latitude")
    lon = panel.get("longitude")
    if lat is not None and lon is not None:
        from timezonefinder import TimezoneFinder

        tz_name = TimezoneFinder().timezone_at(lat=float(lat), lng=float(lon))
        if tz_name is not None:
            return ZoneInfo(tz_name)

    return ZoneInfo("America/Los_Angeles")


class SyntheticHistoryGenerator:
    """Generate companion SQLite history databases from panel config YAMLs."""

    async def generate(
        self,
        config_path: Path,
        *,
        anchor_time: float | None = None,
        years: int | None = None,
    ) -> Path:
        """Generate the companion history DB for a config file.

        Args:
            config_path: Path to the panel YAML config.
            anchor_time: Unix epoch for the "now" end of the window.
                Defaults to current time.
            years: Number of years of history to generate.  Overrides the
                module-level ``_DAYS_TOTAL`` constant when provided.

        Returns:
            Path to the generated ``_history.db`` file.
        """
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            msg = f"Invalid config: {config_path}"
            raise ValueError(msg)

        anchor = anchor_time if anchor_time is not None else time.time()
        days_total = (years * 365) if years is not None else _DAYS_TOTAL
        db_path = config_path.with_name(config_path.stem + "_history.db")

        panel_config = raw.get("panel_config", {})
        if not isinstance(panel_config, dict):
            msg = "Missing panel_config"
            raise ValueError(msg)

        serial = str(panel_config.get("serial_number", "unknown"))
        lat = float(panel_config.get("latitude", 37.7))
        lon = float(panel_config.get("longitude", -122.4))
        tz = _resolve_timezone(raw)
        sim_params = raw.get("simulation_params", {})
        noise_factor = float(
            sim_params.get("noise_factor", 0.02) if isinstance(sim_params, dict) else 0.02
        )

        # Fetch weather data for solar degradation (best-effort)
        weather_monthly: dict[int, float] | None = None
        cached = get_cached_weather(lat, lon)
        if cached is not None:
            weather_monthly = cached.monthly_factors
        else:
            try:
                wd = await fetch_historical_weather(lat, lon)
                weather_monthly = wd.monthly_factors
            except Exception:
                _LOGGER.debug("Weather fetch failed; using deterministic model", exc_info=True)

        # Collect circuits with recorder_entity mappings
        templates = raw.get("circuit_templates", {})
        if not isinstance(templates, dict):
            templates = {}

        circuits_to_generate: list[tuple[str, str, dict[str, object]]] = []
        for tmpl_name, tmpl in templates.items():
            if not isinstance(tmpl, dict):
                continue
            entity = tmpl.get("recorder_entity")
            if isinstance(entity, str) and entity:
                circuits_to_generate.append((tmpl_name, entity, tmpl))

        if not circuits_to_generate:
            _LOGGER.warning(
                "No recorder_entity mappings in %s — nothing to generate",
                config_path.name,
            )
            con = sqlite3.connect(str(db_path))
            con.executescript(SCHEMA_SQL)
            con.close()
            return db_path

        # Compute time boundaries
        hourly_start = anchor - days_total * 86400
        short_term_start = anchor - _DAYS_SHORT_TERM * 86400
        hourly_end = short_term_start

        _LOGGER.info(
            "Generating synthetic history for %s: %d circuits, anchor=%s",
            config_path.name,
            len(circuits_to_generate),
            datetime.fromtimestamp(anchor, tz=UTC).isoformat(),
        )

        con = sqlite3.connect(str(db_path))
        con.executescript(SCHEMA_SQL)

        # Clear any existing data (regeneration case)
        con.execute("DELETE FROM statistics")
        con.execute("DELETE FROM statistics_short_term")
        con.execute("DELETE FROM statistics_meta")

        try:
            for idx, (tmpl_name, entity_id, tmpl) in enumerate(circuits_to_generate, start=1):
                con.execute(
                    "INSERT INTO statistics_meta "
                    "(id, statistic_id, source, unit_of_measurement, name) "
                    "VALUES (?, ?, 'simulator', 'W', ?)",
                    (idx, entity_id, tmpl_name),
                )

                self._generate_rows(
                    con=con,
                    table="statistics",
                    metadata_id=idx,
                    entity_id=entity_id,
                    template=tmpl,
                    start_ts=hourly_start,
                    end_ts=hourly_end,
                    step_seconds=_SECONDS_PER_HOUR,
                    serial=serial,
                    lat=lat,
                    lon=lon,
                    tz=tz,
                    noise_factor=noise_factor,
                    weather_monthly=weather_monthly,
                )

                self._generate_rows(
                    con=con,
                    table="statistics_short_term",
                    metadata_id=idx,
                    entity_id=entity_id,
                    template=tmpl,
                    start_ts=short_term_start,
                    end_ts=anchor,
                    step_seconds=_SECONDS_PER_5MIN,
                    serial=serial,
                    lat=lat,
                    lon=lon,
                    tz=tz,
                    noise_factor=noise_factor,
                    weather_monthly=weather_monthly,
                )

            con.commit()
        finally:
            con.close()

        _LOGGER.info("Wrote synthetic history to %s", db_path.name)
        return db_path

    def _generate_rows(
        self,
        *,
        con: sqlite3.Connection,
        table: str,
        metadata_id: int,
        entity_id: str,
        template: dict[str, object],
        start_ts: float,
        end_ts: float,
        step_seconds: int,
        serial: str,
        lat: float,
        lon: float,
        tz: ZoneInfo,
        noise_factor: float,
        weather_monthly: dict[int, float] | None,
    ) -> None:
        """Generate statistics rows for one circuit into the given table."""
        ep = template.get("energy_profile", {})
        if not isinstance(ep, dict):
            return

        mode = str(ep.get("mode", "consumer"))
        typical_power = float(ep.get("typical_power", 0.0))
        nameplate_w = ep.get("nameplate_capacity_w")
        nameplate = float(nameplate_w) if nameplate_w is not None else None

        # Time-of-day profile
        tod_profile = template.get("time_of_day_profile", {})
        tod_enabled = isinstance(tod_profile, dict) and bool(tod_profile.get("enabled", False))
        hour_factors: dict[int, float] = {}
        if isinstance(tod_profile, dict):
            raw_hf = tod_profile.get("hour_factors", {})
            if isinstance(raw_hf, dict):
                hour_factors = {int(k): float(v) for k, v in raw_hf.items()}

        # Monthly factors
        monthly_factors: dict[int, float] = {}
        raw_mf = template.get("monthly_factors")
        if isinstance(raw_mf, dict):
            monthly_factors = {int(k): float(v) for k, v in raw_mf.items()}

        # HVAC type
        hvac_type = template.get("hvac_type")
        hvac_type_str = str(hvac_type) if isinstance(hvac_type, str) else None

        # Cycling pattern
        cycling = template.get("cycling_pattern")
        duty_cycle: float | None = None
        if isinstance(cycling, dict):
            dc = cycling.get("duty_cycle")
            if dc is not None:
                duty_cycle = float(dc)
            else:
                on_dur = cycling.get("on_duration")
                off_dur = cycling.get("off_duration")
                if on_dur is not None and off_dur is not None:
                    total = int(on_dur) + int(off_dur)
                    if total > 0:
                        duty_cycle = int(on_dur) / total

        # Battery behavior (BESS schedule)
        battery_behavior_raw = template.get("battery_behavior")
        battery_behavior: dict[str, object] | None = None
        if isinstance(battery_behavior_raw, dict) and bool(
            battery_behavior_raw.get("enabled", False)
        ):
            battery_behavior = battery_behavior_raw

        # Active days from time_of_day_profile
        active_days: list[int] = []
        if isinstance(tod_profile, dict):
            ad = tod_profile.get("active_days", [])
            if isinstance(ad, list):
                active_days = [int(d) for d in ad]

        # Power range for clamping
        power_range = ep.get("power_range", [0, 10000])
        if isinstance(power_range, list) and len(power_range) == 2:
            min_power, max_power = float(power_range[0]), float(power_range[1])
        else:
            min_power, max_power = 0.0, 10000.0

        # Mean of hour factors for normalisation
        mean_hf = sum(hour_factors.values()) / len(hour_factors) if hour_factors else 1.0

        # Mean of monthly factors for normalisation
        mean_mf = sum(monthly_factors.values()) / len(monthly_factors) if monthly_factors else 1.0

        batch: list[tuple[object, ...]] = []
        ts = start_ts
        while ts < end_ts:
            power = self._compute_power_at(
                ts=ts,
                mode=mode,
                typical_power=typical_power,
                nameplate=nameplate,
                lat=lat,
                lon=lon,
                tz=tz,
                serial=serial,
                hour_factors=hour_factors,
                mean_hf=mean_hf,
                tod_enabled=tod_enabled,
                monthly_factors=monthly_factors,
                mean_mf=mean_mf,
                hvac_type=hvac_type_str,
                duty_cycle=duty_cycle,
                active_days=active_days,
                weather_monthly=weather_monthly,
                battery_behavior=battery_behavior,
            )

            # Apply deterministic noise
            noise = _deterministic_noise(serial, entity_id, ts)
            noisy_power = power * (1.0 + noise * noise_factor)

            # Clamp
            if mode == "producer":
                noisy_power = max(0.0, min(abs(min_power), noisy_power))
            else:
                noisy_power = max(min_power, min(max_power, noisy_power))

            mean_val = noisy_power
            min_val = mean_val * (1.0 - noise_factor)
            max_val = mean_val * (1.0 + noise_factor)

            batch.append((metadata_id, ts, ts, mean_val, min_val, max_val))

            if len(batch) >= 1000:
                con.executemany(
                    f"INSERT INTO {table} "
                    "(metadata_id, created_ts, start_ts, mean, min, max) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    batch,
                )
                batch.clear()

            ts += step_seconds

        if batch:
            con.executemany(
                f"INSERT INTO {table} "
                "(metadata_id, created_ts, start_ts, mean, min, max) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                batch,
            )

    def _compute_power_at(
        self,
        *,
        ts: float,
        mode: str,
        typical_power: float,
        nameplate: float | None,
        lat: float,
        lon: float,
        tz: ZoneInfo,
        serial: str,
        hour_factors: dict[int, float],
        mean_hf: float,
        tod_enabled: bool,
        monthly_factors: dict[int, float],
        mean_mf: float,
        hvac_type: str | None,
        duty_cycle: float | None,
        active_days: list[int],
        weather_monthly: dict[int, float] | None,
        battery_behavior: dict[str, object] | None = None,
    ) -> float:
        """Compute synthetic power for one time step."""
        dt = datetime.fromtimestamp(ts, tz=tz)
        hour = dt.hour
        weekday = dt.weekday()
        month = dt.month

        if active_days and weekday not in active_days:
            return 0.0

        # BESS schedule takes priority over consumer/producer logic
        if battery_behavior is not None:
            charge_hours_raw = battery_behavior.get("charge_hours", [])
            discharge_hours_raw = battery_behavior.get("discharge_hours", [])
            idle_hours_raw = battery_behavior.get("idle_hours", [])
            charge_hours = list(charge_hours_raw) if isinstance(charge_hours_raw, list) else []
            discharge_hours = (
                list(discharge_hours_raw) if isinstance(discharge_hours_raw, list) else []
            )
            idle_hours = list(idle_hours_raw) if isinstance(idle_hours_raw, list) else []

            if hour in charge_hours:
                max_charge = battery_behavior.get("max_charge_power")
                if isinstance(max_charge, int | float):
                    return -float(max_charge)
                return -typical_power

            if hour in discharge_hours:
                max_discharge = battery_behavior.get("max_discharge_power")
                if isinstance(max_discharge, int | float):
                    return float(max_discharge)
                return typical_power

            if hour in idle_hours:
                idle_range = battery_behavior.get("idle_power_range")
                if isinstance(idle_range, list) and len(idle_range) == 2:
                    return float(idle_range[0])
                return 0.0

        base = typical_power

        if mode == "producer":
            scale = abs(nameplate) if nameplate is not None and nameplate > 0 else abs(base)
            solar = solar_production_factor(ts, lat, lon)
            weather = daily_weather_factor(ts, seed=hash(serial), monthly_factors=weather_monthly)
            return scale * solar * weather

        # Time-of-day for consumers
        if hour_factors and tod_enabled:
            factor = hour_factors.get(hour, 0.0)
            base = typical_power / mean_hf * factor if mean_hf > 0 else 0.0
        elif tod_enabled:
            if hour >= 22 or hour <= 6:
                base = typical_power * 0.3
            elif hour in range(7, 22):
                base = typical_power

        # Monthly/seasonal modulation
        if monthly_factors:
            mf = monthly_factors.get(month, 1.0)
            if mean_mf > 0:
                base = base / mean_mf * mf
        elif hvac_type is not None:
            base = base * hvac_seasonal_factor(ts, lat, hvac_type, tz=tz)

        # Cycling: reduce by duty cycle
        if duty_cycle is not None and duty_cycle < 1.0:
            base = base * duty_cycle

        return base


async def _cli_main() -> None:
    """CLI entry point for standalone generation."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate synthetic history DB from a panel config YAML",
    )
    parser.add_argument("config", type=Path, help="Path to the panel YAML config")
    parser.add_argument(
        "--anchor-time",
        type=float,
        default=None,
        help="Unix epoch for the anchor (default: now)",
    )
    parser.add_argument(
        "--years",
        type=int,
        default=1,
        help="Number of years of history to generate (default: 1)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    gen = SyntheticHistoryGenerator()
    db_path = await gen.generate(args.config, anchor_time=args.anchor_time, years=args.years)
    print(f"Generated: {db_path}")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_cli_main())
