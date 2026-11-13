"""QBench: statistics against known cases, causality of every library strategy, the lab finding a planted
edge (and not finding one in a random walk), factors, pairs, allocation/hedging, confluence, REST and MCP."""

import json
import math

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from autobench.services import indicators as ind
from autobench.services.qbench import confluence, factors, hedge, lab, pairs, profile, stats, xsection
from autobench.services.qbench import strategies as lib
from conftest import make_bars

# ----------------------------------------------------------------------------- statistics


def _ar1(n=2000, phi=0.9, seed=1):
    rng = np.random.default_rng(seed)
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = phi * x[i - 1] + rng.normal()
    return x


def test_mackinnon_p_values_match_critical_values():
    assert stats.mackinnon_p(-2.86, 1) == pytest.approx(0.05, abs=0.005)
    assert stats.mackinnon_p(-3.43, 1) == pytest.approx(0.01, abs=0.004)
    assert stats.mackinnon_p(-3.34, 2) == pytest.approx(0.05, abs=0.005)
    assert stats.mackinnon_p(-1.0, 1) == pytest.approx(0.75, abs=0.03)
    assert stats.mackinnon_p(5, 1) == 1.0


def test_adf_half_life_hurst_and_variance_ratio():
    ar = _ar1()
    rw = np.cumsum(np.random.default_rng(2).normal(size=2000))
    assert stats.adf(ar)["p_value"] < 0.01
    assert stats.adf(rw)["p_value"] > 0.05
    assert stats.half_life(ar) == pytest.approx(math.log(2) / 0.1, rel=0.2)
    assert stats.half_life(rw) > 100 or math.isinf(stats.half_life(rw))
    assert stats.hurst(rw) == pytest.approx(0.5, abs=0.08)
    assert stats.hurst(ar) < 0.4
    vr = stats.variance_ratio(np.cumsum(ar * 0.01), 10)  # AR(1) returns: strong persistence
    assert vr["vr"] > 1 and vr["z"] > 2
    rev = stats.variance_ratio(np.cumsum(np.diff(np.r_[0, ar]) * 0.01), 10)  # differenced AR(1): over-differenced, reverting
    assert rev["vr"] < 1 and rev["z"] < -2
    # calibration: a random walk rejects at the 5% level about 5% of the time
    zs = [stats.variance_ratio(np.cumsum(np.random.default_rng(s).normal(size=504)), 10)["z"] for s in range(200)]
    assert 0.01 <= np.mean(np.abs(zs) > 1.96) <= 0.10


def test_sharpe_inference():
    rng = np.random.default_rng(3)
    good = rng.normal(0.001, 0.01, 2500)  # per-period SR 0.1 (~1.6 annualised)
    assert stats.probabilistic_sharpe(good) > 0.99
    assert stats.probabilistic_sharpe(rng.normal(0, 0.01, 2500)) < 0.95
    # deflating for 100 trials raises the bar
    var = (0.02) ** 2
    assert stats.deflated_sharpe(good, 100, var) < stats.probabilistic_sharpe(good)
    assert stats.expected_max_sharpe(1, var) == 0
    assert stats.expected_max_sharpe(100, var) > stats.expected_max_sharpe(10, var) > 0
    assert 0 < stats.min_track_record(good) < 2500
    ps = stats.perf_stats(good)
    assert ps["sharpe"] == pytest.approx(good.mean() / good.std(ddof=1) * math.sqrt(252), abs=0.01)
    assert ps["max_drawdown_pct"] < 0


def test_ledoit_wolf_is_a_valid_shrunk_covariance():
    rng = np.random.default_rng(4)
    X = rng.normal(size=(60, 20))  # few observations for many assets: needs shrinkage
    cov, k = stats.ledoit_wolf(X)
    assert 0 < k <= 1
    assert np.allclose(cov, cov.T) and np.linalg.eigvalsh(cov).min() > 0


# ----------------------------------------------------------------------------- indicators


