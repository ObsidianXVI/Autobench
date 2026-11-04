import numpy as np
import pytest

from autobench.services import levels, patterns, technicals
from conftest import make_bars


def zigzag(n_cycles=6, low=100.0, high=110.0, half=8, drift=0.0):
    """Repeating up/down legs; each cycle's extremes shift by `drift`."""
    out = []
    for k in range(n_cycles):
        lo, hi = low + k * drift, high + k * drift
        out += list(np.linspace(lo, hi, half, endpoint=False)) + list(np.linspace(hi, lo + drift / 2, half, endpoint=False))
    return np.array(out)


def test_swing_points_on_zigzag():
    df = make_bars(zigzag(), spread=0.0)
    sw = levels.swing_points(df, 3, 3)
    highs = [s for s in sw if s["kind"] == "high"]
    lows = [s for s in sw if s["kind"] == "low"]
    assert len(highs) >= 4 and len(lows) >= 4
    assert all(abs(h["price"] - 110) < 1.5 for h in highs)
    # last `right` bars can never be confirmed swings
    assert max(s["i"] for s in sw) <= len(df) - 4


def test_support_resistance_finds_range_edges():
    c = zigzag(8)
    c = np.r_[c, np.linspace(100, 105, 5)]  # end mid-range
    df = make_bars(c, spread=0.0)
    sr = levels.support_resistance(df)
    assert sr["supports"] and sr["resistances"]
    assert abs(sr["supports"][0]["price"] - 100) < 1.5
    assert abs(sr["resistances"][0]["price"] - 110) < 1.5
    assert sr["resistances"][0]["touches"] >= 3
    assert sr["supports"][0]["distance_pct"] < 0 < sr["resistances"][0]["distance_pct"]


def test_pivots_formula():
    p = levels.pivot_points(110, 100, 105)
    assert p["P"] == pytest.approx(105)
    assert p["R1"] == pytest.approx(110) and p["S1"] == pytest.approx(100)
    assert p["R2"] == pytest.approx(115) and p["S2"] == pytest.approx(95)


def test_fibonacci_direction_and_levels():
    df = make_bars(np.r_[np.linspace(100, 200, 30), np.linspace(200, 150, 10)], spread=0.0)
    fib = levels.fibonacci(df)
    assert fib["direction"] == "up"
    assert fib["retracements"]["0.500"] == pytest.approx(150, abs=1)
    assert fib["current_retracement"] == pytest.approx(0.5, abs=0.02)


def test_volume_profile_poc_at_heavy_volume():
    c = np.r_[np.full(30, 100.0), np.full(10, 120.0)]
    v = np.r_[np.full(30, 1e6), np.full(10, 1e5)]
    df = make_bars(c, spread=0.005, volumes=v)
    vp = levels.volume_profile(df, bins=20)
    assert abs(vp["poc"] - 100) < 2
    assert vp["value_area_low"] <= vp["poc"] <= vp["value_area_high"]
    assert sum(b["volume"] for b in vp["bins"]) == pytest.approx(v.sum(), rel=1e-6)


def test_trendline_candidates_rising_support():
    df = make_bars(zigzag(6, drift=3.0), spread=0.0)
    tl = levels.trendline_candidates(df, 3, 3)
    sup = next(t for t in tl if t["role"] == "support")
    assert sup["direction"] == "rising"
    assert len(sup["points"]) == 2 and sup["points"][0]["time"] < sup["points"][1]["time"]


def test_market_structure_uptrend_and_downtrend():
    up = make_bars(zigzag(6, drift=3.0), spread=0.0)
    assert patterns.market_structure(up, 3, 3)["state"] == "uptrend"
    down = make_bars(zigzag(6, low=200, high=210, drift=-3.0), spread=0.0)
    assert patterns.market_structure(down, 3, 3)["state"] == "downtrend"


def test_bullish_engulfing_after_decline():
    c = list(np.linspace(120, 100, 20))
    o = [x + 0.5 for x in c]  # red candles
    # small red bar then large green engulfing bar
    o += [100.0, 98.5]
    c += [99.0, 101.5]
    h = [max(a, b) + 0.2 for a, b in zip(o, c)]
    lo = [min(a, b) - 0.2 for a, b in zip(o, c)]
    df = make_bars(c, opens=o, highs=h, lows=lo)
    found = patterns.candlestick_patterns(df, 3)
    assert any(p["pattern"] == "bullish_engulfing" and p["bias"] == "bullish" for p in found)


def test_hammer_in_downtrend():
    c = list(np.linspace(120, 100, 20)) + [100.2]
    o = [x + 0.5 for x in c[:-1]] + [100.0]
    h = [max(a, b) + 0.1 for a, b in zip(o, c)]
    lo = [min(a, b) - 0.1 for a, b in zip(o, c[:-1])] + [97.0]  # long lower shadow
    df = make_bars(c, opens=o, highs=h, lows=lo)
    assert any(p["pattern"] == "hammer" for p in patterns.candlestick_patterns(df, 2))


def test_double_top_detected_and_confirmed():
    c = np.r_[np.linspace(100, 130, 15), np.linspace(130, 110, 10), np.linspace(110, 130, 10),
              np.linspace(130, 100, 15)]
    df = make_bars(c, spread=0.002)
    dt = [d for d in patterns.double_tops_bottoms(df, min_separation=5) if d["pattern"] == "double_top"]
    assert dt and dt[0]["confirmed"] and dt[0]["neckline"] == pytest.approx(110, abs=1.5)


def test_recent_events_breakout_and_gap():
    c = np.r_[np.full(60, 100.0) + np.sin(np.arange(60)) * 0.5, [104.0]]
    o = np.r_[c[0], c[:-2], [103.0]]  # +2.5% gap on the last bar
    v = np.r_[np.full(60, 1e6), [3e6]]
    df = make_bars(c, opens=o, volumes=v)
    ev = [e["event"] for e in patterns.recent_events(df, 1)]
    assert any("breakout" in e for e in ev)
    assert any(e.startswith("gap up") for e in ev)
    assert any("volume spike" in e for e in ev)


def test_technical_summary_shape_on_fake_data():
    s = technicals.technical_summary("AAPL", "1d")
    for k in ("trend", "momentum", "volatility", "volume", "levels", "patterns", "recent_events", "relative_strength",
              "mechanical_bias", "returns_pct", "range_52w"):
        assert k in s, k
    assert -100 <= s["trend"]["score"] <= 100
    assert isinstance(s["as_of"], str)
    brief = technicals.technical_summary("AAPL", "1d", detail="brief")
    assert "patterns" not in brief and "levels" in brief
    mtf = technicals.multi_timeframe("AAPL", ["1wk", "1d"])
    assert set(mtf["timeframes"]) == {"1wk", "1d"}
