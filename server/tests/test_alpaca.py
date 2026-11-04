"""Alpaca provider against mocked HTTP (pagination, feed fallback, session filtering, retries)."""

import httpx
import pandas as pd
import pytest
import respx

from autobench.config import Settings
from autobench.providers.alpaca import AlpacaProvider
from autobench.providers.base import ProviderError
from autobench.timeutil import get_interval, parse_time

DATA = "https://data.alpaca.markets"


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setattr("autobench.providers.alpaca.time.sleep", lambda s: None)
    s = Settings(alpaca_api_key_id="K", alpaca_api_secret_key="S", alpaca_data_feed="sip")
    return AlpacaProvider(s, httpx.Client())


def bar(ts, c, v=100):
    return {"t": ts, "o": c, "h": c + 1, "l": c - 1, "c": c, "v": v, "n": 1, "vw": c}


@respx.mock
def test_daily_bars_paginated_and_stamped(provider):
    route = respx.get(f"{DATA}/v2/stocks/AAPL/bars").mock(side_effect=[
        httpx.Response(200, json={"bars": [bar("2025-03-03T05:00:00Z", 10)], "next_page_token": "p2"}),
        httpx.Response(200, json={"bars": [bar("2025-03-04T05:00:00Z", 11)], "next_page_token": None}),
    ])
    df = provider.fetch_bars("aapl", get_interval("1d"), parse_time("2025-03-01"), parse_time("2025-03-05"))
    assert list(df["c"]) == [10, 11]
    assert list(df.index) == [parse_time("2025-03-03"), parse_time("2025-03-04")]  # 00:00 UTC session dates
    req = route.calls[0].request
    assert req.headers["APCA-API-KEY-ID"] == "K" and req.headers["APCA-API-SECRET-KEY"] == "S"
    assert "adjustment=all" in str(req.url) and "feed=sip" in str(req.url) and "timeframe=1Day" in str(req.url)
    assert "page_token=p2" in str(route.calls[1].request.url)


@respx.mock
def test_sip_recent_403_retries_with_delayed_end_then_iex(provider):
    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        if request.url.params["feed"] == "sip":
            return httpx.Response(403, json={"message": "subscription does not permit querying recent SIP data"})
        return httpx.Response(200, json={"bars": [bar("2025-03-03T05:00:00Z", 10)]})

    respx.get(f"{DATA}/v2/stocks/AAPL/bars").mock(side_effect=handler)
    df = provider.fetch_bars("AAPL", get_interval("1d"), parse_time("2025-03-01"), parse_time("2025-03-05"))
    assert len(df) == 1
    assert [c["feed"] for c in calls] == ["sip", "sip", "iex"]
    assert provider.feed == "iex"  # remembered for later requests


@respx.mock
def test_sip_delayed_retry_succeeds(provider):
    def handler(request):
        end = parse_time(request.url.params["end"])
        from autobench.timeutil import now_ts

        if end > now_ts() - 15 * 60:
            return httpx.Response(403, json={"message": "recent SIP"})
        return httpx.Response(200, json={"bars": [bar("2025-03-03T05:00:00Z", 10)]})

    respx.get(f"{DATA}/v2/stocks/AAPL/bars").mock(side_effect=handler)
    from autobench.timeutil import now_ts

    df = provider.fetch_bars("AAPL", get_interval("1d"), now_ts() - 10 * 86400, now_ts())
    assert len(df) == 1 and provider.feed == "sip"