def test_quant_indicators():
    df = make_bars(np.linspace(100, 200, 300))
    assert ind.compute(df, "efficiency_ratio", {"length": 20})["er"].iloc[-1] == pytest.approx(1.0)
    bar = pd.DataFrame({"o": [10.0], "h": [12.0], "l": [10.0], "c": [10.5], "v": [1.0]}, index=pd.Index([0], name="t"))
    assert ind.compute(bar, "ibs")["ibs"].iloc[0] == pytest.approx(0.25)
    rw = make_bars(100 * np.exp(np.cumsum(np.random.default_rng(5).normal(0, 0.01, 1500))))
    h = ind.compute(rw, "hurst", {"length": 256})["hurst"].dropna()
    assert len(h) > 1000 and 0.3 < h.median() < 0.7


# ----------------------------------------------------------------------------- strategy library


def _series(n=2600, seed=7, drift=0.0004, vol=0.015):
    rng = np.random.default_rng(seed)
    return make_bars(100 * np.exp(np.cumsum(rng.normal(drift, vol, n))), start="2014-01-02")


def test_library_cards_are_complete():
    for s in lib.LIBRARY:
        assert s.summary and s.rules and s.references and s.evidence in ("strong", "moderate", "weak", "contested", "myth")
        if s.kind == "single":
            assert s.fn is not None and s.combos()
            assert set(s.grid) <= set(s.params)
    assert {s["id"] for s in lib.catalog(kind="single")} == {s.id for s in lib.SINGLE}


@pytest.mark.parametrize("sid", [s.id for s in lib.SINGLE])
def test_single_strategies_have_no_look_ahead(sid):
    """Positions computed on a truncated history equal the full-history positions on the shared bars."""
    df = _series(900)
    bench = _series(900, seed=8)["c"]
    st = lib.get(sid)
    full = lib.positions(st, df, None, bench)
    k = 700
    part = lib.positions(st, df.iloc[:k], None, bench.iloc[:k])
    assert len(full) == len(df) and np.isfinite(full).all() and (full >= 0).all()
    np.testing.assert_allclose(part[: k - 1], full[: k - 1], atol=1e-9, err_msg=sid)


def test_turn_of_month_window():
    df = make_bars(np.full(60, 100.0), start="2025-01-01")
    pos = lib.positions(lib.get("turn_of_month"), df, {"last_n": 1, "first_m": 3})
    d = pd.to_datetime(df.index, unit="s")
    held_next = {d[i + 1].strftime("%m-%d") for i in range(len(d) - 1) if pos[i] == 1}
    assert {"01-31", "02-03", "02-04", "02-05", "02-28", "03-03"} <= held_next
    assert "02-10" not in held_next


def test_unknown_parameter_is_rejected():
    with pytest.raises(ValueError, match="no parameter"):
        lib.positions(lib.get("rsi2"), _series(400), {"bogus": 1})
    with pytest.raises(ValueError, match="run it with"):
        lib.positions(lib.get("xs_momentum"), _series(400))


# ----------------------------------------------------------------------------- lab


def _mean_reverting(n=3000, seed=11):
    """Uptrend plus a strongly mean-reverting component: short-term dips are buyable."""
    rng = np.random.default_rng(seed)
    dev = np.zeros(n)
    for i in range(1, n):
        dev[i] = 0.75 * dev[i - 1] + rng.normal(0, 0.02)
    trend = np.cumsum(rng.normal(0.0003, 0.004, n))
    return make_bars(100 * np.exp(trend + dev), start="2013-01-02", spread=0.004)


def test_lab_finds_a_planted_reversion_edge(monkeypatch):
    df = _mean_reverting()
    monkeypatch.setattr(lab, "load", lambda symbol, lookback="15y", benchmark="SPY": (df, None))
    res = lab.run_lab("TEST", save=False)
    by = {s["id"]: s for s in res["strategies"]}
    assert res["trials"] == sum(len(s.combos()) for s in lib.SINGLE)
    for sid in ("bb_revert", "rsi2"):
        assert by[sid]["verdict"] in ("robust", "promising"), (sid, by[sid]["oos"])
        assert by[sid]["oos"]["alpha_annual_pct"] > 5
    assert res["strategies"][0]["family"] == "mean_reversion"
    assert res["best"] in ("bb_revert", "rsi2", "ibs", "er_switch")
    wf = by["rsi2"]["walk_forward"]
    assert len(wf) >= 5 and all(w["test_start"] > "2015" for w in wf)  # first test window after warm-up + 3y train


