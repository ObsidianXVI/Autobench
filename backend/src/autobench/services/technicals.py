"""Technical summary: a compact, pre-digested read of trend, momentum, volatility, volume,
levels, patterns and relative strength for one symbol/interval, plus multi-timeframe alignment.

Scores are mechanical (-100..100) and meant as a starting point for reasoning, not a verdict."""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import indicators as ind
from . import levels as lv
from . import patterns as pt
from .market import Bars, get_bars, normalize_symbol, price_round
from ..timeutil import format_ts

_LOOKBACK = {"1m": "5d", "5m": "30d", "15m": "45d", "30m": "55d", "1h": "180d", "1d": "2y", "1wk": "10y", "1mo": "max"}


def _f(x, nd: int = 2):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(v) else round(v, nd)


def _pct(a, b):
    return _f((a / b - 1) * 100) if b else None


def _percentile_rank(s: pd.Series, window: int = 252) -> float | None:
    s = s.dropna().iloc[-window:]
    if len(s) < 20:
        return None
    return _f((s < s.iloc[-1]).mean() * 100, 1)


def _returns(df: pd.DataFrame, intraday: bool) -> dict:
    c = df["c"]
    last = c.iloc[-1]
    out = {}
    if intraday:
        for n, k in ((1, "1_bar"), (5, "5_bars"), (20, "20_bars")):
            if len(c) > n:
                out[k] = _pct(last, c.iloc[-1 - n])
        return out
    t = pd.to_datetime(df.index, unit="s")
    for label, days in (("1w", 7), ("1m", 30), ("3m", 91), ("6m", 182), ("1y", 365)):
        cutoff = t[-1] - pd.Timedelta(days=days)
        prior = c[t <= cutoff]
        if len(prior):
            out[label] = _pct(last, prior.iloc[-1])
    ytd = c[t < pd.Timestamp(year=t[-1].year, month=1, day=1)]
    if len(ytd):
        out["ytd"] = _pct(last, ytd.iloc[-1])
    return out


