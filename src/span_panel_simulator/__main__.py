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
        "--config",
        type=Path,
        default=Path(os.environ.get("SIMULATION_CONFIG", "configs/simulation_config.yaml")),
        help="Path to YAML simulation config",
    )
    parser.add_argument(
        "--serial",
        default=os.environ.get("PANEL_SERIAL"),
        help="Override panel serial number",
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

    if not args.config.exists():
        logging.error("Config file not found: %s", args.config)
        sys.exit(1)

    app = SimulatorApp(
        config_path=args.config,
        serial_override=args.serial,
        tick_interval=args.tick_interval,
        firmware_version=args.firmware,
        broker_host=args.broker_host,
        broker_port=args.broker_port,
        http_port=args.http_port,
        broker_username=args.broker_username,
        broker_password=args.broker_password,
        cert_dir=args.cert_dir,
    )

    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        logging.info("Interrupted — shutting down")


if __name__ == "__main__":
    main()