def test_lab_finds_no_timing_alpha_in_a_random_walk(monkeypatch):
    df = _series(2800, seed=21)
    monkeypatch.setattr(lab, "load", lambda symbol, lookback="15y", benchmark="SPY": (df, None))
    res = lab.run_lab("RW", save=False)
    robust = [s["id"] for s in res["strategies"] if s["verdict"] == "robust"]
    assert not robust, robust


def test_lab_saves_and_run_strategy_reports(monkeypatch):
    res = lab.run_lab("AAPL", ["rsi2", "tsmom"])
    assert lab.last_lab("aapl")["symbol"] == "AAPL" and len(res["strategies"]) == 2
    one = lab.run_strategy("AAPL", "donchian", {"entry": 20})
    assert one["params"]["entry"] == 20 and one["performance"]["bars"] > 1000
    assert one["yearly"] and len(one["equity_curve"]["dates"]) == len(one["equity_curve"]["strategy"])
    assert one["position_now"] in (0.0, 1.0)


def test_simulate_charges_costs_on_turnover():
    df = make_bars([100, 110, 121, 121])
    r = lab.simulate(df, np.array([1.0, 1.0, 0.0, 0.0]), cost_bps=10)
    assert r == pytest.approx([0.1 - 0.001, 0.1, 0.0 - 0.001])


# ----------------------------------------------------------------------------- profile, factors


def test_quant_profile_on_fake_data():
    p = profile.quant_profile("AAPL", factors=True)
    assert p["regime"]["label"] in ("trending", "mean-reverting", "random walk")
    assert {"hurst", "variance_ratios", "adf_log_price"} <= set(p["regime_tests"])
    assert p["factor_exposures"]["betas"]["market"]["beta"] is not None
    assert set(p["seasonality"]["day_of_week_mean_pct"]) <= {"Mon", "Tue", "Wed", "Thu", "Fri"}


def test_factor_ranks_point_the_right_way():
    n = 50
    df = pd.DataFrame({
        "price": 50.0, "market_cap": 1e10, "dollar_volume": 5e7, "sector": ["A", "B"] * 25,
        "pe": np.linspace(5, 50, n), "pfcf": np.linspace(5, 50, n), "pb": np.linspace(1, 10, n), "ps": np.linspace(1, 10, n),
        "roe": np.linspace(30, -5, n), "roa": np.linspace(15, -2, n), "gross_margin": np.linspace(70, 10, n),
        "oper_margin": np.linspace(30, -5, n), "debt_eq": np.linspace(0.1, 3, n),
        "sales_growth": np.linspace(40, -10, n), "eps_growth": np.linspace(50, -20, n), "sales_qoq": np.linspace(30, -5, n),
        "mom_12_1": np.linspace(60, -30, n), "from_high_52w": np.linspace(0, -50, n),
        "volatility_1y": np.linspace(15, 80, n), "beta": np.linspace(0.6, 2.0, n),
    }, index=[f"S{i}" for i in range(n)])
    df.loc["TINY", ["price", "market_cap"]] = [1.0, 5e7]
    f = factors.factor_frame(df)
    for c in ("q_value", "q_quality", "q_growth", "q_momentum", "q_lowvol", "q_score"):
        assert f.loc["S0", c] > 95 and f.loc["S49", c] < 5, c
    assert f.loc["TINY"].isna().all()  # ineligible micro cap
    tilt = factors.factor_frame(df, weights={"value": 0, "quality": 0, "growth": 0, "momentum": 0, "low_vol": 1})
    assert tilt["q_score"].idxmax() == "S0"


def test_universe_and_screener_expose_q_fields():
    from autobench.services import screener
    from autobench.services.universe import universe

    universe.ensure(wait=True)
    t = universe.table()
    assert {"q_score", "q_momentum", "mom_12_1", "volatility_1y"} <= set(t.columns)
    keys = {f.key for f in screener.FIELDS if f.group == "QBench"}
    assert {"q_score", "q_value", "q_quality", "q_growth", "q_momentum", "q_lowvol"} <= keys
    assert "qbench" in screener.VIEWS
    res = screener.screen(None, "q_score >= 0 or q_score < 0 or price > 0", None, "all", "-q_score", ["symbol", "q_score"], None, 0, 5)
    assert res["rows"]
    s = factors.factor_scores(t.index[0])
    assert s["in_universe"] and set(s["scores"]) == set(factors.FACTORS)


