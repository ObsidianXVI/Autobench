"""Technical indicator library.

Every indicator is registered with metadata (category, overlay vs. separate pane, parameters
with defaults in positional order, output names) so the web UI, the REST API, the MCP tools
and the strategy-expression engine all share one definition.

Conventions: Wilder smoothing (RMA) for RSI/ATR/ADX, population std-dev for Bollinger Bands,
EMAs seeded with adjust=False. Values are NaN until enough history exists.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from ..timeutil import ET

SOURCES = ("close", "open", "high", "low", "hl2", "hlc3", "ohlc4", "volume")


@dataclass
class IndicatorSpec:
    name: str
    title: str
    category: str  # trend | momentum | volatility | volume | statistics
    overlay: bool  # drawn on the price pane (True) or a separate pane (False)
    params: dict[str, Any]
    outputs: list[str]
    fn: Callable[..., pd.DataFrame | pd.Series]
    description: str
    levels: list[float] = field(default_factory=list)  # reference lines for oscillators

    def warmup(self, params: dict[str, Any]) -> int:
        nums = [v for v in params.values() if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 1]
        base = max(nums, default=1)
        if self.name == "ichimoku":
            base = params["senkou"] + params["kijun"]
        return int(base * 3) + 10

    def describe(self) -> dict:
        return {
            "name": self.name, "title": self.title, "category": self.category, "overlay": self.overlay,
            "params": self.params, "outputs": self.outputs, "description": self.description,
            **({"levels": self.levels} if self.levels else {}),
        }


REGISTRY: dict[str, IndicatorSpec] = {}


def indicator(name: str, title: str, category: str, overlay: bool, params: dict[str, Any], outputs: list[str],
              description: str, levels: list[float] | None = None):
    def deco(fn):
        REGISTRY[name] = IndicatorSpec(name, title, category, overlay, params, outputs, fn, description, levels or [])
        return fn
    return deco


# --- primitives ----------------------------------------------------------------------------


def price_source(df: pd.DataFrame, src: str) -> pd.Series:
    if src == "close":
        return df["c"]
    if src == "open":
        return df["o"]
    if src == "high":
        return df["h"]
    if src == "low":
        return df["l"]
    if src == "volume":
        return df["v"]
    if src == "hl2":
        return (df["h"] + df["l"]) / 2
    if src == "hlc3":
        return (df["h"] + df["l"] + df["c"]) / 3
    if src == "ohlc4":
        return (df["o"] + df["h"] + df["l"] + df["c"]) / 4
    raise ValueError(f"Unknown source '{src}'. Valid: {', '.join(SOURCES)}")


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rma(s: pd.Series, n: int) -> pd.Series:
    """Wilder's moving average, seeded with the SMA of the first n values (as TA-Lib/TradingView)."""
    x = s.to_numpy(dtype=float)
    out = np.full(len(x), np.nan)
    valid = np.flatnonzero(~np.isnan(x))
    if len(valid) == 0:
        return pd.Series(out, index=s.index)
    first = valid[0]
    if len(x) - first < n:
        return pd.Series(out, index=s.index)
    seed_end = first + n - 1
    prev = x[first : seed_end + 1].mean()
    out[seed_end] = prev
    a = 1.0 / n
    for i in range(seed_end + 1, len(x)):
        v = x[i]
        if not np.isnan(v):
            prev = prev + a * (v - prev)
        out[i] = prev
    return pd.Series(out, index=s.index)


def wma(s: pd.Series, n: int) -> pd.Series:
    w = np.arange(1, n + 1, dtype=float)
    return s.rolling(n, min_periods=n).apply(lambda x: float(np.dot(x, w) / w.sum()), raw=True)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["c"].shift(1)
    tr = pd.concat([df["h"] - df["l"], (df["h"] - prev).abs(), (df["l"] - prev).abs()], axis=1).max(axis=1)
    tr.iloc[0] = df["h"].iloc[0] - df["l"].iloc[0] if len(df) else np.nan
    return tr


def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    return rma(true_range(df), n)


def _rsi(s: pd.Series, n: int) -> pd.Series:
    d = s.diff()
    up = rma(d.clip(lower=0), n)
    down = rma((-d).clip(lower=0), n)
    rs = up / down
    out = 100 - 100 / (1 + rs)
    out[(down == 0) & (up > 0)] = 100.0
    out[(down == 0) & (up == 0)] = 50.0
    return out


