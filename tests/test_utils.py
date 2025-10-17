"""Tests for Secure Meters utility helpers."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

import pytest

from custom_components.securemtr.utils import (
    EnergyCalibration,
    LN10,
    _collect_ratios,
    calibrate_energy_scale,
    cumulative_update,
    energy_from_row,
    report_day_for_sample,
    safe_anchor_datetime,
    to_local,
)


class _BackwardTZ(tzinfo):
    """Test timezone that shifts results one day earlier."""

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "Backward"

    def fromutc(self, dt: datetime) -> datetime:
        return (dt - timedelta(days=1)).replace(tzinfo=self)


class _ForwardTZ(tzinfo):
    """Test timezone that shifts results one day later."""

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "Forward"

    def fromutc(self, dt: datetime) -> datetime:
        return (dt + timedelta(days=1)).replace(tzinfo=self)


@pytest.mark.parametrize(
    "value",
    [1711929600, datetime(2024, 4, 1, 0, 0, tzinfo=timezone.utc), datetime(2024, 4, 1, 0, 0)],
)
def test_to_local_converts_epoch_and_datetime(value: int | datetime) -> None:
    """to_local should return an aware datetime in the provided timezone."""

    tz = ZoneInfo("Europe/Dublin")
    result = to_local(value, tz)
    assert result.tzinfo == tz
    assert result.year == 2024
    assert result.month == 4
    assert result.day == 1


def test_report_day_for_sample_returns_previous_local_day() -> None:
    """report_day_for_sample should map timestamps to the previous local day."""

    tz = ZoneInfo("Europe/Dublin")
    epoch = datetime(2024, 4, 2, 0, 15, tzinfo=timezone.utc).timestamp()
    assert report_day_for_sample(epoch, tz) == date(2024, 4, 1)


def test_safe_anchor_datetime_handles_dst_gap() -> None:
    """safe_anchor_datetime should clamp anchors within the same day across DST."""

    tz = ZoneInfo("Europe/Dublin")
    anchor = safe_anchor_datetime(date(2024, 3, 31), time(1, 30), tz)
    assert anchor.date() == date(2024, 3, 31)
    assert anchor.hour == 2
    assert anchor.minute == 30


def test_safe_anchor_datetime_handles_dst_overlap() -> None:
    """safe_anchor_datetime should prefer the first occurrence during overlaps."""

    tz = ZoneInfo("Europe/Dublin")
    anchor = safe_anchor_datetime(date(2023, 10, 29), time(1, 30), tz)
    assert anchor.date() == date(2023, 10, 29)
    assert anchor.fold == 0


def test_safe_anchor_datetime_clamps_to_start_of_day() -> None:
    """safe_anchor_datetime should clamp to the start of day when conversion underflows."""

    tz = _BackwardTZ()
    anchor = safe_anchor_datetime(date(2024, 4, 1), time(12, 0), tz)
    assert anchor == datetime(2024, 4, 1, 0, 0, tzinfo=tz)


def test_safe_anchor_datetime_clamps_to_end_of_day() -> None:
    """safe_anchor_datetime should clamp to the end of day when conversion overflows."""

    tz = _ForwardTZ()
    anchor = safe_anchor_datetime(date(2024, 4, 1), time(12, 0), tz)
    assert anchor == datetime(2024, 4, 1, 23, 59, 59, 999999, tzinfo=tz)


def test_calibrate_energy_scale_prefers_device_scaling() -> None:
    """calibrate_energy_scale should select the device scale when ratios match ln(10)."""

    rows = [
        {"energy": 6.0 / LN10, "runtime": 120},
        {"energy": 3.0 / LN10, "runtime": 60},
    ]
    calibration = calibrate_energy_scale(rows, "energy", "runtime", fallback_power_kw=3.0)
    assert calibration.use_scale is True
    assert calibration.scale == pytest.approx(LN10)
    assert calibration.source == "device_scaled"


def test_calibrate_energy_scale_falls_back_when_ratio_off() -> None:
    """calibrate_energy_scale should fall back when the ratio deviates from ln(10)."""

    rows = [{"energy": 6.0, "runtime": 120}]
    calibration = calibrate_energy_scale(rows, "energy", "runtime", fallback_power_kw=3.0)
    assert calibration.use_scale is False
    assert calibration.scale == pytest.approx(3.0)
    assert calibration.source == "duration_power"


def test_calibrate_energy_scale_handles_missing_rows() -> None:
    """calibrate_energy_scale should default to duration power when no ratios exist."""

    rows = [{"energy": None, "runtime": 0}]
    calibration = calibrate_energy_scale(rows, "energy", "runtime", fallback_power_kw=2.5)
    assert calibration.use_scale is False
    assert calibration.scale == pytest.approx(2.5)
    assert calibration.source == "duration_power"


def test_collect_ratios_filters_invalid_entries() -> None:
    """_collect_ratios should ignore rows that cannot yield valid ratios."""

    assert _collect_ratios([{"energy": 1.0, "runtime": "bad"}], "energy", "runtime", 3.0) == []
    assert _collect_ratios([{"energy": 1.0, "runtime": -5}], "energy", "runtime", 3.0) == []
    assert _collect_ratios([{"energy": 1.0, "runtime": 60}], "energy", "runtime", 0.0) == []
    assert _collect_ratios([{"energy": -1.0, "runtime": 60}], "energy", "runtime", 3.0) == []


def test_energy_from_row_uses_scaled_energy() -> None:
    """energy_from_row should return the scaled device energy when available."""

    calibration = EnergyCalibration(True, LN10, "device_scaled")
    row = {"energy": 2.0, "runtime": 30}
    energy = energy_from_row(row, "energy", "runtime", calibration, fallback_power_kw=3.0)
    assert energy == pytest.approx(2.0 * LN10)


def test_energy_from_row_uses_fallback_duration() -> None:
    """energy_from_row should fall back to duration and calibration scale when required."""

    calibration = EnergyCalibration(False, 2.5, "duration_power")
    row = {"energy": None, "runtime": 90}
    energy = energy_from_row(row, "energy", "runtime", calibration, fallback_power_kw=3.0)
    assert energy == pytest.approx(3.75)


def test_energy_from_row_respects_fallback_power_argument() -> None:
    """energy_from_row should use the provided fallback power when scaling is active."""

    calibration = EnergyCalibration(True, LN10, "device_scaled")
    row = {"energy": None, "runtime": 120}
    energy = energy_from_row(row, "energy", "runtime", calibration, fallback_power_kw=2.0)
    assert energy == pytest.approx(4.0)


def test_energy_from_row_returns_zero_for_non_positive_duration() -> None:
    """energy_from_row should return zero when duration is not positive."""

    calibration = EnergyCalibration(True, LN10, "device_scaled")
    row = {"energy": None, "runtime": 0}
    energy = energy_from_row(row, "energy", "runtime", calibration, fallback_power_kw=3.0)
    assert energy == pytest.approx(0.0)


def test_cumulative_update_accumulates_values() -> None:
    """cumulative_update should accumulate deltas from an optional base."""

    first = cumulative_update(None, 2.0)
    second = cumulative_update(first, 1.5)
    assert first == pytest.approx(2.0)
    assert second == pytest.approx(3.5)

