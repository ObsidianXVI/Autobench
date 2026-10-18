"""Pattern and event detection: candlesticks, market structure, double tops/bottoms,
consolidation ranges and notable technical events (crosses, breakouts, gaps, volume spikes)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from . import indicators as ind
from .levels import swing_points


def _ctx_trend(df: pd.DataFrame) -> pd.Series:
    """+1 if the 10-bar SMA is rising over 5 bars, -1 if falling (context for reversal candles)."""
    s = ind.sma(df["c"], 10)
    return np.sign(s - s.shift(5)).fillna(0)


def candlestick_patterns(df: pd.DataFrame, last_n: int = 10) -> list[dict]:
    """Classic candlestick patterns found in the last `last_n` bars, most recent first.
    Reversal patterns are only reported in the matching trend context."""
    if len(df) < 15:
        return []
    o, h, l, c = (df[k].to_numpy() for k in ("o", "h", "l", "c"))  # noqa: E741
    body = np.abs(c - o)
    rng = h - l
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - l
    avg_body = pd.Series(body).rolling(14, min_periods=5).mean().to_numpy()
    trend = _ctx_trend(df).to_numpy()
    idx = df.index.to_numpy()
    found = []

    def add(i, name, bias, note=""):
        found.append({"t": int(idx[i]), "pattern": name, "bias": bias, **({"note": note} if note else {})})

    for i in range(max(2, len(df) - last_n), len(df)):
        if rng[i] <= 0:
            continue
        bull, bear = c[i] > o[i], c[i] < o[i]
        if body[i] <= 0.1 * rng[i]:
            add(i, "doji", "neutral", "indecision")
        if lower[i] >= 2 * body[i] and upper[i] <= 0.3 * max(body[i], 1e-9) + 0.1 * rng[i] and body[i] > 0.05 * rng[i]:
            if trend[i] < 0:
                add(i, "hammer", "bullish")
            elif trend[i] > 0:
                add(i, "hanging_man", "bearish")
        if upper[i] >= 2 * body[i] and lower[i] <= 0.3 * max(body[i], 1e-9) + 0.1 * rng[i] and body[i] > 0.05 * rng[i]:
            if trend[i] > 0:
                add(i, "shooting_star", "bearish")
            elif trend[i] < 0:
                add(i, "inverted_hammer", "bullish")
        if body[i] >= 0.9 * rng[i] and body[i] > 1.3 * (avg_body[i] or 0):
            add(i, "bullish_marubozu" if bull else "bearish_marubozu", "bullish" if bull else "bearish")
        p = i - 1
        if bull and c[p] < o[p] and o[i] <= c[p] and c[i] >= o[p] and body[i] > body[p]:
            add(i, "bullish_engulfing", "bullish", "" if trend[i] < 0 else "not after a decline")
        if bear and c[p] > o[p] and o[i] >= c[p] and c[i] <= o[p] and body[i] > body[p]:
            add(i, "bearish_engulfing", "bearish", "" if trend[i] > 0 else "not after an advance")
        if h[i] <= h[p] and l[i] >= l[p]:
            add(i, "inside_bar", "neutral", "compression; trade the break of the mother bar")
        if h[i] > h[p] and l[i] < l[p] and body[i] > body[p]:
            add(i, "outside_bar", "bullish" if bull else "bearish")
        q = i - 2
        small_mid = body[p] < 0.5 * max(body[q], 1e-9)
        if c[q] < o[q] and small_mid and bull and c[i] > (o[q] + c[q]) / 2 and body[q] > (avg_body[i] or 0):
            add(i, "morning_star", "bullish")
        if c[q] > o[q] and small_mid and bear and c[i] < (o[q] + c[q]) / 2 and body[q] > (avg_body[i] or 0):
            add(i, "evening_star", "bearish")
        if all(c[j] > o[j] for j in (q, p, i)) and c[i] > c[p] > c[q] and o[p] > o[q] and o[i] > o[p]:
            add(i, "three_white_soldiers", "bullish")
        if all(c[j] < o[j] for j in (q, p, i)) and c[i] < c[p] < c[q] and o[p] < o[q] and o[i] < o[p]:
            add(i, "three_black_crows", "bearish")
    return sorted(found, key=lambda x: -x["t"])


def market_structure(df: pd.DataFrame, left: int = 5, right: int = 5) -> dict:
    """Label recent swings HH/HL/LH/LL and classify the structure."""
    swings = swing_points(df, left, right)
    highs = [s for s in swings if s["kind"] == "high"][-4:]
    lows = [s for s in swings if s["kind"] == "low"][-4:]

    def labels(pts, up, down):
        return [{"t": b["t"], "price": b["price"], "label": up if b["price"] > a["price"] else down}
                for a, b in zip(pts, pts[1:])]

    hl = labels(highs, "HH", "LH")
    ll = labels(lows, "HL", "LL")
    last_h = hl[-1]["label"] if hl else None
    last_l = ll[-1]["label"] if ll else None
    if last_h == "HH" and last_l == "HL":
        state = "uptrend"
    elif last_h == "LH" and last_l == "LL":
        state = "downtrend"
    elif last_h == "LH" and last_l == "HL":
        state = "contracting (triangle-like)"
    elif last_h == "HH" and last_l == "LL":
        state = "expanding / volatile"
    else:
        state = "undetermined"
    return {
        "state": state,
        "swing_highs": hl,
        "swing_lows": ll,
        "last_swing_high": highs[-1] if highs else None,
        "last_swing_low": lows[-1] if lows else None,
    }


def double_tops_bottoms(df: pd.DataFrame, tolerance_atr: float = 0.6, min_separation: int = 8) -> list[dict]:
    """Double top/bottom candidates among the last few major swings, with neckline and status."""
    if len(df) < 40:
        return []
    atr = float(ind._atr(df, 14).iloc[-1])
    swings = swing_points(df, 5, 5)
    closes = df["c"].to_numpy()
    out = []
    for kind, name in (("high", "double_top"), ("low", "double_bottom")):
        pts = [s for s in swings if s["kind"] == kind][-4:]
        for a, b in zip(pts, pts[1:]):
            if b["i"] - a["i"] < min_separation or abs(a["price"] - b["price"]) > tolerance_atr * atr:
                continue
            seg = df.iloc[a["i"] : b["i"] + 1]
            neck = float(seg["l"].min() if kind == "high" else seg["h"].max())
            depth = abs((a["price"] + b["price"]) / 2 - neck)
            if depth < 1.5 * atr:
                continue
            after = closes[b["i"] + 1 :]
            confirmed = bool((after < neck).any()) if kind == "high" else bool((after > neck).any())
            target = neck - depth if kind == "high" else neck + depth
            out.append({
                "pattern": name, "bias": "bearish" if kind == "high" else "bullish",
                "points": [{"time": a["t"], "price": a["price"]}, {"time": b["t"], "price": b["price"]}],
                "neckline": neck, "confirmed": confirmed, "measured_target": target,
            })
    return out


def consolidation(df: pd.DataFrame, length: int = 20) -> dict | None:
    """Is price in a tight range? Compares the current n-bar range with its own history."""
    if len(df) < length * 5:
        return None
    width = (df["h"].rolling(length).max() - df["l"].rolling(length).min()) / df["c"] * 100
    cur = float(width.iloc[-1])
    pct_rank = float((width.dropna() < cur).mean() * 100)
    return {
        "range_pct": round(cur, 2),
        "range_percentile": round(pct_rank, 1),
        "is_tight": pct_rank < 20,
        "range_high": float(df["h"].iloc[-length:].max()),
        "range_low": float(df["l"].iloc[-length:].min()),
    }


def recent_events(df: pd.DataFrame, last_n: int = 20) -> list[dict]:
    """Notable technical events in the last `last_n` bars, most recent first."""
    if len(df) < 30:
        return []
    c, v = df["c"], df["v"]
    events: list[dict] = []
    start = len(df) - last_n
    idx = df.index

    def cross_events(a: pd.Series, b: pd.Series, up_name: str, down_name: str, bias_up="bullish", bias_down="bearish"):
        above = a > b
        valid = a.notna() & b.notna() & a.shift(1).notna() & b.shift(1).notna()
        up = above & ~above.shift(1, fill_value=False) & valid
        dn = ~above & above.shift(1, fill_value=False) & valid
        for i in np.flatnonzero(up.to_numpy()):
            if i >= start:
                events.append({"t": int(idx[i]), "event": up_name, "bias": bias_up})
        for i in np.flatnonzero(dn.to_numpy()):
            if i >= start:
                events.append({"t": int(idx[i]), "event": down_name, "bias": bias_down})

    s50, s200 = ind.sma(c, 50), ind.sma(c, 200)
    cross_events(s50, s200, "golden_cross (SMA50 above SMA200)", "death_cross (SMA50 below SMA200)")
    cross_events(c, s200, "close crossed above SMA200", "close crossed below SMA200")
    cross_events(c, s50, "close crossed above SMA50", "close crossed below SMA50")
    m = ind.compute(df, "macd")
    cross_events(m["macd"], m["signal"], "MACD bullish signal-line cross", "MACD bearish signal-line cross")
    r = ind.compute(df, "rsi")["rsi"]
    cross_events(r, pd.Series(70.0, index=df.index), "RSI entered overbought (>70)", "RSI fell back below 70", "caution", "bearish")
    cross_events(r, pd.Series(30.0, index=df.index), "RSI recovered above 30", "RSI entered oversold (<30)", "bullish", "caution")
    st = ind.compute(df, "supertrend")["direction"]
    flips = st.diff().fillna(0)
    for i in np.flatnonzero(flips.to_numpy() != 0):
        if i >= start and not np.isnan(st.iloc[i - 1]):
            events.append({"t": int(idx[i]), "event": f"supertrend flipped {'up' if st.iloc[i] > 0 else 'down'}",
                           "bias": "bullish" if st.iloc[i] > 0 else "bearish"})

    prior_high = df["h"].rolling(20).max().shift(1)
    prior_low = df["l"].rolling(20).min().shift(1)
    rvol = v / ind.sma(v, 20).shift(1)
    yr_high = df["h"].rolling(252, min_periods=200).max().shift(1)
    yr_low = df["l"].rolling(252, min_periods=200).min().shift(1)
    for i in range(max(start, 1), len(df)):
        t = int(idx[i])
        rv = rvol.iloc[i]
        if c.iloc[i] > prior_high.iloc[i]:
            events.append({"t": t, "event": "close above prior 20-bar high (breakout)", "bias": "bullish",
                           "rvol": round(float(rv), 2) if np.isfinite(rv) else None})
        if c.iloc[i] < prior_low.iloc[i]:
            events.append({"t": t, "event": "close below prior 20-bar low (breakdown)", "bias": "bearish",
                           "rvol": round(float(rv), 2) if np.isfinite(rv) else None})
        if np.isfinite(yr_high.iloc[i]) and df["h"].iloc[i] > yr_high.iloc[i]:
            events.append({"t": t, "event": "new 52-week high", "bias": "bullish"})
        if np.isfinite(yr_low.iloc[i]) and df["l"].iloc[i] < yr_low.iloc[i]:
            events.append({"t": t, "event": "new 52-week low", "bias": "bearish"})
        gap = df["o"].iloc[i] / c.iloc[i - 1] - 1
        if abs(gap) >= 0.02:
            events.append({"t": t, "event": f"gap {'up' if gap > 0 else 'down'} {gap * 100:+.1f}%",
                           "bias": "bullish" if gap > 0 else "bearish"})
        if np.isfinite(rv) and rv >= 2:
            chg = c.iloc[i] / c.iloc[i - 1] - 1
            events.append({"t": t, "event": f"volume spike {rv:.1f}x average on {chg * 100:+.1f}% move",
                           "bias": "bullish" if chg > 0 else "bearish"})
    sq = ind.compute(df, "squeeze")["squeeze_on"]
    rel = (sq.shift(1) == 1) & (sq == 0)
    for i in np.flatnonzero(rel.to_numpy()):
        if i >= start:
            events.append({"t": int(idx[i]), "event": "volatility squeeze released", "bias": "neutral"})
    return sorted(events, key=lambda e: -e["t"])