# ----------------------------------------------------------------------------- pairs, allocation, hedge, xsection


def _cointegrated(n=900, seed=31):
    rng = np.random.default_rng(seed)
    lb = np.log(50) + np.cumsum(rng.normal(0.0003, 0.015, n))
    sp = np.zeros(n)
    for i in range(1, n):
        sp[i] = 0.9 * sp[i - 1] + rng.normal(0, 0.01)
    la = 0.5 + 1.2 * lb + sp
    idx = make_bars(np.ones(n), start="2022-01-03").index
    return pd.DataFrame({"AAA": np.exp(la), "BBB": np.exp(lb),
                         "CCC": np.exp(np.log(30) + np.cumsum(rng.normal(0, 0.02, n)))}, index=idx)


def test_pairs_detects_cointegration_and_trades_it(monkeypatch):
    px = _cointegrated()
    monkeypatch.setattr(pairs, "closes", lambda syms, lookback: px[[s for s in syms if s in px.columns]])
    scan = pairs.find_pairs(["AAA", "BBB", "CCC"], min_corr=-1)
    top = scan["pairs"][0]
    assert {top["a"], top["b"]} == {"AAA", "BBB"} and top["tradeable"] and top["p_value"] < 0.01
    assert top["hedge_ratio"] == pytest.approx(1.2 if top["a"] == "AAA" else 1 / 1.2, rel=0.25)
    res = pairs.analyze_pair("AAA", "BBB", notional=10_000)
    assert res["trades"]["count"] >= 6 and res["performance"]["sharpe"] > 0.5
    assert res["current"]["hedge_ratio"] == pytest.approx(1.2, rel=0.2)
    with pytest.raises(ValueError):
        pairs.analyze_pair("AAA", "AAA")


def test_pair_backtest_never_enters_beyond_the_stop(monkeypatch):
    px = _cointegrated(seed=32)
    # a structural break: A jumps 25% and stays there, so the spread blows out past the stop
    px.loc[px.index[600]:, "AAA"] *= 1.25
    monkeypatch.setattr(pairs, "closes", lambda syms, lookback: px[[s for s in syms if s in px.columns]])
    res = pairs.analyze_pair("AAA", "BBB", lookback="5y", formation=252)
    opens = [abs(t["z_open"]) for t in res["recent_trades"]]
    assert all(z < 4.0 for z in opens), opens
    stops = [t for t in res["recent_trades"] if t["reason"] == "stop"]
    assert len(stops) <= 2  # one blow-out = one stop, not a stop every day


def test_allocation_methods():
    rng = np.random.default_rng(41)
    X = rng.normal(size=(500, 4)) * np.array([0.01, 0.02, 0.03, 0.04])
    cov, _ = stats.ledoit_wolf(X)
    w = hedge._erc(cov)
    rc = w * (cov @ w)
    assert np.allclose(rc / rc.sum(), 0.25, atol=1e-4)
    mv = hedge._min_var(cov)
    assert mv[0] > mv[-1] and mv.min() >= 0 and mv.sum() == pytest.approx(1)
    hrp = hedge._hrp(cov)
    assert hrp.sum() == pytest.approx(1) and hrp[0] > hrp[3]
    capped = hedge._cap(np.array([0.7, 0.2, 0.1]), 0.5)
    assert capped.max() == pytest.approx(0.5) and capped.sum() == pytest.approx(1)
    res = hedge.optimize_allocation(["AAPL", "MSFT", "XOM", "JPM"], "hrp", capital=100_000, max_weight=0.4)
    assert sum(r["weight_pct"] for r in res["weights"]) == pytest.approx(100, abs=0.1)
    assert set(res["compare_methods"]) == set(hedge.METHODS)


def test_hedge_plan_beta_of_the_index_is_one():
    res = hedge.hedge_plan({"SPY": 10_000}, ["SPY"])
    assert res["portfolio_beta"] == pytest.approx(1.0, abs=1e-6)
    assert res["beta_hedge"]["notional"] == pytest.approx(-10_000, rel=1e-6) and res["beta_hedge"]["shares"] < 0
    assert res["min_variance_hedge"]["variance_removed_pct"] > 99


