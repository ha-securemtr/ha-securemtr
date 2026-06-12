"""Tests for local BLE runtime refresh orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import sys
from unittest.mock import AsyncMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from custom_components.securemtr import (
    CONNECTION_MODE_CLOUD,
    CONNECTION_MODE_LOCAL_BLE,
    DOMAIN,
    SecuremtrController,
    SecuremtrRuntimeData,
    _async_refresh_local_ble_runtime,
    async_refresh_entry_state,
    coerce_end_time,
)
from custom_components.securemtr.local_ble_commissioning import (
    LocalBlePriority,
    LocalBleSnapshot,
)

from tests.helpers import create_config_entry


def _build_runtime(mode: str) -> SecuremtrRuntimeData:
    """Build runtime data with a local controller scaffold."""

    runtime = SecuremtrRuntimeData(backend=SimpleNamespace())
    runtime.connection_mode = mode
    runtime.controller = SecuremtrController(
        identifier="E0044275",
        name="E7+ Smart Water Heater Controller",
        gateway_id="AABBCCDDEEFF",
        serial_number="E0044275",
        model="E7+",
    )
    runtime.controller_ready.set()
    return runtime


def _patch_snapshot_worker(
    monkeypatch: pytest.MonkeyPatch,
    *,
    snapshot: LocalBleSnapshot,
) -> tuple[Any, AsyncMock]:
    """Patch the local BLE worker helper to return a snapshot stub."""

    fake_worker = SimpleNamespace(
        async_read_local_snapshot=AsyncMock(return_value=snapshot),
    )
    get_worker = AsyncMock(return_value=fake_worker)
    monkeypatch.setattr("custom_components.securemtr.async_get_local_ble_worker", get_worker)
    return fake_worker, get_worker


@pytest.mark.asyncio
async def test_refresh_entry_state_routes_local_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure generic entry refresh delegates to local BLE refresh when local mode."""

    runtime = _build_runtime(CONNECTION_MODE_LOCAL_BLE)
    entry = create_config_entry(
        entry_id="entry",
        data={"local_ble_key": "ABEiM0RVZneImaq7zN3u/w=="},
    )
    hass = SimpleNamespace(data={DOMAIN: {entry.entry_id: runtime}})

    local_refresh = AsyncMock()
    weekly_refresh = AsyncMock()
    monkeypatch.setattr(
        "custom_components.securemtr._async_refresh_local_ble_runtime",
        local_refresh,
    )
    monkeypatch.setattr(
        "custom_components.securemtr._async_refresh_weekly_program_cache",
        weekly_refresh,
    )

    await async_refresh_entry_state(hass, entry)

    local_refresh.assert_awaited_once_with(
        hass,
        entry,
        priority=LocalBlePriority.USER_READ,
        coalesce_key=None,
    )
    weekly_refresh.assert_awaited_once_with(
        hass,
        entry,
        priority=LocalBlePriority.USER_READ,
        coalesce_key=None,
    )


