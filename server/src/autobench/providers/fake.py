"""Deterministic synthetic market data for offline tests (AUTOBENCH_BAR_PROVIDER=fake)."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd

from ..timeutil import ET, Interval, day_ts, now_ts
from .base import empty_bars, finalize_bars

EPOCH = date(2015, 1, 2)


def _seed(symbol: str) -> int:
    return int(hashlib.sha256(symbol.upper().encode()).hexdigest()[:8], 16)


class FakeProvider:
    name = "fake"

    def __init__(self, today: date | None = None):
        self.today = today or datetime.now(UTC).date()

    def max_history_seconds(self, interval: Interval) -> int | None:
        return None

    def _daily(self, symbol: str) -> pd.DataFrame:
        days = pd.bdate_range(EPOCH, self.today)
        n = len(days)
        rng = np.random.default_rng(_seed(symbol))
        drift = 0.0004 + (rng.random() - 0.5) * 0.0006
        vol = 0.012 + rng.random() * 0.015
        rets = rng.normal(drift, vol, n)
        close = (20 + rng.random() * 180) * np.exp(np.cumsum(rets))
        open_ = close * np.exp(rng.normal(0, vol / 3, n))
        open_[1:] = close[:-1] * np.exp(rng.normal(0, vol / 3, n - 1))
        span = np.abs(rng.normal(0, vol, n)) * close
        high = np.maximum(open_, close) + span * rng.random(n)
        low = np.minimum(open_, close) - span * rng.random(n)
        volume = (rng.lognormal(15, 0.4, n) * (1 + 3 * np.abs(rets) / vol)).round()
        idx = pd.Index([day_ts(d.date()) for d in days], name="t")
        return pd.DataFrame({"o": open_, "h": high, "l": low, "c": close, "v": volume}, index=idx)

    def fetch_bars(self, symbol: str, interval: Interval, start: int, end: int) -> pd.DataFrame:
        daily = self._daily(symbol)
        if interval.name == "1d":
            df = daily
        elif interval.name in ("1wk", "1mo"):
            dt = pd.to_datetime(daily.index, unit="s")
            rule = "W-MON" if interval.name == "1wk" else "MS"
            grp = daily.groupby(dt.to_period("W-SUN" if rule == "W-MON" else "M").start_time)
            df = pd.DataFrame({"o": grp["o"].first(), "h": grp["h"].max(), "l": grp["l"].min(),
                               "c": grp["c"].last(), "v": grp["v"].sum()})
            df.index = pd.Index([day_ts(d.date()) for d in df.index], name="t")
        else:
            df = self._intraday(symbol, daily, interval, start, end)
        df = df[(df.index >= start - interval.seconds) & (df.index <= end)]
        return finalize_bars(df) if not df.empty else empty_bars()

    def _intraday(self, symbol: str, daily: pd.DataFrame, interval: Interval, start: int, end: int) -> pd.DataFrame:
        per_day = int(390 * 60 / interval.seconds) or 1
        rows = []
        first = datetime.fromtimestamp(max(start, now_ts() - 60 * 86400), UTC).date()
        for t, bar in daily[daily.index >= day_ts(first) - 86400].iterrows():
            d = datetime.fromtimestamp(int(t), UTC).date()
            open_dt = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
            rng = np.random.default_rng(_seed(symbol) ^ int(t))
            path = np.linspace(bar.o, bar.c, per_day + 1) + rng.normal(0, (bar.h - bar.l) / 8, per_day + 1)
            path[0], path[-1] = bar.o, bar.c
            path = np.clip(path, bar.l, bar.h)
            for i in range(per_day):
                o, c = path[i], path[i + 1]
                ts = int((open_dt + timedelta(seconds=i * interval.seconds)).timestamp())
                rows.append((ts, o, max(o, c), min(o, c), c, bar.v / per_day))
        if not rows:
            return empty_bars()
        df = pd.DataFrame(rows, columns=["t", "o", "h", "l", "c", "v"]).set_index("t")
        return df

    def quote(self, symbol: str) -> dict:
        d = self._daily(symbol)
        last, prev = d.iloc[-1], d.iloc[-2]
        return {"price": float(last.c), "previous_close": float(prev.c), "open": float(last.o),
                "day_high": float(last.h), "day_low": float(last.l), "volume": float(last.v), "source": self.name}

    def search(self, query: str, limit: int = 10) -> list[dict]:
        q = query.upper()
        universe = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "SPY", "QQQ", "AMD"]
        return [{"symbol": s, "name": f"{s} Fake Corp", "exchange": "FAKE", "type": "equity"}
                for s in universe if q in s][:limit]

    def news(self, symbol: str, limit: int = 20) -> list[dict]:
        return [{"headline": f"{symbol} synthetic headline {i}", "summary": "", "source": "fake",
                 "published_at": datetime.now(UTC).isoformat(), "url": None, "symbols": [symbol]} for i in range(min(limit, 3))]
