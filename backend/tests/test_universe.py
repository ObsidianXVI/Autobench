"""Universe layers, screener, heatmap, breadth, alerts, layouts and macro (fake provider, no network)."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from autobench.services import alerts, macro, screener, workspace
from autobench.services import indicators as ind
from autobench.services import universe as U


@pytest.fixture
def uni():
    U.universe.ensure(wait=True)
    df = U.universe.table()
    assert len(df) == 10
    return df


# ------------------------------------------------------------------------------- TTM arithmetic


def T(s):
    return pd.Timestamp(s)


def test_ttm_calendar_year_plus_reported_quarters():
    ann = [(T("2024-12-31"), 400.0), (T("2025-12-31"), 500.0)]
    qtr = [(T("2025-03-31"), 110.0), (T("2025-06-30"), 120.0), (T("2026-03-31"), 140.0), (T("2026-06-30"), 160.0),
           (T("2024-03-31"), 90.0), (T("2024-06-30"), 95.0)]
    r = U._ttm(ann, sorted(qtr))
    assert r["ttm"] == 500 + 140 + 160 - 110 - 120  # FY + 2 new quarters − same quarters a year earlier
    assert r["end"] == T("2026-06-30")
    assert r["ttm_prev"] == 400 + 110 + 120 - 90 - 95
    assert r["q"] == 160 and r["q_prev"] == 120
    assert r["fy"] == 500 and r["fy_prev"] == 400


def test_ttm_off_calendar_fiscal_year():
    # fiscal year ends in September (like Apple); quarters after it end Dec, Mar, Jun
    ann = [(T("2025-09-27"), 1000.0)]
    qtr = [(T("2024-12-28"), 300.0), (T("2025-03-29"), 200.0), (T("2025-06-28"), 210.0),
           (T("2025-12-27"), 330.0), (T("2026-03-28"), 220.0), (T("2026-06-27"), 230.0)]
    r = U._ttm(ann, qtr)
    assert r["ttm"] == 1000 + (330 + 220 + 230) - (300 + 200 + 210)


def test_ttm_missing_prior_quarter_falls_back_to_fiscal_year():
    r = U._ttm([(T("2025-12-31"), 500.0)], [(T("2026-03-31"), 140.0)])
    assert r["ttm"] == 500 and r["end"] == T("2025-12-31")


def test_ttm_quarters_only():
    q = [(T("2025-09-30"), 1.0), (T("2025-12-31"), 2.0), (T("2026-03-31"), 3.0), (T("2026-06-30"), 4.0)]
    assert U._ttm([], q)["ttm"] == 10.0


# ------------------------------------------------------------------------------- technicals


def test_vectorized_rma_matches_reference():
    rng = np.random.default_rng(1)
    x = rng.normal(0, 1, (120, 3))
    x[:7, 1] = np.nan  # a column that starts later
    out = U._rma(x, 14)
    for j in range(3):
        ref = ind.rma(pd.Series(x[:, j]), 14).to_numpy()
        np.testing.assert_allclose(out[:, j], ref, equal_nan=True, rtol=1e-12)


def test_technicals_on_constructed_paths():
    dates = pd.bdate_range("2025-01-01", periods=260)
    n = len(dates)
    up = np.linspace(100, 200, n)  # steady uptrend
    flat = np.full(n, 50.0)
    m = {"dates": dates, "symbols": ["SPY", "UP", "FLAT"]}
    closes = np.column_stack([up, up, flat])
    m["close"] = closes
    m["open"] = closes
    m["high"] = closes * 1.01
    m["low"] = closes * 0.99
    m["volume"] = np.column_stack([np.full(n, 1e6)] * 3)
    m["volume"][-1, 1] = 3e6  # unusual volume on the last day
    t = U.technicals(m)
    assert t.loc["UP", "perf_1w"] == pytest.approx((up[-1] / up[-6] - 1) * 100)
    assert t.loc["UP", "rsi14"] == pytest.approx(100.0)  # only up days
    assert t.loc["FLAT", "rsi14"] == pytest.approx(50.0)  # no movement
    assert t.loc["UP", "sma200_pct"] > 0 and t.loc["UP", "from_high_52w"] == pytest.approx(-0.990099, rel=1e-3)
    assert t.loc["UP", "rel_volume"] == pytest.approx(3.0)
    assert t.loc["UP", "beta"] == pytest.approx(1.0, rel=1e-6)  # identical to SPY
    assert t.loc["UP", "streak"] == 15  # streaks are counted over the last 15 closes (capped)
    assert t.loc["FLAT", "atr_pct"] == pytest.approx(2.0, rel=1e-3)  # (1.01-0.99) of price


def test_taxonomy_votes_industry_into_gics_sector():
    df = pd.DataFrame({
        "sector": ["Technology", "Technology", "Industrials", "Miscellaneous"],
        "industry": ["Semis", "Semis", "Lab Instruments", "Unknown"],
        "gics_sector": ["Information Technology", None, None, None],
        "gics_industry": ["Semiconductors", None, None, None],
    }, index=["NVDA", "SMALLSEMI", "LABCO", "MISC"])
    sector, industry = U._unify_taxonomy(df)
    assert sector["SMALLSEMI"] == "Information Technology"  # voted by an S&P member in the same industry
    assert sector["LABCO"] == "Industrials"  # no vote: Nasdaq sector mapping
    assert pd.isna(sector["MISC"])
    assert industry["NVDA"] == "Semiconductors" and industry["SMALLSEMI"] == "Semis"


# ------------------------------------------------------------------------------- screener


def test_screen_filters_sort_and_columns(uni):
    res = screener.screen(filters=[{"field": "sector", "op": "==", "value": "Information Technology"}],
                          sort="-market_cap", columns=["symbol", "market_cap", "sector"])
    syms = [r["symbol"] for r in res["rows"]]
    assert set(syms) == {"AAPL", "MSFT", "NVDA", "AMD"}
    caps = [r["market_cap"] for r in res["rows"]]
    assert caps == sorted(caps, reverse=True)
    assert set(res["rows"][0]) == {"symbol", "market_cap", "sector"}


def test_screen_expression_matches_pandas(uni):
    expr = 'pe < 40 and (sales_growth > 0 or sector == "Energy") and not rsi14 > 80'
    res = screener.screen(expression=expr, limit=500, columns=["symbol"])
    want = uni[(uni.pe < 40) & ((uni.sales_growth > 0) | (uni.sector == "Energy")) & ~(uni.rsi14 > 80)].index
    assert {r["symbol"] for r in res["rows"]} == set(want)
    res = screener.screen(expression='sector in ["Energy", "Financials"]', columns=["symbol"])
    assert {r["symbol"] for r in res["rows"]} == {"XOM", "JPM"}


@pytest.mark.parametrize("bad", ["__import__('os')", "pe.__class__", "lambda: 1", "nosuchfield > 1", "open('x')"])
def test_screen_expression_rejects_unsafe_or_unknown(uni, bad):
    with pytest.raises(ValueError):
        screener.screen(expression=bad)


def test_screen_presets_signals_and_paging(uni):
    big = next(p for p in screener.FIELD_MAP["market_cap"].presets if p["label"].startswith("Mega"))
    res = screener.screen(filters=[{"field": "market_cap", **{k: big[k] for k in ("op", "value")}}], limit=3)
    assert res["total"] == int((uni.market_cap >= 200e9).sum()) and len(res["rows"]) <= 3
    page2 = screener.screen(limit=3, offset=3, columns=["symbol"])
    page1 = screener.screen(limit=3, columns=["symbol"])
    assert not {r["symbol"] for r in page1["rows"]} & {r["symbol"] for r in page2["rows"]}
    g = screener.screen(signal="top_gainers", columns=["symbol", "change_pct"])
    assert all(r["change_pct"] > 0 for r in g["rows"])
    assert [r["change_pct"] for r in g["rows"]] == sorted([r["change_pct"] for r in g["rows"]], reverse=True)
    with pytest.raises(ValueError):
        screener.screen(signal="nope")


def test_screen_universe_watchlist_and_search(uni):
    workspace.update_watchlist("tech", add=["NVDA", "AMD"])
    res = screener.screen(universe_name="watchlist:tech", columns=["symbol"])
    assert {r["symbol"] for r in res["rows"]} == {"NVDA", "AMD"}
    res = screener.screen(search="ms", columns=["symbol"])
    assert {r["symbol"] for r in res["rows"]} == {"MSFT"}


def test_saved_screens_roundtrip(uni):
    screener.save_screen("cheap growth", {"expression": "pe < 30 and sales_growth > 5", "view": "valuation", "junk": 1})
    saved = screener.list_screens()
    assert saved == [{"name": "cheap growth", "expression": "pe < 30 and sales_growth > 5", "view": "valuation"}]
    with pytest.raises(ValueError):
        screener.save_screen("bad", {"expression": "pe <"})
    screener.delete_screen("cheap growth")
    assert screener.list_screens() == []


def test_snapshot_has_every_field(uni):
    snap = screener.snapshot("NVDA")
    assert {f.key for f in screener.FIELDS} <= set(snap)
    assert screener.snapshot("ZZZZ") == {"symbol": "ZZZZ", "in_universe": False}


def test_heatmap_tree_and_weighted_sector_value(uni):
    hm = screener.heatmap("all", "1d")
    tech = next(s for s in hm["sectors"] if s["name"] == "Information Technology")
    stocks = [x for i in tech["industries"] for x in i["stocks"]]
    w = sum(x["market_cap"] for x in stocks)
    assert tech["value"] == pytest.approx(sum(x["value"] * x["market_cap"] for x in stocks) / w, abs=1e-3)  # values are rounded to 4 dp
    assert [s["market_cap"] for s in hm["sectors"]] == sorted([s["market_cap"] for s in hm["sectors"]], reverse=True)
    with pytest.raises(ValueError):
        screener.heatmap("all", "2d")


def test_breadth_counts(uni):
    b = screener.breadth("all")
    assert b["advancing"] + b["declining"] + b["unchanged"] == b["count"]
    assert b["advancing"] == int((uni.change_pct > 0).sum())
    assert 0 <= b["above_sma200_pct"] <= 100
    assert set(b["signals"]) >= {"top_gainers", "new_high", "unusual_volume"}
    assert {s["name"] for s in b["sectors"]} == set(uni.sector)


# ------------------------------------------------------------------------------- alerts


def _quote(monkeypatch, prices: dict):
    monkeypatch.setattr(alerts, "get_quotes", lambda syms: {s: {"price": prices[s], "change_pct": 1.5}
                                                             for s in syms if s in prices})


def test_price_alerts_fire_on_transition(monkeypatch):
    a = alerts.create("AAPL", "above", 110)
    c = alerts.create("AAPL", "crosses", 100, repeat=True)
    _quote(monkeypatch, {"AAPL": 95.0})
    assert alerts.check() == []  # below both levels; crossing needs a previous price
    _quote(monkeypatch, {"AAPL": 105.0})
    fired = alerts.check()
    assert [f["id"] for f in fired] == [c["id"]]  # crossed 100 upwards
    _quote(monkeypatch, {"AAPL": 112.0})
    fired = alerts.check()
    assert [f["id"] for f in fired] == [a["id"]] and fired[0]["trigger_price"] == 112.0
    assert alerts.list_(status="triggered")[0]["id"] == a["id"]
    _quote(monkeypatch, {"AAPL": 98.0})
    assert [f["id"] for f in alerts.check()] == [c["id"]]  # repeat alert crosses back down
    assert alerts.list_(status="active")[0]["trigger_count"] == 2


def test_change_alert_once_per_day_and_validation(monkeypatch):
    alerts.create("MSFT", "change_above", 1.0, repeat=True)
    _quote(monkeypatch, {"MSFT": 10.0})
    assert len(alerts.check()) == 1
    assert alerts.check() == []  # already fired today
    with pytest.raises(ValueError):
        alerts.create("MSFT", "above")  # needs a value
    with pytest.raises(ValueError):
        alerts.create("MSFT", "sideways", 1)


def test_expression_alert_and_linked_line(monkeypatch):
    from autobench.schemas import AnnotationIn, AnnotationPatch, Point
    from autobench.services import annotations

    e = alerts.create("SPY", "expression", expression="close > 0")
    fired = alerts.check()
    assert [f["id"] for f in fired] == [e["id"]]
    with pytest.raises(Exception):
        alerts.create("SPY", "expression", expression="nonsense(")
    line = annotations.create("SPY", [AnnotationIn(type="horizontal_line", points=[Point(price=100)])], author="user")[0]
    la = alerts.create("SPY", "crosses", 100, annotation_id=line["id"])
    annotations.update(line["id"], AnnotationPatch(points=[Point(price=123.5)]))
    assert next(x for x in alerts.list_() if x["id"] == la["id"])["value"] == 123.5  # follows the dragged line
    annotations.delete_([line["id"]])
    assert la["id"] not in {x["id"] for x in alerts.list_()}


# ------------------------------------------------------------------------------- layouts, macro, calendars


def test_layouts_and_ui_state():
    workspace.save_layout("Two charts", {"grid": "2h", "charts": [{"symbol": "SPY"}, {"symbol": "QQQ"}]}, "SPY | QQQ")
    assert workspace.list_layouts()[0]["name"] == "Two charts"
    assert workspace.get_layout("Two charts")["data"]["grid"] == "2h"
    with pytest.raises(ValueError):
        workspace.save_layout("huge", {"x": "y" * 300_000})
    workspace.delete_layout("Two charts")
    with pytest.raises(KeyError):
        workspace.get_layout("Two charts")
    workspace.set_ui_state({"page": "screener"})
    assert workspace.get_ui_state() == {"page": "screener"}


def test_macro_dashboard_and_series_offline():
    d = macro.macro_dashboard()
    names = {g["name"] for g in d["groups"]}
    assert {"Rates", "Inflation", "Labor"} <= names
    cpi = next(i for g in d["groups"] for i in g["indicators"] if i["id"] == "CPIAUCSL")
    assert cpi["unit"] == "% y/y" and cpi["spark"]
    assert [c["tenor"] for c in d["curve"]][:2] == ["1M", "3M"]
    s = macro.macro_series("UNRATE")
    assert s["points"] and s["name"] == "Unemployment rate"
    with pytest.raises(KeyError):
        macro.macro_series("NOPE123")


def test_macro_transform_yoy():
    idx = pd.date_range("2020-01-01", periods=36, freq="MS")
    s = pd.Series(np.arange(100, 136, dtype=float), index=idx)
    yoy = macro._transform(s, "yoy")
    assert yoy.index[0] == pd.Timestamp("2021-01-01")
    assert yoy.iloc[0] == pytest.approx((112 / 100 - 1) * 100)


def test_calendars_offline(uni):
    e = macro.earnings_calendar(days=5, universe_name="sp500")
    assert e["count"] == 6 and e["events"][0]["sector"]
    assert macro.earnings_calendar(days=5, symbols=["NVDA"])["count"] == 1
    assert macro.economic_calendar(days=3)["events"][0]["country"] == "United States"


def test_period_list_includes_enough_history():
    annual, quarters, instants = U._periods(date(2026, 10, 19))
    assert annual == ["CY2025", "CY2024", "CY2023"]
    assert quarters[0] == "CY2023Q2" and quarters[-1] == "CY2026Q3"
    assert instants[0] == "CY2026Q3I"
