"""Schedule utilities for Secure Meters weekly programs."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from .beanbag import MINUTES_PER_DAY, WeeklyProgram

WEEK_MINUTES = MINUTES_PER_DAY * 7


@dataclass(slots=True)
class _Interval:
    """Represent a mutable interval during canonicalisation."""

    start: int
    end: int

    def normalised(self) -> tuple[int, int]:
        """Return the interval as a start/end tuple."""

        return (self.start, self.end)


def canonicalize_weekly(program: WeeklyProgram) -> list[tuple[int, int]]:
    """Return merged start/end minute pairs for the provided program."""

    segments: list[_Interval] = []

    for day_index, daily in enumerate(program):
        base = day_index * MINUTES_PER_DAY
        for on_minute, off_minute in zip(
            daily.on_minutes, daily.off_minutes, strict=False
        ):
            if on_minute is None or off_minute is None:
                continue
            if on_minute == off_minute:
                continue
            start = base + on_minute
            end = base + off_minute
            if end <= start:
                end += MINUTES_PER_DAY
            while end > WEEK_MINUTES:
                if start < WEEK_MINUTES:
                    clipped_end = WEEK_MINUTES
                    if clipped_end > start:
                        segments.append(_Interval(start, clipped_end))
                start = 0
                end -= WEEK_MINUTES
            if end > start:
                segments.append(_Interval(start, end))

    segments.sort(key=lambda interval: interval.start)

    merged: list[_Interval] = []
    for interval in segments:
        if not merged:
            merged.append(interval)
            continue
        current = merged[-1]
        if interval.start <= current.end:
            current.end = max(current.end, interval.end)
        else:
            merged.append(interval)

    return [interval.normalised() for interval in merged]


def _minutes_to_datetime(day: date, minutes: int, tz: tzinfo) -> datetime:
    """Convert minutes from local midnight into an aware datetime."""

    midnight_local = datetime(day.year, day.month, day.day, tzinfo=tz)
    return midnight_local + timedelta(minutes=minutes)


def day_intervals(
    program: WeeklyProgram,
    *,
    day: date,
    tz: tzinfo,
    canonical: Sequence[tuple[int, int]] | None = None,
) -> list[tuple[datetime, datetime]]:
    """Return schedule intervals for a day as timezone-aware datetimes."""

    canonicalised = list(canonical) if canonical is not None else canonicalize_weekly(program)
    day_index = day.weekday()
    day_start = day_index * MINUTES_PER_DAY
    day_end = day_start + MINUTES_PER_DAY

    intervals: list[tuple[datetime, datetime]] = []
    for start_minute, end_minute in canonicalised:
        if end_minute <= day_start or start_minute >= day_end:
            continue
        clipped_start = max(start_minute, day_start) - day_start
        clipped_end = min(end_minute, day_end) - day_start
        if clipped_end <= clipped_start:
            continue
        start_dt = _minutes_to_datetime(day, clipped_start, tz)
        end_dt = _minutes_to_datetime(day, clipped_end, tz)
        intervals.append((start_dt, end_dt))

    return intervals


def choose_anchor(
    intervals: Sequence[tuple[datetime, datetime]],
    strategy: str = "midpoint",
) -> datetime | None:
    """Choose an anchor datetime from intervals using the provided strategy."""

    best: tuple[datetime, datetime] | None = None
    best_duration = timedelta(0)

    for start, end in intervals:
        if end <= start:
            continue
        duration = end - start
        if duration > best_duration:
            best = (start, end)
            best_duration = duration

    if best is None:
        return None

    start, end = best
    if strategy == "start":
        return start
    if strategy == "end":
        return end
    if strategy == "midpoint":
        return start + (end - start) / 2

    return None