def _trend(df: pd.DataFrame) -> dict:
    c = df["c"]
    last = c.iloc[-1]
    s20, s50, s200 = ind.sma(c, 20), ind.sma(c, 50), ind.sma(c, 200)
    e21 = ind.ema(c, 21)
    adx = ind.compute(df, "adx")
    lr = ind.compute(df, "linreg", {"length": 50})
    st = ind.compute(df, "supertrend")
    ich = ind.compute(df, "ichimoku")
    score = 0.0
    weights = 0.0

    def vote(cond, w):
        nonlocal score, weights
        if cond is None:
            return
        score += w if cond else -w
        weights += w

    def above(a, b):
        return None if not (np.isfinite(a) and np.isfinite(b)) else bool(a > b)

    vote(above(last, s20.iloc[-1]), 1)
    vote(above(last, s50.iloc[-1]), 1.5)
    vote(above(last, s200.iloc[-1]), 2)
    vote(above(s50.iloc[-1], s200.iloc[-1]), 1.5)
    vote(above(s20.iloc[-1], s50.iloc[-1]), 1)
    slope200 = _pct(s200.iloc[-1], s200.iloc[-21]) if len(s200.dropna()) > 21 else None
    if slope200 is not None:
        vote(slope200 > 0, 1)
    if np.isfinite(st["direction"].iloc[-1]):
        vote(st["direction"].iloc[-1] > 0, 1)
    cloud_top = np.nanmax([ich["senkou_a"].iloc[-1], ich["senkou_b"].iloc[-1]]) if ich[["senkou_a", "senkou_b"]].iloc[-1].notna().all() else np.nan
    cloud_bot = np.nanmin([ich["senkou_a"].iloc[-1], ich["senkou_b"].iloc[-1]]) if np.isfinite(cloud_top) else np.nan
    cloud = None
    if np.isfinite(cloud_top):
        cloud = "above" if last > cloud_top else "below" if last < cloud_bot else "inside"
        if cloud != "inside":
            vote(cloud == "above", 1)
    adx_v = adx["adx"].iloc[-1]
    structure = pt.market_structure(df)
    if structure["state"] in ("uptrend", "downtrend"):
        vote(structure["state"] == "uptrend", 1.5)
    trend_score = _f(score / weights * 100, 0) if weights else None
    stack = None
    if all(np.isfinite(x) for x in (s20.iloc[-1], s50.iloc[-1], s200.iloc[-1])):
        if last > s20.iloc[-1] > s50.iloc[-1] > s200.iloc[-1]:
            stack = "bullish (price > 20 > 50 > 200)"
        elif last < s20.iloc[-1] < s50.iloc[-1] < s200.iloc[-1]:
            stack = "bearish (price < 20 < 50 < 200)"
        else:
            stack = "mixed"
    return {
        "score": trend_score,
        "sma20": _f(s20.iloc[-1]), "sma50": _f(s50.iloc[-1]), "sma200": _f(s200.iloc[-1]), "ema21": _f(e21.iloc[-1]),
        "price_vs_sma20_pct": _pct(last, s20.iloc[-1]), "price_vs_sma50_pct": _pct(last, s50.iloc[-1]),
        "price_vs_sma200_pct": _pct(last, s200.iloc[-1]),
        "ma_stack": stack,
        "sma200_slope_20bars_pct": slope200,
        "adx": _f(adx_v), "plus_di": _f(adx["plus_di"].iloc[-1]), "minus_di": _f(adx["minus_di"].iloc[-1]),
        "trend_strength": None if not np.isfinite(adx_v) else "strong" if adx_v >= 25 else "weak/ranging" if adx_v < 20 else "moderate",
        "linreg50_slope_pct_per_bar": _f(lr["slope_pct"].iloc[-1], 3), "linreg50_r2": _f(lr["r2"].iloc[-1]),
        "supertrend": {"direction": "up" if st["direction"].iloc[-1] > 0 else "down", "level": _f(st["supertrend"].iloc[-1])}
        if np.isfinite(st["direction"].iloc[-1]) else None,
        "ichimoku_cloud": cloud,
        "structure": structure["state"],
        "last_swing_high": structure["last_swing_high"] and {"date": structure["last_swing_high"]["t"], "price": _f(structure["last_swing_high"]["price"])},
        "last_swing_low": structure["last_swing_low"] and {"date": structure["last_swing_low"]["t"], "price": _f(structure["last_swing_low"]["price"])},
    }


def _momentum(df: pd.DataFrame) -> dict:
    rsi = ind.compute(df, "rsi")["rsi"]
    macd = ind.compute(df, "macd")
    stoch = ind.compute(df, "stoch")
    roc = ind.compute(df, "roc", {"length": 20})["roc"]
    cci = ind.compute(df, "cci")["cci"]
    r = rsi.iloc[-1]
    h = macd["histogram"]
    score, w = 0.0, 0.0
    if np.isfinite(r):
        score += np.clip((r - 50) / 20, -1, 1) * 2
        w += 2
    if np.isfinite(h.iloc[-1]):
        score += (1 if h.iloc[-1] > 0 else -1) * 1.5 + (0.5 if h.iloc[-1] > h.iloc[-2] else -0.5)
        w += 2
    if np.isfinite(macd["macd"].iloc[-1]):
        score += 1 if macd["macd"].iloc[-1] > 0 else -1
        w += 1
    if np.isfinite(roc.iloc[-1]):
        score += float(np.clip(roc.iloc[-1] / 10, -1, 1))
        w += 1
    state = None
    if np.isfinite(r):
        state = "overbought" if r >= 70 else "oversold" if r <= 30 else "bullish" if r >= 55 else "bearish" if r <= 45 else "neutral"
    div = _divergence(df, rsi)
    return {
        "score": _f(score / w * 100, 0) if w else None,
        "rsi14": _f(r), "rsi_state": state, "rsi_5_bars_ago": _f(rsi.iloc[-6]) if len(rsi) > 6 else None,
        "macd": _f(macd["macd"].iloc[-1], 3), "macd_signal": _f(macd["signal"].iloc[-1], 3),
        "macd_histogram": _f(h.iloc[-1], 3),
        "macd_histogram_trend": None if not np.isfinite(h.iloc[-1]) else ("rising" if h.iloc[-1] > h.iloc[-2] else "falling"),
        "stoch_k": _f(stoch["k"].iloc[-1]), "stoch_d": _f(stoch["d"].iloc[-1]),
        "roc20_pct": _f(roc.iloc[-1]), "cci20": _f(cci.iloc[-1], 0),
        "divergence": div,
    }


