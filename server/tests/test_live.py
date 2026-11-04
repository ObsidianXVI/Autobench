"""Smoke tests against real data sources. Run with: pytest -m live"""

import pytest

from autobench.config import get_settings

pytestmark = pytest.mark.live


def test_live_bars_and_summary():
    from autobench.services import market, technicals

    b = market.get_bars("AAPL", "1d", lookback="1y")
    assert len(b) > 200 and b.df["c"].iloc[-1] > 0
    intraday = market.get_bars("AAPL", "5m", lookback="5d")
    assert len(intraday) > 50
    s = technicals.technical_summary("AAPL")
    assert s["trend"]["score"] is not None


def test_live_index_symbols_via_yahoo():
    from autobench.services import market

    assert len(market.get_bars("^VIX", "1d", lookback="3mo")) > 40


def test_live_alpaca_if_configured():
    if not get_settings().alpaca_enabled:
        pytest.skip("Alpaca keys not configured")
    from autobench.services import market, news

    assert market.provider_status()["bar_provider"] == "alpaca"
    assert market.get_quote("MSFT")["price"] > 0
    assert market.search_symbols("micro")[0]["symbol"]
    assert news.get_news("MSFT", 3)["source"] == "alpaca"


def test_live_fundamentals_and_sec():
    from autobench.services import filings, fundamentals

    f = fundamentals.get_fundamentals("MSFT")
    assert f["profile"]["sector"]
    fl = filings.list_filings("MSFT", ["10-K"], 1)
    doc = filings.read_filing(fl["filings"][0]["url"], "risk_factors", max_chars=2000)
    assert doc["section"]["item"] == "1A"
    h = filings.financial_history("MSFT", ["revenue"], "annual", 5)
    assert len(h["metrics"]["revenue"]) >= 4
