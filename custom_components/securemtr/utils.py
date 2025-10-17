"""Utility helpers for Secure Meters statistics processing."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
from math import log
from statistics import median
from typing import Iterable, Mapping, NamedTuple, Sequence
from typing import Literal

LN10 = log(10)


class EnergyCalibration(NamedTuple):
    """Describe energy calibration parameters."""

    use_scale: bool
    scale: float
    source: Literal["device_scaled", "duration_power"]


def to_local(value: datetime | int | float, tz: tzinfo) -> datetime:
    """Convert a timestamp into an aware datetime in the provided timezone."""

    if isinstance(value, datetime):
        dt_value = value
    else:
        dt_value = datetime.fromtimestamp(float(value), timezone.utc)

    if dt_value.tzinfo is None:
        dt_value = dt_value.replace(tzinfo=timezone.utc)

    return dt_value.astimezone(tz)


def report_day_for_sample(value: datetime | int | float, tz: tzinfo) -> date:
    """Return the local calendar day represented by a consumption sample."""

    local_dt = to_local(value, tz)
    return (local_dt - timedelta(days=1)).date()


def safe_anchor_datetime(day: date, anchor: time | None, tz: tzinfo) -> datetime:
    """Return an aware datetime within the requested day even across DST shifts."""

    anchor_time = anchor or time(0)
    midnight_local = datetime(day.year, day.month, day.day, tzinfo=tz)
    midnight_utc = midnight_local.astimezone(timezone.utc)
    delta = timedelta(
        hours=anchor_time.hour,
        minutes=anchor_time.minute,
        seconds=anchor_time.second,
        microseconds=anchor_time.microsecond,
    )
    candidate_utc = midnight_utc + delta
    candidate_local = candidate_utc.astimezone(tz)

    if candidate_local.date() < day:
        return midnight_local

    if candidate_local.date() > day:
        return datetime(day.year, day.month, day.day, 23, 59, 59, 999999, tzinfo=tz)

    return candidate_local


def _collect_ratios(
    rows: Iterable[Mapping[str, object]],
    energy_field: str,
    duration_field: str,
    fallback_power_kw: float,
) -> list[float]:
    """Collect runtime-to-energy ratios from the provided rows."""

    ratios: list[float] = []
    for row in rows:
        energy_raw = row.get(energy_field)
        duration_raw = row.get(duration_field)

        if not isinstance(energy_raw, (int, float)):
            continue
        if not isinstance(duration_raw, (int, float)):
            continue

        duration_minutes = float(duration_raw)
        if duration_minutes <= 0:
            continue

        fallback_energy = (duration_minutes / 60.0) * fallback_power_kw
        if fallback_energy <= 0:
            continue

        reported_energy = float(energy_raw)
        if reported_energy <= 0:
            continue

        ratios.append(fallback_energy / reported_energy)

    return ratios


def calibrate_energy_scale(
    rows: Sequence[Mapping[str, object]] | Iterable[Mapping[str, object]],
    energy_field: str,
    duration_field: str,
    fallback_power_kw: float,
    *,
    tolerance: float = 0.2,
) -> EnergyCalibration:
    """Return calibration data describing how to interpret device energy."""

    ratios = _collect_ratios(rows, energy_field, duration_field, fallback_power_kw)
    if not ratios:
        return EnergyCalibration(False, fallback_power_kw, "duration_power")

    median_ratio = median(ratios)
    if abs(median_ratio - LN10) / LN10 <= tolerance:
        return EnergyCalibration(True, LN10, "device_scaled")

    return EnergyCalibration(False, fallback_power_kw, "duration_power")


def energy_from_row(
    row: Mapping[str, object],
    energy_field: str,
    duration_field: str,
    calibration: EnergyCalibration,
    fallback_power_kw: float,
) -> float:
    """Compute energy for a row using calibration or fallback duration power."""

    energy_raw = row.get(energy_field)
    if calibration.use_scale and isinstance(energy_raw, (int, float)):
        energy = float(energy_raw) * calibration.scale
        if energy >= 0:
            return energy

    duration_raw = row.get(duration_field)
    duration_minutes = float(duration_raw) if isinstance(duration_raw, (int, float)) else 0.0
    if duration_minutes <= 0:
        return 0.0

    power_kw = calibration.scale if not calibration.use_scale else fallback_power_kw
    return (duration_minutes / 60.0) * power_kw


def cumulative_update(current: float | None, delta: float) -> float:
    """Return the updated cumulative value from the provided delta."""

    base = current or 0.0
    return base + delta

