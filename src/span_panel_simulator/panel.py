"""PanelInstance — encapsulates a single simulated panel.

Each instance owns a DynamicSimulationEngine and HomiePublisher pair,
running an independent tick loop that publishes state changes to the
shared MQTT broker under its own serial-based topic namespace.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from span_panel_simulator.engine import DynamicSimulationEngine
from span_panel_simulator.homie_const import HOMIE_STATE_DISCONNECTED, STATE_TOPIC_FMT
from span_panel_simulator.publisher import HomiePublisher

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from pathlib import Path
    from typing import Any

    from span_panel_simulator.recorder import RecorderDataSource
    from span_panel_simulator.schema import HomieSchemaRegistry

_LOGGER = logging.getLogger(__name__)


class PanelInstance:
    """A single simulated panel with its own engine and publisher."""

    def __init__(
        self,
        config_path: Path,
        publish_fn: Callable[[str, str, bool], Coroutine[Any, Any, None]],
        *,
        tick_interval: float = 1.0,
        schema: HomieSchemaRegistry | None = None,
        recorder: RecorderDataSource | None = None,
    ) -> None:
        self._config_path = config_path
        self._publish_fn = publish_fn
        self._tick_interval = tick_interval
        self._schema = schema
        self._recorder = recorder

        self._engine: DynamicSimulationEngine | None = None
        self._publisher: HomiePublisher | None = None
        self._tick_task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def config_path(self) -> Path:
        return self._config_path

    @property
    def serial_number(self) -> str:
        if self._engine is None:
            msg = "Panel not initialised — call start() first"
            raise RuntimeError(msg)
        return self._engine.serial_number

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def engine(self) -> DynamicSimulationEngine | None:
        return self._engine

    @property
    def publisher(self) -> HomiePublisher | None:
        return self._publisher

    async def start(self) -> str:
        """Initialise the engine and begin the tick loop.

        Returns the panel's serial number.
        """
        engine = DynamicSimulationEngine(
            config_path=self._config_path,
            recorder=self._recorder,
        )
        await engine.initialize_async()
        self._engine = engine

        serial = engine.serial_number
        self._publisher = HomiePublisher(
            serial_number=serial,
            publish_fn=self._publish_fn,
            schema=self._schema,
        )

        # Initial full publish
        snapshot = await engine.get_snapshot()
        await self._publisher.publish_init(snapshot)

        self._running = True
        self._tick_task = asyncio.create_task(self._tick_loop(), name=f"tick-{serial}")

        _LOGGER.info("Panel %s started (config=%s)", serial, self._config_path.name)
        return serial

    async def stop(self) -> None:
        """Stop the tick loop and publish disconnected state."""
        self._running = False

        if self._tick_task is not None:
            self._tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tick_task
            self._tick_task = None

        # Publish $state = disconnected
        if self._engine is not None:
            topic = STATE_TOPIC_FMT.format(serial=self._engine.serial_number)
            try:
                await self._publish_fn(topic, HOMIE_STATE_DISCONNECTED, True)
            except Exception:
                _LOGGER.debug("Failed to publish disconnect for %s", self._engine.serial_number)

        serial = self._engine.serial_number if self._engine else "unknown"
        self._engine = None
        self._publisher = None
        _LOGGER.info("Panel %s stopped", serial)

    async def reload(self) -> str:
        """Stop, re-read configuration, and restart.

        Returns the (possibly changed) serial number.
        """
        _LOGGER.info("Reloading panel from %s", self._config_path.name)
        await self.stop()
        return await self.start()

    async def _tick_loop(self) -> None:
        """Produce snapshots and publish diffs on each tick."""
        assert self._engine is not None
        assert self._publisher is not None

        while self._running:
            await asyncio.sleep(self._tick_interval)
            snapshot = await self._engine.get_snapshot()
            await self._publisher.publish_diff(snapshot)
