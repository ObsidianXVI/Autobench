"""Interval definitions and time parsing/formatting.

Conventions used everywhere in the backend:
- Bar times are epoch seconds (UTC) of the bar *start*.
- Daily/weekly/monthly bars are stamped at 00:00 UTC of the session date (so a daily bar's
  time maps to an unambiguous calendar date, matching charting-library conventions).
- Human-readable output: dates as YYYY-MM-DD for daily+ bars, intraday as US/Eastern ISO.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Interval:
    name: str
    seconds: int
    intraday: bool
    pandas_rule: str  # for resampling / downsampling
    bars_per_year: float  # approximate, for annualisation


INTERVALS: dict[str, Interval] = {
    "1m": Interval("1m", 60, True, "1min", 252 * 390),
    "5m": Interval("5m", 300, True, "5min", 252 * 78),
    "15m": Interval("15m", 900, True, "15min", 252 * 26),
    "30m": Interval("30m", 1800, True, "30min", 252 * 13),
    "1h": Interval("1h", 3600, True, "1h", 252 * 7),
    "1d": Interval("1d", 86400, False, "1D", 252),
    "1wk": Interval("1wk", 7 * 86400, False, "W-MON", 52),
    "1mo": Interval("1mo", 30 * 86400, False, "MS", 12),
}

INTERVAL_ALIASES = {
    "1min": "1m", "5min": "5m", "15min": "15m", "30min": "30m", "60m": "1h", "1hour": "1h",
    "d": "1d", "day": "1d", "daily": "1d", "1day": "1d",
    "w": "1wk", "1w": "1wk", "week": "1wk", "weekly": "1wk",
    "mo": "1mo", "1month": "1mo", "month": "1mo", "monthly": "1mo",
}


def get_interval(name: str) -> Interval:
    key = name.strip().lower()
    key = INTERVAL_ALIASES.get(key, key)
    if key not in INTERVALS:
        raise ValueError(f"Unknown interval '{name}'. Valid: {', '.join(INTERVALS)}")
    return INTERVALS[key]


def now_ts() -> int:
    return int(datetime.now(UTC).timestamp())


def parse_time(value: str | int | float | datetime | date | None) -> int | None:
    """Parse epoch seconds/ms, ISO date/datetime strings (naive = UTC), or 'now' into epoch seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"Invalid time: {value!r}")
    if isinstance(value, (int, float)):
        v = float(value)
        return int(v / 1000) if v > 1e11 else int(v)
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return int(dt.timestamp())
    if isinstance(value, date):
        return int(datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp())
    s = str(value).strip()
    if s.lower() == "now":
        return now_ts()
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return parse_time(float(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"Invalid time '{value}': use ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM[:SS][±HH:MM]) or epoch seconds") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


_LOOKBACK_RE = re.compile(r"^(\d+)\s*(d|day|days|w|wk|week|weeks|mo|month|months|y|yr|year|years)$")


def lookback_seconds(lookback: str) -> int | None:
    """'5d', '3mo', '1y', 'ytd', 'max' -> seconds (None for 'max')."""
    s = lookback.strip().lower()
    if s == "max":
        return None
    if s == "ytd":
        now = datetime.now(UTC)
        return int((now - datetime(now.year, 1, 1, tzinfo=UTC)).total_seconds())
    m = _LOOKBACK_RE.match(s)
    if not m:
        raise ValueError(f"Invalid lookback '{lookback}'. Examples: 5d, 2w, 3mo, 1y, 5y, ytd, max")
    n, unit = int(m.group(1)), m.group(2)
    if unit.startswith("d"):
        days = n
    elif unit.startswith("w"):
        days = 7 * n
    elif unit.startswith("mo"):
        days = 30.44 * n
    else:
        days = 365.25 * n
    return int(days * 86400)


def resolve_range(
    start: str | int | None = None,
    end: str | int | None = None,
    lookback: str | None = None,
    default_lookback: str = "1y",
) -> tuple[int, int]:
    """Resolve a (start, end) epoch range from explicit bounds and/or a lookback window."""
    end_ts = parse_time(end) or now_ts()
    start_ts = parse_time(start)
    if start_ts is None:
        secs = lookback_seconds(lookback or default_lookback)
        start_ts = 0 if secs is None else end_ts - secs
    if start_ts >= end_ts:
        raise ValueError("start must be before end")
    return start_ts, end_ts


def day_ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


def format_ts(t: int, intraday: bool) -> str:
    if intraday:
        return datetime.fromtimestamp(t, ET).isoformat(timespec="minutes")
    return datetime.fromtimestamp(t, UTC).date().isoformat()


def to_rfc3339(t: int) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