@pytest.mark.asyncio
async def test_refresh_entry_state_routes_cloud_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure generic entry refresh delegates to cloud consumption refresh."""

    runtime = _build_runtime(CONNECTION_MODE_CLOUD)
    entry = create_config_entry(entry_id="entry")
    hass = SimpleNamespace(data={DOMAIN: {entry.entry_id: runtime}})

    cloud_refresh = AsyncMock()
    monkeypatch.setattr(
        "custom_components.securemtr.consumption_metrics", cloud_refresh
    )

    await async_refresh_entry_state(hass, entry)

    cloud_refresh.assert_awaited_once_with(hass, entry)


@pytest.mark.asyncio
async def test_local_ble_refresh_updates_runtime_and_energy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure local BLE snapshot refresh mutates runtime state and dispatches."""

    runtime = _build_runtime(CONNECTION_MODE_LOCAL_BLE)
    runtime.energy_state = None
    entry = create_config_entry(
        entry_id="entry",
        data={"local_ble_key": "ABEiM0RVZneImaq7zN3u/w=="},
    )
    hass = SimpleNamespace(data={DOMAIN: {entry.entry_id: runtime}})

    snapshot = LocalBleSnapshot(
        primary_power_on=True,
        timed_boost_enabled=True,
        timed_boost_active=False,
        primary_energy_kwh=12.0,
        boost_energy_kwh=3.5,
        statistics_recent={
            "primary": {
                "report_day": "2026-04-14",
                "runtime_hours": 1.5,
                "scheduled_hours": 2.0,
            },
            "boost": {
                "report_day": "2026-04-14",
                "runtime_hours": 0.5,
                "scheduled_hours": 1.0,
            },
        },
    )
    fake_worker, _get_worker = _patch_snapshot_worker(
        monkeypatch,
        snapshot=snapshot,
    )
    dispatch_calls: list[tuple[Any, str]] = []

    monkeypatch.setattr(
        "custom_components.securemtr.async_dispatch_runtime_update",
        lambda hass_obj, entry_id: dispatch_calls.append((hass_obj, entry_id)),
    )

    await _async_refresh_local_ble_runtime(hass, entry)

    fake_worker.async_read_local_snapshot.assert_awaited_once()

    assert runtime.primary_power_on is True
    assert runtime.timed_boost_enabled is True
    assert runtime.timed_boost_active is False
    assert runtime.timed_boost_end_minute is None
    assert runtime.timed_boost_end_time is None
    assert runtime.energy_state is not None
    assert runtime.energy_state["primary"]["energy_sum"] == pytest.approx(12.0)
    assert runtime.energy_state["boost"]["energy_sum"] == pytest.approx(3.5)
    assert runtime.statistics_recent is not None
    assert runtime.statistics_recent["primary"]["runtime_hours"] == pytest.approx(1.5)
    assert runtime.statistics_recent["boost"]["scheduled_hours"] == pytest.approx(1.0)
    assert runtime.statistics_recent["primary"]["energy_sum"] == pytest.approx(12.0)
    assert runtime.statistics_recent["boost"]["energy_sum"] == pytest.approx(3.5)
    assert dispatch_calls == [(hass, entry.entry_id)]


