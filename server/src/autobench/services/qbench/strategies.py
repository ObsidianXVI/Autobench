"""The QBench strategy library: published, well-studied quant strategies with their evidence.

Every entry carries what an agent or trader needs to judge it: the rules, the paper(s), how strong
the evidence is and how much it has decayed since publication, when it works and when it fails, and
how to run it in Autobench.

kind:
  single           runs on one symbol's daily bars; the Strategy Lab tests and walk-forward-optimises it
  cross_sectional  ranks a list of stocks (watchlist / universe): run_factor_backtest, factor_screen
  pairs            two legs, needs a short: find_pairs / analyze_pair
  portfolio        allocation or hedging overlay: optimize_allocation / get_hedge_plan
  reference        knowledge only (needs data Autobench does not have, or a warning about a myth)

Single-asset position functions return the target exposure decided at bar t's close (data up to t only),
held over bar t+1. Long-only by default (the user's account usually cannot short); the lab clips
negative exposure unless allow_short=True."""

from __future__ import annotations

import itertools
import math
from dataclasses import asdict, dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from .. import indicators as ind


@dataclass
class Strategy:
    id: str
    name: str
    family: str  # trend | momentum | mean_reversion | volatility | seasonality | factor | stat_arb | allocation | hedging | event
    kind: str  # single | cross_sectional | pairs | portfolio | reference
    summary: str
    rules: str
    references: list[str]
    evidence: str  # strong | moderate | weak | contested | myth
    evidence_note: str
    works_when: str
    fails_when: str
    horizon: str
    params: dict = field(default_factory=dict)
    grid: dict = field(default_factory=dict)
    expression: dict | None = None  # Autobench expression equivalent (run_backtest / run_signal_study)
    run_with: str = ""
    fn: Callable | None = None

    def combos(self) -> list[dict]:
        if not self.grid:
            return [dict(self.params)]
        keys = list(self.grid)
        return [{**self.params, **dict(zip(keys, vals))} for vals in itertools.product(*(self.grid[k] for k in keys))]

    def describe(self, full: bool = True) -> dict:
        d = asdict(self)
        d.pop("fn")
        if not full:
            return {k: d[k] for k in ("id", "name", "family", "kind", "summary", "evidence", "horizon")}
        d["combinations"] = len(self.combos()) if self.kind == "single" else None
        return d


# ----------------------------------------------------------------------------- helpers


def _hold(entry: np.ndarray, exit_: np.ndarray) -> np.ndarray:
    """Long from an entry bar until (and including) the first exit bar after it."""
    pos = np.zeros(len(entry))
    on = False
    for i in range(len(entry)):
        if on and exit_[i]:
            on = False
        elif not on and entry[i]:
            on = True
        pos[i] = 1.0 if on else 0.0
    return pos


