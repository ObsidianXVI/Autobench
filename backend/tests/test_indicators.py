"""Indicators against textbook values and independent brute-force implementations."""

import math

import numpy as np
import pandas as pd
import pytest

from autobench.services import indicators as ind
from conftest import make_bars

# J. Welles Wilder RSI example (as published by StockCharts): closes and RSI(14) from the 15th close.
WILDER_CLOSES = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28,
                 46.28, 46.00, 46.03, 46.41, 46.22, 45.64, 46.21, 46.25, 45.71, 46.45, 45.78, 45.35, 44.03, 44.18,
                 44.22, 44.57, 43.42, 42.66, 43.13]
WILDER_RSI = [70.53, 66.32, 66.55, 69.41, 66.36, 57.97, 62.93, 63.26, 56.06, 62.38, 54.71, 50.42, 39.99, 41.46,
              41.87, 45.46, 37.30, 33.08, 37.77]


@pytest.fixture
def rand_df():
    rng = np.random.default_rng(7)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, 400)))
    o = c * np.exp(rng.normal(0, 0.005, 400))
    h = np.maximum(o, c) * (1 + rng.random(400) * 0.02)
    lo = np.minimum(o, c) * (1 - rng.random(400) * 0.02)
    v = rng.integers(1e5, 1e6, 400).astype(float)
    return make_bars(c, opens=o, highs=h, lows=lo, volumes=v)


def test_rsi_matches_wilder_reference():
    df = make_bars(WILDER_CLOSES)
    rsi = ind.compute(df, "rsi", {"length": 14})["rsi"]
    assert rsi.iloc[:14].isna().all()
    # exact value produced by TA-Lib's RSI for this series
    assert rsi.iloc[14] == pytest.approx(70.46413502, abs=1e-6)
    # the published table rounds intermediate averages, so allow its rounding error
    np.testing.assert_allclose(rsi.iloc[14:].to_numpy(), WILDER_RSI, atol=0.1)


def test_rsi_edge_cases():
    up = make_bars(np.arange(1, 40, dtype=float))
    assert ind.compute(up, "rsi")["rsi"].iloc[-1] == 100.0
    flat = make_bars(np.full(40, 10.0))
    assert ind.compute(flat, "rsi")["rsi"].iloc[-1] == 50.0


def test_sma_ema_wma_against_manual(rand_df):
    c = rand_df["c"].to_numpy()
    sma = ind.compute(rand_df, "sma", {"length": 10})["sma"].to_numpy()
    assert math.isclose(sma[-1], c[-10:].mean())
    assert np.isnan(sma[8]) and not np.isnan(sma[9])
    ema = ind.compute(rand_df, "ema", {"length": 10})["ema"].to_numpy()
    a = 2 / 11
    e = c[0]
    for x in c[1:]:
        e = a * x + (1 - a) * e
    assert math.isclose(ema[-1], e, rel_tol=1e-12)
    wma = ind.compute(rand_df, "wma", {"length": 5})["wma"].to_numpy()
    w = np.arange(1, 6)
    assert math.isclose(wma[-1], (c[-5:] * w).sum() / w.sum())


def test_rma_seeded_with_sma():
    s = pd.Series([1.0, 2, 3, 4, 5, 6])
    r = ind.rma(s, 3).to_numpy()
    assert np.isnan(r[1]) and r[2] == 2.0
    assert math.isclose(r[3], 2 + (4 - 2) / 3)


def test_atr_against_manual(rand_df):
    h, lo, c = rand_df["h"].to_numpy(), rand_df["l"].to_numpy(), rand_df["c"].to_numpy()
    tr = [h[0] - lo[0]] + [max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(1, len(c))]
    atr = np.mean(tr[:14])
    for x in tr[14:]:
        atr = (atr * 13 + x) / 14
    assert math.isclose(ind.compute(rand_df, "atr")["atr"].iloc[-1], atr, rel_tol=1e-10)


def test_bbands_and_percent_b(rand_df):
    bb = ind.compute(rand_df, "bbands", {"length": 20, "std": 2})
    c = rand_df["c"].to_numpy()[-20:]
    mid, sd = c.mean(), c.std(ddof=0)
    assert math.isclose(bb["middle"].iloc[-1], mid)
    assert math.isclose(bb["upper"].iloc[-1], mid + 2 * sd)
    assert math.isclose(bb["percent_b"].iloc[-1], (c[-1] - (mid - 2 * sd)) / (4 * sd))