def test_factor_backtest_on_a_list():
    syms = ["AAPL", "MSFT", "GOOG", "AMZN", "META", "XOM", "JPM", "KO"]
    res = xsection.run_factor_backtest(syms, "momentum_12_1", top_k=3, lookback="6y")
    assert res["rebalances"] >= 36 and len(res["current_ranking"]) == len(syms)
    assert sum(r["selected"] for r in res["current_ranking"]) == 3
    with pytest.raises(ValueError, match="top_k"):
        xsection.run_factor_backtest(syms[:3], top_k=3)


def test_confluence_and_rank():
    c = confluence.confluence("AAPL")
    assert set(c["pillars"]) == {"technical", "fundamental", "quant"}
    assert c["pillars"]["technical"]["score"] is not None and c["composite"] is not None
    assert {"trend_pullback", "mean_reversion", "breakout"} == set(c["levels"]["plans"])
    r = confluence.rank(["AAPL", "MSFT"])
    assert r["count"] == 2 and all("composite" in row for row in r["rows"])


# ----------------------------------------------------------------------------- REST + MCP


def test_rest_endpoints():
    from autobench.app import create_app

    with TestClient(create_app()) as c:
        strategies = c.get("/api/qbench/strategies").json()["strategies"]
        assert any(s["id"] == "tsmom" and s["references"] for s in strategies)
        assert c.get("/api/qbench/lab/AAPL").json()["result"] is None
        lab_res = c.post("/api/qbench/lab/AAPL", json={"strategies": ["ma_trend"]}).json()
        assert lab_res["strategies"][0]["id"] == "ma_trend"
        assert c.get("/api/qbench/lab/AAPL").json()["result"]["symbol"] == "AAPL"
        assert c.post("/api/qbench/strategy-run", json={"symbol": "AAPL", "strategy": "nope"}).status_code == 400
        assert c.get("/api/qbench/profile/MSFT").json()["regime"]["label"]
        assert c.post("/api/qbench/allocate", json={"symbols": ["AAPL", "MSFT", "KO"]}).json()["weights"]
        assert c.post("/api/qbench/pairs", json={"symbols": ["AAPL", "MSFT"], "min_corr": -1}).json()["pairs_tested"] == 1
        assert c.post("/api/qbench/rank", json={"symbols": ["AAPL"]}).json()["count"] == 1
        assert c.post("/api/qbench/allocate", json={"symbols": ["AAPL"]}).status_code == 400
        c.put("/api/watchlists/qb", json={"add": ["###TECH", "AAPL", "MSFT", "###ENERGY", "XOM"]})
        r = c.post("/api/qbench/rank", json={"watchlist": "qb"}).json()
        assert {row["symbol"] for row in r["rows"]} == {"AAPL", "MSFT", "XOM"}  # section headers skipped




@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_mcp_tools():
    from mcp import Client

    from autobench.mcp_server import mcp

    async with Client(mcp) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert {"run_strategy_lab", "get_quant_profile", "list_quant_strategies", "get_factor_scores", "factor_screen",
                "run_factor_backtest", "find_pairs", "analyze_pair", "optimize_allocation", "get_hedge_plan",
                "get_quant_confluence", "rank_watchlist_quant", "run_quant_strategy"} <= names

        async def call(name, args):
            r = await c.call_tool(name, args)
            return r.is_error, (r.content[0].text if r.is_error else json.loads(r.content[0].text))

        err, card = await call("list_quant_strategies", {"strategy_id": "pairs_cointegration"})
        assert not err and "Gatev" in json.dumps(card)
        err, res = await call("run_strategy_lab", {"symbol": "AAPL", "strategies": ["rsi2"]})
        assert not err and "equity_curve" not in res["strategies"][0] and res["strategies"][0]["verdict"]
        err, msg = await call("run_quant_strategy", {"symbol": "AAPL", "strategy": "xs_momentum"})
        assert err and "run_factor_backtest" in msg
        err, g = await call("get_guide", {"topic": "quant"})
        assert not err and "alpha_dsr" in g["text"]
        prompts = {p.name for p in (await c.list_prompts()).prompts}
        assert "quant_research" in prompts
