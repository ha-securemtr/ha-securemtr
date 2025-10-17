"""Tests for Secure Meters schedule utilities."""

from __future__ import annotations

from datetime import date

from zoneinfo import ZoneInfo

from custom_components.securemtr.beanbag import DailyProgram
from custom_components.securemtr.schedule import (
    canonicalize_weekly,
    choose_anchor,
    day_intervals,
)


EMPTY_DAY = DailyProgram((None, None, None), (None, None, None))


def _program_with_intervals():
    """Build a weekly program with overlaps and cross-day spans."""

    monday = DailyProgram((120, 150, 300), (180, 210, 300))
    tuesday = DailyProgram((1380, None, None), (60, None, None))
    sunday = DailyProgram((1380, None, None), (30, None, None))
    return (
        monday,
        tuesday,
        EMPTY_DAY,
        EMPTY_DAY,
        EMPTY_DAY,
        EMPTY_DAY,
        sunday,
    )


def test_canonicalize_weekly_merges_and_wraps() -> None:
    """canonicalize_weekly should normalise overlaps and wraparound intervals."""

    program = _program_with_intervals()
    intervals = canonicalize_weekly(program)
    assert intervals == [
        (0, 30),
        (120, 210),
        (2820, 2940),
        (10020, 10080),
    ]


def test_canonicalize_weekly_deduplicates_entries() -> None:
    """canonicalize_weekly should collapse duplicate and nested segments."""

    monday = DailyProgram((60, 60, 180), (120, 120, 180))
    wednesday = DailyProgram((1430, None, None), (1435, None, None))
    program = (
        monday,
        EMPTY_DAY,
        wednesday,
        EMPTY_DAY,
        EMPTY_DAY,
        EMPTY_DAY,
        EMPTY_DAY,
    )

    intervals = canonicalize_weekly(program)
    assert intervals == [(60, 120), (2880 + 1430, 2880 + 1435)]


def test_canonicalize_weekly_merges_wraparound_duplicates() -> None:
    """canonicalize_weekly should merge duplicated wraparound segments."""

    monday = DailyProgram((0, None, None), (120, None, None))
    sunday = DailyProgram((1380, None, None), (60, None, None))
    program = (
        monday,
        EMPTY_DAY,
        EMPTY_DAY,
        EMPTY_DAY,
        EMPTY_DAY,
        EMPTY_DAY,
        sunday,
    )

    intervals = canonicalize_weekly(program)
    assert intervals == [(0, 120), (10020, 10080)]


def test_day_intervals_returns_local_datetimes() -> None:
    """day_intervals should yield aware datetimes for the requested day."""

    tz = ZoneInfo("Europe/Dublin")
    program = _program_with_intervals()
    canonical = canonicalize_weekly(program)

    monday_intervals = day_intervals(
        program,
        day=date(2024, 4, 1),
        tz=tz,
        canonical=canonical,
    )
    assert len(monday_intervals) == 2
    assert monday_intervals[0][0].isoformat() == "2024-04-01T00:00:00+01:00"
    assert monday_intervals[0][1].isoformat() == "2024-04-01T00:30:00+01:00"
    assert monday_intervals[1][0].isoformat() == "2024-04-01T02:00:00+01:00"
    assert monday_intervals[1][1].isoformat() == "2024-04-01T03:30:00+01:00"

    tuesday_intervals = day_intervals(
        program,
        day=date(2024, 4, 2),
        tz=tz,
        canonical=canonical,
    )
    assert tuesday_intervals[0][0].isoformat() == "2024-04-02T23:00:00+01:00"
    assert tuesday_intervals[0][1].isoformat() == "2024-04-03T00:00:00+01:00"

    wednesday_intervals = day_intervals(
        program,
        day=date(2024, 4, 3),
        tz=tz,
        canonical=canonical,
    )
    assert wednesday_intervals[0][0].isoformat() == "2024-04-03T00:00:00+01:00"
    assert wednesday_intervals[0][1].isoformat() == "2024-04-03T01:00:00+01:00"

    sunday_intervals = day_intervals(
        program,
        day=date(2024, 3, 31),
        tz=tz,
        canonical=canonical,
    )
    assert sunday_intervals[0][0].isoformat() == "2024-03-31T23:00:00+01:00"
    assert sunday_intervals[0][1].isoformat() == "2024-04-01T00:00:00+01:00"

    zero_length = day_intervals(
        program,
        day=date(2024, 4, 1),
        tz=tz,
        canonical=canonical + [(0, 0)],
    )
    assert zero_length == monday_intervals

    reversed_interval = day_intervals(
        program,
        day=date(2024, 4, 1),
        tz=tz,
        canonical=canonical + [(200, 100)],
    )
    assert reversed_interval == monday_intervals


def test_choose_anchor_supports_strategies() -> None:
    """choose_anchor should return anchors using the requested strategy."""

    tz = ZoneInfo("Europe/Dublin")
    program = _program_with_intervals()
    monday_intervals = day_intervals(
        program,
        day=date(2024, 4, 1),
        tz=tz,
    )

    midpoint = choose_anchor(monday_intervals)
    assert midpoint.isoformat() == "2024-04-01T02:45:00+01:00"

    start = choose_anchor(monday_intervals, strategy="start")
    assert start == monday_intervals[1][0]

    end = choose_anchor(monday_intervals, strategy="end")
    assert end == monday_intervals[1][1]

    assert choose_anchor([(monday_intervals[0][0], monday_intervals[0][0])]) is None
    assert choose_anchor(monday_intervals, strategy="unknown") is None