@respx.mock
def test_intraday_filters_extended_hours_and_builds_hourly_from_30m(provider):
    # 2025-03-03 (EST, UTC-5): 09:00 ET premarket, 09:30, 10:00, 10:30, 16:00 after-hours
    stamps = ["2025-03-03T14:00:00Z", "2025-03-03T14:30:00Z", "2025-03-03T15:00:00Z", "2025-03-03T15:30:00Z",
              "2025-03-03T21:00:00Z"]
    route = respx.get(f"{DATA}/v2/stocks/AAPL/bars").mock(
        return_value=httpx.Response(200, json={"bars": [bar(s, 10 + i, v=10) for i, s in enumerate(stamps)]}))
    df30 = provider.fetch_bars("AAPL", get_interval("30m"), parse_time("2025-03-03"), parse_time("2025-03-04"))
    assert len(df30) == 3  # premarket and after-hours dropped
    h = provider.fetch_bars("AAPL", get_interval("1h"), parse_time("2025-03-03"), parse_time("2025-03-04"))
    assert "timeframe=30Min" in str(route.calls[-1].request.url)
    et = pd.to_datetime(h.index, unit="s", utc=True).tz_convert("America/New_York")
    assert [t.strftime("%H:%M") for t in et] == ["09:30", "10:30"]
    first = h.iloc[0]
    assert first.o == 11 and first.c == 12 and first.v == 20  # 09:30 + 10:00 bars merged


@respx.mock
def test_retry_on_429_then_error_message(provider):
    respx.get(f"{DATA}/v2/stocks/AAPL/snapshot").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json={"latestTrade": {"p": 101.5, "t": "2025-03-03T20:59:59Z"},
                                  "latestQuote": {"bp": 101.4, "ap": 101.6},
                                  "dailyBar": {"o": 100, "h": 102, "l": 99, "c": 101.5, "v": 5000, "vw": 101},
                                  "prevDailyBar": {"c": 100.0}}),
    ])
    q = provider.quote("AAPL")
    assert q["price"] == 101.5 and q["previous_close"] == 100.0 and q["bid"] == 101.4
    respx.get(f"{DATA}/v2/stocks/ZZZZ/snapshot").mock(return_value=httpx.Response(404, json={"message": "not found"}))
    with pytest.raises(ProviderError, match="not found"):
        provider.quote("ZZZZ")


@respx.mock
def test_news_and_assets(provider):
    respx.get(f"{DATA}/v1beta1/news").mock(return_value=httpx.Response(200, json={"news": [
        {"id": 1, "headline": "H", "summary": "S", "source": "benzinga", "created_at": "2025-03-03T12:00:00Z",
         "url": "u", "symbols": ["AAPL"], "author": "a"}], "next_page_token": None}))
    n = provider.news(["aapl"], limit=5)
    assert n[0]["headline"] == "H" and n[0]["symbols"] == ["AAPL"]
    respx.get("https://paper-api.alpaca.markets/v2/assets").mock(return_value=httpx.Response(200, json=[
        {"symbol": "AAPL", "name": "Apple Inc. Common Stock", "exchange": "NASDAQ", "class": "us_equity", "tradable": True}]))
    a = provider.list_assets()
    assert a[0]["symbol"] == "AAPL" and a[0]["tradable"] is True


@respx.mock
def test_search_uses_alpaca_asset_directory(monkeypatch, provider):
    from autobench.services import market

    respx.get("https://paper-api.alpaca.markets/v2/assets").mock(return_value=httpx.Response(200, json=[
        {"symbol": "AAPL", "name": "Apple Inc.", "exchange": "NASDAQ", "class": "us_equity", "tradable": True},
        {"symbol": "APLE", "name": "Apple Hospitality REIT", "exchange": "NYSE", "class": "us_equity", "tradable": True},
        {"symbol": "AAP", "name": "Advance Auto Parts", "exchange": "NYSE", "class": "us_equity", "tradable": True},
    ]))
    monkeypatch.setattr("autobench.services.market.alpaca_provider", lambda: provider)
    res = market.search_symbols("aap")
    assert res[0]["symbol"] == "AAP"  # exact ticker first
    assert {r["symbol"] for r in market.search_symbols("apple")} == {"AAPL", "APLE"}


def _snap(price, day_v, prev_c, day="2025-03-04"):
    return {"latestTrade": {"p": price, "t": f"{day}T15:00:00Z"}, "latestQuote": {"bp": price - 0.1, "ap": price + 0.1},
            "dailyBar": {"t": f"{day}T05:00:00Z", "o": 100, "h": 102, "l": 99, "c": price, "v": day_v, "vw": 101},
            "prevDailyBar": {"t": "2025-03-03T05:00:00Z", "c": prev_c}}


