"""Price levels: swing points, clustered support/resistance, pivot points, Fibonacci,
volume profile and auto trendline candidates. All functions take a bar frame (see providers.base)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .indicators import _atr

FIB_RATIOS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
FIB_EXTENSIONS = [1.272, 1.618, 2.0, 2.618]


def swing_points(df: pd.DataFrame, left: int = 5, right: int = 5) -> list[dict]:
    """Fractal swing highs/lows: a bar whose high (low) is the extreme of the surrounding window.
    The last `right` bars cannot be confirmed yet and are never returned."""
    h, l = df["h"].to_numpy(), df["l"].to_numpy()  # noqa: E741
    idx = df.index.to_numpy()
    out = []
    for i in range(left, len(df) - right):
        wh = h[i - left : i + right + 1]
        wl = l[i - left : i + right + 1]
        # strictly greater than bars to the right, >= to the left (one pivot per plateau)
        if h[i] >= wh[:left].max() and h[i] > wh[left + 1 :].max():
            out.append({"t": int(idx[i]), "i": i, "price": float(h[i]), "kind": "high"})
        if l[i] <= wl[:left].min() and l[i] < wl[left + 1 :].min():
            out.append({"t": int(idx[i]), "i": i, "price": float(l[i]), "kind": "low"})
    return out


def support_resistance(df: pd.DataFrame, max_levels: int = 5, tolerance_atr: float = 0.5) -> dict:
    """Cluster swing pivots (minor and major) into horizontal levels.

    Each level reports how many pivots formed it (touches), when it was last touched and its
    distance from the last close (in % and ATR multiples). Levels are split into supports (below
    the last close) and resistances (above), nearest first.
    """
    if len(df) < 20:
        return {"supports": [], "resistances": []}
    atr = float(_atr(df, 14).iloc[-1])
    last = float(df["c"].iloc[-1])
    tol = tolerance_atr * atr if np.isfinite(atr) and atr > 0 else last * 0.01
    pivots = swing_points(df, 3, 3) + swing_points(df, 10, 10)
    if not pivots:
        return {"supports": [], "resistances": []}
    n = len(df)
    pivots.sort(key=lambda p: p["price"])
    clusters: list[list[dict]] = []
    for p in pivots:
        # compare with the cluster mean (not its last member) so clusters cannot chain across a wide band
        if clusters and abs(p["price"] - np.mean([q["price"] for q in clusters[-1]])) <= tol:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    levels = []
    for c in clusters:
        prices = np.array([p["price"] for p in c])
        weights = np.array([0.5 + p["i"] / n for p in c])  # favour recent pivots
        price = float(np.average(prices, weights=weights))
        touches = len({p["t"] for p in c})
        last_i = max(p["i"] for p in c)
        recency = last_i / n
        levels.append({
            "price": price,
            "touches": touches,
            "first_touch": min(p["t"] for p in c),
            "last_touch": max(p["t"] for p in c),
            "strength": round(touches * (0.5 + recency), 2),
            "distance_pct": round((price / last - 1) * 100, 2),
            "distance_atr": round((price - last) / atr, 2) if atr > 0 else None,
        })
    supports = sorted([lv for lv in levels if lv["price"] < last], key=lambda x: -x["price"])
    resistances = sorted([lv for lv in levels if lv["price"] >= last], key=lambda x: x["price"])

    def pick(lvls):
        # nearest levels first, but drop single-touch levels that have a stronger neighbour
        strong = [lv for lv in lvls if lv["touches"] >= 2] or lvls
        return strong[:max_levels]

    return {"supports": pick(supports), "resistances": pick(resistances), "tolerance": tol, "atr": atr}


def pivot_points(prev_high: float, prev_low: float, prev_close: float) -> dict:
    """Classic floor-trader pivots from the previous completed period."""
    p = (prev_high + prev_low + prev_close) / 3
    rng = prev_high - prev_low
    return {
        "P": p,
        "R1": 2 * p - prev_low, "S1": 2 * p - prev_high,
        "R2": p + rng, "S2": p - rng,
        "R3": prev_high + 2 * (p - prev_low), "S3": prev_low - 2 * (prev_high - p),
    }


def period_pivots(daily: pd.DataFrame) -> dict:
    """Daily, weekly and monthly classic pivots from a daily bar frame (uses last *completed* periods)."""
    out = {}
    if len(daily) >= 2:
        prev = daily.iloc[-2]
        out["daily"] = pivot_points(prev.h, prev.l, prev.c)
    dt = pd.to_datetime(daily.index, unit="s", utc=True)
    for name, period in (("weekly", "W"), ("monthly", "M")):
        keys = dt.tz_localize(None).to_period(period)
        g = daily.groupby(keys)
        agg = pd.DataFrame({"h": g["h"].max(), "l": g["l"].min(), "c": g["c"].last()})
        if len(agg) >= 2:
            prev = agg.iloc[-2]
            out[name] = pivot_points(prev.h, prev.l, prev.c)
    return out


def fibonacci(df: pd.DataFrame) -> dict | None:
    """Retracement/extension levels of the dominant swing in the frame (highest high vs lowest low).
    direction 'up' means the low came first (retracements measured down from the high)."""
    if len(df) < 10:
        return None
    hi_i, lo_i = int(df["h"].to_numpy().argmax()), int(df["l"].to_numpy().argmin())
    hi, lo = float(df["h"].iloc[hi_i]), float(df["l"].iloc[lo_i])
    if hi <= lo:
        return None
    up = lo_i < hi_i
    rng = hi - lo
    if up:
        retr = {f"{r:.3f}": hi - r * rng for r in FIB_RATIOS}
        ext = {f"{r:.3f}": lo + r * rng for r in FIB_EXTENSIONS}
    else:
        retr = {f"{r:.3f}": lo + r * rng for r in FIB_RATIOS}
        ext = {f"{r:.3f}": hi - r * rng for r in FIB_EXTENSIONS}
    last = float(df["c"].iloc[-1])
    retraced = (hi - last) / rng if up else (last - lo) / rng
    return {
        "direction": "up" if up else "down",
        "swing_low": {"t": int(df.index[lo_i]), "price": lo},
        "swing_high": {"t": int(df.index[hi_i]), "price": hi},
        "retracements": retr,
        "extensions": ext,
        "current_retracement": round(float(retraced), 3),
    }


def volume_profile(df: pd.DataFrame, bins: int = 30, value_area: float = 0.7) -> dict | None:
    """Volume-at-price histogram. Each bar's volume is spread uniformly over its high-low range.
    Returns point of control (POC), value-area high/low and the high/low volume nodes."""
    if df.empty:
        return None
    lo, hi = float(df["l"].min()), float(df["h"].max())
    if hi <= lo:
        return None
    edges = np.linspace(lo, hi, bins + 1)
    vol = np.zeros(bins)
    for bl, bh, bv in zip(df["l"].to_numpy(), df["h"].to_numpy(), df["v"].to_numpy()):
        if bv <= 0:
            continue
        if bh <= bl:
            vol[min(bins - 1, int((bl - lo) / (hi - lo) * bins))] += bv
            continue
        overlap = np.clip(np.minimum(edges[1:], bh) - np.maximum(edges[:-1], bl), 0, None)
        vol += bv * overlap / (bh - bl)
    centers = (edges[:-1] + edges[1:]) / 2
    poc_i = int(vol.argmax())
    # value area: expand from POC towards the larger neighbour until value_area of volume is enclosed
    total = vol.sum()
    a, b = poc_i, poc_i
    acc = vol[poc_i]
    while acc < value_area * total and (a > 0 or b < bins - 1):
        left = vol[a - 1] if a > 0 else -1
        right = vol[b + 1] if b < bins - 1 else -1
        if right >= left:
            b += 1
            acc += vol[b]
        else:
            a -= 1
            acc += vol[a]
    order = np.argsort(vol)
    return {
        "poc": float(centers[poc_i]),
        "value_area_high": float(edges[b + 1]),
        "value_area_low": float(edges[a]),
        "high_volume_nodes": sorted(float(centers[i]) for i in order[-3:][::-1]),
        "low_volume_nodes": sorted(float(centers[i]) for i in order[:3] if vol[i] < vol.mean() * 0.5),
        "bins": [{"price": float(c), "volume": float(v)} for c, v in zip(centers, vol)],
    }


def trendline_candidates(df: pd.DataFrame, left: int = 5, right: int = 5) -> list[dict]:
    """Lines through the two most recent swing highs (resistance) and swing lows (support).

    For each line: its two anchor points (ready to draw as a trendline annotation), slope in % per
    bar, the projected value at the last bar, how many other pivots touch it (within 0.5 ATR) and
    whether price has since closed through it."""
    swings = swing_points(df, left, right)
    if len(df) < 20 or not swings:
        return []
    atr = float(_atr(df, 14).iloc[-1])
    closes = df["c"].to_numpy()
    n = len(df)
    out = []
    for kind, role in (("high", "resistance"), ("low", "support")):
        pts = [s for s in swings if s["kind"] == kind]
        if len(pts) < 2:
            continue
        p1, p2 = pts[-2], pts[-1]
        if p2["i"] == p1["i"]:
            continue
        slope = (p2["price"] - p1["price"]) / (p2["i"] - p1["i"])
        line_at = lambda i: p1["price"] + slope * (i - p1["i"])  # noqa: E731
        touches = sum(1 for s in pts if abs(s["price"] - line_at(s["i"])) <= 0.5 * atr)
        after = range(p2["i"] + 1, n)
        if role == "resistance":
            broken = any(closes[i] > line_at(i) + 0.25 * atr for i in after)
        else:
            broken = any(closes[i] < line_at(i) - 0.25 * atr for i in after)
        out.append({
            "role": role,
            "points": [{"time": p1["t"], "price": p1["price"]}, {"time": p2["t"], "price": p2["price"]}],
            "slope_pct_per_bar": round(slope / p2["price"] * 100, 4),
            "direction": "rising" if slope > 0 else "falling" if slope < 0 else "flat",
            "value_at_last_bar": float(line_at(n - 1)),
            "touches": touches,
            "broken": bool(broken),
        })
    return out
