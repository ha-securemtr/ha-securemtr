"""Local BLE commissioning transport and RPC helpers."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import IntEnum
import json
import logging
import math
import random
import secrets
import time
from typing import Any, TYPE_CHECKING
from uuid import UUID

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

try:  # pragma: no cover - depends on runtime environment
    from bleak import BleakClient
    from bleak.exc import BleakError
except ImportError:  # pragma: no cover - depends on runtime environment
    BleakClient = None

    class BleakError(Exception):
        """Fallback Bleak error type when bleak is unavailable."""


try:  # pragma: no cover - depends on runtime environment
    from bleak_retry_connector import establish_connection
except ImportError:  # pragma: no cover - depends on runtime environment
    establish_connection = None


_LOGGER = logging.getLogger(__name__)

UART_SERVICE_UUID = UUID("b973f2e0-b19e-11e2-9e96-0800200c9a66")
UART_RX_UUID = UUID("da73f3e1-b19e-11e2-9e96-0800200c9a66")
UART_TX_UUID = UUID("e973f2e2-b19e-11e2-9e96-0800200c9a66")

PACKET_SIZE = 19
FINAL_PACKET_INDEX = 255
BLE_DISCOVERY_TIMEOUT_SECONDS = 20.0
BLE_CONNECT_TIMEOUT_SECONDS = 15.0
# The meter answers within ~3 s or not at all on that connection, so fail fast
# and spend the time on a fresh reconnect rather than waiting out a dead request.
BLE_REQUEST_TIMEOUT_SECONDS = 8.0
BLE_CONSUMPTION_REQUEST_TIMEOUT_SECONDS = 5.0
# Settle delay before each packet write; sending a multi-packet payload as fast
# as the GATT stack allows appears to overwhelm the meter.
BLE_INTER_PACKET_DELAY_SECONDS = 0.1

SERVICE_ID_HAN_MANAGEMENT = 11
SERVICE_ID_BBSERVER = 1
SERVICE_ID_COMMS_MANAGER = 151
SERVICE_ID_TIME_MANAGEMENT = 103
SERVICE_ID_WLAN = 102
SERVICE_ID_MODE = 15
SERVICE_ID_MODE_LEGACY = 13
SERVICE_ID_HOT_WATER = 16
SERVICE_ID_PRIMARY_STATE = 33
SERVICE_ID_DYNAMIC_TARIFF = 36
HANDLER_WRITE_DATA = 2
HANDLER_SET_TIME = 2
HANDLER_GET_ALL_SERVICE_VALUES = 3
HANDLER_GET_CONSUMPTION_STATE = 9
HANDLER_SET_TIMEZONE_ID = 7
HANDLER_SET_OWNER_IN_RECEIVER = 7
HANDLER_UPDATE_PHYSICAL_DEVICE_DETAILS = 6
HANDLER_GET_BLE_KEY = 50
HANDLER_SET_WIFI_CREDENTIAL = 5
HANDLER_ADD_HAN_DEVICES = 48

E7_PLUS_HARDWARE_TYPE = 65
E7_PLUS_CHANNEL_COUNT = 2
MODE_HOME_VALUE = 2
DEVICE_CHARACTERISTIC_HOT_WATER_STATE = 4
DEVICE_CHARACTERISTIC_MODE = 6
DEVICE_CHARACTERISTIC_ACTIVE_ENERGY = 19
DEVICE_CHARACTERISTIC_SCHEDULE_ENABLE_DISABLE = 27
OVERRIDE_TYPE_ADVANCE = 2
DEFAULT_TIMEZONE_ID = 2
SET_OWNER_TIMEOUT_ERROR = 20001
SET_OWNER_RETRY_LIMIT = 5
# Transient drops clear within one reconnect; more retries only prolong a
# sustained drop on the serial worker.
BLE_COMMAND_RETRY_LIMIT = 3
# The serial worker cannot preempt an in-flight job, so cap the refresh's retries
# to bound how long it can delay a queued user command. It is self-healing, so
# one reconnect is enough.
BLE_BACKGROUND_REFRESH_RETRY_LIMIT = 1

# Time between requests after a failure of any kind
BLE_COMMAND_RETRY_DELAY_SECONDS = 3.0
BLE_NOTIFY_RECOVERY_DELAY_SECONDS = 0.25
BLE_DEBUG_PAYLOAD_MAX_CHARS = 4000
BLE_SESSION_IDLE_TIMEOUT_SECONDS = 30.0
BLE_RESPONSE_TIMEOUT_MESSAGE = "Timed out waiting for BLE response"

BLE_ACK_SUCCESS = 0
BLE_ACK_INVALID_MESSAGE = 1
BLE_ACK_KEY_MISMATCH = 2

DEFAULT_OWNER_EMAIL = "homeassistant@local"

_APP_TIMEZONE_ID_LOOKUP: dict[str, int] = {
    "asia/dubai": 1,
    "asia/kolkata": 2,
    "asia/calcutta": 2,
    "ist": 2,
    "europe/london": 3,
    "utc": 3,
    "gmt": 3,
    "europe/paris": 4,
    "europe/stockholm": 5,
    "australia/sydney": 6,
    "austraila/nsw": 6,
    "australia/melbourne": 7,
    "austraila/victoria": 7,
    "antarctica/macquarie": 8,
    "australia/currie": 9,
    "australia/hobart": 10,
    "austraila/tasmania": 10,
    "australia/lord_howe": 11,
    "australia/canberra": 12,
    "australia/adelaide": 13,
    "austraila/south": 13,
    "australia/broken_hill": 14,
    "australia/brisbane": 15,
    "austraila/queensland": 15,
    "australia/lindeman": 16,
    "australia/darwin": 17,
    "austraila/north": 17,
    "australia/eucla": 18,
    "australia/perth": 19,
    "austraila/west": 19,
    "asia/singapore": 20,
    "asia/brunei": 20,
    "sst": 20,
    "asia/riyadh": 21,
    "asia/kuwait": 22,
    "asia/muscat": 23,
    "asia/qatar": 24,
    "asia/bahrain": 25,
    "asia/kuala_lumpur": 26,
    "asia/kuching": 26,
    "mst": 26,
    "asia/bangkok": 27,
    "tha": 27,
    "asia/aden": 28,
    "ast": 21,
}

SERVICE_ID_SCHEDULE = 17
HANDLER_READ_WEEKLY_PROGRAM = 22
HANDLER_WRITE_WEEKLY_PROGRAM = 21

PROGRAM_ZONE_INDEX: dict[str, int] = {
    "primary": 1,
    "boost": 2,
}

if TYPE_CHECKING:
    from .beanbag import WeeklyProgram


class LocalBleCommissioningError(HomeAssistantError):
    """Raised when local BLE commissioning cannot complete."""

    def __init__(self, message: str, *, error_code: int | None = None) -> None:
        """Initialize the commissioning error with an optional RPC code."""

        super().__init__(message)
        self.error_code = error_code


def _is_notify_already_acquired_error(error: Exception) -> bool:
    """Return True when BlueZ reports notifications already acquired."""

    message = str(error).lower()
    return "notify acquired" in message or (
        "notpermitted" in message and "notify" in message
    )


def _format_debug_payload(
    payload: Any, *, max_chars: int = BLE_DEBUG_PAYLOAD_MAX_CHARS
) -> str:
    """Return a bounded JSON-like string for BLE debug logging."""

    try:
        rendered = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    except TypeError:
        rendered = repr(payload)

    if len(rendered) <= max_chars:
        return rendered

    return f"{rendered[:max_chars]}...(truncated {len(rendered) - max_chars} chars)"


def _summarize_service_values(response: Any) -> list[dict[str, Any]]:
    """Return compact per-service diagnostics from service values payloads."""

    summaries: list[dict[str, Any]] = []
    for service_entry in _extract_service_values(response):
        service_id = service_entry.get("SI")
        if not isinstance(service_id, int):
            service_id = service_entry.get("value")
        if not isinstance(service_id, int):
            continue

        boi = _extract_service_boi(service_entry)
        characteristic_ids: list[int] = []
        for char_id in _extract_characteristic_map(service_entry):
            characteristic_ids.append(char_id)
        characteristic_ids.sort()

        summaries.append(
            {
                "si": service_id,
                "boi": boi,
                "chars": characteristic_ids,
            }
        )

    return summaries


@dataclass(slots=True)
class LocalBleSnapshot:
    """Represent parsed local BLE values for runtime sensors."""

    primary_power_on: bool | None = None
    timed_boost_enabled: bool | None = None
    timed_boost_active: bool | None = None
    timed_boost_duration_minutes: int | None = None
    primary_energy_kwh: float | None = None
    boost_energy_kwh: float | None = None
    consumption_days: list[dict[str, Any]] | None = None
    statistics_recent: dict[str, dict[str, Any]] | None = None
    schedule_zone_bois: dict[str, int] | None = None


class LocalBlePriority(IntEnum):
    """Represent local BLE worker queue priorities."""

    USER_COMMAND = 0
    SCHEDULE_WRITE = 1
    USER_READ = 5
    SCHEDULE_READ = 6
    BACKGROUND_REFRESH = 10


@dataclass(slots=True)
class _LocalBleQueuedJob:
    """Store metadata for one queued local BLE worker job."""

    priority: int
    order: int
    name: str
    coalesce_key: str | None
    operation: Callable[[], Awaitable[Any]]
    future: asyncio.Future[Any]
    submitted_monotonic: float
    queue_depth_at_submit: int


class LocalBleWorker:
    """Queue and execute local BLE operations on one reusable session."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        mac_address: str,
        serial_number: str | None,
        ble_key: str,
    ) -> None:
        """Initialize one local BLE worker bound to a gateway identity."""

        self._hass = hass
        self._mac_address = mac_address.strip().upper()
        self._ble_address = _format_mac_address(mac_address)
        self._serial_number = serial_number.strip() if isinstance(serial_number, str) else None
        if self._serial_number == "":
            self._serial_number = None
        self._gateway_mac_id = str(int(self._mac_address, 16))
        self._ble_key = ble_key

        try:
            decoded_key = base64.b64decode(ble_key)
        except Exception as error:
            raise LocalBleCommissioningError(
                "Stored BLE key is not valid base64"
            ) from error
        if len(decoded_key) != 16:
            raise LocalBleCommissioningError("Stored BLE key must decode to 16 bytes")
        self._auth_key = decoded_key

        self._queue: asyncio.PriorityQueue[tuple[int, int, _LocalBleQueuedJob]] = (
            asyncio.PriorityQueue()
        )
        self._coalesced_futures: dict[str, asyncio.Future[Any]] = {}
        self._submit_lock = asyncio.Lock()
        self._job_counter = 0
        self._worker_task: asyncio.Task[Any] | None = None
        self._closing = False

        self._client: Any | None = None
        self._rpc_client: _BleUartRpcClient | None = None
        self._mode_hot_water_service_bois: tuple[int, int] | None = None
        self._schedule_zone_bois: dict[str, int] | None = None
        self._preferred_consumption_args: tuple[int, ...] | None = None

    def matches(
        self,
        *,
        mac_address: str,
        serial_number: str | None,
        ble_key: str,
    ) -> bool:
        """Return whether this worker still matches entry connection settings."""

        serial = serial_number.strip() if isinstance(serial_number, str) else None
        if serial == "":
            serial = None
        return (
            mac_address.strip().upper() == self._mac_address
            and serial == self._serial_number
            and ble_key == self._ble_key
        )

    async def async_close(self) -> None:
        """Stop the queue worker and close any open BLE session."""

        self._closing = True
        _LOGGER.debug(
            "Stopping local BLE worker for %s (queued_jobs=%s, coalesced_futures=%s)",
            self._ble_address,
            self._queue.qsize(),
            len(self._coalesced_futures),
        )

        if self._worker_task is not None and not self._worker_task.done():
            self._worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker_task
        self._worker_task = None

        await self._async_clear_queued_jobs()
        await self._async_disconnect_session()

    async def async_execute_local_command(
        self,
        *,
        method_name: str,
        operation_kwargs: dict[str, Any],
        priority: LocalBlePriority | int = LocalBlePriority.USER_COMMAND,
        coalesce_key: str | None = None,
    ) -> Any:
        """Queue and execute one local BLE command with retries."""

        async def _operation() -> Any:
            """Execute one queued local BLE command operation."""

            last_error: LocalBleCommissioningError | None = None
            for attempt in range(BLE_COMMAND_RETRY_LIMIT + 1):
                attempt_started = time.monotonic()
                try:
                    result = await self._async_execute_command_once(
                        method_name=method_name,
                        operation_kwargs=operation_kwargs,
                    )
                    _LOGGER.info(
                        "BLE command %s attempt %s/%s succeeded in %.1f ms",
                        method_name,
                        attempt + 1,
                        BLE_COMMAND_RETRY_LIMIT + 1,
                        (time.monotonic() - attempt_started) * 1000,
                    )
                    return result
                except LocalBleCommissioningError as error:
                    last_error = error
                    self._invalidate_service_cache()
                    await self._async_disconnect_session()
                    _LOGGER.debug(
                        "BLE command %s attempt %s/%s failed in %.1f ms: %s",
                        method_name,
                        attempt + 1,
                        BLE_COMMAND_RETRY_LIMIT + 1,
                        (time.monotonic() - attempt_started) * 1000,
                        error,
                    )
                    if attempt < BLE_COMMAND_RETRY_LIMIT:
                        _LOGGER.warning(
                            "BLE command %s failed (attempt %s/%s): %s, retrying",
                            method_name,
                            attempt + 1,
                            BLE_COMMAND_RETRY_LIMIT + 1,
                            error,
                        )
                        await asyncio.sleep(BLE_COMMAND_RETRY_DELAY_SECONDS)
                    else:
                        raise

            if last_error is None:
                raise LocalBleCommissioningError(
                    f"BLE command {method_name} failed unexpectedly"
                )
            raise last_error

        return await self.async_submit(
            priority=priority,
            job_name=f"command:{method_name}",
            operation=_operation,
            coalesce_key=coalesce_key,
        )

    async def async_read_local_snapshot(
        self,
        *,
        priority: LocalBlePriority | int = LocalBlePriority.USER_READ,
        coalesce_key: str | None = None,
        consumption_timeout: float = BLE_CONSUMPTION_REQUEST_TIMEOUT_SECONDS,
        retry_limit: int = BLE_COMMAND_RETRY_LIMIT,
    ) -> LocalBleSnapshot:
        """Queue and execute one local BLE snapshot read operation."""

        async def _operation() -> LocalBleSnapshot:
            """Execute one queued local BLE snapshot read operation."""

            last_error: LocalBleCommissioningError | None = None
            for attempt in range(retry_limit + 1):
                attempt_started = time.monotonic()
                try:
                    result = await self._async_read_local_snapshot_once(
                        consumption_timeout=consumption_timeout,
                    )
                    _LOGGER.debug(
                        "BLE snapshot read attempt %s/%s succeeded in %.1f ms",
                        attempt + 1,
                        retry_limit + 1,
                        (time.monotonic() - attempt_started) * 1000,
                    )
                    return result
                except LocalBleCommissioningError as error:
                    last_error = error
                    self._invalidate_service_cache()
                    await self._async_disconnect_session()
                    _LOGGER.debug(
                        "BLE snapshot read attempt %s/%s failed in %.1f ms: %s",
                        attempt + 1,
                        retry_limit + 1,
                        (time.monotonic() - attempt_started) * 1000,
                        error,
                    )
                    if attempt < retry_limit:
                        _LOGGER.warning(
                            "BLE snapshot read failed (attempt %s/%s): %s, retrying",
                            attempt + 1,
                            retry_limit + 1,
                            error,
                        )
                        await asyncio.sleep(BLE_COMMAND_RETRY_DELAY_SECONDS)
                    else:
                        raise

            if last_error is None:
                raise LocalBleCommissioningError("BLE snapshot retries exhausted")
            raise last_error

        result = await self.async_submit(
            priority=priority,
            job_name="snapshot:read",
            operation=_operation,
            coalesce_key=coalesce_key,
        )
        if not isinstance(result, LocalBleSnapshot):
            raise LocalBleCommissioningError("Local BLE snapshot read returned invalid data")
        return result

    async def async_read_local_weekly_programs(
        self,
        *,
        zone_bois: Mapping[str, int] | None = None,
        priority: LocalBlePriority | int = LocalBlePriority.SCHEDULE_READ,
        coalesce_key: str | None = None,
    ) -> tuple[
        dict[str, "WeeklyProgram | None"],
        dict[str, list[tuple[int, int]] | None],
    ]:
        """Queue and execute one local BLE weekly schedule read operation."""

        async def _operation() -> tuple[
            dict[str, "WeeklyProgram | None"],
            dict[str, list[tuple[int, int]] | None],
        ]:
            """Execute one queued local BLE weekly schedule read operation."""

            last_error: LocalBleCommissioningError | None = None
            for attempt in range(BLE_COMMAND_RETRY_LIMIT + 1):
                attempt_started = time.monotonic()
                try:
                    result = await self._async_read_local_weekly_programs_once(
                        zone_bois=zone_bois,
                    )
                    _LOGGER.debug(
                        "BLE weekly schedule read attempt %s/%s succeeded in %.1f ms",
                        attempt + 1,
                        BLE_COMMAND_RETRY_LIMIT + 1,
                        (time.monotonic() - attempt_started) * 1000,
                    )
                    return result
                except LocalBleCommissioningError as error:
                    last_error = error
                    self._invalidate_service_cache()
                    await self._async_disconnect_session()
                    _LOGGER.debug(
                        "BLE weekly schedule read attempt %s/%s failed in %.1f ms: %s",
                        attempt + 1,
                        BLE_COMMAND_RETRY_LIMIT + 1,
                        (time.monotonic() - attempt_started) * 1000,
                        error,
                    )
                    if attempt < BLE_COMMAND_RETRY_LIMIT:
                        _LOGGER.warning(
                            "BLE weekly schedule read failed (attempt %s/%s): %s, retrying",
                            attempt + 1,
                            BLE_COMMAND_RETRY_LIMIT + 1,
                            error,
                        )
                        await asyncio.sleep(BLE_COMMAND_RETRY_DELAY_SECONDS)
                    else:
                        raise

            if last_error is None:
                raise LocalBleCommissioningError("BLE weekly schedule retries exhausted")
            raise last_error

        result = await self.async_submit(
            priority=priority,
            job_name="schedule:read",
            operation=_operation,
            coalesce_key=coalesce_key,
        )
        if not isinstance(result, tuple) or len(result) != 2:
            raise LocalBleCommissioningError(
                "Local BLE weekly schedule read returned invalid data"
            )
        return result

    async def async_write_local_weekly_program(
        self,
        *,
        zone: str,
        program: "WeeklyProgram",
        zone_bois: Mapping[str, int] | None = None,
        priority: LocalBlePriority | int = LocalBlePriority.SCHEDULE_WRITE,
        coalesce_key: str | None = None,
    ) -> None:
        """Queue and execute one local BLE weekly schedule write operation."""

        async def _operation() -> None:
            """Execute one queued local BLE weekly schedule write operation."""

            last_error: LocalBleCommissioningError | None = None
            for attempt in range(BLE_COMMAND_RETRY_LIMIT + 1):
                attempt_started = time.monotonic()
                try:
                    await self._async_write_local_weekly_program_once(
                        zone=zone,
                        program=program,
                        zone_bois=zone_bois,
                    )
                    _LOGGER.debug(
                        "BLE weekly schedule write %s attempt %s/%s succeeded in %.1f ms",
                        zone,
                        attempt + 1,
                        BLE_COMMAND_RETRY_LIMIT + 1,
                        (time.monotonic() - attempt_started) * 1000,
                    )
                    return
                except LocalBleCommissioningError as error:
                    last_error = error
                    self._invalidate_service_cache()
                    await self._async_disconnect_session()
                    _LOGGER.debug(
                        "BLE weekly schedule write %s attempt %s/%s failed in %.1f ms: %s",
                        zone,
                        attempt + 1,
                        BLE_COMMAND_RETRY_LIMIT + 1,
                        (time.monotonic() - attempt_started) * 1000,
                        error,
                    )
                    if attempt < BLE_COMMAND_RETRY_LIMIT:
                        _LOGGER.warning(
                            "BLE weekly schedule write failed (attempt %s/%s): %s, retrying",
                            attempt + 1,
                            BLE_COMMAND_RETRY_LIMIT + 1,
                            error,
                        )
                        await asyncio.sleep(BLE_COMMAND_RETRY_DELAY_SECONDS)
                    else:
                        raise

            if last_error is None:
                raise LocalBleCommissioningError(
                    "BLE weekly schedule write retries exhausted"
                )
            raise last_error

        await self.async_submit(
            priority=priority,
            job_name=f"schedule:write:{zone}",
            operation=_operation,
            coalesce_key=coalesce_key,
        )

    async def async_submit(
        self,
        *,
        priority: LocalBlePriority | int,
        job_name: str,
        operation: Callable[[], Awaitable[Any]],
        coalesce_key: str | None = None,
    ) -> Any:
        """Queue one callable and await its result."""

        if self._closing:
            raise LocalBleCommissioningError("Local BLE worker is shutting down")

        await self._async_ensure_worker()

        submit_started = time.monotonic()

        existing_future: asyncio.Future[Any] | None = None
        queued_future: asyncio.Future[Any] | None = None
        async with self._submit_lock:
            if coalesce_key is not None:
                candidate = self._coalesced_futures.get(coalesce_key)
                if candidate is not None and not candidate.done():
                    existing_future = candidate

            queue_depth_before = self._queue.qsize()

            if existing_future is None:
                loop = asyncio.get_running_loop()
                queued_future = loop.create_future()
                job = _LocalBleQueuedJob(
                    priority=int(priority),
                    order=self._job_counter,
                    name=job_name,
                    coalesce_key=coalesce_key,
                    operation=operation,
                    future=queued_future,
                    submitted_monotonic=time.monotonic(),
                    queue_depth_at_submit=queue_depth_before,
                )
                self._job_counter += 1

                if coalesce_key is not None:
                    self._coalesced_futures[coalesce_key] = queued_future

                    def _cleanup(done_future: asyncio.Future[Any], *, key: str) -> None:
                        """Drop finished coalesced futures from the lookup map."""

                        self._remove_coalesced_future(key, done_future)

                    queued_future.add_done_callback(
                        lambda done_future, key=coalesce_key: _cleanup(
                            done_future,
                            key=key,
                        )
                    )

                self._queue.put_nowait((job.priority, job.order, job))
                _LOGGER.debug(
                    "Queued local BLE job %s (priority=%s, order=%s, queue_depth=%s->%s, coalesce_key=%s)",
                    job_name,
                    job.priority,
                    job.order,
                    queue_depth_before,
                    self._queue.qsize(),
                    coalesce_key,
                )
            else:
                _LOGGER.debug(
                    "Coalesced local BLE job %s onto key %s (queue_depth=%s, coalesced_futures=%s)",
                    job_name,
                    coalesce_key,
                    queue_depth_before,
                    len(self._coalesced_futures),
                )

        active_future = existing_future if existing_future is not None else queued_future
        if active_future is None:
            raise LocalBleCommissioningError("Failed to queue local BLE job")

        try:
            return await asyncio.shield(active_future)
        finally:
            _LOGGER.debug(
                "Local BLE job wait complete for %s in %.1f ms (coalesced=%s)",
                job_name,
                (time.monotonic() - submit_started) * 1000,
                existing_future is not None,
            )

    async def _async_ensure_worker(self) -> None:
        """Start the background worker task when not running."""

        if self._worker_task is not None and not self._worker_task.done():
            return

        task_name = (
            f"securemtr_local_ble_worker_{self._ble_address.lower().replace(':', '')}"
        )
        create_background_task = getattr(self._hass, "async_create_background_task", None)
        if callable(create_background_task):
            self._worker_task = create_background_task(self._async_worker(), task_name)
            _LOGGER.debug(
                "Started local BLE worker task for %s using Home Assistant background task API",
                self._ble_address,
            )
            return

        create_task = getattr(self._hass, "async_create_task", None)
        if callable(create_task):
            self._worker_task = create_task(self._async_worker())
        else:
            self._worker_task = asyncio.create_task(self._async_worker(), name=task_name)
        _LOGGER.debug(
            "Started local BLE worker task for %s using fallback task API",
            self._ble_address,
        )

    async def _async_worker(self) -> None:
        """Process queued jobs while keeping a reusable BLE session open."""

        _LOGGER.debug("Local BLE worker loop running for %s", self._ble_address)
        try:
            while True:
                try:
                    _priority, _order, job = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=BLE_SESSION_IDLE_TIMEOUT_SECONDS,
                    )
                except TimeoutError:
                    if self._client is not None or self._rpc_client is not None:
                        _LOGGER.debug(
                            "Local BLE worker idle for %.1f s, disconnecting session for %s",
                            BLE_SESSION_IDLE_TIMEOUT_SECONDS,
                            self._ble_address,
                        )
                    await self._async_disconnect_session()
                    continue

                queue_wait_ms = (time.monotonic() - job.submitted_monotonic) * 1000
                queue_depth_after_get = self._queue.qsize()
                _LOGGER.debug(
                    "Starting local BLE job %s (priority=%s, queue_wait_ms=%.1f, queue_depth_submit=%s, queue_depth_now=%s)",
                    job.name,
                    job.priority,
                    queue_wait_ms,
                    job.queue_depth_at_submit,
                    queue_depth_after_get,
                )

                operation_started = time.monotonic()
                try:
                    result = await job.operation()
                except asyncio.CancelledError:
                    if not job.future.done():
                        job.future.cancel()
                    _LOGGER.debug(
                        "Cancelled local BLE job %s after %.1f ms",
                        job.name,
                        (time.monotonic() - operation_started) * 1000,
                    )
                    raise
                except Exception as error:
                    if not job.future.done():
                        job.future.set_exception(error)
                    _LOGGER.debug(
                        "Local BLE job %s failed after %.1f ms: %s",
                        job.name,
                        (time.monotonic() - operation_started) * 1000,
                        error,
                    )
                else:
                    if not job.future.done():
                        job.future.set_result(result)
                    _LOGGER.debug(
                        "Local BLE job %s completed in %.1f ms",
                        job.name,
                        (time.monotonic() - operation_started) * 1000,
                    )
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            _LOGGER.debug("Local BLE worker loop cancelled for %s", self._ble_address)
        finally:
            await self._async_clear_queued_jobs()
            await self._async_disconnect_session()
            _LOGGER.debug("Local BLE worker loop stopped for %s", self._ble_address)

    async def _async_clear_queued_jobs(self) -> None:
        """Fail every queued job when the worker stops."""

        cleared = 0
        while True:
            try:
                _priority, _order, job = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            if not job.future.done():
                job.future.set_exception(
                    LocalBleCommissioningError("Local BLE worker was stopped")
                )
            self._queue.task_done()
            cleared += 1

        if cleared > 0:
            _LOGGER.debug(
                "Cleared %s pending local BLE jobs for %s",
                cleared,
                self._ble_address,
            )

    def _remove_coalesced_future(
        self,
        key: str,
        done_future: asyncio.Future[Any],
    ) -> None:
        """Remove one finished coalesced future entry when it matches."""

        current = self._coalesced_futures.get(key)
        if current is done_future:
            self._coalesced_futures.pop(key, None)

    async def _async_ensure_rpc_client(self) -> _BleUartRpcClient:
        """Return an authorized RPC client, reconnecting when needed."""

        if (
            self._client is not None
            and self._rpc_client is not None
            and self._is_client_connected(self._client)
        ):
            _LOGGER.debug("Reusing connected local BLE session for %s", self._ble_address)
            return self._rpc_client

        reconnect_started = time.monotonic()
        await self._async_disconnect_session()

        resolve_started = time.monotonic()
        ble_device = await _async_resolve_connectable_device(
            self._hass,
            self._ble_address,
            expected_serial=self._serial_number,
        )
        resolve_ms = (time.monotonic() - resolve_started) * 1000

        def _refresh_ble_device() -> Any:
            """Refresh the Home Assistant BLEDevice instance before reconnects."""

            from homeassistant.components import bluetooth  # noqa: PLC0415

            refreshed = bluetooth.async_ble_device_from_address(
                self._hass,
                self._ble_address,
                connectable=True,
            )
            return refreshed if refreshed is not None else ble_device

        connect_started = time.monotonic()
        client = await _async_connect_client(
            ble_device,
            ble_device_callback=_refresh_ble_device,
        )
        connect_ms = (time.monotonic() - connect_started) * 1000
        rpc_client = _BleUartRpcClient(client)
        try:
            initialize_started = time.monotonic()
            await rpc_client.async_initialize()
            await rpc_client.async_authorize(self._auth_key)
            initialize_ms = (time.monotonic() - initialize_started) * 1000
        except Exception:
            await rpc_client.async_close()
            await _async_disconnect_client(client)
            raise

        self._client = client
        self._rpc_client = rpc_client
        self._invalidate_service_cache()
        _LOGGER.debug(
            "Established local BLE session for %s (resolve=%.1f ms, connect=%.1f ms, init_auth=%.1f ms, total=%.1f ms)",
            self._ble_address,
            resolve_ms,
            connect_ms,
            initialize_ms,
            (time.monotonic() - reconnect_started) * 1000,
        )
        return rpc_client

    def _is_client_connected(self, client: Any) -> bool:
        """Return whether a Bleak-style client appears connected."""

        is_connected = getattr(client, "is_connected", None)
        if isinstance(is_connected, bool):
            return is_connected
        if callable(is_connected):
            with suppress(Exception):
                result = is_connected()
                if isinstance(result, bool):
                    return result
        return is_connected is None

    def _invalidate_service_cache(self) -> None:
        """Clear cached service BOIs after reconnect-sensitive failures."""

        self._mode_hot_water_service_bois = None

    async def _async_disconnect_session(self) -> None:
        """Close the current BLE session and clear stateful caches."""

        disconnect_started = time.monotonic()
        rpc_client = self._rpc_client
        client = self._client
        self._rpc_client = None
        self._client = None
        self._invalidate_service_cache()

        if rpc_client is not None:
            await rpc_client.async_close()
        if client is not None:
            await _async_disconnect_client(client)

        if rpc_client is not None or client is not None:
            _LOGGER.debug(
                "Disconnected local BLE session for %s in %.1f ms",
                self._ble_address,
                (time.monotonic() - disconnect_started) * 1000,
            )

    async def _async_resolve_mode_and_hot_water_service_bois_cached(
        self,
        rpc_client: _BleUartRpcClient,
    ) -> tuple[int, int]:
        """Return mode and hot-water BOIs using a reconnect-aware cache."""

        if self._mode_hot_water_service_bois is not None:
            return self._mode_hot_water_service_bois

        resolved = await _async_resolve_mode_and_hot_water_service_bois(
            rpc_client,
            gateway_mac_id=self._gateway_mac_id,
        )
        self._mode_hot_water_service_bois = resolved
        return resolved

    async def _async_execute_command_once(
        self,
        *,
        method_name: str,
        operation_kwargs: dict[str, Any],
    ) -> Any:
        """Execute one local BLE command with the active reusable session."""

        rpc_client = await self._async_ensure_rpc_client()
        mode_service_boi, hot_water_service_boi = (
            await self._async_resolve_mode_and_hot_water_service_bois_cached(rpc_client)
        )
        handler_id, service_id, args = _command_to_rpc_payload(
            method_name,
            operation_kwargs,
            mode_service_boi=mode_service_boi,
            hot_water_service_boi=hot_water_service_boi,
        )
        result = await rpc_client.async_rpc_request(
            gateway_mac_id=self._gateway_mac_id,
            handler_id=handler_id,
            service_id=service_id,
            args=args,
        )
        if result not in (0, "0", None):
            raise LocalBleCommissioningError(
                f"Unexpected local BLE acknowledgement for {method_name}: {result}"
            )
        return result

    async def _async_read_local_snapshot_once(
        self,
        *,
        consumption_timeout: float,
    ) -> LocalBleSnapshot:
        """Read one local BLE snapshot using the active reusable session."""

        rpc_client = await self._async_ensure_rpc_client()
        successful_consumption_args: list[tuple[int, ...]] = []
        snapshot = await _async_read_ble_snapshot_payload(
            rpc_client,
            ble_address=self._ble_address,
            gateway_mac_id=self._gateway_mac_id,
            consumption_timeout=consumption_timeout,
            consumption_args_candidates=self._ordered_consumption_args_candidates(),
            successful_consumption_args=successful_consumption_args,
        )
        if successful_consumption_args:
            self._preferred_consumption_args = successful_consumption_args[0]
        if isinstance(snapshot.schedule_zone_bois, dict):
            self._schedule_zone_bois = dict(snapshot.schedule_zone_bois)
        return snapshot

    def _ordered_consumption_args_candidates(self) -> list[list[int]]:
        """Return consumption selector candidates ordered by recent success."""

        return [[0], [1]] if self._preferred_consumption_args == (0,) else [[1], [0]]

    async def _async_read_local_weekly_programs_once(
        self,
        *,
        zone_bois: Mapping[str, int] | None,
    ) -> tuple[
        dict[str, "WeeklyProgram | None"],
        dict[str, list[tuple[int, int]] | None],
    ]:
        """Read one weekly schedule payload set using the reusable session."""

        rpc_client = await self._async_ensure_rpc_client()
        fallback_zone_bois = (
            zone_bois if zone_bois is not None else self._schedule_zone_bois
        )
        programs, canonicals, resolved_zone_bois = (
            await _async_read_ble_weekly_programs_payload(
                rpc_client,
                ble_address=self._ble_address,
                gateway_mac_id=self._gateway_mac_id,
                zone_bois=fallback_zone_bois,
            )
        )
        self._schedule_zone_bois = dict(resolved_zone_bois)
        return programs, canonicals

    async def _async_write_local_weekly_program_once(
        self,
        *,
        zone: str,
        program: "WeeklyProgram",
        zone_bois: Mapping[str, int] | None,
    ) -> None:
        """Write one weekly schedule payload using the reusable session."""

        rpc_client = await self._async_ensure_rpc_client()
        fallback_zone_bois = (
            zone_bois if zone_bois is not None else self._schedule_zone_bois
        )
        resolved_zone_bois = await _async_write_local_weekly_program_payload(
            rpc_client,
            gateway_mac_id=self._gateway_mac_id,
            zone=zone,
            program=program,
            zone_bois=fallback_zone_bois,
        )
        self._schedule_zone_bois = dict(resolved_zone_bois)


