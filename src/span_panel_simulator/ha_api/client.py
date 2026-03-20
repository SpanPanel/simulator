"""Dual-mode Home Assistant API client.

Detects the runtime environment and configures the correct base URL and
authentication automatically:

- **Add-on mode**: ``SUPERVISOR_TOKEN`` env var is set by the HA Supervisor.
  Base URL is ``http://supervisor/core/api``.
- **Local mode**: User provides ``ha_url`` and ``ha_token`` via CLI args
  or env vars.  Base URL is ``http://<host>:8123/api``.

Both modes hit the same HA REST API surface — the Supervisor endpoint is
just an authenticated proxy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass

import aiohttp

_LOGGER = logging.getLogger(__name__)

_TIMEOUT_S = 15.0
_WS_TIMEOUT_S = 30.0

# Supervisor proxy URL — only reachable from inside an add-on container.
_SUPERVISOR_API_BASE = "http://supervisor/core/api"


@dataclass(frozen=True, slots=True)
class HAConnectionConfig:
    """Resolved connection parameters for the HA REST API."""

    base_url: str
    token: str
    is_supervisor: bool

    @staticmethod
    def from_environment(
        *,
        ha_url: str | None = None,
        ha_token: str | None = None,
    ) -> HAConnectionConfig | None:
        """Resolve connection config from the environment.

        Priority:
          1. ``SUPERVISOR_TOKEN`` or ``HASSIO_TOKEN`` env var (add-on mode)
          2. Explicit ``ha_url`` + ``ha_token`` (local mode)
          3. ``HA_URL`` + ``HA_TOKEN`` env vars (local mode fallback)

        Returns ``None`` if no valid configuration is found — the caller
        should treat HA integration as unavailable.
        """
        # Modern Supervisor uses SUPERVISOR_TOKEN; older versions set
        # HASSIO_TOKEN.  Check both so the add-on works across versions.
        supervisor_token = os.environ.get(
            "SUPERVISOR_TOKEN",
        ) or os.environ.get("HASSIO_TOKEN")
        if supervisor_token:
            _LOGGER.info("HA API: running as add-on (Supervisor token detected)")
            return HAConnectionConfig(
                base_url=_SUPERVISOR_API_BASE,
                token=supervisor_token,
                is_supervisor=True,
            )

        # Log a hint when we appear to be inside an add-on container but
        # no token was injected — most likely the Supervisor metadata
        # needs refreshing (Settings → Add-ons → ⋮ → Check for updates).
        if os.path.isfile("/data/options.json"):
            _LOGGER.warning(
                "HA API: /data/options.json exists (add-on container) but "
                "SUPERVISOR_TOKEN is not set — ensure homeassistant_api is "
                "true in config.yaml and refresh the add-on store",
            )

        url = ha_url or os.environ.get("HA_URL")
        token = ha_token or os.environ.get("HA_TOKEN")

        if url and token:
            # Normalise: ensure the URL ends with /api
            base = url.rstrip("/")
            if not base.endswith("/api"):
                base = f"{base}/api"
            _LOGGER.info("HA API: local mode -> %s", base)
            return HAConnectionConfig(
                base_url=base,
                token=token,
                is_supervisor=False,
            )

        return None


class HAClient:
    """Async HTTP client for the Home Assistant REST API.

    Wraps ``aiohttp`` and provides typed helpers for the specific
    endpoints the simulator needs.  The session is created lazily on
    first use and closed explicitly via :meth:`close`.
    """

    def __init__(self, config: HAConnectionConfig) -> None:
        self._config = config
        self._session: aiohttp.ClientSession | None = None
        self._ws_id = 0

    @property
    def is_supervisor(self) -> bool:
        """Whether we are running inside the HA Supervisor (add-on mode)."""
        return self._config.is_supervisor

    @property
    def _ws_url(self) -> str:
        """WebSocket URL derived from the REST base URL.

        ``http://host:8123/api`` -> ``ws://host:8123/api/websocket``
        ``http://supervisor/core/api`` -> ``ws://supervisor/core/api/websocket``
        """
        base = self._config.base_url
        ws_base = base.replace("http://", "ws://").replace("https://", "wss://")
        return f"{ws_base}/websocket"

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {
                "Authorization": f"Bearer {self._config.token}",
                "Content-Type": "application/json",
            }
            timeout = aiohttp.ClientTimeout(total=_TIMEOUT_S)
            # Skip SSL verification for local dev — HA typically uses
            # self-signed certs.  In add-on mode the connection is
            # over localhost HTTP so this has no effect.
            connector = aiohttp.TCPConnector(ssl=False)
            self._session = aiohttp.ClientSession(
                headers=headers,
                timeout=timeout,
                connector=connector,
            )
        return self._session

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Low-level request helpers
    # ------------------------------------------------------------------

    async def _get(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, object] | list[object]:
        """GET ``{base_url}/{path}`` and return parsed JSON."""
        session = self._ensure_session()
        url = f"{self._config.base_url}/{path.lstrip('/')}"
        async with session.get(url, params=params) as resp:
            if resp.status == 401:
                raise PermissionError("HA API returned 401 — token may be invalid or expired")
            resp.raise_for_status()
            return await resp.json()  # type: ignore[no-any-return]

    async def _post(
        self,
        path: str,
        json_body: dict[str, object] | None = None,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, object] | list[object]:
        """POST ``{base_url}/{path}`` with a JSON body."""
        session = self._ensure_session()
        url = f"{self._config.base_url}/{path.lstrip('/')}"
        async with session.post(url, json=json_body, headers=extra_headers) as resp:
            if resp.status == 401:
                raise PermissionError("HA API returned 401 — token may be invalid or expired")
            resp.raise_for_status()
            return await resp.json()  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # HA REST API: service calls
    # ------------------------------------------------------------------

    async def async_call_service(
        self,
        domain: str,
        service: str,
        service_data: dict[str, object] | None = None,
        *,
        return_response: bool = False,
    ) -> dict[str, object] | list[object]:
        """Call a Home Assistant service.

        Args:
            domain: Service domain (e.g. ``"span_panel"``).
            service: Service name (e.g. ``"export_circuit_manifest"``).
            service_data: Optional data payload for the service call.
            return_response: When ``True``, request that HA return the
                service response data (requires HA 2023.7+).
        """
        path = f"services/{domain}/{service}"
        if return_response:
            path += "?return_response"
        return await self._post(
            path,
            json_body=service_data or {},
        )

    # ------------------------------------------------------------------
    # HA REST API: connectivity check
    # ------------------------------------------------------------------

    async def async_validate(self) -> bool:
        """Validate that the connection and token work.

        Calls ``GET /api/`` which returns ``{"message": "API running."}``
        on success.
        """
        try:
            result = await self._get("/")
            ok = isinstance(result, dict) and "message" in result
            if ok:
                _LOGGER.info("HA API: connection validated")
            else:
                _LOGGER.warning("HA API: unexpected response from /api/: %s", result)
            return ok
        except (aiohttp.ClientError, PermissionError):
            _LOGGER.exception("HA API: validation failed")
            return False

    # ------------------------------------------------------------------
    # HA WebSocket API
    # ------------------------------------------------------------------

    async def _ws_command(self, payload: dict[str, object]) -> dict[str, object]:
        """Execute a single command over the HA WebSocket API.

        Opens a connection, authenticates, sends the command, reads the
        response, and closes.  This is stateless — each call is
        independent.  For high-frequency use, a persistent connection
        would be better, but for profile building (called once on clone
        or on-demand) this is sufficient.
        """
        session = self._ensure_session()
        self._ws_id += 1
        msg_id = self._ws_id

        async with session.ws_connect(
            self._ws_url,
            timeout=aiohttp.ClientWSTimeout(ws_close=_WS_TIMEOUT_S),
            max_msg_size=0,  # Disable limit — statistics responses can be large
        ) as ws:
            # 1. Receive auth_required
            auth_req = await asyncio.wait_for(ws.receive_json(), timeout=_WS_TIMEOUT_S)
            if auth_req.get("type") != "auth_required":
                msg = f"Expected auth_required, got {auth_req.get('type')}"
                raise ConnectionError(msg)

            # 2. Send auth
            await ws.send_json(
                {
                    "type": "auth",
                    "access_token": self._config.token,
                }
            )
            auth_resp = await asyncio.wait_for(ws.receive_json(), timeout=_WS_TIMEOUT_S)
            if auth_resp.get("type") != "auth_ok":
                msg_text = json.dumps(auth_resp)
                raise PermissionError(f"HA WebSocket auth failed: {msg_text}")

            # 3. Send command
            payload["id"] = msg_id
            await ws.send_json(payload)

            # 4. Read response
            result = await asyncio.wait_for(ws.receive_json(), timeout=_WS_TIMEOUT_S)
            if not result.get("success"):
                error = result.get("error", {})
                error_msg = (
                    error.get("message", "Unknown error")
                    if isinstance(error, dict)
                    else str(error)
                )
                msg = f"HA WebSocket command failed: {error_msg}"
                raise RuntimeError(msg)

            raw = result.get("result", {})
            return raw if isinstance(raw, dict) else {}

    # ------------------------------------------------------------------
    # HA WebSocket API: recorder statistics
    # ------------------------------------------------------------------

    async def async_get_statistics(
        self,
        statistic_ids: list[str],
        *,
        period: str = "hour",
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> dict[str, list[dict[str, object]]]:
        """Fetch recorder long-term statistics for the given entity IDs.

        Uses the HA WebSocket API (``recorder/statistics_during_period``)
        which provides pre-aggregated mean/min/max statistics — not
        available via REST.

        Args:
            statistic_ids: List of statistic IDs (usually entity_ids).
            period: Aggregation period — ``"5minute"``, ``"hour"``, or
                ``"month"``.
            start_time: ISO 8601 start (defaults to server's choice).
            end_time: ISO 8601 end (defaults to now).

        Returns a dict mapping statistic_id to a list of statistic dicts,
        each containing ``start``, ``end``, ``mean``, ``min``, ``max``,
        ``sum``, ``state``, etc.
        """
        payload: dict[str, object] = {
            "type": "recorder/statistics_during_period",
            "statistic_ids": statistic_ids,
            "period": period,
        }
        if start_time is not None:
            payload["start_time"] = start_time
        if end_time is not None:
            payload["end_time"] = end_time

        result = await self._ws_command(payload)
        return result if isinstance(result, dict) else {}  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # HA REST API: entity states
    # ------------------------------------------------------------------

    async def async_get_states(self) -> list[dict[str, object]]:
        """Fetch all entity states."""
        result = await self._get("states")
        return result if isinstance(result, list) else []  # type: ignore[return-value]

    async def async_get_state(self, entity_id: str) -> dict[str, object]:
        """Fetch a single entity's current state."""
        result = await self._get(f"states/{entity_id}")
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------
    # HA REST API: location
    # ------------------------------------------------------------------

    async def async_get_home_location(self) -> tuple[float, float] | None:
        """Return (latitude, longitude) from the ``zone.home`` entity.

        Returns ``None`` if the zone entity is missing or malformed.
        """
        try:
            state = await self.async_get_state("zone.home")
            attrs = state.get("attributes")
            if not isinstance(attrs, dict):
                return None
            lat = attrs.get("latitude")
            lon = attrs.get("longitude")
            if isinstance(lat, int | float) and isinstance(lon, int | float):
                return float(lat), float(lon)
        except (aiohttp.ClientError, KeyError):
            _LOGGER.debug("Could not fetch zone.home location", exc_info=True)
        return None