def _vol(c: pd.Series, n: int) -> pd.Series:
    return np.log(c).diff().rolling(n, min_periods=max(5, n // 2)).std() * math.sqrt(252)


def _nan0(x) -> np.ndarray:
    a = np.asarray(x, float)
    return np.where(np.isfinite(a), a, 0.0)


def ibs(df: pd.DataFrame) -> pd.Series:
    rng = (df["h"] - df["l"]).replace(0, np.nan)
    return ((df["c"] - df["l"]) / rng).clip(0, 1)


# ----------------------------------------------------------------------------- single-asset strategies


def _tsmom(df, bench=None, lookback=252, skip=21, vol_target=0.0, max_leverage=1.0):
    c = df["c"]
    past = c.shift(skip) / c.shift(lookback) - 1
    sig = np.sign(past)
    if vol_target and vol_target > 0:
        scale = (vol_target / _vol(c, 63)).clip(upper=max_leverage)
        sig = sig * scale
    return _nan0(sig.clip(-max_leverage, max_leverage))


def _ma_trend(df, bench=None, length=200):
    c = df["c"]
    return _nan0((c > ind.sma(c, length)).astype(float).where(ind.sma(c, length).notna()))


def _ma_cross(df, bench=None, fast=50, slow=200):
    c = df["c"]
    f, s = ind.sma(c, fast), ind.sma(c, slow)
    return _nan0((f > s).astype(float).where(s.notna()))


def _donchian(df, bench=None, entry=55, exit=20):
    hi = df["h"].rolling(entry).max().shift(1)
    lo = df["l"].rolling(exit).min().shift(1)
    return _hold((df["c"] > hi).to_numpy(), (df["c"] < lo).to_numpy())


def _rsi2(df, bench=None, threshold=10, trend=200, exit_ma=5):
    c = df["c"]
    r = ind.compute(df, "rsi", {"length": 2})["rsi"]
    up = c > ind.sma(c, trend) if trend else pd.Series(True, index=c.index)
    return _hold(((r < threshold) & up).to_numpy(), (c > ind.sma(c, exit_ma)).to_numpy())


def _ibs(df, bench=None, threshold=0.2, trend=0):
    x = ibs(df)
    cond = x < threshold
    if trend:
        cond &= df["c"] > ind.sma(df["c"], trend)
    return _nan0(cond.astype(float))


def _bb_revert(df, bench=None, length=20, z=2.0, trend=0):
    c = df["c"]
    zs = (c - ind.sma(c, length)) / c.rolling(length).std(ddof=0)
    entry = zs < -z
    if trend:
        entry &= c > ind.sma(c, trend)
    return _hold(entry.to_numpy(), (zs > 0).to_numpy())


def _vol_target(df, bench=None, window=21, target=0.20, max_leverage=1.0):
    return _nan0((target / _vol(df["c"], window)).clip(upper=max_leverage))


def _high52(df, bench=None, near=0.05, window=252):
    c = df["c"]
    hi = df["h"].rolling(window, min_periods=window // 2).max()
    ratio = c / hi
    return _hold((ratio >= 1 - near).to_numpy(), (ratio < 1 - 2 * near).to_numpy())


def _month_windows(index: pd.Index, last_n: int, first_m: int) -> np.ndarray:
    """1 where the NEXT bar falls in the turn-of-month window (last_n trading days of a month or first_m of the next)."""
    d = pd.to_datetime(np.asarray(index, dtype="int64"), unit="s")
    nxt = pd.bdate_range(d[-1] + pd.Timedelta(days=1), periods=1)
    d_ext = d.append(nxt)
    ym = d_ext.year * 12 + d_ext.month
    s = pd.Series(1, index=range(len(d_ext)))
    first_rank = s.groupby(ym).cumcount() + 1
    last_rank = s[::-1].groupby(ym[::-1]).cumcount()[::-1] + 1
    # the final (synthetic) month is incomplete: estimate its remaining business days from the calendar
    last_month = ym[-1]
    idx_last = np.flatnonzero(ym == last_month)
    month_end = (d_ext[-1] + pd.offsets.BMonthEnd(0))
    rem = len(pd.bdate_range(d_ext[-1], month_end)) - 1
    for k, i in enumerate(idx_last[::-1]):
        last_rank.iloc[i] = rem + 1 + k
    window = ((last_rank.to_numpy() <= last_n) | (first_rank.to_numpy() <= first_m)).astype(float)
    return window[1:]  # position at t = window at t+1


def _turn_of_month(df, bench=None, last_n=1, first_m=3):
    return _month_windows(df.index, last_n, first_m)


def _dual_momentum(df, bench=None, lookback=252):
    c = df["c"]
    own = c / c.shift(lookback) - 1
    if bench is None:
        return _nan0((own > 0).astype(float).where(own.notna()))
    b = bench.reindex(c.index).ffill()
    rel = b / b.shift(lookback) - 1
    return _nan0(((own > 0) & (own > rel)).astype(float).where(own.notna() & rel.notna()))


def _er_switch(df, bench=None, length=20, threshold=0.35):
    """Regime switch on Kaufman's efficiency ratio: trend-follow when price moves efficiently,
    buy short-term dips (RSI(2)) when it is choppy."""
    c = df["c"]
    er = (c - c.shift(length)).abs() / c.diff().abs().rolling(length).sum()
    trend = (er > threshold) & (c > ind.ema(c, length)) & (c > c.shift(length))
    r2 = ind.compute(df, "rsi", {"length": 2})["rsi"]
    revert_entry = ((er <= threshold) & (r2 < 10) & (c > ind.sma(c, 200))).to_numpy()
    revert = _hold(revert_entry, (c > ind.sma(c, 5)).to_numpy())
    return np.maximum(_nan0(trend.astype(float)), revert)


# ----------------------------------------------------------------------------- the library

LIBRARY: list[Strategy] = [
    # ---------------------------------------------------------------- trend / momentum (single)
    Strategy(
        "tsmom", "Time-series momentum", "trend", "single",
        "Own past 3-12 month return predicts the next month's sign; size by inverse volatility.",
        "Long when the return from t-lookback to t-skip is positive (short when negative, if allowed). "
        "Optional volatility targeting: exposure = target vol / 63-day realised vol, capped.",
        ["Moskowitz, Ooi & Pedersen (2012) 'Time Series Momentum', JFE",
         "Hurst, Ooi & Pedersen (2017) 'A Century of Evidence on Trend-Following Investing'",
         "Kim, Tse & Wald (2016) 'Time series momentum and volatility scaling', JFM",
         "Lim, Wang & Yao (2018) 'Time-series momentum in nearly 100 years of stock returns', JBF"],
        "strong", "Robust across asset classes and a century of data, mostly as a diversified futures strategy. "
        "Kim-Tse-Wald show much of the published alpha comes from volatility scaling; on single stocks the edge is "
        "weaker and noisier. Expect long flat stretches and sharp losses at trend reversals (e.g. 2009 rebound).",
        "Persistent macro or sector trends; rising dispersion; crisis periods (long-vol-like payoff).",
        "Choppy, range-bound markets and V-shaped reversals; whipsaw costs on volatile single names.",
        "weeks to months", {"lookback": 252, "skip": 21, "vol_target": 0.0, "max_leverage": 1.0},
        {"lookback": [63, 126, 252], "vol_target": [0.0, 0.25]},
        {"entry": "prev(close, 21) > prev(close, 252)", "exit": "prev(close, 21) < prev(close, 252)"},
        "run_quant_strategy / run_strategy_lab", _tsmom),
    Strategy(
        "ma_trend", "Moving-average trend filter", "trend", "single",
        "Hold while price is above its N-day moving average (Faber's 10-month rule on daily bars).",
        "Long when close > SMA(length); flat otherwise.",
        ["Faber (2007) 'A Quantitative Approach to Tactical Asset Allocation', JWM",
         "Brock, Lakonishok & LeBaron (1992) 'Simple Technical Trading Rules', JF"],
        "moderate", "Mainly reduces drawdowns rather than raising returns; on indices it historically cut "
        "max drawdown roughly in half at a small CAGR cost. Single stocks whipsaw more. BLL's predictive power "
        "faded after the early 1990s in US large caps.",
        "Long bear markets (2000-02, 2008); slow-moving secular trends.",
        "Sideways markets with frequent crossings; fast crashes that recover quickly (2020).",
        "months", {"length": 200}, {"length": [50, 100, 150, 200]},
        {"entry": "crosses_above(close, sma(200))", "exit": "crosses_below(close, sma(200))"},
        "run_quant_strategy / run_backtest", _ma_trend),
    Strategy(
        "ma_cross", "Dual moving-average crossover", "trend", "single",
        "Long while the fast average is above the slow one (e.g. golden cross 50/200).",
        "Long when SMA(fast) > SMA(slow).",
        ["Brock, Lakonishok & LeBaron (1992)", "Zakamulin (2017) 'Market Timing with Moving Averages'"],
        "weak", "Lags price; mostly equivalent to a slower single-MA filter. Golden-cross 'signals' have little "
        "forward-return edge on their own in modern data; the value is risk control.",
        "Long trends.", "Ranges; late entries after big moves.", "months",
        {"fast": 50, "slow": 200}, {"fast": [20, 50], "slow": [100, 200]},
        {"entry": "crosses_above(sma(50), sma(200))", "exit": "crosses_below(sma(50), sma(200))"},
        "run_quant_strategy / run_backtest", _ma_cross),
    Strategy(
        "donchian", "Donchian channel breakout (Turtle)", "trend", "single",
        "Buy a close above the N-day high, exit on a close below the M-day low.",
        "Entry: close > highest(high, entry) of prior bars. Exit: close < lowest(low, exit) of prior bars.",
        ["Donchian (1960s); Faith (2007) 'Way of the Turtle'",
         "Clenow (2013) 'Following the Trend'"],
        "moderate", "The canonical CTA rule; works on diversified futures portfolios. Single stocks show lower "
        "win rates (often < 40%) with a positive skew of payoffs; profitability depends on a few big trends.",
        "Breakouts from long bases with volume; strong sector rotation.",
        "Failed breakouts in range-bound names; gap-downs through the exit.", "weeks to months",
        {"entry": 55, "exit": 20}, {"entry": [20, 55], "exit": [10, 20]},
        {"entry": "close > highest(high, 55, 1)", "exit": "close < lowest(low, 20, 1)"},
        "run_quant_strategy / run_backtest", _donchian),
    Strategy(
        "high52", "52-week-high proximity", "momentum", "single",
        "Stocks near their 52-week high keep outperforming: investors anchor on the high and under-react.",
        "Long when close ≥ (1 - near) × 52-week high; exit when it falls below (1 - 2·near) × high.",
        ["George & Hwang (2004) 'The 52-Week High and Momentum Investing', JF"],
        "moderate", "Cross-sectionally, nearness to the 52-week high subsumed much of classic momentum in the "
        "original sample and shows less crash risk. As a single-stock timing rule it is a trend filter.",
        "Leadership markets; earnings-driven breakouts.", "Momentum crashes, sharp rotations into laggards.",
        "weeks to months", {"near": 0.05, "window": 252}, {"near": [0.05, 0.10]},
        {"entry": "close >= 0.95 * highest(high, 252)", "exit": "close < 0.90 * highest(high, 252)"},
        "run_quant_strategy / factor_screen (from_high_52w)", _high52),
    Strategy(
        "dual_momentum", "Dual (absolute + relative) momentum", "momentum", "single",
        "Own the stock only when its 12-month return is positive AND beats the benchmark's (SPY).",
        "Long when ret(lookback) > 0 and ret(lookback) > SPY ret(lookback).",
        ["Antonacci (2014) 'Dual Momentum Investing'", "Jegadeesh & Titman (1993)"],
        "moderate", "Combines time-series and relative momentum; designed for asset classes, applied here as a "
        "single-stock filter. Lags at turns.", "Persistent leadership.", "Sharp rotations; whipsaw around zero.",
        "months", {"lookback": 252}, {"lookback": [126, 252]}, None,
        "run_quant_strategy", _dual_momentum),
    # ---------------------------------------------------------------- mean reversion (single)
    Strategy(
        "rsi2", "Connors RSI(2) pullback", "mean_reversion", "single",
        "Buy short, sharp pullbacks inside a long-term uptrend; sell into the first bounce.",
        "Entry: RSI(2) < threshold and close > SMA(trend). Exit: close > SMA(exit_ma).",
        ["Connors & Alvarez (2009) 'Short Term Trading Strategies That Work'",
         "Jegadeesh (1990), Lehmann (1990): short-term reversal"],
        "moderate", "Strong on US equity indices/ETFs since the 1990s (high win rate ~65-75%, small average gains); "
        "weaker on single stocks and after costs; much less effective in bear markets (the SMA filter helps). "
        "Has decayed somewhat since popularised (~2009).",
        "Bull markets with liquidity-driven dips; index ETFs and large caps.",
        "Trend breakdowns and crashes (falling knives); earnings gaps; small caps with wide spreads.",
        "2-10 days", {"threshold": 10, "trend": 200, "exit_ma": 5}, {"threshold": [5, 10, 20], "exit_ma": [5, 10]},
        {"entry": "rsi(2) < 10 and close > sma(200)", "exit": "close > sma(5)"},
        "run_quant_strategy / run_backtest", _rsi2),
    Strategy(
        "ibs", "Internal Bar Strength reversal", "mean_reversion", "single",
        "A close near the day's low (IBS = (C-L)/(H-L) small) predicts a higher next-day return.",
        "Long for one day when IBS < threshold (optional SMA trend filter).",
        ["Pagonidis (2013) 'The IBS Effect: Mean Reversion in Equity ETFs', NAAIM",
         "Levy, Zheng & Liu (2020) on IBS in futures"],
        "moderate", "Well documented in equity index ETFs (SPY, QQQ) and some futures; daily turnover makes it very "
        "cost-sensitive; weak on most single stocks.",
        "Liquid index products in calm-to-moderate volatility.", "Trending sell-offs; high costs; illiquid names.",
        "1 day", {"threshold": 0.2, "trend": 0}, {"threshold": [0.1, 0.2, 0.3], "trend": [0, 200]},
        {"entry": "ibs() < 0.2", "exit": "ibs() > 0.8"},
        "run_quant_strategy / run_backtest", _ibs),
    Strategy(
        "bb_revert", "Bollinger z-score reversion", "mean_reversion", "single",
        "Buy when price is k standard deviations below its N-day mean; exit at the mean.",
        "Entry: z = (C - SMA(n)) / σ(n) < -z. Exit: z > 0.",
        ["Bollinger (2001)", "Poterba & Summers (1988) mean reversion in prices", "Avellaneda & Lee (2010)"],
        "weak", "Generic reversion; only works where the series really mean-reverts (check Hurst < 0.5, VR < 1, "
        "short half-life in get_quant_profile). On trending stocks it buys falling knives.",
        "Range-bound names, low Hurst, VR(5) < 1.", "Trending or news-driven moves.", "days to weeks",
        {"length": 20, "z": 2.0, "trend": 0}, {"length": [10, 20], "z": [1.5, 2.0]},
        {"entry": "zscore(20) < -2", "exit": "zscore(20) > 0"},
        "run_quant_strategy / run_backtest", _bb_revert),
    Strategy(
        "er_switch", "Efficiency-ratio regime switch", "mean_reversion", "single",
        "Follow the trend when price moves efficiently (Kaufman ER high), buy RSI(2) dips when it chops.",
        "ER(n) = |C - C[n]| / Σ|ΔC|. ER > threshold: long when close > EMA(n) and rising; else RSI(2) < 10 above SMA200 "
        "until close > SMA5.",
        ["Kaufman (1995) 'Smarter Trading' (efficiency ratio)", "Connors & Alvarez (2009)"],
        "weak", "An Autobench composite of two published ideas; treat as experimental and demand a strong "
        "walk-forward result before trusting it.", "Names that alternate between trends and ranges.",
        "Fast regime flips.", "days to weeks", {"length": 20, "threshold": 0.35}, {"threshold": [0.3, 0.4]},
        None, "run_quant_strategy", _er_switch),
    # ---------------------------------------------------------------- volatility / seasonality (single)
    Strategy(
        "vol_target", "Volatility targeting", "volatility", "single",
        "Scale exposure inversely to recent volatility: hold more when calm, less when turbulent.",
        "Exposure = min(max_leverage, target / realised vol(window)).",
        ["Moreira & Muir (2017) 'Volatility-Managed Portfolios', JF",
         "Cederburg, O'Doherty, Wang & Yan (2020) 'On the performance of volatility-managed portfolios', JFE",
         "Harvey et al. (2018) 'The Impact of Volatility Targeting', JPM"],
        "contested", "In-sample alphas are large, but Cederburg et al. find that real-time implementations beat "
        "the unmanaged version in only ~half of 103 strategies; benefits are mainly smoother risk and smaller "
        "left tails for equities, not higher returns. Without leverage (cap 1.0) it can only de-risk.",
        "Volatility clustering (it persists), equity crashes preceded by rising vol.",
        "Fast V-shaped recoveries (de-risked at the low); volatility spikes from calm (1987, Feb 2018).",
        "continuous overlay", {"window": 21, "target": 0.20, "max_leverage": 1.0},
        {"window": [21, 63], "target": [0.15, 0.25]}, None,
        "run_quant_strategy; size positions with target / vol (get_quant_confluence)", _vol_target),
    Strategy(
        "turn_of_month", "Turn-of-the-month effect", "seasonality", "single",
        "Equity returns concentrate around month-ends: the last trading day(s) and the first few of the next month.",
        "Long from the close before the last `last_n` trading days of a month through the first `first_m` days.",
        ["Ariel (1987) 'A Monthly Effect in Stock Returns', JFE",
         "Lakonishok & Smidt (1988) 'Are Seasonal Anomalies Real?', RFS",
         "McConnell & Xu (2008) 'Equity Returns at the Turn of the Month', FAJ"],
        "moderate", "McConnell & Xu found essentially all of the US equity premium (1926-2005) earned in the 4-day TOM "
        "window and the effect in 30 of 35 countries; attributed to month-end pay/flow timing. Weaker in recent "
        "years and for single stocks; best on indices.",
        "Indices / large caps; regular fund-flow calendars.", "Idiosyncratic single-name news dominates.",
        "4-5 days per month", {"last_n": 1, "first_m": 3}, {"last_n": [1, 2], "first_m": [3, 4]}, None,
        "run_quant_strategy", _turn_of_month),
    # ---------------------------------------------------------------- cross-sectional factors
    Strategy(
        "xs_momentum", "Cross-sectional momentum (12-1)", "factor", "cross_sectional",
        "Buy the stocks with the best 12-month returns excluding the last month; avoid/short the worst.",
        "Each month rank the list by return from t-252 to t-21; hold the top k equally weighted.",
        ["Jegadeesh & Titman (1993) 'Returns to Buying Winners and Selling Losers', JF",
         "Asness, Moskowitz & Pedersen (2013) 'Value and Momentum Everywhere', JF",
         "Daniel & Moskowitz (2016) 'Momentum Crashes', JFE"],
        "strong", "One of the most robust anomalies (US since 1927, 40+ countries, other asset classes), but with "
        "rare, violent crashes after bear-market rebounds (1932, 2009: -70%+ for the long-short). Long-only top-k "
        "captures less but avoids the short-side crash. Post-publication decay is modest compared with most anomalies.",
        "Trending markets with dispersion.", "Junk rallies after bear markets; sharp factor rotations.",
        "1-12 months", {"factor": "momentum_12_1", "top_k": 5}, {}, None,
        "run_factor_backtest(factor='momentum_12_1') / factor_screen", None),
    Strategy(
        "residual_momentum", "Residual (idiosyncratic) momentum", "factor", "cross_sectional",
        "Momentum in returns after removing market beta: less crash risk, higher risk-adjusted returns.",
        "Regress each stock's daily returns on SPY over the past year; rank by the residual 12-1 return divided by "
        "residual volatility.",
        ["Blitz, Huij & Martens (2011) 'Residual Momentum', JEF", "Gutierrez & Pirinsky (2007)"],
        "moderate", "Roughly doubles the Sharpe of total-return momentum in the original study and avoids most of "
        "the momentum crash exposure; replicated internationally. Less widely traded, so less crowded.",
        "Stock-specific trends (earnings revisions).", "Factor-driven markets where idiosyncratic moves are noise.",
        "1-12 months", {"factor": "residual_momentum", "top_k": 5}, {}, None,
        "run_factor_backtest(factor='residual_momentum')", None),
    Strategy(
        "short_term_reversal", "Short-term reversal (1 month)", "mean_reversion", "cross_sectional",
        "Last month's losers outperform last month's winners next month (liquidity provision).",
        "Rank by the past 21-day return; hold the bottom k.",
        ["Jegadeesh (1990) 'Evidence of Predictable Behavior of Security Returns', JF", "Lehmann (1990), QJE",
         "Nagel (2012) 'Evaporating Liquidity', RFS"],
        "moderate", "Profitable gross but high turnover eats most of it in large caps; returns spike when "
        "liquidity is scarce (high VIX). Industry-adjusted versions work better.",
        "High-volatility, illiquid markets.", "Momentum-driven markets; high costs.", "1 month",
        {"factor": "reversal_1m", "top_k": 5}, {}, None, "run_factor_backtest(factor='reversal_1m')", None),
    Strategy(
        "low_volatility", "Low volatility / betting against beta", "factor", "cross_sectional",
        "Low-volatility and low-beta stocks earn about the same as high-beta ones with much less risk.",
        "Rank by 1-year volatility (or beta); hold the lowest k; for BAB, lever low-beta and de-lever high-beta.",
        ["Ang, Hodrick, Xing & Zhang (2006) 'The Cross-Section of Volatility and Expected Returns', JF",
         "Frazzini & Pedersen (2014) 'Betting Against Beta', JFE", "Baker, Bradley & Wurgler (2011), FAJ"],
        "strong", "Documented globally; explained by leverage constraints and lottery preferences. Underperforms "
        "in sharp rallies and when rates rise (bond-like sectors). Valuations of low-vol stocks matter.",
        "Bear and sideways markets.", "Junk rallies; rising-rate shocks.", "months to years",
        {"factor": "low_vol", "top_k": 5}, {}, None, "run_factor_backtest(factor='low_vol') / factor_screen", None),
    Strategy(
        "value_quality", "Value + quality (quality at a reasonable price)", "factor", "cross_sectional",
        "Cheap stocks (high earnings/FCF/book yield) with high profitability and low leverage.",
        "Composite of value ranks (E/P, FCF/P, B/P, S/P) and quality ranks (ROE, margins, low debt); hold the top k.",
        ["Fama & French (1992, 2015)", "Novy-Marx (2013) 'The Other Side of Value: gross profitability', JFE",
         "Asness, Frazzini & Pedersen (2019) 'Quality Minus Junk', RAS", "Piotroski (2000) F-score"],
        "strong", "Value suffered a long drawdown (2007-2020) before recovering; quality/profitability is the more "
        "persistent premium and hedges value's junk exposure. Combine both rather than using either alone.",
        "Normal-to-late cycle; rate normalisation.", "Speculative growth manias.", "months to years",
        {"factor": "value_quality", "top_k": 10}, {}, None, "factor_screen(weights={value, quality})", None),
    # ---------------------------------------------------------------- pairs / stat arb
    Strategy(
        "pairs_cointegration", "Pairs trading (cointegration)", "stat_arb", "pairs",
        "Trade the spread between two cointegrated stocks back to its mean.",
        "Formation (1y): Engle-Granger test of log prices, hedge ratio β, spread half-life. Trade: when the spread "
        "z-score < -entry buy A / short β·B (reverse above +entry); exit at z = 0; stop at |z| > stop or after 3 half-lives.",
        ["Gatev, Goetzmann & Rouwenhorst (2006) 'Pairs Trading', RFS", "Vidyamurthy (2004)",
         "Do & Faff (2010, 2012) on declining profitability", "Rad, Low & Faff (2016) distance vs cointegration vs copula",
         "Engle & Granger (1987)"],
        "moderate", "GGR earned ~11%/yr (1962-2002) with a distance rule; Do & Faff show profits fell from the 1990s "
        "and are thin after costs, mostly because more pairs never re-converge. Requires shorting. Same-industry "
        "pairs with economic links (e.g. KO/PEP, dual share classes) are far more reliable than statistical matches.",
        "Stable economic link; range-bound sector; normal liquidity.",
        "Structural breaks (M&A, divergent fundamentals); crowded deleveraging (Aug 2007 quant quake).",
        "days to weeks", {"entry_z": 2.0, "exit_z": 0.0, "stop_z": 4.0}, {}, None,
        "find_pairs → analyze_pair", None),
    Strategy(
        "stat_arb_residual", "Statistical arbitrage on ETF residuals", "stat_arb", "pairs",
        "Trade each stock's residual vs its sector ETF as an Ornstein-Uhlenbeck process (s-score).",
        "Regress stock returns on its sector ETF over 60 days; model cumulative residual as OU; buy when s < -1.25, "
        "sell when s > +1.25, close near 0. Hedge with the ETF (beta-neutral).",
        ["Avellaneda & Lee (2010) 'Statistical Arbitrage in the U.S. Equities Market', Quant Finance"],
        "moderate", "Sharpe ~1.4 (1997-2007) before costs, deteriorating after 2002; the reference model for "
        "residual mean reversion. Needs many names and cheap execution; the single-pair version is analyze_pair(a, sector ETF).",
        "Normal liquidity; many independent residuals.", "Liquidity crises; factor crowding.", "days",
        {}, {}, None, "analyze_pair(symbol, sector_etf)", None),
    # ---------------------------------------------------------------- portfolio / hedging
    Strategy(
        "risk_parity", "Risk parity & hierarchical risk parity", "allocation", "portfolio",
        "Allocate so each holding contributes equally to risk (ERC) or by a clustered tree (HRP).",
        "Inverse-vol, equal risk contribution on a shrunk covariance, minimum variance, or HRP (López de Prado).",
        ["Maillard, Roncalli & Teiletche (2010) ERC", "Ledoit & Wolf (2004) shrinkage",
         "López de Prado (2016) 'Building Diversified Portfolios that Outperform Out of Sample', JPM",
         "DeMiguel, Garlappi & Uppal (2009) 1/N"],
        "strong", "Risk-based weights are more stable out of sample than mean-variance (which maximises estimation "
        "error). 1/N is a hard benchmark to beat; shrinkage and HRP help when assets are correlated.",
        "Many correlated holdings.", "Correlations jump to 1 in a crash.", "rebalance monthly",
        {}, {}, None, "optimize_allocation", None),
    Strategy(
        "beta_hedge", "Beta / factor hedging", "hedging", "portfolio",
        "Neutralise market (or sector) exposure with an index ETF so P&L reflects stock selection.",
        "Hedge notional = -Σ βᵢ·valueᵢ in SPY (or a min-variance regression on chosen ETFs). Re-estimate betas monthly.",
        ["Sharpe (1964) CAPM beta", "Fama & French factor models", "Ederington (1979) minimum-variance hedge ratio"],
        "strong", "Mechanics are textbook; the risk is beta instability (rolling betas drift, rise in crashes). "
        "Hedging cuts variance but also the equity premium.", "Stock-specific theses in a fragile market.",
        "Low-correlation names (hedge adds noise).", "overlay", {}, {}, None, "get_hedge_plan", None),
    # ---------------------------------------------------------------- reference knowledge
    Strategy(
        "pead", "Post-earnings announcement drift", "event", "reference",
        "Prices keep drifting in the direction of an earnings surprise for weeks.",
        "Buy top-decile standardized unexpected earnings (SUE) or strong 3-day announcement returns; hold ~60 days.",
        ["Ball & Brown (1968)", "Bernard & Thomas (1989) 'Post-Earnings-Announcement Drift', JAR",
         "Chordia et al. (2009); Martineau (2022) 'Rest in Peace Post-Earnings Announcement Drift'"],
        "contested", "A classic, but Martineau (2022) finds it has largely disappeared in large caps since ~2006; it "
        "survives mainly in small, illiquid stocks. Test with run_signal_study on the post-earnings gap.",
        "Small caps, under-followed names.", "Mega caps (priced in instantly).", "1-3 months", {}, {}, None,
        "get_earnings + run_signal_study('gap_pct'-style conditions)", None),
    Strategy(
        "overnight_drift", "Overnight vs intraday returns", "seasonality", "reference",
        "For US stocks most of the long-run return has accrued overnight (close→open), not intraday.",
        "Decomposed in get_quant_profile (overnight vs intraday). Trading it requires two trades a day, so costs dominate.",
        ["Lou, Polk & Skouras (2019) 'A Tug of War: Overnight Versus Intraday Expected Returns', JFE",
         "Cliff, Cooper & Gulen (2008)"], "moderate", "Persistent documented pattern; mostly unexploitable retail "
        "after spreads, but useful for timing entries (buying at the open has historically been cheaper than the "
        "prior close for momentum names).", "", "", "intraday", {}, {}, None, "get_quant_profile", None),
    Strategy(
        "merger_arb", "Merger / index arbitrage", "stat_arb", "reference",
        "Earn the spread between a target's price and the deal value; or front-run index additions.",
        "Not supported: needs deal terms and index-change data Autobench does not have.",
        ["Mitchell & Pulvino (2001) 'Characteristics of Risk and Return in Risk Arbitrage', JF",
         "Petajisto (2011) index premium"], "moderate", "Merger arb behaves like selling insurance (small steady "
        "gains, large losses on broken deals). Index-inclusion effects have shrunk since the 2000s.", "", "", "",
        {}, {}, None, "", None),
    Strategy(
        "dividend_capture", "Dividend capture (myth)", "event", "reference",
        "Buying before the ex-date to 'collect' the dividend does not create value.",
        "The price drops by about the dividend on the ex-date; taxes and costs make it negative.",
        ["Elton & Gruber (1970)", "Kalay (1982)"], "myth", "Included so the agent recognises it: do not recommend.",
        "", "", "", {}, {}, None, "", None),
]

BY_ID = {s.id: s for s in LIBRARY}
SINGLE = [s for s in LIBRARY if s.kind == "single"]


def get(strategy_id: str) -> Strategy:
    key = strategy_id.strip().lower()
    if key not in BY_ID:
        raise ValueError(f"Unknown strategy '{strategy_id}'. Known: {', '.join(BY_ID)}")
    return BY_ID[key]


def catalog(kind: str | None = None, family: str | None = None, full: bool = False) -> list[dict]:
    out = []
    for s in LIBRARY:
        if kind and s.kind != kind:
            continue
        if family and s.family != family:
            continue
        out.append(s.describe(full))
    return out


def positions(strategy: Strategy, df: pd.DataFrame, params: dict | None = None, bench: pd.Series | None = None,
              allow_short: bool = False) -> np.ndarray:
    if strategy.fn is None:
        raise ValueError(f"{strategy.name} is a {strategy.kind} strategy; run it with {strategy.run_with}")
    p = {**strategy.params, **(params or {})}
    unknown = set(p) - set(strategy.params)
    if unknown:
        raise ValueError(f"{strategy.id} has no parameter(s) {', '.join(sorted(unknown))}. "
                         f"Parameters: {', '.join(strategy.params)}")
    pos = np.asarray(strategy.fn(df, bench=bench, **p), float)
    pos = np.where(np.isfinite(pos), pos, 0.0)
    return pos if allow_short else np.clip(pos, 0, None)