def _check_int(name: str, v: Any, minimum: int = 1) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v or v < minimum:
        raise ValueError(f"parameter '{name}' must be an integer >= {minimum}")
    return int(v)


# --- trend / moving averages ---------------------------------------------------------------


@indicator("sma", "Simple Moving Average", "trend", True, {"length": 20, "source": "close"}, ["sma"],
           "Arithmetic mean of the last `length` values.")
def _sma_ind(df, length, source="close"):
    return sma(price_source(df, source), _check_int("length", length))


@indicator("ema", "Exponential Moving Average", "trend", True, {"length": 20, "source": "close"}, ["ema"],
           "Exponentially weighted mean, alpha = 2/(length+1).")
def _ema_ind(df, length, source="close"):
    return ema(price_source(df, source), _check_int("length", length))


@indicator("wma", "Weighted Moving Average", "trend", True, {"length": 20, "source": "close"}, ["wma"],
           "Linearly weighted mean (most recent bar weighted highest).")
def _wma_ind(df, length, source="close"):
    return wma(price_source(df, source), _check_int("length", length))


@indicator("hma", "Hull Moving Average", "trend", True, {"length": 20, "source": "close"}, ["hma"],
           "Low-lag MA: WMA(2*WMA(n/2) - WMA(n), sqrt(n)).")