def _divergence(df: pd.DataFrame, osc: pd.Series) -> str | None:
    """Regular RSI divergence between the last two confirmed swing highs/lows."""
    swings = lv.swing_points(df, 5, 3)
    highs = [s for s in swings if s["kind"] == "high"][-2:]
    lows = [s for s in swings if s["kind"] == "low"][-2:]
    n = len(df)
    if len(highs) == 2 and highs[1]["i"] > n - 30:
        a, b = highs
        if b["price"] > a["price"] and osc.iloc[b["i"]] < osc.iloc[a["i"]] - 2:
            return f"bearish: higher price high but lower RSI high ({format_ts(a['t'], False)} -> {format_ts(b['t'], False)})"
    if len(lows) == 2 and lows[1]["i"] > n - 30:
        a, b = lows
        if b["price"] < a["price"] and osc.iloc[b["i"]] > osc.iloc[a["i"]] + 2:
            return f"bullish: lower price low but higher RSI low ({format_ts(a['t'], False)} -> {format_ts(b['t'], False)})"
    return None


def _volatility(df: pd.DataFrame, bars_per_year: float) -> dict:
    atr = ind.compute(df, "atr")["atr"]
    bb = ind.compute(df, "bbands")
    sq = ind.compute(df, "squeeze")
    hv = ind.compute(df, "hv", {"bars_per_year": bars_per_year})["hv"]
    last = df["c"].iloc[-1]
    bw_rank = _percentile_rank(bb["bandwidth"])
    return {
        "atr14": _f(atr.iloc[-1], 4), "atr_pct": _pct(last + atr.iloc[-1], last),
        "hv20_annualized_pct": _f(hv.iloc[-1]),
        "hv20_percentile_1y": _percentile_rank(hv),
        "bb_upper": _f(bb["upper"].iloc[-1]), "bb_lower": _f(bb["lower"].iloc[-1]),
        "bb_percent_b": _f(bb["percent_b"].iloc[-1]),
        "bb_bandwidth_percentile_1y": bw_rank,
        "squeeze_on": bool(sq["squeeze_on"].iloc[-1] == 1) if np.isfinite(sq["squeeze_on"].iloc[-1]) else None,
        "regime": None if bw_rank is None else "compressed" if bw_rank < 20 else "expanded" if bw_rank > 80 else "normal",
    }


def _volume(df: pd.DataFrame) -> dict:
    v = df["v"]
    if v.sum() == 0:
        return {"available": False}
    obv = ind.compute(df, "obv")["obv"]
    cmf = ind.compute(df, "cmf")["cmf"]
    mfi = ind.compute(df, "mfi")["mfi"]
    rvol = ind.compute(df, "rvol")["rvol"]
    last20 = df.iloc[-20:]
    up_v = last20.loc[last20["c"] > last20["o"], "v"].sum()
    dn_v = last20.loc[last20["c"] < last20["o"], "v"].sum()
    obv_slope = obv.iloc[-1] - obv.iloc[-21] if len(obv) > 21 else np.nan
    price_slope = df["c"].iloc[-1] - df["c"].iloc[-21] if len(df) > 21 else np.nan
    conf = None
    if np.isfinite(obv_slope) and np.isfinite(price_slope):
        conf = "confirming" if np.sign(obv_slope) == np.sign(price_slope) else "diverging (volume not confirming price)"
    return {
        "last_volume": int(v.iloc[-1]), "avg_volume_20": int(v.iloc[-21:-1].mean()) if len(v) > 21 else None,
        "rvol": _f(rvol.iloc[-1]), "cmf20": _f(cmf.iloc[-1], 3), "mfi14": _f(mfi.iloc[-1]),
        "up_down_volume_ratio_20": _f(up_v / dn_v) if dn_v > 0 else None,
        "obv_vs_price_20bars": conf,
    }