class _BleUartRpcClient:
    """Manage packetized UART-over-BLE RPC exchanges."""

    def __init__(self, client: Any) -> None:
        """Store the BLE client and initialize response state."""

        self._client = client
        self._loop = asyncio.get_running_loop()
        self._response_packets: dict[int, bytes] = {}
        self._response_event = asyncio.Event()
        self._auth_key: bytes | None = None

    async def async_initialize(self) -> None:
        """Validate service characteristics and subscribe to TX notifications."""

        services = getattr(self._client, "services", None)
        if services is not None:
            service = services.get_service(UART_SERVICE_UUID)
            if service is None:
                raise LocalBleCommissioningError("SecureMTR UART service was not found")
            if service.get_characteristic(UART_RX_UUID) is None:
                raise LocalBleCommissioningError(
                    "SecureMTR UART RX characteristic missing"
                )
            if service.get_characteristic(UART_TX_UUID) is None:
                raise LocalBleCommissioningError(
                    "SecureMTR UART TX characteristic missing"
                )

        try:
            await self._client.start_notify(UART_TX_UUID, self._notification_callback)
        except (BleakError, RuntimeError, OSError, EOFError) as error:
            if _is_notify_already_acquired_error(error):
                _LOGGER.debug(
                    "TX notify channel already acquired; attempting one recovery cycle"
                )
                with suppress(BleakError, RuntimeError, OSError, EOFError):
                    await self._client.stop_notify(UART_TX_UUID)
                await asyncio.sleep(BLE_NOTIFY_RECOVERY_DELAY_SECONDS)
                try:
                    await self._client.start_notify(
                        UART_TX_UUID,
                        self._notification_callback,
                    )
                    return
                except (BleakError, RuntimeError, OSError, EOFError) as retry_error:
                    raise LocalBleCommissioningError(
                        "Failed to subscribe to SecureMTR TX notifications"
                    ) from retry_error

            raise LocalBleCommissioningError(
                "Failed to subscribe to SecureMTR TX notifications"
            ) from error

    async def async_close(self) -> None:
        """Stop notifications when possible."""

        try:
            await self._client.stop_notify(UART_TX_UUID)
        except (BleakError, RuntimeError, OSError, EOFError) as error:
            if "Service Discovery has not been performed yet" in str(error):
                _LOGGER.debug("TX notifications already unavailable while closing")
            else:
                _LOGGER.debug("Unable to stop TX notifications", exc_info=True)

    async def async_rpc_request(
        self,
        *,
        gateway_mac_id: str,
        handler_id: int,
        service_id: int,
        args: list[Any] | None,
        timeout: float = BLE_REQUEST_TIMEOUT_SECONDS,
    ) -> Any:
        """Send one RPC request and return the response result payload."""

        request_started = time.monotonic()
        request_id = f"{int(time.time())}-{random.randint(1, 2_147_483_647)}"
        request_payload: dict[str, Any] = {
            "V": "1.0",
            "DTS": int(time.time()),
            "I": request_id,
            "M": "Request",
            "P": [
                {
                    "GMI": gateway_mac_id,
                    "HI": handler_id,
                    "SI": service_id,
                }
            ],
        }
        if args is not None:
            request_payload["P"].append(args)

        await self._async_send_payload(
            json.dumps(request_payload, separators=(",", ":")).encode("utf-8"),
            use_java_utf8_length=True,
        )

        ignored_malformed_payloads = 0
        ignored_malformed_json = 0
        ignored_non_object_json = 0
        ignored_mismatched_ids = 0

        deadline = self._loop.time() + timeout
        while True:
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                raise LocalBleCommissioningError(
                    f"RPC response ID mismatch (expected {request_id})"
                )

            try:
                response_body = await self._async_wait_for_response_payload(
                    timeout=remaining,
                )
            except LocalBleCommissioningError as error:
                if str(error) == BLE_RESPONSE_TIMEOUT_MESSAGE:
                    _LOGGER.debug(
                        "BLE RPC request timed out (request_id=%s, hi=%s, si=%s, elapsed_ms=%.1f, ignored_payload=%s, ignored_json=%s, ignored_non_object=%s, ignored_id=%s)",
                        request_id,
                        handler_id,
                        service_id,
                        (time.monotonic() - request_started) * 1000,
                        ignored_malformed_payloads,
                        ignored_malformed_json,
                        ignored_non_object_json,
                        ignored_mismatched_ids,
                    )
                    raise

                ignored_malformed_payloads += 1
                _LOGGER.debug(
                    "Ignoring malformed BLE response payload while waiting for id %s: %s",
                    request_id,
                    error,
                )
                self._reset_response_state()
                continue

            try:
                response = json.loads(response_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                ignored_malformed_json += 1
                _LOGGER.debug(
                    "Ignoring malformed BLE JSON response while waiting for id %s: %s",
                    request_id,
                    error,
                )
                self._reset_response_state()
                continue

            if not isinstance(response, dict):
                ignored_non_object_json += 1
                _LOGGER.debug(
                    "Ignoring non-object BLE JSON response while waiting for id %s",
                    request_id,
                )
                self._reset_response_state()
                continue

            response_id = response.get("I")
            if response_id != request_id:
                ignored_mismatched_ids += 1
                _LOGGER.debug(
                    "Ignoring BLE response with mismatched id (expected=%s, got=%s)",
                    request_id,
                    response_id,
                )
                self._reset_response_state()
                continue
            break

        error_data = response.get("E")
        if isinstance(error_data, dict):
            error_code = error_data.get("C")
            error_message = error_data.get("M")
            parsed_error_code: int | None = None
            if isinstance(error_code, int):
                parsed_error_code = error_code
            elif isinstance(error_code, str) and error_code.isdigit():
                parsed_error_code = int(error_code)
            raise LocalBleCommissioningError(
                f"Gateway returned RPC error {error_code}: {error_message}",
                error_code=parsed_error_code,
            )

        if "R" not in response:
            raise LocalBleCommissioningError("RPC response did not include a result")

        _LOGGER.debug(
            "BLE RPC request completed (request_id=%s, hi=%s, si=%s, elapsed_ms=%.1f, ignored_payload=%s, ignored_json=%s, ignored_non_object=%s, ignored_id=%s)",
            request_id,
            handler_id,
            service_id,
            (time.monotonic() - request_started) * 1000,
            ignored_malformed_payloads,
            ignored_malformed_json,
            ignored_non_object_json,
            ignored_mismatched_ids,
        )
        return response["R"]

    async def async_authorize(self, auth_key: bytes) -> None:
        """Execute 4-pass BLE authorization using the provided key bytes."""

        if len(auth_key) != 16:
            raise LocalBleCommissioningError("BLE key must decode to 16 bytes")

        app_random = secrets.token_bytes(16)
        pass1_request = bytes([1, 0, 16, 0]) + app_random
        pass2_response = await self._async_exchange_bytes(
            pass1_request,
            use_java_utf8_length=True,
        )
        meter_random = _parse_four_pass_response(pass2_response, expected_type=2)

        encrypted_meter_random = _aes_ecb_encrypt(auth_key, meter_random)
        pass3_request = bytes([3, 0, 16, 0]) + encrypted_meter_random
        pass4_response = await self._async_exchange_bytes(
            pass3_request,
            use_java_utf8_length=True,
        )
        encrypted_app_random = _parse_four_pass_response(
            pass4_response, expected_type=4
        )

        decrypted_app_random = _aes_ecb_decrypt(auth_key, encrypted_app_random)
        if decrypted_app_random != app_random:
            raise LocalBleCommissioningError(
                "BLE authorization failed due to random key mismatch"
            )

        self._auth_key = auth_key

    async def _async_exchange_json(
        self,
        request_payload: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        """Write packetized JSON and wait for the packetized response."""

        request_json = json.dumps(request_payload, separators=(",", ":"))
        response_body = await self._async_exchange_bytes(
            request_json.encode("utf-8"),
            timeout=timeout,
            use_java_utf8_length=True,
        )
        try:
            response = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LocalBleCommissioningError(
                "Failed to parse BLE JSON response"
            ) from error

        if not isinstance(response, dict):
            raise LocalBleCommissioningError("BLE JSON response is not an object")
        return response

    async def _async_exchange_bytes(
        self,
        request_payload: bytes,
        *,
        timeout: float = BLE_REQUEST_TIMEOUT_SECONDS,
        use_java_utf8_length: bool = False,
    ) -> bytes:
        """Write packetized bytes and wait for a packetized byte response."""

        await self._async_send_payload(
            request_payload,
            use_java_utf8_length=use_java_utf8_length,
        )
        return await self._async_wait_for_response_payload(timeout=timeout)

    async def _async_send_payload(
        self,
        request_payload: bytes,
        *,
        use_java_utf8_length: bool,
    ) -> None:
        """Frame and transmit one BLE payload."""

        self._reset_response_state()

        payload_length = (
            _java_utf8_character_length(request_payload)
            if use_java_utf8_length
            else len(request_payload)
        )
        framed_request = payload_length.to_bytes(2, byteorder="big") + request_payload
        if self._auth_key is not None:
            framed_request = _encrypt_payload(self._auth_key, framed_request)

        packets = _packetize_payload(framed_request)
        _LOGGER.debug(
            "Sending BLE payload (raw_len=%s, framed_len=%s, packet_count=%s, encrypted=%s)",
            len(request_payload),
            len(framed_request),
            len(packets),
            self._auth_key is not None,
        )

        for packet in packets:
            # Pace and acknowledge each packet so a payload does not flood the meter.
            await asyncio.sleep(BLE_INTER_PACKET_DELAY_SECONDS)
            try:
                await self._client.write_gatt_char(UART_RX_UUID, packet, response=True)
            except (BleakError, RuntimeError, OSError) as error:
                raise LocalBleCommissioningError(
                    "Failed to write BLE UART packet"
                ) from error

    def _reset_response_state(self) -> None:
        """Clear staged response packets and readiness state."""

        self._response_packets.clear()
        self._response_event.clear()

    async def _async_wait_for_response_payload(self, *, timeout: float) -> bytes:
        """Wait for one BLE response payload and decode transport framing."""

        wait_started = time.monotonic()
        try:
            await asyncio.wait_for(self._response_event.wait(), timeout=timeout)
        except TimeoutError as error:
            raise LocalBleCommissioningError(BLE_RESPONSE_TIMEOUT_MESSAGE) from error

        response_body = _reassemble_packet_bytes(self._response_packets)
        if self._auth_key is not None:
            response_body = _decrypt_payload(self._auth_key, response_body)
        payload = _extract_length_prefixed_payload(response_body)
        _LOGGER.debug(
            "Received BLE payload (packets=%s, framed_len=%s, payload_len=%s, wait_ms=%.1f)",
            len(self._response_packets),
            len(response_body),
            len(payload),
            (time.monotonic() - wait_started) * 1000,
        )
        return payload

    def _notification_callback(self, _characteristic: Any, data: bytearray) -> None:
        """Capture incoming packetized data from TX notifications."""

        packet = bytes(data)
        self._loop.call_soon_threadsafe(self._handle_packet, packet)

    def _handle_packet(self, packet: bytes) -> None:
        """Store one response packet and signal completion when final arrives."""

        if not packet:
            return
        packet_index = packet[0]
        self._response_packets[packet_index] = packet[1:]
        if packet_index == FINAL_PACKET_INDEX:
            self._response_event.set()


def _packetize_payload(payload: bytes) -> list[bytes]:
    """Split payload into UART packets with packet index prefixes."""

    packets: list[bytes] = []
    total_packets = (len(payload) // PACKET_SIZE) + 1
    offset = 0

    for packet_number in range(1, total_packets + 1):
        if packet_number < total_packets:
            chunk = payload[offset : offset + PACKET_SIZE]
            packets.append(bytes([packet_number]) + chunk)
            offset += PACKET_SIZE
        else:
            packets.append(bytes([FINAL_PACKET_INDEX]) + payload[offset:])

    return packets


def _reassemble_packet_bytes(response_packets: dict[int, bytes]) -> bytes:
    """Reassemble packetized bytes without decoding the framed payload."""

    if FINAL_PACKET_INDEX not in response_packets:
        raise LocalBleCommissioningError("Response did not include the final packet")

    return b"".join(response_packets[index] for index in sorted(response_packets))


def _extract_length_prefixed_payload(payload: bytes) -> bytes:
    """Return payload bytes referenced by the leading 2-byte length field."""

    if len(payload) < 2:
        raise LocalBleCommissioningError("Response was shorter than the length prefix")

    payload_length = int.from_bytes(payload[:2], byteorder="big")
    payload_slice = payload[2 : 2 + payload_length]
    if len(payload_slice) != payload_length:
        raise LocalBleCommissioningError("Response payload length mismatch")

    return payload_slice


def _java_utf8_character_length(payload: bytes) -> int:
    """Return the UTF-8 decoded character length matching Java String behavior."""

    return len(payload.decode("utf-8", errors="replace"))


def _pad_zero(data: bytes) -> bytes:
    """Pad a payload with zeroes to the AES 16-byte block size."""

    remainder = len(data) % 16
    if remainder == 0:
        return data
    return data + (b"\x00" * (16 - remainder))


def _aes_ecb_encrypt(key: bytes, payload: bytes) -> bytes:
    """Encrypt bytes using AES-ECB without adding padding."""

    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(payload) + encryptor.finalize()


def _aes_ecb_decrypt(key: bytes, payload: bytes) -> bytes:
    """Decrypt bytes using AES-ECB without stripping padding."""

    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return decryptor.update(payload) + decryptor.finalize()


def _encrypt_payload(key: bytes, payload: bytes) -> bytes:
    """Encrypt a length-prefixed payload with zero-padding for BLE transport."""

    return _aes_ecb_encrypt(key, _pad_zero(payload))


def _decrypt_payload(key: bytes, payload: bytes) -> bytes:
    """Decrypt an AES-encrypted BLE transport payload."""

    if len(payload) % 16 != 0:
        raise LocalBleCommissioningError(
            "Encrypted BLE payload length is not a 16-byte multiple"
        )
    return _aes_ecb_decrypt(key, payload)


def _parse_four_pass_response(payload: bytes, *, expected_type: int) -> bytes:
    """Parse and validate one BLE 4-pass payload response."""

    if len(payload) < 4:
        raise LocalBleCommissioningError("4-pass response was shorter than 4 bytes")

    response_type = payload[0]
    ack = payload[1]
    value_length = payload[2]

    if response_type == 5:
        if ack == BLE_ACK_INVALID_MESSAGE:
            raise LocalBleCommissioningError("4-pass rejected invalid message format")
        if ack == BLE_ACK_KEY_MISMATCH:
            raise LocalBleCommissioningError("4-pass key mismatch")
        raise LocalBleCommissioningError(f"4-pass failed with ack code {ack}")

    if response_type != expected_type:
        raise LocalBleCommissioningError(
            f"Unexpected 4-pass response type {response_type}; expected {expected_type}"
        )

    if ack != BLE_ACK_SUCCESS:
        raise LocalBleCommissioningError(f"4-pass failed with ack code {ack}")

    if len(payload) < 4 + value_length:
        raise LocalBleCommissioningError("4-pass response payload length mismatch")

    return payload[4 : 4 + value_length]


def _format_mac_address(mac_address: str) -> str:
    """Convert compact uppercase MAC into colon-separated canonical form."""

    return ":".join(
        mac_address[index : index + 2] for index in range(0, len(mac_address), 2)
    )


def _service_uuid_matches(service_info: Any) -> bool | None:
    """Return True/False if UUID match is known, or None when unavailable."""

    service_uuids = getattr(service_info, "service_uuids", None)
    if not isinstance(service_uuids, list):
        return None
    if not service_uuids:
        return None
    expected = str(UART_SERVICE_UUID)
    return any(str(uuid).lower() == expected for uuid in service_uuids)


def _advertised_serial(service_info: Any) -> str | None:
    """Extract the advertised serial/name from Home Assistant service info."""

    name = getattr(service_info, "name", None)
    if isinstance(name, str) and name.strip():
        return name.strip()

    device = getattr(service_info, "device", None)
    device_name = getattr(device, "name", None)
    if isinstance(device_name, str) and device_name.strip():
        return device_name.strip()
    return None


def _extract_raw_scan_record(service_info: Any) -> bytes | None:
    """Best-effort extraction of raw scan-record bytes from service metadata."""

    advertisement = getattr(service_info, "advertisement", None)
    if advertisement is not None:
        for attribute in ("bytes", "raw_data", "data"):
            value = getattr(advertisement, attribute, None)
            if isinstance(value, (bytes, bytearray)):
                return bytes(value)
        platform_data = getattr(advertisement, "platform_data", None)
        candidate = _extract_first_bytes(platform_data)
        if candidate is not None:
            return candidate

    device = getattr(service_info, "device", None)
    details = getattr(device, "details", None)
    candidate = _extract_first_bytes(details)
    if candidate is not None:
        return candidate

    manufacturer_data = getattr(service_info, "manufacturer_data", None)
    if isinstance(manufacturer_data, dict):
        candidate = _extract_first_bytes(tuple(manufacturer_data.values()))
        if candidate is not None:
            return candidate

    return None


def _extract_first_bytes(value: Any) -> bytes | None:
    """Recursively return the first bytes-like payload found in nested objects."""

    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, dict):
        for nested in value.values():
            candidate = _extract_first_bytes(nested)
            if candidate is not None:
                return candidate
        return None
    if isinstance(value, (list, tuple, set)):
        for nested in value:
            candidate = _extract_first_bytes(nested)
            if candidate is not None:
                return candidate
        return None
    return None


def _validate_advertisement_identity(
    service_info: Any,
    *,
    expected_address: str,
    expected_serial: str | None,
) -> None:
    """Validate discovered advertisement identity before BLE connection."""

    address = getattr(service_info, "address", None)
    if not isinstance(address, str) or address.upper() != expected_address.upper():
        raise LocalBleCommissioningError(
            f"Advertisement address mismatch: expected {expected_address}, got {address}"
        )

    uuid_match = _service_uuid_matches(service_info)
    if uuid_match is False:
        raise LocalBleCommissioningError(
            "Advertisement did not include SecureMTR UART service UUID"
        )
    if uuid_match is None:
        _LOGGER.debug(
            "Advertisement service UUID list unavailable for %s", expected_address
        )

    if expected_serial is not None:
        serial = _advertised_serial(service_info)
        if serial is None or serial.upper() != expected_serial.upper():
            raise LocalBleCommissioningError(
                f"Advertisement serial mismatch: expected {expected_serial}, got {serial}"
            )

    raw_scan_record = _extract_raw_scan_record(service_info)
    if raw_scan_record is None:
        _LOGGER.debug("Raw advertisement bytes unavailable for %s", expected_address)
        return

    if len(raw_scan_record) <= 30:
        _LOGGER.debug(
            "Raw advertisement bytes shorter than expected for protocol validation"
        )
        return

    hardware_type = raw_scan_record[21]
    if hardware_type != E7_PLUS_HARDWARE_TYPE:
        raise LocalBleCommissioningError(
            f"Advertisement hardware type mismatch: expected {E7_PLUS_HARDWARE_TYPE}, got {hardware_type}"
        )


async def _async_resolve_connectable_device(
    hass: HomeAssistant,
    address: str,
    *,
    expected_serial: str | None = None,
) -> Any:
    """Resolve a connectable BLEDevice via Home Assistant's shared scanner."""

    from homeassistant.components import bluetooth  # noqa: PLC0415

    scanner_count = bluetooth.async_scanner_count(hass, connectable=True)
    if scanner_count <= 0:
        raise LocalBleCommissioningError(
            "No connectable Bluetooth adapters are available in Home Assistant"
        )

    last_service_info = bluetooth.async_last_service_info(
        hass,
        address,
        connectable=True,
    )
    if last_service_info is not None:
        _validate_advertisement_identity(
            last_service_info,
            expected_address=address,
            expected_serial=expected_serial,
        )

    ble_device = bluetooth.async_ble_device_from_address(
        hass, address, connectable=True
    )
    if ble_device is not None:
        return ble_device

    _LOGGER.info("Waiting for BLE advertisement from %s", address)

    def _process_advertisement(service_info: Any) -> bool:
        if service_info.address.upper() != address.upper():
            return False
        try:
            _validate_advertisement_identity(
                service_info,
                expected_address=address,
                expected_serial=expected_serial,
            )
        except LocalBleCommissioningError:
            return False
        return True

    try:
        await bluetooth.async_process_advertisements(
            hass,
            _process_advertisement,
            {"address": address, "connectable": True},
            bluetooth.BluetoothScanningMode.ACTIVE,
            BLE_DISCOVERY_TIMEOUT_SECONDS,
        )
    except TimeoutError as error:
        raise LocalBleCommissioningError(
            f"Device {address} was not discovered within {BLE_DISCOVERY_TIMEOUT_SECONDS:.0f}s"
        ) from error

    ble_device = bluetooth.async_ble_device_from_address(
        hass, address, connectable=True
    )
    if ble_device is None:
        raise LocalBleCommissioningError(
            f"Device {address} is not reachable from a connectable adapter"
        )

    return ble_device


async def _async_connect_client(
    ble_device: Any,
    *,
    ble_device_callback: Callable[[], Any] | None = None,
) -> Any:
    """Connect to the BLE device and return a connected BleakClient."""

    if BleakClient is None:
        raise LocalBleCommissioningError("Bleak is not available in this environment")

    if establish_connection is not None:
        try:
            return await establish_connection(
                BleakClient,
                ble_device,
                ble_device.address,
                max_attempts=4,
                ble_device_callback=ble_device_callback,
                timeout=BLE_CONNECT_TIMEOUT_SECONDS,
            )
        except Exception as error:
            raise LocalBleCommissioningError(
                f"Could not establish BLE connection to {ble_device.address}"
            ) from error

    client = BleakClient(ble_device, timeout=BLE_CONNECT_TIMEOUT_SECONDS)
    try:
        connected = await client.connect()
    except Exception as error:
        raise LocalBleCommissioningError(
            f"Failed to connect to BLE device {ble_device.address}"
        ) from error

    if not connected:
        raise LocalBleCommissioningError(
            f"Could not establish BLE connection to {ble_device.address}"
        )

    return client


async def _async_disconnect_client(client: Any) -> None:
    """Disconnect BLE client safely."""

    try:
        await client.disconnect()
    except (BleakError, RuntimeError, OSError, EOFError):
        _LOGGER.debug("Failed to disconnect BLE client cleanly", exc_info=True)


async def _async_commission_over_rpc(
    rpc_client: _BleUartRpcClient,
    *,
    gateway_mac_id: str,
    timezone_id: int,
    owner_email: str,
    receiver_name: str,
    include_wifi_step: bool = False,
) -> dict[str, str | None]:
    """Commission over BLE, trying the already-commissioned path first.

    Matches the app behavior: SystemAlreadyCommissionedActivity tries
    SetOwner -> GetBLEKey without touching timezone/time/device details.
    Only falls back to the full first-time sequence if the simple path
    fails with a transport-level error.
    """

    try:
        return await _async_commission_already_paired(
            rpc_client,
            gateway_mac_id=gateway_mac_id,
            owner_email=owner_email,
            receiver_name=receiver_name,
        )
    except LocalBleCommissioningError as error:
        _LOGGER.info(
            "Already-commissioned path failed (%s), trying full commissioning",
            error,
        )

    await rpc_client.async_rpc_request(
        gateway_mac_id=gateway_mac_id,
        handler_id=HANDLER_SET_TIMEZONE_ID,
        service_id=SERVICE_ID_TIME_MANAGEMENT,
        args=[timezone_id],
    )
    await rpc_client.async_rpc_request(
        gateway_mac_id=gateway_mac_id,
        handler_id=HANDLER_SET_TIME,
        service_id=SERVICE_ID_TIME_MANAGEMENT,
        args=[int(time.time())],
    )
    await rpc_client.async_rpc_request(
        gateway_mac_id=gateway_mac_id,
        handler_id=HANDLER_UPDATE_PHYSICAL_DEVICE_DETAILS,
        service_id=SERVICE_ID_HAN_MANAGEMENT,
        args=[
            {
                "P": 1,
                "N": receiver_name,
                "L": "",
                "LO": "",
                "FT": 0,
            }
        ],
    )

    owner_result = await _async_set_owner_with_retry(
        rpc_client,
        gateway_mac_id=gateway_mac_id,
        owner_email=owner_email,
        receiver_name=receiver_name,
    )

    ble_key_result = await rpc_client.async_rpc_request(
        gateway_mac_id=gateway_mac_id,
        handler_id=HANDLER_GET_BLE_KEY,
        service_id=SERVICE_ID_HAN_MANAGEMENT,
        args=None,
    )
    ble_key = ble_key_result if isinstance(ble_key_result, str) else None
    if not ble_key:
        raise LocalBleCommissioningError("Gateway did not return a BLE key")

    if include_wifi_step:
        raise LocalBleCommissioningError(
            "SetWifiCredential commissioning step is not implemented yet"
        )

    await rpc_client.async_rpc_request(
        gateway_mac_id=gateway_mac_id,
        handler_id=HANDLER_ADD_HAN_DEVICES,
        service_id=SERVICE_ID_HAN_MANAGEMENT,
        args=_default_han_device_details(),
    )

    owner_token = owner_result if isinstance(owner_result, str) else None
    return {
        "local_ble_key": ble_key,
        "local_owner_token": owner_token,
    }


async def _async_commission_already_paired(
    rpc_client: _BleUartRpcClient,
    *,
    gateway_mac_id: str,
    owner_email: str,
    receiver_name: str,
) -> dict[str, str | None]:
    """Retrieve credentials from an already-commissioned device.

    Tries GetBLEKey first — if the device already has a key it is returned
    without touching ownership.  Only calls SetOwner when the device does
    not yet have a key assigned.
    """

    ble_key_result = await rpc_client.async_rpc_request(
        gateway_mac_id=gateway_mac_id,
        handler_id=HANDLER_GET_BLE_KEY,
        service_id=SERVICE_ID_HAN_MANAGEMENT,
        args=None,
    )
    ble_key = ble_key_result if isinstance(ble_key_result, str) else None
    if ble_key:
        return {
            "local_ble_key": ble_key,
            "local_owner_token": None,
        }

    owner_result = await _async_set_owner_with_retry(
        rpc_client,
        gateway_mac_id=gateway_mac_id,
        owner_email=owner_email,
        receiver_name=receiver_name,
    )

    ble_key_result = await rpc_client.async_rpc_request(
        gateway_mac_id=gateway_mac_id,
        handler_id=HANDLER_GET_BLE_KEY,
        service_id=SERVICE_ID_HAN_MANAGEMENT,
        args=None,
    )
    ble_key = ble_key_result if isinstance(ble_key_result, str) else None
    if not ble_key:
        raise LocalBleCommissioningError("Gateway did not return a BLE key")

    owner_token = owner_result if isinstance(owner_result, str) else None
    return {
        "local_ble_key": ble_key,
        "local_owner_token": owner_token,
    }


async def _async_set_owner_with_retry(
    rpc_client: _BleUartRpcClient,
    *,
    gateway_mac_id: str,
    owner_email: str,
    receiver_name: str,
) -> Any:
    """Set receiver owner with app-equivalent retries for timeout error 20001."""

    for attempt in range(SET_OWNER_RETRY_LIMIT + 1):
        try:
            return await rpc_client.async_rpc_request(
                gateway_mac_id=gateway_mac_id,
                handler_id=HANDLER_SET_OWNER_IN_RECEIVER,
                service_id=SERVICE_ID_COMMS_MANAGER,
                args=[{"UEA": owner_email, "SN": receiver_name, "MN": None}],
                timeout=BLE_REQUEST_TIMEOUT_SECONDS,
            )
        except LocalBleCommissioningError as error:
            if error.error_code != SET_OWNER_TIMEOUT_ERROR:
                raise
            if attempt >= SET_OWNER_RETRY_LIMIT:
                raise
            _LOGGER.warning(
                "SetOwnerInReceiver timed out with %s (attempt %s/%s), retrying",
                SET_OWNER_TIMEOUT_ERROR,
                attempt + 1,
                SET_OWNER_RETRY_LIMIT + 1,
            )

    raise LocalBleCommissioningError("SetOwnerInReceiver retries exhausted")


def _default_han_device_details() -> list[dict[str, Any]]:
    """Build a default two-channel HAN device list for E7+ commissioning."""

    return [
        {
            "ZT": 1,
            "CN": 1,
            "DT": 0,
            "ZN": 0,
            "MC": 0,
            "SN": "",
            "ZNM": "Hot Water",
            "RHT": E7_PLUS_HARDWARE_TYPE,
            "CT": 0,
        },
        {
            "ZT": 2,
            "CN": 2,
            "DT": 0,
            "ZN": 0,
            "MC": 0,
            "SN": "",
            "ZNM": "Timer",
            "RHT": E7_PLUS_HARDWARE_TYPE,
            "CT": 0,
        },
    ]


def _resolve_timezone_id(hass: HomeAssistant) -> int:
    """Resolve the gateway timezone ID using app-parity timezone mappings."""

    timezone_name = getattr(getattr(hass, "config", None), "time_zone", "")
    if isinstance(timezone_name, str):
        lookup_key = timezone_name.strip().lower()
        if lookup_key:
            matched_timezone = _APP_TIMEZONE_ID_LOOKUP.get(lookup_key)
            if matched_timezone is not None:
                return matched_timezone
    return DEFAULT_TIMEZONE_ID


def _extract_service_values(response: Any) -> list[dict[str, Any]]:
    """Extract ServiceValuesDTO entries from a GetAllServiceValues response."""

    service_values: list[Any] | None = None
    if isinstance(response, dict):
        response_values = response.get("V")
        if isinstance(response_values, list):
            service_values = response_values
    elif isinstance(response, list):
        service_values = response

    if not isinstance(service_values, list):
        return []

    return [entry for entry in service_values if isinstance(entry, dict)]


def _extract_characteristic_map(
    service_entry: dict[str, Any],
) -> dict[int, dict[str, Any]]:
    """Build a characteristic-id map from a service values entry."""

    raw_characteristics = service_entry.get("V")
    if not isinstance(raw_characteristics, list):
        return {}

    characteristic_map: dict[int, dict[str, Any]] = {}
    for characteristic in raw_characteristics:
        if not isinstance(characteristic, dict):
            continue
        char_id = characteristic.get("I")
        if not isinstance(char_id, int):
            continue
        if char_id in characteristic_map:
            continue
        characteristic_map[char_id] = characteristic

    return characteristic_map


def _extract_numeric_value(payload: dict[str, Any], key: str) -> float | None:
    """Return a float value for a dictionary key when numeric."""

    value = payload.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        candidate = value.strip()
        if candidate == "":
            return None
        try:
            parsed = float(candidate)
        except ValueError:
            return None
        if math.isfinite(parsed):
            return parsed
    return None


def _extract_service_boi(service_entry: dict[str, Any]) -> int | None:
    """Return a valid business-object identifier from a service values entry."""

    boi = service_entry.get("I")
    if isinstance(boi, int):
        return boi
    return None


def _extract_service_identifier(service_entry: dict[str, Any]) -> int | None:
    """Return a service identifier from a service-values entry."""

    service_id = service_entry.get("SI")
    if not isinstance(service_id, int):
        service_id = service_entry.get("value")
    if isinstance(service_id, int):
        return service_id
    return None


def _find_service_boi(response: Any, *, service_id: int) -> int | None:
    """Find the first BOI for the requested service identifier."""

    for service_entry in _extract_service_values(response):
        if _extract_service_identifier(service_entry) != service_id:
            continue
        boi = _extract_service_boi(service_entry)
        if boi is not None:
            return boi
    return None


def _find_service_bois(response: Any, *, service_id: int) -> list[int]:
    """Find all distinct BOIs for the requested service identifier."""

    bois: list[int] = []
    for service_entry in _extract_service_values(response):
        if _extract_service_identifier(service_entry) != service_id:
            continue
        boi = _extract_service_boi(service_entry)
        if boi is None or boi in bois:
            continue
        bois.append(boi)
    return bois


def _coerce_schedule_zone_bois(
    fallback_zone_bois: Mapping[str, Any] | None,
) -> dict[str, int]:
    """Normalize optional zone-BOI hints into a strict mapping."""

    zone_bois = dict(PROGRAM_ZONE_INDEX)
    if not isinstance(fallback_zone_bois, Mapping):
        return zone_bois

    for zone in PROGRAM_ZONE_INDEX:
        candidate = fallback_zone_bois.get(zone)
        if isinstance(candidate, int) and candidate > 0:
            zone_bois[zone] = candidate

    return zone_bois


def _resolve_mode_and_hot_water_service_bois(
    response: Any,
) -> tuple[int | None, int | None]:
    """Resolve mode-service and hot-water-service BOIs from service values."""

    mode_boi = _find_service_boi(response, service_id=SERVICE_ID_MODE)
    if mode_boi is None:
        mode_boi = _find_service_boi(response, service_id=SERVICE_ID_MODE_LEGACY)

    hot_water_boi = _find_service_boi(response, service_id=SERVICE_ID_HOT_WATER)
    return mode_boi, hot_water_boi


def _resolve_schedule_zone_bois_from_service_values(
    response: Any,
    *,
    fallback_zone_bois: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    """Resolve primary/boost schedule BOIs from GetAllServiceValues payloads."""

    zone_bois = _coerce_schedule_zone_bois(fallback_zone_bois)

    primary_zone_boi_hint, boost_zone_boi_hint = (
        _resolve_mode_and_hot_water_service_bois(response)
    )
    if primary_zone_boi_hint is not None:
        zone_bois["primary"] = primary_zone_boi_hint
    if boost_zone_boi_hint is not None:
        zone_bois["boost"] = boost_zone_boi_hint

    schedule_bois = _find_service_bois(response, service_id=SERVICE_ID_SCHEDULE)
    if not schedule_bois:
        return zone_bois

    if zone_bois["primary"] not in schedule_bois:
        zone_bois["primary"] = schedule_bois[0]

    if (
        zone_bois["boost"] == zone_bois["primary"]
        or zone_bois["boost"] not in schedule_bois
    ):
        alternate = next(
            (
                boi
                for boi in schedule_bois
                if boi != zone_bois["primary"]
            ),
            None,
        )
        if alternate is not None:
            zone_bois["boost"] = alternate

    return zone_bois


async def _async_get_all_service_values(
    rpc_client: _BleUartRpcClient,
    *,
    gateway_mac_id: str,
) -> Any:
    """Fetch the local BLE GetAllServiceValues payload."""

    return await rpc_client.async_rpc_request(
        gateway_mac_id=gateway_mac_id,
        handler_id=HANDLER_GET_ALL_SERVICE_VALUES,
        service_id=SERVICE_ID_BBSERVER,
        args=None,
    )


async def _async_resolve_schedule_zone_bois(
    rpc_client: _BleUartRpcClient,
    *,
    gateway_mac_id: str,
    fallback_zone_bois: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    """Resolve primary/boost schedule BOIs from live service values."""

    response = await _async_get_all_service_values(
        rpc_client,
        gateway_mac_id=gateway_mac_id,
    )
    return _resolve_schedule_zone_bois_from_service_values(
        response,
        fallback_zone_bois=fallback_zone_bois,
    )


def _active_energy_to_kwh(raw_value: float | None) -> float | None:
    """Convert ActiveEnergy characteristic values to kilowatt-hours."""

    if raw_value is None or raw_value < 0:
        return None
    return raw_value / 1000.0


def _minutes_to_hours(value: float | None) -> float | None:
    """Convert runtime minutes into fractional hours."""

    if value is None or value < 0:
        return None
    return value / 60.0


def _extract_duration_minutes(row: dict[str, Any], key: str) -> float:
    """Extract duration minutes from a consumption row with zero default."""

    value = _extract_numeric_value(row, key)
    if value is None or value < 0:
        return 0.0
    return value


def _extract_consumption_energy_kwh(row: dict[str, Any], key: str) -> float | None:
    """Extract a daily energy value from DynamicTariff rows as kWh."""

    value = _extract_numeric_value(row, key)
    if value is None or value < 0:
        return None
    return value / 1000.0


def _parse_consumption_report_day(timestamp_value: float | None) -> str | None:
    """Parse the dynamic tariff timestamp into an ISO report day."""

    if timestamp_value is None or timestamp_value <= 0:
        return None
    timestamp_seconds = timestamp_value
    if timestamp_seconds >= 1_000_000_000_000:
        timestamp_seconds = timestamp_seconds / 1000.0

    try:
        return time.strftime("%Y-%m-%d", time.gmtime(timestamp_seconds))
    except (OverflowError, ValueError):
        return None


def _extract_consumption_rows(response: Any) -> list[dict[str, Any]]:
    """Extract DynamicDaySchedule-like rows from a consumption payload."""

    payload = response
    if isinstance(payload, dict):
        payload_values = payload.get("V")
        if isinstance(payload_values, list):
            payload = payload_values
        elif isinstance(payload.get("D"), list):
            payload = [payload]
        elif isinstance(payload.get("D"), dict):
            payload = [{"D": [payload.get("D")]}]
        elif any(key in payload for key in ("OA", "OS", "BA", "BS", "T")):
            payload = [payload]

    if not isinstance(payload, list):
        _LOGGER.debug(
            "DynamicTariff consumption payload is not a list: %s",
            _format_debug_payload(response),
        )
        return []

    rows: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        if any(key in item for key in ("OA", "OS", "BA", "BS", "T")):
            rows.append(item)
            continue
        day_entries = item.get("D")
        if not isinstance(day_entries, list):
            continue
        rows.extend(entry for entry in day_entries if isinstance(entry, dict))

    return rows


def _parse_consumption_day_rows(response: Any) -> list[dict[str, Any]]:
    """Parse DynamicTariff payload rows into normalized per-day values."""

    rows = _extract_consumption_rows(response)
    if not rows:
        _LOGGER.debug(
            "DynamicTariff consumption payload had no day rows: %s",
            _format_debug_payload(response),
        )
        return []

    parsed_rows: list[dict[str, Any]] = []
    for row in rows:
        timestamp = _extract_numeric_value(row, "T")
        report_day = _parse_consumption_report_day(timestamp)

        parsed_rows.append(
            {
                "timestamp": timestamp,
                "report_day": report_day,
                "primary_runtime_hours": _minutes_to_hours(
                    _extract_duration_minutes(row, "OA")
                ),
                "primary_scheduled_hours": _minutes_to_hours(
                    _extract_duration_minutes(row, "OS")
                ),
                "boost_runtime_hours": _minutes_to_hours(
                    _extract_duration_minutes(row, "BA")
                ),
                "boost_scheduled_hours": _minutes_to_hours(
                    _extract_duration_minutes(row, "BS")
                ),
                "primary_energy_kwh": _extract_consumption_energy_kwh(row, "OP"),
                "boost_energy_kwh": _extract_consumption_energy_kwh(row, "BP"),
                "raw": row,
            }
        )

    return parsed_rows


def _select_latest_consumption_row(
    parsed_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Select the latest parsed consumption row using timestamp ordering."""

    if not parsed_rows:
        return None

    def _sort_key(item: dict[str, Any]) -> float:
        timestamp = item.get("timestamp")
        if not isinstance(timestamp, (int, float)):
            return float("-inf")
        return float(timestamp)

    return max(parsed_rows, key=_sort_key)


def _consumption_rows_have_energy(parsed_rows: list[dict[str, Any]]) -> bool:
    """Return whether parsed consumption rows contain OP/BP energy values."""

    for row in parsed_rows:
        if not isinstance(row, dict):
            continue
        for field_name in ("primary_energy_kwh", "boost_energy_kwh"):
            value = row.get(field_name)
            if isinstance(value, (int, float)) and value >= 0:
                return True
    return False


def _parse_consumption_state(response: Any) -> dict[str, dict[str, Any]] | None:
    """Parse DynamicTariff consumption payload into recent duration summaries."""

    parsed_rows = _parse_consumption_day_rows(response)
    if not parsed_rows:
        return None

    latest = _select_latest_consumption_row(parsed_rows)
    if latest is None:
        return None

    report_day = latest.get("report_day")
    if not isinstance(report_day, str):
        report_day = None

    parsed = {
        "primary": {
            "report_day": report_day,
            "runtime_hours": latest.get("primary_runtime_hours"),
            "scheduled_hours": latest.get("primary_scheduled_hours"),
            "energy_kwh": latest.get("primary_energy_kwh"),
        },
        "boost": {
            "report_day": report_day,
            "runtime_hours": latest.get("boost_runtime_hours"),
            "scheduled_hours": latest.get("boost_scheduled_hours"),
            "energy_kwh": latest.get("boost_energy_kwh"),
        },
    }

    raw_row = latest.get("raw")
    if not isinstance(raw_row, dict):
        raw_row = {}

    _LOGGER.debug(
        "Parsed DynamicTariff consumption row: report_day=%s OA=%s OS=%s BA=%s BS=%s OP=%s BP=%s",
        report_day,
        raw_row.get("OA"),
        raw_row.get("OS"),
        raw_row.get("BA"),
        raw_row.get("BS"),
        raw_row.get("OP"),
        raw_row.get("BP"),
    )
    return parsed


def _parse_local_ble_snapshot(response: Any) -> LocalBleSnapshot:
    """Parse GetAllServiceValues payload into local runtime fields."""

    snapshot = LocalBleSnapshot()
    snapshot.schedule_zone_bois = _resolve_schedule_zone_bois_from_service_values(
        response
    )

    for service_entry in _extract_service_values(response):
        service_id = service_entry.get("SI")
        if not isinstance(service_id, int):
            service_id = service_entry.get("value")
        if not isinstance(service_id, int):
            continue

        characteristic_map = _extract_characteristic_map(service_entry)
        if service_id in (
            SERVICE_ID_MODE,
            SERVICE_ID_MODE_LEGACY,
            SERVICE_ID_PRIMARY_STATE,
        ):
            mode_characteristic = characteristic_map.get(DEVICE_CHARACTERISTIC_MODE)
            if mode_characteristic is not None:
                mode_value = _extract_numeric_value(mode_characteristic, "V")
                if mode_value is not None:
                    snapshot.primary_power_on = int(mode_value) == MODE_HOME_VALUE

            primary_energy_characteristic = characteristic_map.get(
                DEVICE_CHARACTERISTIC_ACTIVE_ENERGY
            )
            if primary_energy_characteristic is not None:
                energy_value = _extract_numeric_value(
                    primary_energy_characteristic, "V"
                )
                snapshot.primary_energy_kwh = _active_energy_to_kwh(energy_value)

        elif service_id == SERVICE_ID_HOT_WATER:
            boost_state_characteristic = characteristic_map.get(
                DEVICE_CHARACTERISTIC_HOT_WATER_STATE
            )
            if boost_state_characteristic is not None:
                boost_state_value = _extract_numeric_value(
                    boost_state_characteristic, "V"
                )
                override_type = _extract_numeric_value(boost_state_characteristic, "OT")
                duration_value = _extract_numeric_value(boost_state_characteristic, "D")

                if boost_state_value is not None:
                    snapshot.timed_boost_active = (
                        override_type is not None
                        and int(override_type) == OVERRIDE_TYPE_ADVANCE
                    )

                if duration_value is not None and duration_value > 0:
                    snapshot.timed_boost_duration_minutes = int(duration_value)

            timed_boost_enabled_characteristic = characteristic_map.get(
                DEVICE_CHARACTERISTIC_SCHEDULE_ENABLE_DISABLE
            )
            if timed_boost_enabled_characteristic is not None:
                enabled_value = _extract_numeric_value(
                    timed_boost_enabled_characteristic,
                    "V",
                )
                if enabled_value is not None:
                    snapshot.timed_boost_enabled = int(enabled_value) != 0

            boost_energy_characteristic = characteristic_map.get(
                DEVICE_CHARACTERISTIC_ACTIVE_ENERGY
            )
            if boost_energy_characteristic is not None:
                boost_energy_value = _extract_numeric_value(
                    boost_energy_characteristic, "V"
                )
                snapshot.boost_energy_kwh = _active_energy_to_kwh(boost_energy_value)

    _LOGGER.debug(
        "Parsed local BLE snapshot: primary_power_on=%s timed_boost_enabled=%s timed_boost_active=%s boost_duration=%s primary_energy_kwh=%s boost_energy_kwh=%s schedule_zone_bois=%s",
        snapshot.primary_power_on,
        snapshot.timed_boost_enabled,
        snapshot.timed_boost_active,
        snapshot.timed_boost_duration_minutes,
        snapshot.primary_energy_kwh,
        snapshot.boost_energy_kwh,
        snapshot.schedule_zone_bois,
    )

    return snapshot


def _command_to_rpc_payload(
    method_name: str,
    operation_kwargs: dict[str, Any],
    *,
    mode_service_boi: int,
    hot_water_service_boi: int,
) -> tuple[int, int, list[Any] | None]:
    """Translate a local command method name to RPC header and arguments."""

    if method_name == "start_timed_boost":
        duration = operation_kwargs.get("duration_minutes")
        if not isinstance(duration, int) or duration <= 0:
            raise ValueError("Boost duration must be a positive number of minutes")
        return (
            HANDLER_WRITE_DATA,
            SERVICE_ID_HOT_WATER,
            [hot_water_service_boi, {"D": duration, "I": 4, "OT": 2, "V": 0}],
        )

    if method_name == "stop_timed_boost":
        return (
            HANDLER_WRITE_DATA,
            SERVICE_ID_HOT_WATER,
            [hot_water_service_boi, {"D": 0, "I": 4, "OT": 2, "V": 0}],
        )

    if method_name == "set_timed_boost_enabled":
        enabled = operation_kwargs.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("Timed boost enabled flag must be boolean")
        return (
            HANDLER_WRITE_DATA,
            SERVICE_ID_HOT_WATER,
            [hot_water_service_boi, {"I": 27, "V": 1 if enabled else 0}],
        )

    if method_name == "turn_controller_on":
        return (
            HANDLER_WRITE_DATA,
            SERVICE_ID_MODE,
            [mode_service_boi, {"I": 6, "V": 2}],
        )

    if method_name == "turn_controller_off":
        return (
            HANDLER_WRITE_DATA,
            SERVICE_ID_MODE,
            [mode_service_boi, {"I": 6, "V": 0}],
        )

    raise LocalBleCommissioningError(
        f"Unsupported local BLE command method: {method_name}"
    )


async def _async_resolve_mode_and_hot_water_service_bois(
    rpc_client: _BleUartRpcClient,
    *,
    gateway_mac_id: str,
) -> tuple[int, int]:
    """Resolve mode-service and hot-water-service BOIs with defaults."""

    response = await _async_get_all_service_values(
        rpc_client,
        gateway_mac_id=gateway_mac_id,
    )

    resolved_mode_service_boi, resolved_hot_water_service_boi = (
        _resolve_mode_and_hot_water_service_bois(response)
    )

    mode_service_boi = (
        resolved_mode_service_boi if resolved_mode_service_boi is not None else 1
    )
    hot_water_service_boi = (
        resolved_hot_water_service_boi if resolved_hot_water_service_boi is not None else 2
    )

    return mode_service_boi, hot_water_service_boi


async def async_read_local_snapshot(
    hass: HomeAssistant,
    *,
    mac_address: str,
    serial_number: str | None,
    ble_key: str,
    consumption_timeout: float = BLE_CONSUMPTION_REQUEST_TIMEOUT_SECONDS,
) -> LocalBleSnapshot:
    """Read and parse local BLE service values for runtime sensors."""

    ble_address = _format_mac_address(mac_address)
    gateway_mac_id = str(int(mac_address, 16))

    try:
        decoded_key = base64.b64decode(ble_key)
    except Exception as error:
        raise LocalBleCommissioningError(
            "Stored BLE key is not valid base64"
        ) from error

    if len(decoded_key) != 16:
        raise LocalBleCommissioningError("Stored BLE key must decode to 16 bytes")

    for attempt in range(BLE_COMMAND_RETRY_LIMIT + 1):
        try:
            return await _async_read_ble_snapshot_once(
                hass,
                ble_address=ble_address,
                expected_serial=serial_number,
                gateway_mac_id=gateway_mac_id,
                auth_key=decoded_key,
                consumption_timeout=consumption_timeout,
            )
        except LocalBleCommissioningError as error:
            if attempt >= BLE_COMMAND_RETRY_LIMIT:
                raise
            _LOGGER.warning(
                "BLE snapshot read failed (attempt %s/%s): %s, retrying",
                attempt + 1,
                BLE_COMMAND_RETRY_LIMIT + 1,
                error,
            )
            await asyncio.sleep(BLE_COMMAND_RETRY_DELAY_SECONDS)

    raise LocalBleCommissioningError("BLE snapshot retries exhausted")


async def _async_read_ble_snapshot_once(
    hass: HomeAssistant,
    *,
    ble_address: str,
    expected_serial: str | None,
    gateway_mac_id: str,
    auth_key: bytes,
    consumption_timeout: float = BLE_CONSUMPTION_REQUEST_TIMEOUT_SECONDS,
) -> LocalBleSnapshot:
    """Connect over BLE once and read a snapshot payload."""

    ble_device = await _async_resolve_connectable_device(
        hass,
        ble_address,
        expected_serial=expected_serial,
    )

    def _refresh_ble_device() -> Any:
        from homeassistant.components import bluetooth  # noqa: PLC0415

        refreshed = bluetooth.async_ble_device_from_address(
            hass,
            ble_address,
            connectable=True,
        )
        return refreshed if refreshed is not None else ble_device

    client = await _async_connect_client(
        ble_device,
        ble_device_callback=_refresh_ble_device,
    )
    rpc_client = _BleUartRpcClient(client)
    try:
        await rpc_client.async_initialize()
        await rpc_client.async_authorize(auth_key)
        return await _async_read_ble_snapshot_payload(
            rpc_client,
            ble_address=ble_address,
            gateway_mac_id=gateway_mac_id,
            consumption_timeout=consumption_timeout,
        )
    finally:
        await rpc_client.async_close()
        await _async_disconnect_client(client)


async def _async_read_ble_snapshot_payload(
    rpc_client: _BleUartRpcClient,
    *,
    ble_address: str,
    gateway_mac_id: str,
    consumption_timeout: float = BLE_CONSUMPTION_REQUEST_TIMEOUT_SECONDS,
    consumption_args_candidates: list[list[int]] | None = None,
    successful_consumption_args: list[tuple[int, ...]] | None = None,
) -> LocalBleSnapshot:
    """Read one local BLE snapshot payload using an active RPC client."""

    response = await rpc_client.async_rpc_request(
        gateway_mac_id=gateway_mac_id,
        handler_id=HANDLER_GET_ALL_SERVICE_VALUES,
        service_id=SERVICE_ID_BBSERVER,
        args=None,
    )
    _LOGGER.debug(
        "GetAllServiceValues summary for %s: %s",
        ble_address,
        _summarize_service_values(response),
    )
    _LOGGER.debug(
        "GetAllServiceValues raw payload for %s: %s",
        ble_address,
        _format_debug_payload(response),
    )
    snapshot = _parse_local_ble_snapshot(response)

    dynamic_tariff_boi = _find_service_boi(
        response,
        service_id=SERVICE_ID_DYNAMIC_TARIFF,
    )
    if consumption_args_candidates is None:
        consumption_args_candidates = [[1], [0]]
    else:
        consumption_args_candidates = list(consumption_args_candidates)
    if dynamic_tariff_boi is not None:
        consumption_args_candidates.append([dynamic_tariff_boi])

    deduped_consumption_args: list[list[int]] = []
    seen_consumption_args: set[tuple[int, ...]] = set()
    for consumption_args in consumption_args_candidates:
        key = tuple(consumption_args)
        if key in seen_consumption_args:
            continue
        seen_consumption_args.add(key)
        deduped_consumption_args.append(consumption_args)

    if not deduped_consumption_args:
        _LOGGER.debug(
            "No local BLE consumption selector candidates available for %s",
            ble_address,
        )
        return snapshot

    for consumption_args in deduped_consumption_args:
        try:
            consumption_response = await rpc_client.async_rpc_request(
                gateway_mac_id=gateway_mac_id,
                handler_id=HANDLER_GET_CONSUMPTION_STATE,
                service_id=SERVICE_ID_DYNAMIC_TARIFF,
                args=consumption_args,
                timeout=consumption_timeout,
            )
            _LOGGER.debug(
                "GetConsumptionState raw payload for %s args=%s: %s",
                ble_address,
                consumption_args,
                _format_debug_payload(consumption_response),
            )
            snapshot.consumption_days = _parse_consumption_day_rows(
                consumption_response
            )
            has_consumption_energy = _consumption_rows_have_energy(
                snapshot.consumption_days
            )
            snapshot.statistics_recent = _parse_consumption_state(consumption_response)
            if snapshot.statistics_recent is not None:
                if successful_consumption_args is not None:
                    successful_consumption_args[:] = [tuple(consumption_args)]
                primary_stats = snapshot.statistics_recent.get("primary")
                if (
                    snapshot.primary_energy_kwh is None
                    and isinstance(primary_stats, dict)
                    and isinstance(primary_stats.get("energy_kwh"), (int, float))
                ):
                    snapshot.primary_energy_kwh = float(primary_stats["energy_kwh"])

                boost_stats = snapshot.statistics_recent.get("boost")
                if (
                    snapshot.boost_energy_kwh is None
                    and isinstance(boost_stats, dict)
                    and isinstance(boost_stats.get("energy_kwh"), (int, float))
                ):
                    snapshot.boost_energy_kwh = float(boost_stats["energy_kwh"])

                _LOGGER.debug(
                    "Parsed statistics_recent from GetConsumptionState args=%s: %s",
                    consumption_args,
                    _format_debug_payload(snapshot.statistics_recent),
                )
                if (
                    has_consumption_energy
                    or snapshot.primary_energy_kwh is not None
                    or snapshot.boost_energy_kwh is not None
                ):
                    break
        except LocalBleCommissioningError as error:
            _LOGGER.debug(
                "Failed to read local BLE consumption state for args %s: %s",
                consumption_args,
                error,
            )

    if snapshot.statistics_recent is None:
        _LOGGER.debug(
            "No statistics_recent parsed from local BLE consumption responses for %s",
            ble_address,
        )

    return snapshot


def _parse_local_weekly_program(payload: Any) -> "WeeklyProgram":
    """Parse one weekly program payload from local BLE RPC format."""

    from .beanbag import BeanbagBackend, BeanbagError  # noqa: PLC0415

    parse_payload = payload
    if isinstance(parse_payload, dict):
        values = parse_payload.get("V")
        if isinstance(values, list):
            parse_payload = values

    try:
        return BeanbagBackend._parse_weekly_program(parse_payload)
    except BeanbagError as error:
        raise LocalBleCommissioningError(
            f"Failed to parse local BLE weekly program payload: {error}"
        ) from error


async def async_read_local_weekly_programs(
    hass: HomeAssistant,
    *,
    mac_address: str,
    serial_number: str | None,
    ble_key: str,
    zone_bois: Mapping[str, int] | None = None,
) -> tuple[
    dict[str, "WeeklyProgram | None"],
    dict[str, list[tuple[int, int]] | None],
]:
    """Read primary and boost weekly schedules from local BLE RPC."""

    ble_address = _format_mac_address(mac_address)
    gateway_mac_id = str(int(mac_address, 16))

    try:
        decoded_key = base64.b64decode(ble_key)
    except Exception as error:
        raise LocalBleCommissioningError(
            "Stored BLE key is not valid base64"
        ) from error

    if len(decoded_key) != 16:
        raise LocalBleCommissioningError("Stored BLE key must decode to 16 bytes")

    for attempt in range(BLE_COMMAND_RETRY_LIMIT + 1):
        try:
            return await _async_read_ble_weekly_programs_once(
                hass,
                ble_address=ble_address,
                expected_serial=serial_number,
                gateway_mac_id=gateway_mac_id,
                auth_key=decoded_key,
                zone_bois=zone_bois,
            )
        except LocalBleCommissioningError as error:
            if attempt >= BLE_COMMAND_RETRY_LIMIT:
                raise
            _LOGGER.warning(
                "BLE weekly schedule read failed (attempt %s/%s): %s, retrying",
                attempt + 1,
                BLE_COMMAND_RETRY_LIMIT + 1,
                error,
            )
            await asyncio.sleep(BLE_COMMAND_RETRY_DELAY_SECONDS)

    raise LocalBleCommissioningError("BLE weekly schedule retries exhausted")


async def _async_read_ble_weekly_programs_once(
    hass: HomeAssistant,
    *,
    ble_address: str,
    expected_serial: str | None,
    gateway_mac_id: str,
    auth_key: bytes,
    zone_bois: Mapping[str, int] | None = None,
) -> tuple[
    dict[str, "WeeklyProgram | None"],
    dict[str, list[tuple[int, int]] | None],
]:
    """Connect once and read both weekly programs from local BLE RPC."""

    ble_device = await _async_resolve_connectable_device(
        hass,
        ble_address,
        expected_serial=expected_serial,
    )

    def _refresh_ble_device() -> Any:
        from homeassistant.components import bluetooth  # noqa: PLC0415

        refreshed = bluetooth.async_ble_device_from_address(
            hass,
            ble_address,
            connectable=True,
        )
        return refreshed if refreshed is not None else ble_device

    client = await _async_connect_client(
        ble_device,
        ble_device_callback=_refresh_ble_device,
    )
    rpc_client = _BleUartRpcClient(client)
    try:
        await rpc_client.async_initialize()
        await rpc_client.async_authorize(auth_key)
        programs, canonicals, _resolved_zone_bois = (
            await _async_read_ble_weekly_programs_payload(
                rpc_client,
                ble_address=ble_address,
                gateway_mac_id=gateway_mac_id,
                zone_bois=zone_bois,
            )
        )
        return programs, canonicals
    finally:
        await rpc_client.async_close()
        await _async_disconnect_client(client)


async def _async_read_ble_weekly_programs_payload(
    rpc_client: _BleUartRpcClient,
    *,
    ble_address: str,
    gateway_mac_id: str,
    zone_bois: Mapping[str, int] | None = None,
) -> tuple[
    dict[str, "WeeklyProgram | None"],
    dict[str, list[tuple[int, int]] | None],
    dict[str, int],
]:
    """Read weekly schedules with an active RPC client and return resolved BOIs."""

    from .schedule import canonicalize_weekly  # noqa: PLC0415

    resolved_zone_bois = await _async_resolve_schedule_zone_bois(
        rpc_client,
        gateway_mac_id=gateway_mac_id,
        fallback_zone_bois=zone_bois,
    )

    programs: dict[str, Any] = {}
    canonicals: dict[str, list[tuple[int, int]] | None] = {}

    for zone_key in PROGRAM_ZONE_INDEX:
        zone_index = resolved_zone_bois[zone_key]
        response = await rpc_client.async_rpc_request(
            gateway_mac_id=gateway_mac_id,
            handler_id=HANDLER_READ_WEEKLY_PROGRAM,
            service_id=SERVICE_ID_SCHEDULE,
            args=[zone_index],
        )
        _LOGGER.debug(
            "ReadWeeklyProgram raw payload for %s zone=%s idx=%s: %s",
            ble_address,
            zone_key,
            zone_index,
            _format_debug_payload(response),
        )

        program = _parse_local_weekly_program(response)
        programs[zone_key] = program
        canonicals[zone_key] = canonicalize_weekly(program)

    return programs, canonicals, resolved_zone_bois


async def async_write_local_weekly_program(
    hass: HomeAssistant,
    *,
    mac_address: str,
    serial_number: str | None,
    ble_key: str,
    zone: str,
    program: "WeeklyProgram",
    zone_bois: Mapping[str, int] | None = None,
) -> None:
    """Write a weekly schedule for one zone over local BLE RPC."""

    ble_address = _format_mac_address(mac_address)
    gateway_mac_id = str(int(mac_address, 16))

    try:
        decoded_key = base64.b64decode(ble_key)
    except Exception as error:
        raise LocalBleCommissioningError(
            "Stored BLE key is not valid base64"
        ) from error

    if len(decoded_key) != 16:
        raise LocalBleCommissioningError("Stored BLE key must decode to 16 bytes")

    if zone not in PROGRAM_ZONE_INDEX:
        raise ValueError(f"Unsupported weekly schedule zone: {zone}")

    for attempt in range(BLE_COMMAND_RETRY_LIMIT + 1):
        try:
            await _async_write_local_weekly_program_once(
                hass,
                ble_address=ble_address,
                expected_serial=serial_number,
                gateway_mac_id=gateway_mac_id,
                auth_key=decoded_key,
                zone=zone,
                program=program,
                zone_bois=zone_bois,
            )
            return
        except LocalBleCommissioningError as error:
            if attempt >= BLE_COMMAND_RETRY_LIMIT:
                raise
            _LOGGER.warning(
                "BLE weekly schedule write failed (attempt %s/%s): %s, retrying",
                attempt + 1,
                BLE_COMMAND_RETRY_LIMIT + 1,
                error,
            )
            await asyncio.sleep(BLE_COMMAND_RETRY_DELAY_SECONDS)

    raise LocalBleCommissioningError("BLE weekly schedule write retries exhausted")


async def _async_write_local_weekly_program_once(
    hass: HomeAssistant,
    *,
    ble_address: str,
    expected_serial: str | None,
    gateway_mac_id: str,
    auth_key: bytes,
    zone: str,
    program: "WeeklyProgram",
    zone_bois: Mapping[str, int] | None = None,
) -> None:
    """Connect once and write one weekly program payload over BLE."""

    ble_device = await _async_resolve_connectable_device(
        hass,
        ble_address,
        expected_serial=expected_serial,
    )

    def _refresh_ble_device() -> Any:
        from homeassistant.components import bluetooth  # noqa: PLC0415

        refreshed = bluetooth.async_ble_device_from_address(
            hass,
            ble_address,
            connectable=True,
        )
        return refreshed if refreshed is not None else ble_device

    client = await _async_connect_client(
        ble_device,
        ble_device_callback=_refresh_ble_device,
    )
    rpc_client = _BleUartRpcClient(client)
    try:
        await rpc_client.async_initialize()
        await rpc_client.async_authorize(auth_key)
        await _async_write_local_weekly_program_payload(
            rpc_client,
            gateway_mac_id=gateway_mac_id,
            zone=zone,
            program=program,
            zone_bois=zone_bois,
        )
    finally:
        await rpc_client.async_close()
        await _async_disconnect_client(client)


async def _async_write_local_weekly_program_payload(
    rpc_client: _BleUartRpcClient,
    *,
    gateway_mac_id: str,
    zone: str,
    program: "WeeklyProgram",
    zone_bois: Mapping[str, int] | None = None,
) -> dict[str, int]:
    """Write one weekly schedule payload with an active RPC client."""

    from .beanbag import BeanbagBackend  # noqa: PLC0415

    resolved_zone_bois = _coerce_schedule_zone_bois(zone_bois)

    async def _async_write_with_zone_index(zone_index: int) -> Any:
        """Write one weekly payload using the provided zone index."""

        payload = BeanbagBackend._build_weekly_program_payload(program, zone_index)
        return await rpc_client.async_rpc_request(
            gateway_mac_id=gateway_mac_id,
            handler_id=HANDLER_WRITE_WEEKLY_PROGRAM,
            service_id=SERVICE_ID_SCHEDULE,
            args=payload,
        )

    zone_index = resolved_zone_bois[zone]
    try:
        acknowledgement = await _async_write_with_zone_index(zone_index)
    except LocalBleCommissioningError as error:
        if str(error) == BLE_RESPONSE_TIMEOUT_MESSAGE:
            raise

        _LOGGER.debug(
            "WriteWeeklyProgram failed with cached zone BOI %s for %s (%s); resolving schedule BOIs and retrying once",
            zone_index,
            zone,
            error,
        )
        resolved_zone_bois = await _async_resolve_schedule_zone_bois(
            rpc_client,
            gateway_mac_id=gateway_mac_id,
            fallback_zone_bois=resolved_zone_bois,
        )
        acknowledgement = await _async_write_with_zone_index(resolved_zone_bois[zone])

    if acknowledgement not in (0, "0", None):
        _LOGGER.debug(
            "WriteWeeklyProgram returned acknowledgement %s with cached zone BOI %s for %s; resolving schedule BOIs and retrying once",
            acknowledgement,
            zone_index,
            zone,
        )
        resolved_zone_bois = await _async_resolve_schedule_zone_bois(
            rpc_client,
            gateway_mac_id=gateway_mac_id,
            fallback_zone_bois=resolved_zone_bois,
        )
        acknowledgement = await _async_write_with_zone_index(resolved_zone_bois[zone])

    if acknowledgement not in (0, "0", None):
        raise LocalBleCommissioningError(
            f"Unexpected local BLE weekly schedule acknowledgement: {acknowledgement}"
        )

    return resolved_zone_bois


async def async_execute_local_command(
    hass: HomeAssistant,
    *,
    mac_address: str,
    serial_number: str | None,
    ble_key: str,
    method_name: str,
    operation_kwargs: dict[str, Any],
) -> Any:
    """Execute one local BLE command over an authenticated BLE session."""

    ble_address = _format_mac_address(mac_address)
    gateway_mac_id = str(int(mac_address, 16))

    try:
        decoded_key = base64.b64decode(ble_key)
    except Exception as error:
        raise LocalBleCommissioningError(
            "Stored BLE key is not valid base64"
        ) from error

    if len(decoded_key) != 16:
        raise LocalBleCommissioningError("Stored BLE key must decode to 16 bytes")

    last_error: LocalBleCommissioningError | None = None
    for attempt in range(BLE_COMMAND_RETRY_LIMIT + 1):
        try:
            result = await _async_execute_ble_rpc_command(
                hass,
                ble_address=ble_address,
                expected_serial=serial_number,
                gateway_mac_id=gateway_mac_id,
                method_name=method_name,
                operation_kwargs=operation_kwargs,
                auth_key=decoded_key,
            )
            break
        except LocalBleCommissioningError as error:
            last_error = error
            if attempt < BLE_COMMAND_RETRY_LIMIT:
                _LOGGER.warning(
                    "BLE command %s failed (attempt %s/%s): %s, retrying",
                    method_name,
                    attempt + 1,
                    BLE_COMMAND_RETRY_LIMIT + 1,
                    error,
                )
                await asyncio.sleep(BLE_COMMAND_RETRY_DELAY_SECONDS)
            else:
                raise
    else:
        raise last_error  # type: ignore[misc]

    if result not in (0, "0", None):
        raise LocalBleCommissioningError(
            f"Unexpected local BLE acknowledgement for {method_name}: {result}"
        )

    return result


async def _async_execute_ble_rpc_command(
    hass: HomeAssistant,
    *,
    ble_address: str,
    expected_serial: str | None,
    gateway_mac_id: str,
    method_name: str,
    operation_kwargs: dict[str, Any],
    auth_key: bytes | None,
) -> Any:
    """Connect over BLE, optionally authorize, and execute one RPC request."""

    ble_device = await _async_resolve_connectable_device(
        hass,
        ble_address,
        expected_serial=expected_serial,
    )

    def _refresh_ble_device() -> Any:
        from homeassistant.components import bluetooth  # noqa: PLC0415

        refreshed = bluetooth.async_ble_device_from_address(
            hass,
            ble_address,
            connectable=True,
        )
        return refreshed if refreshed is not None else ble_device

    client = await _async_connect_client(
        ble_device,
        ble_device_callback=_refresh_ble_device,
    )
    rpc_client = _BleUartRpcClient(client)
    try:
        await rpc_client.async_initialize()
        if auth_key is not None:
            await rpc_client.async_authorize(auth_key)
        mode_service_boi, hot_water_service_boi = (
            await _async_resolve_mode_and_hot_water_service_bois(
                rpc_client,
                gateway_mac_id=gateway_mac_id,
            )
        )
        handler_id, service_id, args = _command_to_rpc_payload(
            method_name,
            operation_kwargs,
            mode_service_boi=mode_service_boi,
            hot_water_service_boi=hot_water_service_boi,
        )
        return await rpc_client.async_rpc_request(
            gateway_mac_id=gateway_mac_id,
            handler_id=handler_id,
            service_id=service_id,
            args=args,
        )
    finally:
        await rpc_client.async_close()
        await _async_disconnect_client(client)


async def async_commission_local_ble(
    hass: HomeAssistant,
    *,
    serial_number: str,
    mac_address: str,
    device_type: str,
) -> dict[str, str | None]:
    """Commission an E7+ device and return persisted BLE credential values."""

    if device_type.strip().lower() != "e7plus":
        raise LocalBleCommissioningError(
            f"Unsupported local BLE device type for commissioning: {device_type}"
        )

    ble_address = _format_mac_address(mac_address)
    gateway_mac_id = str(int(mac_address, 16))
    receiver_name = f"SecureMTR {serial_number}"
    timezone_id = _resolve_timezone_id(hass)

    _LOGGER.info(
        "Starting local BLE commissioning for serial %s at %s",
        serial_number,
        ble_address,
    )

    ble_device = await _async_resolve_connectable_device(
        hass,
        ble_address,
        expected_serial=serial_number,
    )
    _LOGGER.info("Resolved connectable BLE device %s", ble_device.address)

    def _refresh_ble_device() -> Any:
        from homeassistant.components import bluetooth  # noqa: PLC0415

        refreshed = bluetooth.async_ble_device_from_address(
            hass,
            ble_address,
            connectable=True,
        )
        return refreshed if refreshed is not None else ble_device

    client = await _async_connect_client(
        ble_device,
        ble_device_callback=_refresh_ble_device,
    )
    _LOGGER.info("Connected to BLE device %s", ble_device.address)
    rpc_client = _BleUartRpcClient(client)
    try:
        await rpc_client.async_initialize()
        _LOGGER.info("Initialized UART notification channel for %s", ble_device.address)
        credentials = await _async_commission_over_rpc(
            rpc_client,
            gateway_mac_id=gateway_mac_id,
            timezone_id=timezone_id,
            owner_email=DEFAULT_OWNER_EMAIL,
            receiver_name=receiver_name,
        )
    finally:
        await rpc_client.async_close()
        await _async_disconnect_client(client)

    _LOGGER.info(
        "Local BLE commissioning succeeded for serial %s",
        serial_number,
    )
    return credentials