@pytest.mark.asyncio
async def test_local_ble_refresh_does_not_drift_boost_end_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure active boost polling keeps the prior end time until boost stops."""

    runtime = _build_runtime(CONNECTION_MODE_LOCAL_BLE)
    runtime.timed_boost_active = True
    runtime.timed_boost_end_minute = 480
    runtime.timed_boost_end_time = coerce_end_time(480)
    previous_end_minute = runtime.timed_boost_end_minute
    previous_end_time = runtime.timed_boost_end_time

    entry = create_config_entry(
        entry_id="entry",
        data={"local_ble_key": "ABEiM0RVZneImaq7zN3u/w=="},
    )
    hass = SimpleNamespace(data={DOMAIN: {entry.entry_id: runtime}})

    snapshot = LocalBleSnapshot(
        timed_boost_active=True,
        timed_boost_duration_minutes=30,
    )

    fake_worker, _get_worker = _patch_snapshot_worker(
        monkeypatch,
        snapshot=snapshot,
    )
    dispatch_calls: list[tuple[Any, str]] = []

    monkeypatch.setattr(
        "custom_components.securemtr.async_dispatch_runtime_update",
        lambda hass_obj, entry_id: dispatch_calls.append((hass_obj, entry_id)),
    )
    monkeypatch.setattr(
        "custom_components.securemtr.dt_util.now",
        lambda: datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc),
    )

    await _async_refresh_local_ble_runtime(hass, entry)

    fake_worker.async_read_local_snapshot.assert_awaited_once()

    assert runtime.timed_boost_end_minute == previous_end_minute
    assert runtime.timed_boost_end_time == previous_end_time
    assert dispatch_calls == []


@pytest.mark.asyncio
async def test_local_ble_refresh_sets_boost_end_time_on_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure boost end time is set when a boost first becomes active."""

    runtime = _build_runtime(CONNECTION_MODE_LOCAL_BLE)
    runtime.timed_boost_active = False
    entry = create_config_entry(
        entry_id="entry",
        data={"local_ble_key": "ABEiM0RVZneImaq7zN3u/w=="},
    )
    hass = SimpleNamespace(data={DOMAIN: {entry.entry_id: runtime}})

    snapshot = LocalBleSnapshot(
        timed_boost_active=True,
        timed_boost_duration_minutes=30,
    )
    fake_worker, _get_worker = _patch_snapshot_worker(
        monkeypatch,
        snapshot=snapshot,
    )

    monkeypatch.setattr(
        "custom_components.securemtr.dt_util.now",
        lambda: datetime(2026, 4, 14, 9, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        "custom_components.securemtr.async_dispatch_runtime_update",
        lambda hass_obj, entry_id: None,
    )

    await _async_refresh_local_ble_runtime(hass, entry)

    fake_worker.async_read_local_snapshot.assert_awaited_once()

    assert runtime.timed_boost_active is True
    assert runtime.timed_boost_end_minute == 570


@pytest.mark.asyncio
async def test_local_ble_refresh_prefers_consumption_day_energy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure consumption-day OP/BP data updates runtime energy state."""

    runtime = _build_runtime(CONNECTION_MODE_LOCAL_BLE)
    runtime.energy_state = {
        "primary": {
            "energy_sum": 0.0,
            "last_day": None,
            "series_start": None,
            "offset_kwh": 0.0,
        },
        "boost": {
            "energy_sum": 0.0,
            "last_day": None,
            "series_start": None,
            "offset_kwh": 0.0,
        },
    }
    entry = create_config_entry(
        entry_id="entry",
        data={"local_ble_key": "ABEiM0RVZneImaq7zN3u/w=="},
    )
    hass = SimpleNamespace(data={DOMAIN: {entry.entry_id: runtime}})

    snapshot = LocalBleSnapshot(
        primary_energy_kwh=99.0,
        boost_energy_kwh=55.0,
        consumption_days=[
            {
                "report_day": "2026-04-13",
                "primary_energy_kwh": 8.0,
                "boost_energy_kwh": 2.0,
            }
        ],
        statistics_recent={
            "primary": {"report_day": "2026-04-13", "runtime_hours": 1.0},
            "boost": {"report_day": "2026-04-13", "runtime_hours": 0.5},
        },
    )
    fake_worker, _get_worker = _patch_snapshot_worker(
        monkeypatch,
        snapshot=snapshot,
    )

    class FakeAccumulator:
        def __init__(self) -> None:
            self.add_calls: list[tuple[str, Any, float]] = []

        async def async_add_day(
            self, zone: str, report_day: Any, energy: float
        ) -> bool:
            self.add_calls.append((zone, report_day, energy))
            return True

        def as_sensor_state(self) -> dict[str, dict[str, Any]]:
            return {
                "primary": {
                    "energy_sum": 8.0,
                    "last_day": "2026-04-13",
                    "series_start": "2026-04-13",
                    "offset_kwh": 0.0,
                },
                "boost": {
                    "energy_sum": 2.0,
                    "last_day": "2026-04-13",
                    "series_start": "2026-04-13",
                    "offset_kwh": 0.0,
                },
            }

    fake_accumulator = FakeAccumulator()

    ensure_accumulator = AsyncMock(return_value=fake_accumulator)
    dispatch_calls: list[tuple[Any, str]] = []

    monkeypatch.setattr(
        "custom_components.securemtr._ensure_energy_accumulator",
        ensure_accumulator,
    )
    monkeypatch.setattr(
        "custom_components.securemtr.async_dispatch_runtime_update",
        lambda hass_obj, entry_id: dispatch_calls.append((hass_obj, entry_id)),
    )

    await _async_refresh_local_ble_runtime(hass, entry)

    fake_worker.async_read_local_snapshot.assert_awaited_once()

    assert runtime.energy_state is not None
    assert runtime.energy_state["primary"]["energy_sum"] == pytest.approx(8.0)
    assert runtime.energy_state["boost"]["energy_sum"] == pytest.approx(2.0)
    assert len(fake_accumulator.add_calls) == 2
    assert dispatch_calls == [(hass, entry.entry_id)]
