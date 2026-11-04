"""Provider interface. A bar frame is a DataFrame indexed by epoch-second bar start ('t'),
with float columns o, h, l, c, v (split/dividend adjusted), sorted ascending."""

from __future__ import annotations

from datetime import datetime, time
from typing import Protocol

import pandas as pd

from ..timeutil import ET, Interval, day_ts

BAR_COLUMNS = ["o", "h", "l", "c", "v"]


class ProviderError(RuntimeError):
    pass


class BarProvider(Protocol):
    name: str

    def fetch_bars(self, symbol: str, interval: Interval, start: int, end: int) -> pd.DataFrame: ...

    def quote(self, symbol: str) -> dict: ...

    def max_history_seconds(self, interval: Interval) -> int | None: ...


def empty_bars() -> pd.DataFrame:
    df = pd.DataFrame(columns=BAR_COLUMNS, dtype=float)
    df.index = pd.Index([], dtype="int64", name="t")
    return df


def finalize_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Sort, de-duplicate, drop NaN rows and enforce dtypes."""
    if df.empty:
        return empty_bars()
    df = df[BAR_COLUMNS].astype(float)
    df = df.dropna(subset=["o", "h", "l", "c"])
    df["v"] = df["v"].fillna(0.0)
    df.index = df.index.astype("int64")
    df.index.name = "t"
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def session_date_ts(ts: pd.Timestamp) -> int:
    """Map a tz-aware timestamp to 00:00 UTC of its US/Eastern calendar date."""
    return day_ts(ts.tz_convert(ET).date())


REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)


def is_regular_session(ts: pd.Timestamp) -> bool:
    et = ts.tz_convert(ET)
    return et.weekday() < 5 and REGULAR_OPEN <= et.time() < REGULAR_CLOSE


def et_datetime(t: int) -> datetime:
    return datetime.fromtimestamp(t, ET)