def test_macd_against_manual(rand_df):
    c = rand_df["c"]
    m = ind.compute(rand_df, "macd")
    f = c.ewm(span=12, adjust=False).mean()
    s = c.ewm(span=26, adjust=False).mean()
    macd = f - s
    sig = macd.ewm(span=9, adjust=False).mean()
    assert math.isclose(m["macd"].iloc[-1], macd.iloc[-1], rel_tol=1e-9)
    # signal uses min_periods on a series that starts NaN, so compare after warm-up convergence
    assert abs(m["signal"].iloc[-1] - sig.iloc[-1]) < 1e-3 * abs(c.iloc[-1])
    assert math.isclose(m["histogram"].iloc[-1], m["macd"].iloc[-1] - m["signal"].iloc[-1])


def test_stoch_willr_donchian_manual(rand_df):
    h, lo, c = rand_df["h"], rand_df["l"], rand_df["c"]
    raw = [(c.iloc[i] - lo.iloc[i - 13:i + 1].min()) / (h.iloc[i - 13:i + 1].max() - lo.iloc[i - 13:i + 1].min()) * 100
           for i in range(len(c) - 3, len(c))]
    st = ind.compute(rand_df, "stoch", {"k": 14, "d": 3, "smooth": 3})
    assert math.isclose(st["k"].iloc[-1], np.mean(raw), rel_tol=1e-9)
    wr = ind.compute(rand_df, "willr")["willr"].iloc[-1]
    hh, ll = h.iloc[-14:].max(), lo.iloc[-14:].min()
    assert math.isclose(wr, (hh - c.iloc[-1]) / (hh - ll) * -100)
    dc = ind.compute(rand_df, "donchian", {"length": 20})
    assert dc["upper"].iloc[-1] == h.iloc[-20:].max() and dc["lower"].iloc[-1] == lo.iloc[-20:].min()


def test_obv_and_cmf(rand_df):
    obv = ind.compute(rand_df, "obv")["obv"].to_numpy()
    c, v = rand_df["c"].to_numpy(), rand_df["v"].to_numpy()
    manual = np.cumsum(np.r_[0, np.sign(np.diff(c)) * v[1:]])
    np.testing.assert_allclose(obv, manual)
    cmf = ind.compute(rand_df, "cmf")["cmf"].dropna()
    assert ((cmf >= -1) & (cmf <= 1)).all()


def test_bounded_oscillators(rand_df):
    for name, col, lo, hi in [("rsi", "rsi", 0, 100), ("mfi", "mfi", 0, 100), ("stochrsi", "k", 0, 100),
                              ("adx", "adx", 0, 100), ("aroon", "up", 0, 100), ("willr", "willr", -100, 0)]:
        s = ind.compute(rand_df, name)[col].dropna()
        assert len(s) > 100, name
        assert s.min() >= lo - 1e-9 and s.max() <= hi + 1e-9, name


def test_aroon_bars_since_high():
    c = np.r_[np.arange(10, 40, dtype=float), np.arange(39, 29, -1, dtype=float)]  # peak at index 29
    df = make_bars(c, spread=0.0)
    df["h"] = df["c"] + 0.5  # unique peak at index 29 (first 39 close)
    df.iloc[30, df.columns.get_loc("h")] = 39.2
    df["l"] = df["c"] - 0.5
    a = ind.compute(df, "aroon", {"length": 25})
    since = len(c) - 1 - 29
    assert math.isclose(a["up"].iloc[-1], 100 * (25 - since) / 25)


def test_linreg_on_perfect_line():
    df = make_bars(np.linspace(100, 149, 50))
    lr = ind.compute(df, "linreg", {"length": 20})
    assert math.isclose(lr["r2"].iloc[-1], 1.0)
    assert math.isclose(lr["linreg"].iloc[-1], 149.0)
    assert math.isclose(lr["slope_pct"].iloc[-1], 1 / 149 * 100)


