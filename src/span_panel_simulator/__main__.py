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
    DEFAULT_BROKER_PASSWORD,
    DEFAULT_BROKER_USERNAME,
    DEFAULT_FIRMWARE_VERSION,
    DEFAULT_TICK_INTERVAL_S,
    HTTPS_PORT,
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
        "--firmware",
        default=os.environ.get("FIRMWARE_VERSION", DEFAULT_FIRMWARE_VERSION),
        help="Simulated firmware version",
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
        "--http-port",
        type=int,
        default=int(os.environ.get("HTTP_PORT", str(HTTPS_PORT))),
        help="Bootstrap HTTP server port",
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
        "--advertise-address",
        default=os.environ.get("ADVERTISE_ADDRESS"),
        help="IP address to advertise via mDNS (required when running in a VM)",
    )
    parser.add_argument(
        "--advertise-http-port",
        type=int,
        default=int(os.environ.get("ADVERTISE_HTTP_PORT", "0")) or None,
        help="Port to advertise via mDNS (when host port differs from container port)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    args = _parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    config_dir: Path = args.config_dir
    if not config_dir.is_dir():
        logging.error("Config directory not found: %s", config_dir)
        sys.exit(1)

    # Resolve which config(s) to load
    config_filter: str | None = args.config
    if config_filter:
        # Explicit --config given: load only that file
        config_path = config_dir / config_filter
        if not config_path.exists():
            logging.error("Config file not found: %s", config_path)
            sys.exit(1)
        logging.info("Using config: %s", config_path.name)
    else:
        # No --config: use default_config.yaml if it exists
        default_path = config_dir / "default_config.yaml"
        if default_path.exists():
            config_filter = "default_config.yaml"
            logging.info("Using default config: %s", default_path.name)
        else:
            # Fall back to all configs
            yamls = list(config_dir.glob("*.yaml")) + list(config_dir.glob("*.yml"))
            if not yamls:
                logging.error("No YAML configs found in %s", config_dir)
                sys.exit(1)
            logging.info("Found %d config(s) in %s", len(yamls), config_dir)

    app = SimulatorApp(
        config_dir=config_dir,
        config_filter=config_filter,
        tick_interval=args.tick_interval,
        firmware_version=args.firmware,
        broker_host=args.broker_host,
        broker_port=args.broker_port,
        http_port=args.http_port,
        broker_username=args.broker_username,
        broker_password=args.broker_password,
        cert_dir=args.cert_dir,
        advertise_address=args.advertise_address,
        advertise_http_port=args.advertise_http_port,
    )

    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        logging.info("Interrupted — shutting down")


if __name__ == "__main__":
    main()