def _hma_ind(df, length, source="close"):
    n = _check_int("length", length, 2)
    s = price_source(df, source)
    return wma(2 * wma(s, max(1, n // 2)) - wma(s, n), max(1, int(math.sqrt(n))))


@indicator("dema", "Double EMA", "trend", True, {"length": 20, "source": "close"}, ["dema"], "2*EMA - EMA(EMA).")
def _dema_ind(df, length, source="close"):
    n = _check_int("length", length)
    e = ema(price_source(df, source), n)
    return 2 * e - ema(e, n)


@indicator("tema", "Triple EMA", "trend", True, {"length": 20, "source": "close"}, ["tema"], "3*E1 - 3*E2 + E3.")
def _tema_ind(df, length, source="close"):
    n = _check_int("length", length)
    e1 = ema(price_source(df, source), n)
    e2 = ema(e1, n)
    return 3 * e1 - 3 * e2 + ema(e2, n)


@indicator("vwma", "Volume-Weighted MA", "trend", True, {"length": 20}, ["vwma"],
           "Rolling sum(close*volume)/sum(volume).")
def _vwma_ind(df, length):
    n = _check_int("length", length)
    return (df["c"] * df["v"]).rolling(n, min_periods=n).sum() / df["v"].rolling(n, min_periods=n).sum()


@indicator("vwap", "VWAP (session)", "volume", True, {}, ["vwap"],
           "Volume-weighted average typical price, reset each trading session. Intraday intervals only; "
           "on daily+ bars use anchored_vwap.")
def _vwap_ind(df):
    tp = (df["h"] + df["l"] + df["c"]) / 3
    days = pd.to_datetime(df.index, unit="s", utc=True).tz_convert(ET).date
    pv = (tp * df["v"]).groupby(days).cumsum()
    vv = df["v"].groupby(days).cumsum()
    return pv / vv.replace(0, np.nan)


@indicator("anchored_vwap", "Anchored VWAP", "volume", True, {"anchor": ""}, ["avwap"],
           "VWAP accumulated from an anchor time (ISO date/datetime or epoch). Empty anchor = first bar. "
           "Anchor at a major high/low, gap or earnings date to see the average cost of participants since then.")
def _avwap_ind(df, anchor=""):
    from ..timeutil import parse_time

    tp = (df["h"] + df["l"] + df["c"]) / 3
    t0 = parse_time(anchor) if anchor else (int(df.index[0]) if len(df) else 0)
    mask = df.index >= t0
    pv = (tp * df["v"]).where(mask).cumsum()
    vv = df["v"].where(mask).cumsum()
    return (pv / vv.replace(0, np.nan)).where(mask)


@indicator("supertrend", "Supertrend", "trend", True, {"length": 10, "multiplier": 3.0},
           ["supertrend", "direction"],
           "ATR trailing stop that flips with trend. direction = 1 (uptrend, line below price) or -1 (downtrend).")
def _supertrend_ind(df, length, multiplier):
    n = _check_int("length", length)
    atr = _atr(df, n).to_numpy()
    hl2 = ((df["h"] + df["l"]) / 2).to_numpy()
    close = df["c"].to_numpy()
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr
    st = np.full(len(df), np.nan)
    direction = np.full(len(df), np.nan)
    fu, fl = np.nan, np.nan
    d = 1
    for i in range(len(df)):
        if np.isnan(atr[i]):
            continue
        if np.isnan(fu):
            fu, fl = upper[i], lower[i]
        else:
            fu = upper[i] if (upper[i] < fu or close[i - 1] > fu) else fu
            fl = lower[i] if (lower[i] > fl or close[i - 1] < fl) else fl
        if d == 1 and close[i] < fl:
            d = -1
        elif d == -1 and close[i] > fu:
            d = 1
        st[i] = fl if d == 1 else fu
        direction[i] = d
    return pd.DataFrame({"supertrend": st, "direction": direction}, index=df.index)


@indicator("psar", "Parabolic SAR", "trend", True, {"step": 0.02, "max_step": 0.2}, ["psar", "direction"],
           "Wilder's stop-and-reverse. direction 1 = long (SAR below price), -1 = short.")
def _psar_ind(df, step, max_step):
    h, l = df["h"].to_numpy(), df["l"].to_numpy()  # noqa: E741
    n = len(df)
    sar = np.full(n, np.nan)
    dirn = np.full(n, np.nan)
    if n < 2:
        return pd.DataFrame({"psar": sar, "direction": dirn}, index=df.index)
    up = h[1] >= h[0]
    ep = h[1] if up else l[1]
    s = l[0] if up else h[0]
    af = step
    for i in range(1, n):
        s = s + af * (ep - s)
        if up:
            s = min(s, l[i - 1], l[i - 2] if i >= 2 else l[i - 1])
            if l[i] < s:
                up, s, ep, af = False, ep, l[i], step
            elif h[i] > ep:
                ep, af = h[i], min(af + step, max_step)
        else:
            s = max(s, h[i - 1], h[i - 2] if i >= 2 else h[i - 1])
            if h[i] > s:
                up, s, ep, af = True, ep, h[i], step
            elif l[i] < ep:
                ep, af = l[i], min(af + step, max_step)
        sar[i] = s
        dirn[i] = 1 if up else -1
    return pd.DataFrame({"psar": sar, "direction": dirn}, index=df.index)


@indicator("ichimoku", "Ichimoku Cloud", "trend", True, {"tenkan": 9, "kijun": 26, "senkou": 52},
           ["tenkan", "kijun", "senkou_a", "senkou_b", "chikou"],
           "Tenkan/Kijun midlines; senkou spans are shifted forward by `kijun` bars so the value at bar t is "
           "the cloud plotted at t; chikou is close shifted back by `kijun` (NaN for the last `kijun` bars).")
def _ichimoku_ind(df, tenkan, kijun, senkou):
    t, k, s = _check_int("tenkan", tenkan), _check_int("kijun", kijun), _check_int("senkou", senkou)
    mid = lambda n: (df["h"].rolling(n, min_periods=n).max() + df["l"].rolling(n, min_periods=n).min()) / 2  # noqa: E731
    ten, kij = mid(t), mid(k)
    return pd.DataFrame({
        "tenkan": ten, "kijun": kij,
        "senkou_a": ((ten + kij) / 2).shift(k), "senkou_b": mid(s).shift(k),
        "chikou": df["c"].shift(-k),
    }, index=df.index)


@indicator("linreg", "Linear Regression", "statistics", True, {"length": 50, "source": "close"},
           ["linreg", "slope_pct", "r2"],
           "Rolling least-squares fit: end-point value, slope as % of price per bar, and R² (trend quality 0..1).")
def _linreg_ind(df, length, source="close"):
    n = _check_int("length", length, 3)
    s = price_source(df, source)
    x = np.arange(n, dtype=float)
    xm = x.mean()
    sxx = ((x - xm) ** 2).sum()

    def fit(y):
        ym = y.mean()
        slope = ((x - xm) * (y - ym)).sum() / sxx
        val = ym + slope * (x[-1] - xm)
        ss_tot = ((y - ym) ** 2).sum()
        ss_res = ((y - (ym + slope * (x - xm))) ** 2).sum()
        return val, slope, (1 - ss_res / ss_tot) if ss_tot > 0 else 0.0

    vals = np.full((len(s), 3), np.nan)
    arr = s.to_numpy(dtype=float)
    for i in range(n - 1, len(arr)):
        w = arr[i - n + 1 : i + 1]
        if not np.isnan(w).any():
            vals[i] = fit(w)
    out = pd.DataFrame(vals, index=df.index, columns=["linreg", "slope", "r2"])
    out["slope_pct"] = out["slope"] / out["linreg"] * 100
    return out[["linreg", "slope_pct", "r2"]]


# --- volatility / channels -----------------------------------------------------------------


@indicator("bbands", "Bollinger Bands", "volatility", True, {"length": 20, "std": 2.0, "source": "close"},
           ["upper", "middle", "lower", "bandwidth", "percent_b"],
           "SMA ± std·σ. bandwidth = (upper-lower)/middle·100 (low = squeeze); percent_b = position within bands.")
def _bbands_ind(df, length, std, source="close"):
    n = _check_int("length", length, 2)
    s = price_source(df, source)
    mid = sma(s, n)
    sd = s.rolling(n, min_periods=n).std(ddof=0)
    up, lo = mid + std * sd, mid - std * sd
    return pd.DataFrame({
        "upper": up, "middle": mid, "lower": lo,
        "bandwidth": (up - lo) / mid * 100, "percent_b": (s - lo) / (up - lo).replace(0, np.nan),
    }, index=df.index)


@indicator("keltner", "Keltner Channels", "volatility", True, {"length": 20, "atr_length": 10, "multiplier": 2.0},
           ["upper", "middle", "lower"], "EMA(close) ± multiplier·ATR.")
def _keltner_ind(df, length, atr_length, multiplier):
    mid = ema(df["c"], _check_int("length", length))
    a = _atr(df, _check_int("atr_length", atr_length))
    return pd.DataFrame({"upper": mid + multiplier * a, "middle": mid, "lower": mid - multiplier * a}, index=df.index)


@indicator("donchian", "Donchian Channels", "volatility", True, {"length": 20}, ["upper", "middle", "lower"],
           "Highest high / lowest low of the last `length` bars (includes the current bar).")
def _donchian_ind(df, length):
    n = _check_int("length", length)
    up = df["h"].rolling(n, min_periods=n).max()
    lo = df["l"].rolling(n, min_periods=n).min()
    return pd.DataFrame({"upper": up, "middle": (up + lo) / 2, "lower": lo}, index=df.index)


@indicator("atr", "Average True Range", "volatility", False, {"length": 14}, ["atr"],
           "Wilder-smoothed true range, in price units. Use for stops and position sizing.")
def _atr_ind(df, length):
    return _atr(df, _check_int("length", length))


@indicator("natr", "Normalized ATR", "volatility", False, {"length": 14}, ["natr"], "ATR as % of close.")
def _natr_ind(df, length):
    return _atr(df, _check_int("length", length)) / df["c"] * 100


@indicator("hv", "Historical Volatility", "volatility", False, {"length": 20, "bars_per_year": 252}, ["hv"],
           "Annualized std-dev of log returns, in %. Set bars_per_year for intraday (e.g. 1h ≈ 1764).")
def _hv_ind(df, length, bars_per_year=252):
    n = _check_int("length", length, 2)
    r = np.log(df["c"] / df["c"].shift(1))
    return r.rolling(n, min_periods=n).std() * math.sqrt(bars_per_year) * 100


@indicator("squeeze", "TTM Squeeze", "volatility", False,
           {"length": 20, "bb_std": 2.0, "kc_multiplier": 1.5}, ["squeeze_on", "momentum"],
           "squeeze_on = 1 when Bollinger Bands are inside Keltner Channels (volatility compression, often "
           "precedes expansion). momentum = linear-regression of price minus the channel mean; sign gives direction.")
def _squeeze_ind(df, length, bb_std, kc_multiplier):
    n = _check_int("length", length, 2)
    bb = _bbands_ind(df, n, bb_std)
    mid = sma(df["c"], n)
    a = _atr(df, n)
    kc_up, kc_lo = mid + kc_multiplier * a, mid - kc_multiplier * a
    on = ((bb["upper"] < kc_up) & (bb["lower"] > kc_lo)).astype(float).where(a.notna() & bb["upper"].notna())
    hh = df["h"].rolling(n, min_periods=n).max()
    ll = df["l"].rolling(n, min_periods=n).min()
    delta = df["c"] - ((hh + ll) / 2 + mid) / 2
    mom = _linreg_ind(pd.DataFrame({"c": delta, "o": delta, "h": delta, "l": delta, "v": 0}, index=df.index), n)["linreg"]
    return pd.DataFrame({"squeeze_on": on, "momentum": mom}, index=df.index)


@indicator("stddev", "Standard Deviation", "statistics", False, {"length": 20, "source": "close"}, ["stddev"],
           "Rolling population standard deviation, price units.")
def _stddev_ind(df, length, source="close"):
    n = _check_int("length", length, 2)
    return price_source(df, source).rolling(n, min_periods=n).std(ddof=0)


@indicator("zscore", "Z-Score", "statistics", False, {"length": 20, "source": "close"}, ["zscore"],
           "(value - SMA) / σ over `length` bars. |z| > 2 is statistically stretched.", [-2, 0, 2])
def _zscore_ind(df, length, source="close"):
    n = _check_int("length", length, 2)
    s = price_source(df, source)
    return (s - sma(s, n)) / s.rolling(n, min_periods=n).std(ddof=0).replace(0, np.nan)


# --- momentum ------------------------------------------------------------------------------


@indicator("rsi", "Relative Strength Index", "momentum", False, {"length": 14, "source": "close"}, ["rsi"],
           "Wilder RSI, 0-100. >70 overbought, <30 oversold; in strong trends 40-80 / 20-60 ranges are typical.",
           [30, 50, 70])
def _rsi_ind(df, length, source="close"):
    return _rsi(price_source(df, source), _check_int("length", length))


@indicator("stoch", "Stochastic Oscillator", "momentum", False, {"k": 14, "d": 3, "smooth": 3}, ["k", "d"],
           "Slow stochastic: %K = SMA(smooth) of close position within the k-bar range; %D = SMA(d) of %K.",
           [20, 80])
def _stoch_ind(df, k, d, smooth):
    kn, dn, sn = _check_int("k", k), _check_int("d", d), _check_int("smooth", smooth)
    hh = df["h"].rolling(kn, min_periods=kn).max()
    ll = df["l"].rolling(kn, min_periods=kn).min()
    raw = (df["c"] - ll) / (hh - ll).replace(0, np.nan) * 100
    kk = sma(raw, sn)
    return pd.DataFrame({"k": kk, "d": sma(kk, dn)}, index=df.index)


@indicator("stochrsi", "Stochastic RSI", "momentum", False, {"length": 14, "rsi_length": 14, "k": 3, "d": 3},
           ["k", "d"], "Stochastic applied to RSI, 0-100.", [20, 80])
def _stochrsi_ind(df, length, rsi_length, k, d):
    n = _check_int("length", length)
    r = _rsi(df["c"], _check_int("rsi_length", rsi_length))
    lo, hi = r.rolling(n, min_periods=n).min(), r.rolling(n, min_periods=n).max()
    raw = (r - lo) / (hi - lo).replace(0, np.nan) * 100
    kk = sma(raw, _check_int("k", k))
    return pd.DataFrame({"k": kk, "d": sma(kk, _check_int("d", d))}, index=df.index)


@indicator("macd", "MACD", "momentum", False, {"fast": 12, "slow": 26, "signal": 9},
           ["macd", "signal", "histogram"], "EMA(fast) - EMA(slow); signal = EMA of MACD; histogram = macd - signal.",
           [0])
def _macd_ind(df, fast, slow, signal):
    f, s, g = _check_int("fast", fast), _check_int("slow", slow), _check_int("signal", signal)
    m = ema(df["c"], f) - ema(df["c"], s)
    sig = ema(m, g)
    return pd.DataFrame({"macd": m, "signal": sig, "histogram": m - sig}, index=df.index)


@indicator("ppo", "Percentage Price Oscillator", "momentum", False, {"fast": 12, "slow": 26, "signal": 9},
           ["ppo", "signal", "histogram"], "MACD expressed as % of the slow EMA (comparable across symbols).", [0])
def _ppo_ind(df, fast, slow, signal):
    f, s, g = _check_int("fast", fast), _check_int("slow", slow), _check_int("signal", signal)
    slow_e = ema(df["c"], s)
    p = (ema(df["c"], f) - slow_e) / slow_e * 100
    sig = ema(p, g)
    return pd.DataFrame({"ppo": p, "signal": sig, "histogram": p - sig}, index=df.index)


@indicator("cci", "Commodity Channel Index", "momentum", False, {"length": 20}, ["cci"],
           "(TP - SMA(TP)) / (0.015·mean deviation). ±100 thresholds.", [-100, 0, 100])
def _cci_ind(df, length):
    n = _check_int("length", length)
    tp = (df["h"] + df["l"] + df["c"]) / 3
    m = sma(tp, n)
    md = tp.rolling(n, min_periods=n).apply(lambda x: float(np.mean(np.abs(x - x.mean()))), raw=True)
    return (tp - m) / (0.015 * md.replace(0, np.nan))


@indicator("willr", "Williams %R", "momentum", False, {"length": 14}, ["willr"],
           "Close position in the n-bar range, -100..0. Above -20 overbought, below -80 oversold.", [-80, -20])
def _willr_ind(df, length):
    n = _check_int("length", length)
    hh = df["h"].rolling(n, min_periods=n).max()
    ll = df["l"].rolling(n, min_periods=n).min()
    return (hh - df["c"]) / (hh - ll).replace(0, np.nan) * -100


@indicator("roc", "Rate of Change", "momentum", False, {"length": 10, "source": "close"}, ["roc"],
           "% change over `length` bars.", [0])
def _roc_ind(df, length, source="close"):
    n = _check_int("length", length)
    s = price_source(df, source)
    return (s / s.shift(n) - 1) * 100


@indicator("mom", "Momentum", "momentum", False, {"length": 10}, ["mom"], "close - close[length].", [0])
def _mom_ind(df, length):
    return df["c"] - df["c"].shift(_check_int("length", length))


@indicator("trix", "TRIX", "momentum", False, {"length": 15}, ["trix"],
           "1-bar % change of a triple-smoothed EMA (filters noise).", [0])
def _trix_ind(df, length):
    n = _check_int("length", length)
    e3 = ema(ema(ema(df["c"], n), n), n)
    return (e3 / e3.shift(1) - 1) * 100


@indicator("adx", "Average Directional Index", "trend", False, {"length": 14}, ["adx", "plus_di", "minus_di"],
           "Trend strength (not direction). ADX > 25 trending, < 20 ranging. +DI > -DI = bullish pressure.",
           [20, 25])
def _adx_ind(df, length):
    n = _check_int("length", length)
    up = df["h"].diff()
    down = -df["l"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    atr = _atr(df, n)
    pdi = 100 * rma(plus_dm, n) / atr.replace(0, np.nan)
    mdi = 100 * rma(minus_dm, n) / atr.replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return pd.DataFrame({"adx": rma(dx, n), "plus_di": pdi, "minus_di": mdi}, index=df.index)


@indicator("aroon", "Aroon", "trend", False, {"length": 25}, ["up", "down", "oscillator"],
           "Bars since n-bar high/low scaled 0-100. up > 70 with down < 30 = strong uptrend.", [30, 70])
def _aroon_ind(df, length):
    n = _check_int("length", length)
    # bars since the most recent n-bar high/low (reverse so argmax picks the latest occurrence)
    since_high = df["h"].rolling(n + 1, min_periods=n + 1).apply(lambda x: int(np.argmax(x[::-1])), raw=True)
    since_low = df["l"].rolling(n + 1, min_periods=n + 1).apply(lambda x: int(np.argmin(x[::-1])), raw=True)
    up = 100 * (n - since_high) / n
    dn = 100 * (n - since_low) / n
    return pd.DataFrame({"up": up, "down": dn, "oscillator": up - dn}, index=df.index)


# --- volume --------------------------------------------------------------------------------


@indicator("obv", "On-Balance Volume", "volume", False, {}, ["obv"],
           "Cumulative volume signed by close direction. Divergence vs price flags weak moves.")
def _obv_ind(df):
    sign = np.sign(df["c"].diff()).fillna(0)
    return (sign * df["v"]).cumsum()


@indicator("ad", "Accumulation/Distribution", "volume", False, {}, ["ad"],
           "Cumulative money-flow volume (close location in range × volume).")
def _ad_ind(df):
    rng = (df["h"] - df["l"]).replace(0, np.nan)
    mfm = ((df["c"] - df["l"]) - (df["h"] - df["c"])) / rng
    return (mfm.fillna(0) * df["v"]).cumsum()


@indicator("cmf", "Chaikin Money Flow", "volume", False, {"length": 20}, ["cmf"],
           "Sum of money-flow volume / sum of volume over n bars, -1..1. >0 accumulation.", [0])
def _cmf_ind(df, length):
    n = _check_int("length", length)
    rng = (df["h"] - df["l"]).replace(0, np.nan)
    mfv = (((df["c"] - df["l"]) - (df["h"] - df["c"])) / rng).fillna(0) * df["v"]
    return mfv.rolling(n, min_periods=n).sum() / df["v"].rolling(n, min_periods=n).sum().replace(0, np.nan)


@indicator("mfi", "Money Flow Index", "volume", False, {"length": 14}, ["mfi"],
           "Volume-weighted RSI of typical price, 0-100. >80 overbought, <20 oversold.", [20, 80])
def _mfi_ind(df, length):
    n = _check_int("length", length)
    tp = (df["h"] + df["l"] + df["c"]) / 3
    mf = tp * df["v"]
    d = tp.diff()
    pos = mf.where(d > 0, 0.0).rolling(n, min_periods=n).sum()
    neg = mf.where(d < 0, 0.0).rolling(n, min_periods=n).sum()
    out = 100 - 100 / (1 + pos / neg.replace(0, np.nan))
    return out.where(neg != 0, 100.0).where(pos.notna())


@indicator("volume_sma", "Volume SMA", "volume", False, {"length": 20}, ["volume_sma"], "Average volume over n bars.")
def _vol_sma_ind(df, length):
    return sma(df["v"], _check_int("length", length))


@indicator("rvol", "Relative Volume", "volume", False, {"length": 20}, ["rvol"],
           "Volume / average volume of the *previous* n bars. >1.5 = unusual participation.", [1])
def _rvol_ind(df, length):
    n = _check_int("length", length)
    return df["v"] / sma(df["v"], n).shift(1).replace(0, np.nan)


@indicator("force", "Force Index", "volume", False, {"length": 13}, ["force"],
           "EMA of (close change × volume): combines direction, magnitude and participation.", [0])
def _force_ind(df, length):
    return ema(df["c"].diff() * df["v"], _check_int("length", length))


# --- public API ----------------------------------------------------------------------------


def list_indicators() -> list[dict]:
    return [s.describe() for s in REGISTRY.values()]


def get_spec(name: str) -> IndicatorSpec:
    key = name.strip().lower()
    if key not in REGISTRY:
        raise ValueError(f"Unknown indicator '{name}'. Use list_indicators to see the {len(REGISTRY)} available.")
    return REGISTRY[key]


def resolve_params(spec: IndicatorSpec, params: dict[str, Any] | None = None, args: list[Any] | None = None) -> dict:
    """Merge defaults with positional args (in declared order) and keyword params, validating names."""
    out = dict(spec.params)
    names = list(spec.params)
    for i, a in enumerate(args or []):
        if i >= len(names):
            raise ValueError(f"{spec.name} takes at most {len(names)} parameters ({', '.join(names)})")
        out[names[i]] = a
    for k, v in (params or {}).items():
        if k not in out:
            raise ValueError(f"{spec.name} has no parameter '{k}'. Parameters: {', '.join(names) or 'none'}")
        out[k] = v
    for k, v in out.items():
        default = spec.params[k]
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            try:
                out[k] = float(v) if isinstance(default, float) else (int(v) if float(v) == int(float(v)) else float(v))
            except (TypeError, ValueError) as e:
                raise ValueError(f"{spec.name}.{k} must be numeric") from e
    return out


def label(spec: IndicatorSpec, params: dict) -> str:
    vals = [str(v) for k, v in params.items() if v != "" and not (k == "source" and v == "close")]
    return f"{spec.name}({','.join(vals)})" if vals else spec.name


def compute(df: pd.DataFrame, name: str, params: dict[str, Any] | None = None, args: list[Any] | None = None) -> pd.DataFrame:
    """Compute one indicator over a bar frame. Returns a DataFrame whose columns are the outputs."""
    spec = get_spec(name)
    p = resolve_params(spec, params, args)
    if "source" in p and p["source"] not in SOURCES:
        raise ValueError(f"Unknown source '{p['source']}'. Valid: {', '.join(SOURCES)}")
    if df.empty:
        return pd.DataFrame(columns=spec.outputs, index=df.index, dtype=float)
    res = spec.fn(df, **p)
    if isinstance(res, pd.Series):
        res = res.to_frame(spec.outputs[0])
    return res[spec.outputs].astype(float).replace([np.inf, -np.inf], np.nan)
