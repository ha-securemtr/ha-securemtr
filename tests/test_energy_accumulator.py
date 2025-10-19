from __future__ import annotations

import logging
from datetime import date
from typing import Any, cast

import pytest

from custom_components.securemtr import energy as energy_module

from custom_components.securemtr.energy import EnergyAccumulator


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure tests use a stable date for freeze calculations."""

    energy_module._today()
    monkeypatch.setattr(energy_module, "_today", lambda: date(2024, 8, 20))


class FakeStore:
    """Provide an in-memory persistence helper for accumulator tests."""

    def __init__(self, initial: dict[str, object] | None = None) -> None:
        self.data = initial
        self.saved: list[dict[str, object]] = []

    async def async_load(self) -> dict[str, object] | None:
        """Return previously persisted data."""

        return self.data

    async def async_save(self, data: dict[str, object]) -> None:
        """Record the saved payload for inspection."""

        self.data = data
        self.saved.append(data)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "events",
        "expected_sum",
        "expected_offset",
        "expected_start",
        "expected_last",
        "expected_raw",
    ),
    [
        (
            [
                (date(2024, 8, 18), 2.0),
                (date(2024, 8, 19), 2.5),
            ],
            4.5,
            0.0,
            "2024-08-18",
            "2024-08-19",
            4.5,
        ),
        (
            [
                (date(2024, 8, 17), 5.0),
                (date(2024, 8, 18), 2.5),
                (date(2024, 8, 17), 2.0),
            ],
            7.5,
            3.0,
            "2024-08-17",
            "2024-08-18",
            4.5,
        ),
        (
            [
                (date(2024, 8, 19), 4.0),
                (date(2024, 8, 10), 3.0),
            ],
            7.0,
            0.0,
            "2024-08-10",
            "2024-08-19",
            7.0,
        ),
    ],
)
async def test_accumulator_daily_matrix(
    events: list[tuple[date, float]],
    expected_sum: float,
    expected_offset: float,
    expected_start: str,
    expected_last: str,
    expected_raw: float,
) -> None:
    """Exercise the accumulator across the daily growth scenarios matrix."""

    store = FakeStore()
    accumulator = EnergyAccumulator(store=cast(Any, store))
    await accumulator.async_load()

    for report_day, energy in events:
        await accumulator.async_add_day("primary", report_day, energy)

    zone_state = accumulator.as_sensor_state()["primary"]
    assert zone_state["energy_sum"] == pytest.approx(expected_sum)
    assert zone_state["offset_kwh"] == pytest.approx(expected_offset)
    assert zone_state["series_start"] == expected_start
    assert zone_state["last_day"] == expected_last

    assert store.saved, "Accumulator should persist every scenario"
    persisted = store.saved[-1]["primary"]
    assert persisted["raw_cumulative_kwh"] == pytest.approx(expected_raw)
    assert persisted["monotonic_offset"] == pytest.approx(expected_offset)


@pytest.mark.asyncio
async def test_accumulator_progresses_monotonically() -> None:
    """Ensure sequential days increase the cumulative total and persist."""

    store = FakeStore()
    accumulator = EnergyAccumulator(store=cast(Any, store))
    await accumulator.async_load()

    day_1 = date(2024, 3, 1)
    day_2 = date(2024, 3, 2)

    await accumulator.async_add_day("primary", day_1, 2.5)
    await accumulator.async_add_day("primary", day_2, 3.0)

    assert store.saved
    state = accumulator.as_sensor_state()
    primary_state = state["primary"]
    assert primary_state["energy_sum"] == pytest.approx(5.5)
    assert primary_state["last_day"] == day_2.isoformat()
    assert primary_state["series_start"] == day_1.isoformat()
    assert primary_state["offset_kwh"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_accumulator_ignores_duplicate_day() -> None:
    """Ensure duplicate samples do not trigger persistence."""

    store = FakeStore()
    accumulator = EnergyAccumulator(store=cast(Any, store))
    await accumulator.async_load()

    day = date(2024, 4, 5)
    await accumulator.async_add_day("boost", day, 1.2)
    first_save_count = len(store.saved)

    await accumulator.async_add_day("boost", day, 1.2)

    assert len(store.saved) == first_save_count
    boost_state = accumulator.as_sensor_state()["boost"]
    assert boost_state["energy_sum"] == pytest.approx(1.2)
    assert boost_state["offset_kwh"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_accumulator_handles_revisions_with_offset(caplog: pytest.LogCaptureFixture) -> None:
    """Ensure revisions keep the exposed total monotonic via offsets."""

    store = FakeStore()
    accumulator = EnergyAccumulator(store=cast(Any, store))
    await accumulator.async_load()

    day_2 = date(2024, 5, 2)
    day_1 = date(2024, 5, 1)

    await accumulator.async_add_day("primary", day_2, 4.0)
    await accumulator.async_add_day("primary", day_1, 3.0)

    primary_state = accumulator.as_sensor_state()["primary"]
    assert primary_state["energy_sum"] == pytest.approx(7.0)
    assert primary_state["series_start"] == day_1.isoformat()
    assert primary_state["offset_kwh"] == pytest.approx(0.0)

    caplog.set_level(logging.INFO)
    caplog.clear()
    await accumulator.async_add_day("primary", day_2, 1.0)

    primary_state = accumulator.as_sensor_state()["primary"]
    assert primary_state["energy_sum"] == pytest.approx(7.0)
    assert primary_state["series_start"] == day_1.isoformat()
    assert primary_state["offset_kwh"] == pytest.approx(3.0)
    assert any("offset" in record.message for record in caplog.records)

    saved_payload = store.saved[-1]["primary"]
    assert saved_payload["monotonic_offset"] == pytest.approx(3.0)
    assert saved_payload["raw_cumulative_kwh"] == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_accumulator_freeze_skips_small_revision(caplog: pytest.LogCaptureFixture) -> None:
    """Ensure freeze tolerance ignores insignificant late revisions."""

    store = FakeStore()
    accumulator = EnergyAccumulator(store=cast(Any, store))
    await accumulator.async_load()

    day = date(2024, 8, 17)
    await accumulator.async_add_day("primary", day, 5.0)
    first_save = len(store.saved)

    caplog.set_level(logging.DEBUG)
    caplog.clear()
    await accumulator.async_add_day("primary", day, 5.008)

    assert len(store.saved) == first_save
    primary_state = accumulator.as_sensor_state()["primary"]
    assert primary_state["energy_sum"] == pytest.approx(5.0)
    assert primary_state["offset_kwh"] == pytest.approx(0.0)
    assert any("freeze active" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_accumulator_freeze_accepts_material_revision() -> None:
    """Ensure material late revisions apply and rely on offsets."""

    store = FakeStore()
    accumulator = EnergyAccumulator(store=cast(Any, store))
    await accumulator.async_load()

    day = date(2024, 8, 17)
    await accumulator.async_add_day("primary", day, 5.0)

    await accumulator.async_add_day("primary", day, 3.2)

    primary_state = accumulator.as_sensor_state()["primary"]
    assert primary_state["energy_sum"] == pytest.approx(5.0)
    assert primary_state["offset_kwh"] == pytest.approx(1.8)
    saved_payload = store.saved[-1]["primary"]
    assert saved_payload["monotonic_offset"] == pytest.approx(1.8)
    assert saved_payload["raw_cumulative_kwh"] == pytest.approx(3.2)


@pytest.mark.asyncio
async def test_accumulator_recent_revision_bypasses_freeze(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure near-term days always accept revisions above tolerance."""

    monkeypatch.setattr(energy_module, "_today", lambda: date(2024, 8, 3))

    store = FakeStore()
    accumulator = EnergyAccumulator(store=cast(Any, store))
    await accumulator.async_load()

    day = date(2024, 8, 2)
    await accumulator.async_add_day("primary", day, 2.0)
    first_save = len(store.saved)

    await accumulator.async_add_day("primary", day, 2.015)

    assert len(store.saved) == first_save + 1
    primary_state = accumulator.as_sensor_state()["primary"]
    assert primary_state["energy_sum"] == pytest.approx(2.015)
    assert primary_state["offset_kwh"] == pytest.approx(0.0)
    payload = store.saved[-1]["primary"]
    assert payload["monotonic_offset"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_accumulator_accepts_out_of_order_insert_beyond_horizon() -> None:
    """Ensure new late days are incorporated without churn."""

    store = FakeStore()
    accumulator = EnergyAccumulator(store=cast(Any, store))
    await accumulator.async_load()

    newer_day = date(2024, 8, 19)
    older_day = date(2024, 8, 10)

    await accumulator.async_add_day("primary", newer_day, 4.0)
    await accumulator.async_add_day("primary", older_day, 3.0)

    primary_state = accumulator.as_sensor_state()["primary"]
    assert primary_state["energy_sum"] == pytest.approx(7.0)
    assert primary_state["last_day"] == newer_day.isoformat()
    assert primary_state["offset_kwh"] == pytest.approx(0.0)
    payload = store.saved[-1]["primary"]
    assert payload["raw_cumulative_kwh"] == pytest.approx(7.0)
    assert payload["monotonic_offset"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_accumulator_handles_non_numeric_ledger_revision() -> None:
    """Ensure corrupt legacy energy entries coerce to zero before revision."""

    store = FakeStore()
    accumulator = EnergyAccumulator(store=cast(Any, store))
    await accumulator.async_load()

    day = date(2024, 8, 15)
    zone_state = accumulator._state["primary"]
    zone_state["ledger"][day.isoformat()] = {"energy": object(), "digest": "legacy"}

    await accumulator.async_add_day("primary", day, 1.0)

    payload = store.saved[-1]["primary"]
    assert payload["ledger"][day.isoformat()]["energy"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_accumulator_thaws_offset_when_totals_surpass_freeze() -> None:
    """Ensure offsets clear once the raw ledger exceeds the frozen total."""

    store = FakeStore()
    accumulator = EnergyAccumulator(store=cast(Any, store))
    await accumulator.async_load()

    day_1 = date(2024, 5, 1)
    day_2 = date(2024, 5, 2)

    await accumulator.async_add_day("primary", day_1, 5.0)
    await accumulator.async_add_day("primary", day_2, 5.0)
    await accumulator.async_add_day("primary", day_2, 3.0)

    assert store.saved[-1]["primary"]["monotonic_offset"] == pytest.approx(2.0)

    await accumulator.async_add_day("primary", day_2, 7.0)

    primary_state = accumulator.as_sensor_state()["primary"]
    assert primary_state["energy_sum"] == pytest.approx(12.0)
    final_payload = store.saved[-1]["primary"]
    assert final_payload["monotonic_offset"] == pytest.approx(0.0)
    assert accumulator.as_sensor_state()["primary"]["offset_kwh"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_accumulator_defaults_before_load() -> None:
    """Ensure as_sensor_state returns defaults before loading."""

    store = FakeStore()
    accumulator = EnergyAccumulator(store=cast(Any, store))
    state = accumulator.as_sensor_state()
    assert state["primary"]["energy_sum"] == 0.0
    assert state["primary"]["last_day"] is None
    assert state["primary"]["offset_kwh"] == 0.0


@pytest.mark.asyncio
async def test_accumulator_load_sanitises_state() -> None:
    """Ensure stored data is normalised when loaded."""

    store = FakeStore(
        {
            "primary": {
                "days": {
                    "2024-04-01": 2.0,
                    5: 3.0,
                    "2024-04-02": "4.0",
                    "bad": 1.0,
                    "2024-04-03": -1.0,
                    "2024-04-04": object(),
                },
                "cumulative_kwh": "not-a-number",
                "last_day": "2024-03-01",
                "series_start": "2024-03-01",
            },
            "boost": {
                "days": None,
                "cumulative_kwh": -5,
                "last_day": None,
                "series_start": None,
            },
        }
    )
    accumulator = EnergyAccumulator(store=cast(Any, store))
    await accumulator.async_load()

    state = accumulator.as_sensor_state()
    assert state["primary"]["energy_sum"] == pytest.approx(6.0)
    assert state["primary"]["last_day"] == "2024-04-02"
    assert state["primary"]["series_start"] == "2024-04-01"
    assert state["boost"]["energy_sum"] == 0.0
    assert state["primary"]["offset_kwh"] == pytest.approx(0.0)
    assert state["boost"]["offset_kwh"] == pytest.approx(0.0)
    primary_zone = accumulator._state["primary"]
    assert primary_zone["monotonic_offset"] == pytest.approx(0.0)
    assert primary_zone["raw_cumulative_kwh"] == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_accumulator_load_preserves_existing_offset() -> None:
    """Ensure stored offsets are maintained for legacy data."""

    store = FakeStore(
        {
            "primary": {
                "ledger": {
                    "2024-04-01": {"energy": 2.0},
                    "2024-04-02": {"energy": 3.0},
                },
                "cumulative_kwh": 10.0,
                "monotonic_offset": 5.0,
            }
        }
    )
    accumulator = EnergyAccumulator(store=cast(Any, store))
    await accumulator.async_load()

    state = accumulator.as_sensor_state()["primary"]
    assert state["energy_sum"] == pytest.approx(10.0)
    assert state["offset_kwh"] == pytest.approx(5.0)
    primary_zone = accumulator._state["primary"]
    assert primary_zone["monotonic_offset"] == pytest.approx(5.0)
    assert primary_zone["raw_cumulative_kwh"] == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_accumulator_load_clamps_negative_offset() -> None:
    """Ensure negative offsets are discarded when loading stored data."""

    store = FakeStore(
        {
            "primary": {
                "ledger": {"2024-04-01": {"energy": 4.0}},
                "cumulative_kwh": 4.0,
                "monotonic_offset": -2.0,
            }
        }
    )
    accumulator = EnergyAccumulator(store=cast(Any, store))
    await accumulator.async_load()

    primary_zone = accumulator._state["primary"]
    assert primary_zone["monotonic_offset"] == pytest.approx(0.0)
    assert primary_zone["raw_cumulative_kwh"] == pytest.approx(4.0)
    assert accumulator.as_sensor_state()["primary"]["offset_kwh"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_accumulator_rejects_unknown_zone() -> None:
    """Ensure unsupported zone names raise a ValueError."""

    store = FakeStore()
    accumulator = EnergyAccumulator(store=cast(Any, store))
    with pytest.raises(ValueError):
        await accumulator.async_add_day("invalid", date(2024, 5, 1), 1.0)


@pytest.mark.asyncio
async def test_accumulator_ignores_non_dict_zone() -> None:
    """Ensure stored payloads with unexpected structures are skipped."""

    store = FakeStore({"primary": "invalid"})
    accumulator = EnergyAccumulator(store=cast(Any, store))
    await accumulator.async_load()

    state = accumulator.as_sensor_state()
    assert state["primary"]["energy_sum"] == 0.0
    assert state["primary"]["offset_kwh"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_accumulator_persistence_roundtrip() -> None:
    """Ensure persisted state reloads and continues accumulating."""

    store = FakeStore()
    accumulator = EnergyAccumulator(store=cast(Any, store))
    await accumulator.async_load()

    day_1 = date(2024, 6, 1)
    await accumulator.async_add_day("primary", day_1, 2.0)

    saved_payload = store.data
    assert saved_payload is not None

    reload_store = FakeStore(saved_payload)
    reloaded = EnergyAccumulator(store=cast(Any, reload_store))
    await reloaded.async_load()

    day_2 = date(2024, 6, 2)
    await reloaded.async_add_day("primary", day_2, 1.5)

    state = reloaded.as_sensor_state()["primary"]
    assert state["energy_sum"] == pytest.approx(3.5)
    assert state["last_day"] == day_2.isoformat()
    assert state["offset_kwh"] == pytest.approx(0.0)
    assert reload_store.saved, "Reloaded accumulator should persist updates"


@pytest.mark.asyncio
async def test_accumulator_reset_zone() -> None:
    """Ensure resetting a zone clears the ledger and totals."""

    store = FakeStore()
    accumulator = EnergyAccumulator(store=cast(Any, store))
    await accumulator.async_load()

    day = date(2024, 7, 1)
    await accumulator.async_add_day("boost", day, 4.5)
    assert accumulator.as_sensor_state()["boost"]["energy_sum"] == pytest.approx(4.5)

    await accumulator.async_reset_zone("boost")

    state = accumulator.as_sensor_state()["boost"]
    assert state["energy_sum"] == 0.0
    assert state["last_day"] is None
    assert state["offset_kwh"] == pytest.approx(0.0)
    assert store.saved[-1]["boost"]["ledger"] == {}


@pytest.mark.asyncio
async def test_accumulator_reset_rejects_unknown_zone() -> None:
    """Ensure reset requests validate the zone identifier."""

    store = FakeStore()
    accumulator = EnergyAccumulator(store=cast(Any, store))

    with pytest.raises(ValueError):
        await accumulator.async_reset_zone("invalid")


@pytest.mark.asyncio
async def test_accumulator_add_day_sanitises_corrupt_state() -> None:
    """Ensure add-day processing tolerates corrupt stored totals."""

    store = FakeStore()
    accumulator = EnergyAccumulator(store=cast(Any, store))
    await accumulator.async_load()

    zone_state = accumulator._state["primary"]
    zone_state["cumulative_kwh"] = object()
    zone_state["monotonic_offset"] = object()

    day = date(2024, 8, 1)
    await accumulator.async_add_day("primary", day, 2.0)

    primary_state = accumulator.as_sensor_state()["primary"]
    assert primary_state["energy_sum"] == pytest.approx(2.0)
    assert primary_state["offset_kwh"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_accumulator_add_day_clamps_negative_offset() -> None:
    """Ensure negative offsets are clamped before monotonic calculations."""

    store = FakeStore()
    accumulator = EnergyAccumulator(store=cast(Any, store))
    await accumulator.async_load()

    zone_state = accumulator._state["primary"]
    zone_state["cumulative_kwh"] = 0.5
    zone_state["monotonic_offset"] = -1.5

    day = date(2024, 8, 2)
    await accumulator.async_add_day("primary", day, 1.0)

    stored = store.saved[-1]["primary"]
    assert stored["monotonic_offset"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_as_sensor_state_sanitises_invalid_offset() -> None:
    """Ensure invalid or negative offsets are clamped for sensor views."""

    store = FakeStore()
    accumulator = EnergyAccumulator(store=cast(Any, store))
    await accumulator.async_load()

    zone_state = accumulator._state["primary"]
    zone_state["monotonic_offset"] = object()
    assert accumulator.as_sensor_state()["primary"]["offset_kwh"] == pytest.approx(0.0)

    zone_state["monotonic_offset"] = -5.0
    assert accumulator.as_sensor_state()["primary"]["offset_kwh"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_zone_total_covers_all_paths() -> None:
    """Ensure zone_total handles unloaded state, bad data, and invalid zones."""

    store = FakeStore()
    accumulator = EnergyAccumulator(store=cast(Any, store))

    assert accumulator.zone_total("primary") == 0.0

    await accumulator.async_load()
    assert accumulator.zone_total("primary") == 0.0

    accumulator._state["primary"] = None  # type: ignore[assignment]
    assert accumulator.zone_total("primary") == 0.0

    accumulator._state["primary"] = {"cumulative_kwh": object()}  # type: ignore[assignment]
    assert accumulator.zone_total("primary") == 0.0

    accumulator._state["primary"] = {
        "ledger": {},
        "cumulative_kwh": 0.0,
        "last_processed_day": None,
        "series_start": None,
    }

    day = date(2024, 7, 1)
    await accumulator.async_add_day("primary", day, 1.5)
    assert accumulator.zone_total("primary") == pytest.approx(1.5)

    with pytest.raises(ValueError):
        accumulator.zone_total("invalid")