@respx.mock
def test_realtime_403_uses_iex_without_demoting_bars(provider):
    def snap(request):
        if request.url.params["feed"] == "sip":
            return httpx.Response(403, json={"message": "subscription does not permit querying recent SIP data"})
        return httpx.Response(200, json=_snap(101.0, 2_000, 99.5))

    respx.get(f"{DATA}/v2/stocks/AAPL/snapshot").mock(side_effect=snap)
    # consolidated (SIP) daily bars: today's partial bar and the official previous close
    multi = respx.get(f"{DATA}/v2/stocks/bars").mock(return_value=httpx.Response(200, json={"bars": {"AAPL": [
        {"t": "2025-03-03T05:00:00Z", "o": 98, "h": 100, "l": 97, "c": 99.0, "v": 900_000, "vw": 99},
        {"t": "2025-03-04T05:00:00Z", "o": 100, "h": 101.5, "l": 98.5, "c": 100.8, "v": 750_000, "vw": 100.5},
    ]}}))
    q = provider.quote("AAPL")
    assert q["price"] == 101.0 and q["feed"] == "iex"
    assert q["volume"] == 750_000 and q["volume_feed"].startswith("sip")  # consolidated, not IEX-only
    assert q["previous_close"] == 99.0  # official close, not IEX's last print
    assert q["day_high"] == 101.5 and q["day_low"] == 98.5
    assert multi.calls[0].request.url.params["feed"] == "sip"
    assert provider.feed == "sip"  # historical bars stay on the consolidated tape

    respx.get(f"{DATA}/v2/stocks/AAPL/bars").mock(return_value=httpx.Response(200, json={"bars": [bar("2025-03-03T05:00:00Z", 10)]}))
    provider.fetch_bars("AAPL", get_interval("1d"), parse_time("2025-03-01"), parse_time("2025-03-05"))
    assert respx.calls[-1].request.url.params["feed"] == "sip"


@respx.mock
def test_snapshots_batch_skips_unknown_and_chunks(provider):
    def snaps(request):
        syms = request.url.params["symbols"].split(",")
        return httpx.Response(200, json={s: _snap(10.0, 5, 9.0) for s in syms if s != "NOPE"})

    route = respx.get(f"{DATA}/v2/stocks/snapshots").mock(side_effect=snaps)
    syms = [f"S{i:03d}" for i in range(250)] + ["NOPE"]
    out = provider.snapshots(syms)
    assert len(out) == 250 and "NOPE" not in out
    assert len(route.calls) == 2  # 200 + 51 symbols
    assert out["S000"]["previous_close"] == 9.0


@respx.mock
def test_concurrent_recent_403s_all_retry_delayed_and_keep_sip(provider):
    """Several threads hit the first 403 at once; none may demote the provider to IEX."""
    import threading

    from autobench.timeutil import now_ts

    barrier = threading.Barrier(6)

    def handler(request):
        if request.url.params["feed"] != "sip":
            return httpx.Response(200, json={"bars": []})
        if parse_time(request.url.params["end"]) > now_ts() - 15 * 60:
            try:
                barrier.wait(timeout=2)  # make every thread see the 403 before any retries
            except threading.BrokenBarrierError:
                pass
            return httpx.Response(403, json={"message": "subscription does not permit querying recent SIP data"})
        return httpx.Response(200, json={"bars": [bar("2025-03-03T05:00:00Z", 10)]})

    respx.get(url__regex=rf"{DATA}/v2/stocks/\w+/bars").mock(side_effect=handler)
    results = []

    def run(sym):
        results.append(len(provider.fetch_bars(sym, get_interval("1d"), now_ts() - 10 * 86400, now_ts())))

    threads = [threading.Thread(target=run, args=(s,)) for s in ["A", "B", "C", "D", "E", "F"]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [1] * 6 and provider.feed == "sip"