def test_supertrend_and_psar_follow_trend():
    c = np.r_[np.linspace(100, 150, 60), np.linspace(150, 90, 60)]
    df = make_bars(c)
    st = ind.compute(df, "supertrend")
    assert st["direction"].iloc[55] == 1 and st["direction"].iloc[-1] == -1
    assert st["supertrend"].iloc[55] < df["c"].iloc[55]
    ps = ind.compute(df, "psar")
    assert ps["direction"].iloc[55] == 1 and ps["psar"].iloc[55] < df["l"].iloc[55]
    assert ps["direction"].iloc[-1] == -1 and ps["psar"].iloc[-1] > df["h"].iloc[-1]


def test_vwap_resets_each_session():
    # two sessions of 5-minute bars
    times = []
    for day in ("2025-03-03", "2025-03-04"):
        base = pd.Timestamp(f"{day} 09:30", tz="America/New_York")
        times += [int((base + pd.Timedelta(minutes=5 * i)).timestamp()) for i in range(3)]
    df = pd.DataFrame({"o": 10.0, "h": [11, 12, 13, 21, 22, 23], "l": [9, 10, 11, 19, 20, 21],
                       "c": [10, 11, 12, 20, 21, 22], "v": [100, 100, 200, 100, 100, 100]},
                      index=pd.Index(times, name="t"), dtype=float)
    vw = ind.compute(df, "vwap")["vwap"]
    assert math.isclose(vw.iloc[3], 20.0)  # first bar of day 2 = its own typical price
    assert math.isclose(vw.iloc[2], (10 * 100 + 11 * 100 + 12 * 200) / 400)


def test_anchored_vwap_starts_at_anchor(rand_df):
    anchor = int(rand_df.index[100])
    av = ind.compute(rand_df, "anchored_vwap", {"anchor": str(anchor)})["avwap"]
    assert av.iloc[:100].isna().all()
    tp = (rand_df["h"] + rand_df["l"] + rand_df["c"]) / 3
    assert math.isclose(av.iloc[100], tp.iloc[100])


def test_ichimoku_shifts(rand_df):
    ich = ind.compute(rand_df, "ichimoku")
    assert ich["chikou"].iloc[-26:].isna().all()
    h, lo = rand_df["h"], rand_df["l"]
    exp_tenkan = (h.iloc[-9:].max() + lo.iloc[-9:].min()) / 2
    assert math.isclose(ich["tenkan"].iloc[-1], exp_tenkan)


def test_every_indicator_runs_and_handles_short_data(rand_df):
    short = rand_df.iloc[:5]
    for spec in ind.REGISTRY.values():
        full = ind.compute(rand_df, spec.name)
        assert list(full.columns) == spec.outputs
        assert len(full) == len(rand_df)
        assert not np.isinf(full.to_numpy()).any(), spec.name
        assert full.iloc[-1].notna().any() or spec.name == "ichimoku", spec.name
        ind.compute(short, spec.name)  # must not raise
        ind.compute(rand_df.iloc[:0], spec.name)


def test_params_validation():
    spec = ind.get_spec("bbands")
    assert ind.resolve_params(spec, None, [10, 1.5]) == {"length": 10, "std": 1.5, "source": "close"}
    with pytest.raises(ValueError, match="no parameter"):
        ind.resolve_params(spec, {"bogus": 1})
    with pytest.raises(ValueError, match="at most"):
        ind.resolve_params(spec, None, [1, 2, "close", 4])
    with pytest.raises(ValueError, match="Unknown indicator"):
        ind.get_spec("nope")
    df = make_bars(np.arange(1, 50, dtype=float))
    with pytest.raises(ValueError, match="integer"):
        ind.compute(df, "sma", {"length": 2.5})
    with pytest.raises(ValueError, match="source"):
        ind.compute(df, "sma", {"source": "nope"})
    assert ind.label(ind.get_spec("macd"), ind.resolve_params(ind.get_spec("macd"))) == "macd(12,26,9)"


def test_registry_metadata_complete():
    assert len(ind.REGISTRY) >= 40
    for d in ind.list_indicators():
        assert d["category"] in {"trend", "momentum", "volatility", "volume", "statistics"}
        assert d["description"] and d["outputs"]
