"""Bar cache behaviour, downsampling and time utilities."""

import numpy as np
import pandas as pd
import pytest

from autobench import providers
from autobench.providers.fake import FakeProvider
from autobench.services import market
from autobench.timeutil import format_ts, get_interval, lookback_seconds, now_ts, parse_time, resolve_range


class CountingProvider(FakeProvider):
    name = "fake"

    def __init__(self):
        super().__init__()
        self.calls = []
        self.scale = 1.0

    def fetch_bars(self, symbol, interval, start, end):
        self.calls.append((symbol, interval.name, start, end))
        df = super().fetch_bars(symbol, interval, start, end).copy()
        df[["o", "h", "l", "c"]] *= self.scale
        return df


@pytest.fixture
def counting(monkeypatch):
    p = CountingProvider()
    monkeypatch.setattr("autobench.services.market.bar_provider_for", lambda s: p)
    return p


def _age_cache(symbol, interval, seconds):
    from autobench.db import BarCoverage, session_scope

    with session_scope() as s:
        cov = s.get(BarCoverage, (symbol, interval))
        cov.end -= seconds
        cov.fetched_at -= seconds


def test_cache_hit_and_incremental_topup(counting):
    b1 = market.get_bars("AAPL", "1d", lookback="1y")
    assert len(counting.calls) == 1 and len(b1) > 200
    b2 = market.get_bars("AAPL", "1d", lookback="6mo")
    assert len(counting.calls) == 1, "sub-range should be served from cache"
    assert b2.df.equals(b1.df[b1.df.index >= b2.df.index[0]])
    # cache older than the TTL: tail top-up only (short range), not a full refetch
    _age_cache("AAPL", "1d", 3600)
    market.get_bars("AAPL", "1d", lookback="1y")
    assert len(counting.calls) == 2
    _, _, s, e = counting.calls[-1]
    assert e - s < 30 * 86400


def test_cache_extends_head(counting):
    market.get_bars("AAPL", "1d", lookback="6mo")
    market.get_bars("AAPL", "1d", lookback="2y")
    assert len(counting.calls) == 2
    _, _, s, e = counting.calls[-1]
    assert e < now_ts() - 100 * 86400, "only the missing head should be fetched"
    market.get_bars("AAPL", "1d", lookback="2y")
    assert len(counting.calls) == 2


def test_split_readjustment_triggers_full_refetch(counting):
    market.get_bars("AAPL", "1d", lookback="1y")
    counting.scale = 0.5  # provider history re-adjusted (e.g. 2:1 split)
    _age_cache("AAPL", "1d", 3600)
    b = market.get_bars("AAPL", "1d", lookback="1y")
    real_now = now_ts()
    raw = FakeProvider().fetch_bars("AAPL", get_interval("1d"), real_now - 400 * 86400, real_now)
    common = b.df.index.intersection(raw.index)
    assert np.allclose(b.df.loc[common, "c"], raw.loc[common, "c"] * 0.5), "whole cached series must be replaced"


def test_aggregate_bars_is_ohlc_correct():
    idx = pd.Index(range(0, 10 * 60, 60), name="t")
    df = pd.DataFrame({"o": np.arange(10.0), "h": np.arange(10.0) + 5, "l": np.arange(10.0) - 5,
                       "c": np.arange(10.0) + 1, "v": np.ones(10)}, index=idx)
    agg, k = market.aggregate_bars(df, 4)
    assert k == 3 and len(agg) == 4
    last = agg.iloc[-1]  # groups aligned to the end: last group = bars 7,8,9
    assert last.o == 7 and last.c == 10 and last.h == 14 and last.l == 2 and last.v == 3
    assert agg.index[-1] == 7 * 60
    same, k1 = market.aggregate_bars(df, 50)
    assert k1 == 1 and same is df


def test_symbol_normalisation_and_errors():
    assert market.normalize_symbol(" brk.b ") == "BRK.B"
    with pytest.raises(ValueError):
        market.normalize_symbol("  ")
    with pytest.raises(ValueError, match="Unknown interval"):
        market.get_bars("AAPL", "7m")


def test_yahoo_only_symbols_route_to_yahoo(monkeypatch):
    monkeypatch.setattr(providers, "get_settings", lambda: type("S", (), {"bar_provider": "alpaca", "alpaca_enabled": True})())
    monkeypatch.setattr(providers, "_get", lambda name: name)
    assert providers.bar_provider_for("^VIX") == "yahoo"
    assert providers.bar_provider_for("CL=F") == "yahoo"
    assert providers.bar_provider_for("AAPL") == "alpaca"


def test_quote_and_search_on_fake():
    q = market.get_quote("aapl")
    assert q["symbol"] == "AAPL" and "change_pct" in q
    assert market.search_symbols("AA")[0]["symbol"] == "AAPL"
    assert market.search_symbols("") == []


def test_intraday_bars_regular_session():
    b = market.get_bars("AAPL", "5m", lookback="5d")
    et = pd.to_datetime(b.df.index, unit="s", utc=True).tz_convert("America/New_York")
    assert len(b) > 0
    assert ((et.hour * 60 + et.minute) >= 570).all() and ((et.hour * 60 + et.minute) < 960).all()


def test_timeutil():
    assert parse_time("2024-01-02") == 1704153600
    assert parse_time("2024-01-02T09:30-05:00") == 1704205800
    assert parse_time(1704153600000) == 1704153600
    assert parse_time("1704153600") == 1704153600
    assert parse_time(None) is None
    with pytest.raises(ValueError):
        parse_time("yesterday-ish")
    assert lookback_seconds("1y") == int(365.25 * 86400)
    assert lookback_seconds("max") is None
    with pytest.raises(ValueError):
        lookback_seconds("3 fortnights")
    s, e = resolve_range(lookback="5d")
    assert e - s == 5 * 86400
    with pytest.raises(ValueError):
        resolve_range(start="2025-01-02", end="2025-01-01")
    assert format_ts(1704153600, False) == "2024-01-02"
    assert format_ts(1704205800, True).startswith("2024-01-02T09:30")
    assert get_interval("daily").name == "1d" and get_interval("60m").name == "1h"
