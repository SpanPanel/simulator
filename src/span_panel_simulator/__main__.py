"""Entry point for the SPAN panel eBus simulator."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from span_panel_simulator.app import SimulatorApp
from span_panel_simulator.const import (
    DASHBOARD_PORT,
    DEFAULT_BASE_HTTP_PORT,
    DEFAULT_BROKER_PASSWORD,
    DEFAULT_BROKER_USERNAME,
    DEFAULT_TICK_INTERVAL_S,
    MQTTS_PORT,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="span-simulator",
        description="Standalone eBus simulator for SPAN panels",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path(os.environ.get("CONFIG_DIR", "configs")),
        help="Directory containing YAML simulation configs (one per panel)",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("CONFIG_NAME"),
        help="Name of a specific config file to load (e.g., default_config.yaml). "
        "When omitted, loads default_config.yaml if it exists, otherwise all configs.",
    )
    parser.add_argument(
        "--tick-interval",
        type=float,
        default=float(os.environ.get("TICK_INTERVAL", str(DEFAULT_TICK_INTERVAL_S))),
        help="Seconds between simulation ticks",
    )
    parser.add_argument(
        "--broker-host",
        default=os.environ.get("BROKER_HOST", "localhost"),
        help="MQTT broker hostname",
    )
    parser.add_argument(
        "--broker-port",
        type=int,
        default=int(os.environ.get("BROKER_PORT", str(MQTTS_PORT))),
        help="MQTT broker port",
    )
    parser.add_argument(
        "--base-http-port",
        type=int,
        default=int(os.environ.get("HTTP_PORT", str(DEFAULT_BASE_HTTP_PORT))),
        help="Base port for per-panel bootstrap HTTP servers. "
        "First panel uses this port, second uses port+1, etc.",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=None,
        help="Deprecated: use --base-http-port instead",
    )
    parser.add_argument(
        "--broker-username",
        default=os.environ.get("BROKER_USERNAME", DEFAULT_BROKER_USERNAME),
    )
    parser.add_argument(
        "--broker-password",
        default=os.environ.get("BROKER_PASSWORD", DEFAULT_BROKER_PASSWORD),
    )
    parser.add_argument(
        "--cert-dir",
        type=Path,
        default=Path(os.environ.get("CERT_DIR", "/tmp/span-sim-certs")),
        help="Directory for generated TLS certificates",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=int(os.environ.get("DASHBOARD_PORT", str(DASHBOARD_PORT))),
        help="Port for the configuration dashboard (default: 8080)",
    )
    parser.add_argument(
        "--advertise-address",
        default=os.environ.get("ADVERTISE_ADDRESS"),
        help="IP address to advertise via mDNS (required when running in a VM)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    # Home Assistant API — for local development.  When running as an
    # add-on, SUPERVISOR_TOKEN is injected automatically and these are
    # not needed.
    parser.add_argument(
        "--ha-url",
        default=os.environ.get("HA_URL"),
        help="Home Assistant URL (e.g. http://192.168.1.10:8123). "
        "Not needed when running as an HA add-on.",
    )
    parser.add_argument(
        "--ha-token",
        default=os.environ.get("HA_TOKEN"),
        help="Long-lived access token for Home Assistant. "
        "Not needed when running as an HA add-on.",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    args = _parse_args(argv)

    # Resolve deprecated --http-port alias
    base_http_port = args.base_http_port
    if args.http_port is not None:
        logging.warning("--http-port is deprecated, use --base-http-port instead")
        base_http_port = args.http_port

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    # Suppress noisy per-request access logs unless explicitly at DEBUG.
    logging.getLogger("aiohttp.access").setLevel(logging.DEBUG)

    config_dir: Path = args.config_dir
    if not config_dir.is_dir():
        logging.error("Config directory not found: %s", config_dir)
        sys.exit(1)

    # Resolve which config(s) to load.
    # When --config is given explicitly, that panel auto-starts.
    # Otherwise, resume the last-used config if saved.
    # If nothing to resume, start idle (empty filter) so the dashboard
    # is ready for the user to choose a config.
    config_filter: str | None = args.config
    if config_filter:
        config_path = config_dir / config_filter
        if not config_path.exists():
            logging.error("Config file not found: %s", config_path)
            sys.exit(1)
        logging.info("Using config: %s", config_path.name)
    else:
        last_config_file = config_dir / ".last_config"
        if last_config_file.exists():
            last_name = last_config_file.read_text(encoding="utf-8").strip()
            if last_name and (config_dir / last_name).exists():
                config_filter = last_name
                logging.info("Resuming last config: %s", last_name)

        if config_filter is None:
            config_filter = ""  # idle — no panel until user picks one
            logging.info("No config to resume — dashboard ready, no panel running")

    # Resolve HA API connection (add-on mode auto-detects via env var)
    from span_panel_simulator.ha_api.client import HAConnectionConfig

    ha_config = HAConnectionConfig.from_environment(
        ha_url=args.ha_url,
        ha_token=args.ha_token,
    )
    if ha_config:
        logging.info("HA API configured: %s", ha_config.base_url)
    else:
        logging.info("HA API not configured — running without HA integration")

    app = SimulatorApp(
        config_dir=config_dir,
        config_filter=config_filter,
        tick_interval=args.tick_interval,
        broker_host=args.broker_host,
        broker_port=args.broker_port,
        base_http_port=base_http_port,
        broker_username=args.broker_username,
        broker_password=args.broker_password,
        cert_dir=args.cert_dir,
        dashboard_port=args.dashboard_port,
        advertise_address=args.advertise_address,
        ha_config=ha_config,
    )

    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        logging.info("Interrupted — shutting down")


if __name__ == "__main__":
    main()