def _relative_strength(bars: Bars, benchmark: str) -> dict | None:
    if bars.symbol == benchmark:
        return None
    try:
        b = get_bars(benchmark, bars.interval.name, start=int(bars.df.index[0]))
    except Exception:
        return None
    j = pd.concat([bars.df["c"], b.df["c"]], axis=1, keys=["s", "b"]).dropna()
    if len(j) < 30:
        return None
    rs = j["s"] / j["b"]
    out = {"benchmark": benchmark}
    for label, n in (("1m", 21), ("3m", 63), ("6m", 126)):
        if len(j) > n:
            out[f"outperformance_{label}_pct"] = _f(((j["s"].iloc[-1] / j["s"].iloc[-1 - n]) - (j["b"].iloc[-1] / j["b"].iloc[-1 - n])) * 100)
    rs_ma = rs.rolling(50).mean()
    out["rs_line_vs_50ma"] = None if not np.isfinite(rs_ma.iloc[-1]) else ("above (leading)" if rs.iloc[-1] > rs_ma.iloc[-1] else "below (lagging)")
    out["rs_line_new_high_3m"] = bool(rs.iloc[-1] >= rs.iloc[-63:].max()) if len(rs) >= 63 else None
    return out


def _round_levels(d):
    if isinstance(d, dict):
        return {k: _round_levels(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_round_levels(v) for v in d]
    if isinstance(d, float):
        return price_round(d)
    return d


def technical_summary(symbol: str, interval: str = "1d", benchmark: str = "SPY", detail: str = "full") -> dict:
    """detail='full' (everything) or 'brief' (trend/momentum/key levels only, for multi-symbol scans)."""
    symbol = normalize_symbol(symbol)
    from ..timeutil import get_interval

    iv = get_interval(interval)
    bars = get_bars(symbol, iv.name, lookback=_LOOKBACK[iv.name])
    df = bars.df
    if len(df) < 30:
        raise ValueError(f"Only {len(df)} {iv.name} bars available for {symbol}; need at least 30")
    last = float(df["c"].iloc[-1])
    out: dict = {
        "symbol": symbol, "interval": iv.name, "source": bars.source,
        "as_of": format_ts(int(df.index[-1]), iv.intraday), "bars_analyzed": len(df),
        "price": price_round(last),
        "change_pct": _pct(last, df["c"].iloc[-2]),
        "returns_pct": _returns(df, iv.intraday),
    }
    if not iv.intraday:
        yr = df.iloc[-252:]
        out["range_52w"] = {"high": price_round(float(yr["h"].max())), "low": price_round(float(yr["l"].min())),
                            "from_high_pct": _pct(last, yr["h"].max()), "from_low_pct": _pct(last, yr["l"].min())}
    out["trend"] = _trend(df)
    out["momentum"] = _momentum(df)
    sr = lv.support_resistance(df.iloc[-min(len(df), 500):])
    out["levels"] = {
        "supports": [{"price": price_round(x["price"]), "touches": x["touches"], "distance_pct": x["distance_pct"],
                      "distance_atr": x["distance_atr"]} for x in sr["supports"][:3]],
        "resistances": [{"price": price_round(x["price"]), "touches": x["touches"], "distance_pct": x["distance_pct"],
                         "distance_atr": x["distance_atr"]} for x in sr["resistances"][:3]],
    }
    if detail == "brief":
        out["volatility"] = {"atr14": _f(ind.compute(df, "atr")["atr"].iloc[-1], 4)}
        return _round_levels(_fmt_times(out, iv.intraday))
    out["volatility"] = _volatility(df, iv.bars_per_year)
    out["volume"] = _volume(df)
    if not iv.intraday:
        piv = lv.period_pivots(df) if iv.name == "1d" else {}
        out["levels"]["pivots"] = {k: {kk: price_round(vv) for kk, vv in v.items()} for k, v in piv.items()}
        fib = lv.fibonacci(df.iloc[-126:])
        if fib:
            out["levels"]["fibonacci_6m"] = {
                "direction": fib["direction"], "swing_low": price_round(fib["swing_low"]["price"]),
                "swing_high": price_round(fib["swing_high"]["price"]),
                "current_retracement": fib["current_retracement"],
                "retracements": {k: price_round(v) for k, v in fib["retracements"].items()},
            }
    vp = lv.volume_profile(df.iloc[-min(len(df), 126):], bins=30)
    if vp:
        out["levels"]["volume_profile"] = {"poc": price_round(vp["poc"]), "value_area_high": price_round(vp["value_area_high"]),
                                           "value_area_low": price_round(vp["value_area_low"])}
    out["levels"]["trendlines"] = [
        {"role": t["role"], "direction": t["direction"], "value_now": price_round(t["value_at_last_bar"]),
         "touches": t["touches"], "broken": t["broken"], "points": t["points"]}
        for t in lv.trendline_candidates(df.iloc[-min(len(df), 250):])
    ]
    out["patterns"] = {
        "candles_last_5": [{"date": c["t"], "pattern": c["pattern"], "bias": c["bias"]} for c in pt.candlestick_patterns(df, 5)],
        "double_tops_bottoms": [{k: v for k, v in d.items() if k != "points"} for d in pt.double_tops_bottoms(df.iloc[-250:])],
        "consolidation": pt.consolidation(df),
    }
    out["recent_events"] = [{"date": e["t"], **{k: v for k, v in e.items() if k != "t"}} for e in pt.recent_events(df, 15)][:12]
    rs = _relative_strength(bars, benchmark)
    if rs:
        out["relative_strength"] = rs
    t, m = out["trend"]["score"], out["momentum"]["score"]
    out["mechanical_bias"] = _bias(t, m)
    return _round_levels(_fmt_times(out, iv.intraday))


def _bias(t, m) -> str | None:
    if t is None or m is None:
        return None
    comp = 0.6 * t + 0.4 * m
    label = ("strongly bullish" if comp >= 60 else "bullish" if comp >= 25 else "strongly bearish" if comp <= -60
             else "bearish" if comp <= -25 else "neutral")
    return f"{label} (composite {comp:.0f}: 60% trend, 40% momentum)"


def _fmt_times(d, intraday: bool):
    """Convert epoch fields named date/t/time into readable strings."""
    if isinstance(d, dict):
        return {k: (format_ts(v, intraday) if k in ("date", "t", "time", "first_touch", "last_touch") and isinstance(v, (int, np.integer)) and v > 10**8
                    else _fmt_times(v, intraday)) for k, v in d.items()}
    if isinstance(d, list):
        return [_fmt_times(v, intraday) for v in d]
    return d


def multi_timeframe(symbol: str, intervals: list[str] | None = None) -> dict:
    intervals = intervals or ["1wk", "1d", "1h"]
    rows = {}
    for iv in intervals:
        try:
            s = technical_summary(symbol, iv, detail="brief")
            rows[iv] = {
                "trend_score": s["trend"]["score"], "momentum_score": s["momentum"]["score"],
                "ma_stack": s["trend"]["ma_stack"], "structure": s["trend"]["structure"],
                "adx": s["trend"]["adx"], "rsi14": s["momentum"]["rsi14"],
                "macd_histogram_trend": s["momentum"]["macd_histogram_trend"],
                "nearest_support": s["levels"]["supports"][0]["price"] if s["levels"]["supports"] else None,
                "nearest_resistance": s["levels"]["resistances"][0]["price"] if s["levels"]["resistances"] else None,
            }
        except Exception as e:
            rows[iv] = {"error": str(e)}
    scores = [r["trend_score"] for r in rows.values() if r.get("trend_score") is not None]
    if scores and all(s > 20 for s in scores):
        align = "aligned bullish"
    elif scores and all(s < -20 for s in scores):
        align = "aligned bearish"
    else:
        align = "mixed / conflicting"
    return {"symbol": normalize_symbol(symbol), "timeframes": rows, "alignment": align}
