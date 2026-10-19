"""MCP interface for AI agents: market data, research, quantitative testing, chart control and
persistent research memory. Tools return compact JSON text; failures come back as tool errors
with an actionable message."""

import functools
import inspect
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated, Any, Literal

import numpy as np
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field, ValidationError

from . import guides
from .schemas import AnalysisIn, AnnotationIn, AnnotationPatch, ChartView, ClosePosition, NoteIn, OrderSpec, Point, Style
from .services import analyses, annotations, backtest, filings, fundamentals, levels, market, market_context
from .services import news as news_svc
from .services import options as options_svc
from .services import alerts as alerts_svc
from .services import macro, patterns, risk, screener, technicals, workspace
from .services.events import bus
from .services.trading import trading
from .services import indicators as ind
from .services.expressions import evaluate
from .timeutil import format_ts, get_interval

log = logging.getLogger(__name__)

mcp = MCPServer(name="autobench", title="Autobench stock research", instructions=guides.SERVER_INSTRUCTIONS,
                version="0.1.0")

AUTHOR = "agent"


def _jsonable(o: Any):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        f = float(o)
        return None if not np.isfinite(f) else f
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, BaseModel):
        return o.model_dump()
    return str(o)


def _clean_floats(o: Any):
    if isinstance(o, float):
        return None if not np.isfinite(o) else o
    if isinstance(o, dict):
        return {k: _clean_floats(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean_floats(v) for v in o]
    return o


def dumps(obj: Any) -> str:
    return json.dumps(_clean_floats(obj), default=_jsonable, separators=(",", ":"), ensure_ascii=False)


def tool(**kwargs):
    """Register a tool that returns compact JSON and converts exceptions into clear tool errors."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            try:
                return dumps(fn(*a, **kw))
            except ToolError:
                raise
            except ValidationError as e:
                msgs = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
                raise ToolError(f"Invalid input: {msgs}") from e
            except KeyError as e:
                raise ToolError(str(e).strip("'\"")) from e
            except ValueError as e:
                raise ToolError(str(e)) from e
            except Exception as e:
                log.exception("tool %s failed", fn.__name__)
                raise ToolError(f"{type(e).__name__}: {e}") from e

        sig = inspect.signature(fn, eval_str=True).replace(return_annotation=str)
        wrapper.__signature__ = sig
        wrapper.__annotations__ = {**{n: p.annotation for n, p in sig.parameters.items()}, "return": str}
        return mcp.tool(structured_output=False, **kwargs)(wrapper)

    return deco


Symbol = Annotated[str, Field(description="Ticker, e.g. AAPL, BRK.B, SPY; indices like ^VIX also work for price data")]
IntervalArg = Annotated[str, Field(description="Bar interval: 1m 5m 15m 30m 1h 1d 1wk 1mo")]
Lookback = Annotated[str, Field(description="Window ending now: e.g. 5d 3mo 6mo 1y 2y 5y 10y ytd max")]


# ===================================================================================== market data


@tool()
def search_symbols(query: Annotated[str, Field(description="Ticker or company name fragment")],
                   limit: Annotated[int, Field(ge=1, le=50)] = 10) -> Any:
    """Find US stocks/ETFs by ticker or company name."""
    return market.search_symbols(query, limit)


@tool()
def get_quote(symbol: Symbol) -> Any:
    """Latest price, day range, volume and change vs previous close."""
    return market.get_quote(symbol)


@tool()
def get_price_history(
    symbol: Symbol,
    interval: IntervalArg = "1d",
    lookback: Lookback = "6mo",
    start: Annotated[str | None, Field(description="ISO start (overrides lookback)")] = None,
    end: Annotated[str | None, Field(description="ISO end (default now)")] = None,
    max_bars: Annotated[int, Field(ge=10, le=2000, description="Consecutive bars are merged (OHLC-correct) to stay under this")] = 250,
) -> Any:
    """OHLCV bars as rows [date, open, high, low, close, volume] plus summary stats. Adjusted for splits
    and dividends. Daily dates are sessions; intraday times are US/Eastern, regular session only."""
    bars = market.get_bars(symbol, interval, start=start, end=end, lookback=None if start else lookback)
    df = bars.df
    if df.empty:
        raise ToolError(f"No {interval} bars for {bars.symbol} in that range")
    agg, k = market.aggregate_bars(df, max_bars)
    c = df["c"]
    return {
        "symbol": bars.symbol, "interval": bars.interval.name, "source": bars.source,
        "bars_in_range": len(df), "bars_returned": len(agg), "bars_merged_per_row": k,
        "columns": ["date", "open", "high", "low", "close", "volume"],
        "rows": market.bars_to_rows(agg, bars.interval.intraday),
        "stats": {
            "first_close": market.price_round(float(c.iloc[0])), "last_close": market.price_round(float(c.iloc[-1])),
            "change_pct": round(float((c.iloc[-1] / c.iloc[0] - 1) * 100), 2),
            "high": market.price_round(float(df["h"].max())), "low": market.price_round(float(df["l"].min())),
            "high_date": format_ts(int(df["h"].idxmax()), bars.interval.intraday),
            "low_date": format_ts(int(df["l"].idxmin()), bars.interval.intraday),
            "avg_volume": int(df["v"].mean()),
        },
    }


# ===================================================================================== technicals


@tool()
def list_indicators(category: Annotated[str | None, Field(description="trend | momentum | volatility | volume | statistics")] = None) -> Any:
    """All indicators with parameters (in positional order), outputs and interpretation notes."""
    items = ind.list_indicators()
    return [i for i in items if not category or i["category"] == category]


class IndicatorRequest(BaseModel):
    name: str = Field(description="Indicator name from list_indicators, e.g. rsi, macd, bbands")
    params: dict[str, Any] = Field(default_factory=dict, description="e.g. {'length': 50}; omitted params use defaults")


@tool()
def get_indicators(
    symbol: Symbol,
    indicators: Annotated[list[IndicatorRequest], Field(min_length=1, max_length=12)],
    interval: IntervalArg = "1d",
    points: Annotated[int, Field(ge=1, le=500, description="How many most-recent values to return")] = 30,
) -> Any:
    """Compute indicators (with full warm-up history) and return their latest values plus a short series."""
    iv = get_interval(interval)
    specs = []
    warm = 0
    for r in indicators:
        spec = ind.get_spec(r.name)
        p = ind.resolve_params(spec, r.params)
        specs.append((spec, p))
        warm = max(warm, spec.warmup(p))
    need = warm + points
    lookback = {"1m": "7d", "5m": "59d", "15m": "59d", "30m": "59d", "1h": "720d"}.get(iv.name)
    if lookback is None:
        days = int(need * (1.6 if iv.name == "1d" else 7.2 if iv.name == "1wk" else 31)) + 30
        lookback = f"{days}d"
    bars = market.get_bars(symbol, iv.name, lookback=lookback)
    df = bars.df
    if df.empty:
        raise ToolError(f"No data for {bars.symbol}")
    dates = [format_ts(int(t), iv.intraday) for t in df.index[-points:]]
    series, latest = {}, {}
    for spec, p in specs:
        res = ind.compute(df, spec.name, p)
        lbl = ind.label(spec, p)
        tail = res.iloc[-points:]
        series[lbl] = {col: [None if not np.isfinite(v) else round(float(v), 4) for v in tail[col]] for col in tail.columns}
        latest[lbl] = {col: (None if not np.isfinite(v) else round(float(v), 4)) for col, v in res.iloc[-1].items()}
    return {"symbol": bars.symbol, "interval": iv.name, "close": [market.price_round(float(x)) for x in df["c"].iloc[-points:]],
            "dates": dates, "latest": latest, "series": series}


@tool()
def get_technical_summary(symbol: Symbol, interval: IntervalArg = "1d",
                          benchmark: Annotated[str, Field(description="For relative strength")] = "SPY") -> Any:
    """One-call technical read: returns, 52w range, trend (MAs, ADX, structure, supertrend, cloud), momentum
    (RSI, MACD, stochastic, divergences), volatility regime, volume confirmation, key levels (S/R, pivots, fib,
    volume profile, trendlines), candles, recent events and relative strength. Scores are -100..100."""
    return technicals.technical_summary(symbol, interval, benchmark)


@tool()
def get_multi_timeframe(symbol: Symbol,
                        intervals: Annotated[list[str], Field(description="e.g. ['1wk','1d','1h']")] = ["1wk", "1d", "1h"]) -> Any:  # noqa: B006
    """Trend/momentum alignment across timeframes (higher timeframe = context, lower = timing)."""
    return technicals.multi_timeframe(symbol, intervals)


@tool()
def get_levels(symbol: Symbol, interval: IntervalArg = "1d", lookback: Lookback = "1y",
               swing_window: Annotated[int, Field(ge=2, le=30, description="Bars each side for swing detection")] = 5) -> Any:
    """Detailed price levels with exact times/prices for drawing: clustered support/resistance, recent swing
    highs/lows, trendline candidates, Fibonacci of the dominant swing, volume profile (POC/value area/nodes)
    and classic pivots."""
    bars = market.get_bars(symbol, interval, lookback=lookback)
    df = bars.df
    if len(df) < 20:
        raise ToolError(f"Not enough bars ({len(df)}) for levels; use a longer lookback")
    iv = bars.interval
    fmt = lambda t: format_ts(int(t), iv.intraday)  # noqa: E731
    sr = levels.support_resistance(df, max_levels=6)
    swings = levels.swing_points(df, swing_window, swing_window)[-16:]
    vp = levels.volume_profile(df, bins=40)
    fib = levels.fibonacci(df)
    pr = market.price_round
    out = {
        "symbol": bars.symbol, "interval": iv.name, "last_close": pr(float(df["c"].iloc[-1])),
        "atr14": round(float(sr.get("atr", float("nan"))), 4) if sr.get("atr") else None,
        "supports": [{**{k: v for k, v in s.items() if k not in ("first_touch", "last_touch")}, "price": pr(s["price"]),
                      "last_touch": fmt(s["last_touch"])} for s in sr["supports"]],
        "resistances": [{**{k: v for k, v in s.items() if k not in ("first_touch", "last_touch")}, "price": pr(s["price"]),
                         "last_touch": fmt(s["last_touch"])} for s in sr["resistances"]],
        "swing_points": [{"time": fmt(s["t"]), "price": pr(s["price"]), "kind": s["kind"]} for s in swings],
        "trendlines": [{**t, "points": [{"time": fmt(p["time"]), "price": pr(p["price"])} for p in t["points"]],
                        "value_at_last_bar": pr(t["value_at_last_bar"])} for t in levels.trendline_candidates(df, swing_window, swing_window)],
    }
    if fib:
        out["fibonacci"] = {"direction": fib["direction"],
                            "swing_low": {"time": fmt(fib["swing_low"]["t"]), "price": pr(fib["swing_low"]["price"])},
                            "swing_high": {"time": fmt(fib["swing_high"]["t"]), "price": pr(fib["swing_high"]["price"])},
                            "retracements": {k: pr(v) for k, v in fib["retracements"].items()},
                            "extensions": {k: pr(v) for k, v in fib["extensions"].items()},
                            "current_retracement": fib["current_retracement"]}
    if vp:
        out["volume_profile"] = {"poc": pr(vp["poc"]), "value_area_high": pr(vp["value_area_high"]),
                                 "value_area_low": pr(vp["value_area_low"]),
                                 "high_volume_nodes": [pr(x) for x in vp["high_volume_nodes"]],
                                 "low_volume_nodes": [pr(x) for x in vp["low_volume_nodes"]]}
    if not iv.intraday:
        daily = df if iv.name == "1d" else market.get_bars(symbol, "1d", lookback="120d").df
        out["pivots"] = {k: {kk: pr(vv) for kk, vv in v.items()} for k, v in levels.period_pivots(daily).items()}
    return out


@tool()
def get_patterns(symbol: Symbol, interval: IntervalArg = "1d", lookback: Lookback = "1y",
                 recent_bars: Annotated[int, Field(ge=5, le=250)] = 30) -> Any:
    """Candlestick patterns, market structure (HH/HL/LH/LL), double tops/bottoms, consolidation, and technical
    events (MA/MACD/RSI crosses, breakouts, gaps, 52w highs/lows, volume spikes, squeeze releases)."""
    bars = market.get_bars(symbol, interval, lookback=lookback)
    df = bars.df
    iv = bars.interval
    fmt = lambda t: format_ts(int(t), iv.intraday)  # noqa: E731
    st = patterns.market_structure(df)
    return {
        "symbol": bars.symbol, "interval": iv.name,
        "structure": {"state": st["state"],
                      "swing_highs": [{**s, "t": fmt(s["t"])} for s in st["swing_highs"]],
                      "swing_lows": [{**s, "t": fmt(s["t"])} for s in st["swing_lows"]]},
        "candles": [{**c, "t": fmt(c["t"])} for c in patterns.candlestick_patterns(df, recent_bars)],
        "double_tops_bottoms": [{**d, "points": [{"time": fmt(p["time"]), "price": p["price"]} for p in d["points"]]}
                                for d in patterns.double_tops_bottoms(df)],
        "consolidation": patterns.consolidation(df),
        "events": [{**e, "t": fmt(e["t"])} for e in patterns.recent_events(df, recent_bars)],
    }


# ===================================================================================== quant


@tool()
def evaluate_expression(symbol: Symbol, expression: Annotated[str, Field(description="See get_guide('expressions')")],
                        interval: IntervalArg = "1d", points: Annotated[int, Field(ge=1, le=300)] = 20) -> Any:
    """Evaluate an expression (number or condition) over recent bars, e.g. to check a hypothesis or debug a
    backtest rule. Returns the last `points` values."""
    lb = {"1d": "3y", "1wk": "15y", "1mo": "max"}.get(get_interval(interval).name, "59d")
    bars = market.get_bars(symbol, interval, lookback=lb)
    s = evaluate(bars.df, expression)
    tail = s.iloc[-points:]
    vals = [bool(v) if isinstance(v, (bool, np.bool_)) else (None if not np.isfinite(v) else round(float(v), 4)) for v in tail]
    out = {"symbol": bars.symbol, "expression": expression,
           "dates": [format_ts(int(t), bars.interval.intraday) for t in tail.index], "values": vals}
    if s.dtype == bool:
        out["true_count_in_history"] = int(s.sum())
        out["history_bars"] = len(s)
    return out


@tool()
def run_backtest(
    symbol: Symbol,
    entry: Annotated[str, Field(description="Entry condition expression, e.g. 'crosses_above(ema(20), ema(50))'")],
    exit: Annotated[str | None, Field(description="Exit condition expression (optional if stops/targets/max_hold given)")] = None,
    interval: IntervalArg = "1d",
    lookback: Lookback = "5y",
    side: Literal["long", "short"] = "long",
    stop_loss_pct: Annotated[float | None, Field(gt=0, le=90)] = None,
    take_profit_pct: Annotated[float | None, Field(gt=0, le=1000)] = None,
    stop_atr: Annotated[float | None, Field(gt=0, le=20, description="Stop at entry ∓ N × ATR(14)")] = None,
    target_atr: Annotated[float | None, Field(gt=0, le=50)] = None,
    max_hold_bars: Annotated[int | None, Field(ge=1)] = None,
    commission_bps: Annotated[float, Field(ge=0, le=100)] = 1.0,
    slippage_bps: Annotated[float, Field(ge=0, le=200)] = 5.0,
    symbols: Annotated[list[str] | None, Field(description="Also run the same rules on these symbols (robustness check)")] = None,
) -> Any:
    """Backtest a rule-based strategy: no look-ahead (signal at close, fill next open), costs, intrabar stops,
    metrics (win rate, expectancy, profit factor, t-stat, CAGR, Sharpe, max DD), buy-and-hold comparison,
    in-sample vs out-of-sample split, warnings about weak evidence, and recent trades."""
    kw = dict(entry=entry, exit=exit, interval=interval, lookback=lookback, side=side, stop_loss_pct=stop_loss_pct,
              take_profit_pct=take_profit_pct, stop_atr=stop_atr, target_atr=target_atr, max_hold_bars=max_hold_bars,
              commission_bps=commission_bps, slippage_bps=slippage_bps)
    main = backtest.run_backtest(symbol, **kw)
    if not symbols:
        return main
    others = {}
    for s in symbols:
        try:
            r = backtest.run_backtest(s, **kw)
            others[r["symbol"]] = {**r["performance"], "buy_and_hold_pct": r["buy_and_hold"]["total_return_pct"]}
        except Exception as e:
            others[s.upper()] = {"error": str(e)}
    main["robustness"] = others
    return main


@tool()
def run_signal_study(
    symbol: Symbol,
    condition: Annotated[str, Field(description="Condition expression, e.g. 'rsi(14) < 30 and close > sma(200)'")],
    interval: IntervalArg = "1d",
    lookback: Lookback = "10y",
    horizons: Annotated[list[int], Field(description="Forward windows in bars")] = [5, 10, 20],  # noqa: B006
    first_only: Annotated[bool, Field(description="Count only bars where the condition turns true (recommended)")] = True,
    symbols: Annotated[list[str] | None, Field(description="Also study these symbols (robustness)")] = None,
) -> Any:
    """Event study: forward returns after a condition fires vs the unconditional baseline (mean, median, win rate,
    MAE/MFE, t-stat), recent occurrences, and whether it is true right now."""
    main = backtest.signal_study(symbol, condition, interval, lookback, horizons, first_only)
    if symbols:
        rob = {}
        for s in symbols:
            try:
                r = backtest.signal_study(s, condition, interval, lookback, horizons, first_only)
                rob[r["symbol"]] = {"occurrences": r["occurrences"],
                                    **{h: {k: v.get(k) for k in ("mean_pct", "win_rate_pct", "edge_vs_baseline_pct", "t_stat_vs_baseline")}
                                       for h, v in r["forward_returns"].items()}}
            except Exception as e:
                rob[s.upper()] = {"error": str(e)}
        main["robustness"] = rob
    return main


@tool()
def scan_symbols(
    condition: Annotated[str, Field(description="Condition expression evaluated on the latest bar")],
    symbols: Annotated[list[str] | None, Field(description="Symbols to scan")] = None,
    watchlist: Annotated[str | None, Field(description="Or scan a saved watchlist")] = None,
    interval: IntervalArg = "1d",
) -> Any:
    """Which symbols satisfy a condition right now (e.g. 'close > sma(50) and rsi(14) < 35')."""
    syms = list(symbols or [])
    if watchlist:
        syms += workspace.get_watchlist(watchlist)["symbols"]
    if not syms:
        raise ToolError("Provide symbols or a watchlist")
    return backtest.scan(list(dict.fromkeys(syms)), condition, interval)


@tool()
def get_risk_metrics(symbol: Symbol, benchmark: str = "SPY", lookback: Lookback = "3y",
                     risk_free_rate: Annotated[float, Field(ge=0, le=0.2)] = 0.04) -> Any:
    """Volatility, Sharpe/Sortino, max & current drawdown, VaR/CVaR, skew/kurtosis, beta/correlation/alpha vs benchmark."""
    return risk.risk_metrics(symbol, benchmark, lookback, risk_free_rate)


@tool()
def get_correlations(symbols: Annotated[list[str], Field(min_length=2, max_length=25)], lookback: Lookback = "1y") -> Any:
    """Daily-return correlation matrix and volatilities (diversification / pair analysis)."""
    return risk.correlation_matrix(symbols, lookback)


@tool()
def calculate_position_size(account_size: float, entry: float, stop: float,
                            risk_pct: Annotated[float, Field(gt=0, le=10, description="% of account risked")] = 1.0,
                            max_position_pct: Annotated[float, Field(gt=0, le=100)] = 25.0,
                            targets: list[float] | None = None) -> Any:
    """Fixed-fractional position size from entry/stop, with R-multiples for targets."""
    return risk.position_size(account_size, entry, stop, risk_pct, max_position_pct, targets)


# ===================================================================================== fundamentals & research


@tool()
def get_fundamentals(symbol: Symbol) -> Any:
    """Company profile, size, valuation multiples, margins/returns, growth, balance-sheet health, dividends,
    short interest/ownership and analyst consensus (Yahoo; cross-check key numbers with SEC data)."""
    return fundamentals.get_fundamentals(symbol)


@tool()
def get_financial_statements(symbol: Symbol, statement: Literal["income", "balance", "cashflow"] = "income",
                             period: Literal["annual", "quarterly"] = "quarterly",
                             rows: Literal["key", "all"] = "key") -> Any:
    """Income statement / balance sheet / cash flow (last ~4-5 periods, oldest first) with derived margins,
    growth, coverage and dilution ratios."""
    return fundamentals.get_financial_statements(symbol, statement, period, rows)


@tool()
def get_financial_history(
    symbol: Symbol,
    metrics: Annotated[list[str] | None, Field(description=f"Any of: {', '.join(filings.METRICS)}")] = None,
    period: Literal["annual", "quarterly"] = "annual",
    years: Annotated[int, Field(ge=1, le=20)] = 10,
) -> Any:
    """Long-run as-reported financials from SEC XBRL (up to ~15 years) for trend and cycle analysis."""
    return filings.financial_history(symbol, metrics, period, years)


@tool()
def get_earnings(symbol: Symbol) -> Any:
    """Earnings history with surprises, beat rate, next date, consensus EPS/revenue estimates, estimate
    revisions (eps_trend / eps_revisions) and growth estimates."""
    return fundamentals.get_earnings(symbol)


@tool()
def get_analyst_ratings(symbol: Symbol, limit: Annotated[int, Field(ge=1, le=50)] = 15) -> Any:
    """Price targets, recommendation trend (last 4 months) and recent upgrades/downgrades."""
    return fundamentals.get_analyst_ratings(symbol, limit)


@tool()
def get_ownership(symbol: Symbol) -> Any:
    """Insider transactions & 6-month buy/sell summary, top institutional and fund holders."""
    return fundamentals.get_ownership(symbol)


@tool()
def compare_peers(symbols: Annotated[list[str], Field(min_length=2, max_length=15)]) -> Any:
    """Side-by-side valuation, profitability, growth and leverage for a peer group, with peer medians, plus
    3m/6m/1y price performance."""
    out = fundamentals.compare_fundamentals(symbols)
    perf = {}
    for s in out["companies"]:
        try:
            r = technicals._returns(market.get_bars(s, "1d", lookback="400d").df, False)
            perf[s] = {k: r.get(k) for k in ("1m", "3m", "6m", "1y", "ytd")}
        except Exception as e:
            perf[s] = {"error": str(e)}
    out["performance_pct"] = perf
    return out


@tool()
def get_options_overview(symbol: Symbol) -> Any:
    """Options-implied view: ATM IV term structure, expected move per expiry, put/call volume & OI ratios,
    skew, max pain, largest OI strikes and unusual activity."""
    return options_svc.options_overview(symbol)


@tool()
def get_option_chain(symbol: Symbol, expiration: Annotated[str | None, Field(description="YYYY-MM-DD; default nearest")] = None,
                     strikes_around_spot: Annotated[int, Field(ge=1, le=40)] = 8) -> Any:
    """Near-the-money calls and puts for one expiration (bid/ask/last/volume/OI/IV)."""
    return options_svc.option_chain(symbol, expiration, strikes_around_spot)


@tool()
def get_news(symbol: Annotated[str | None, Field(description="Ticker; omit for market-wide news (Alpaca)")] = None,
             limit: Annotated[int, Field(ge=1, le=50)] = 15, days: Annotated[int, Field(ge=1, le=365)] = 14,
             include_content: Annotated[bool, Field(description="Include article bodies (Alpaca only; long)")] = False) -> Any:
    """Recent news headlines with summaries, sources and timestamps."""
    return news_svc.get_news(symbol, limit, days, include_content)


@tool()
def list_sec_filings(symbol: Symbol,
                     forms: Annotated[list[str] | None, Field(description="e.g. ['10-K','10-Q','8-K','4','DEF 14A']")] = None,
                     limit: Annotated[int, Field(ge=1, le=100)] = 20) -> Any:
    """Recent SEC filings with URLs (feed a URL to read_sec_filing)."""
    return filings.list_filings(symbol, forms, limit)


@tool()
def read_sec_filing(
    url: Annotated[str, Field(description="sec.gov document URL from list_sec_filings / get_filing_documents")],
    section: Annotated[str | None, Field(description="10-K/10-Q section: business, risk_factors, mdna, market_risk, "
                                                     "financial_statements, legal_proceedings, controls, or an item code like '7A'")] = None,
    offset: Annotated[int, Field(ge=0)] = 0,
    max_chars: Annotated[int, Field(ge=1000, le=40000)] = 15000,
) -> Any:
    """Plain text of a filing (or one section), paginated via offset/next_offset. Lists sections available."""
    return filings.read_filing(url, section, offset, max_chars)


@tool()
def get_filing_documents(url: Annotated[str, Field(description="Any document URL inside the filing")]) -> Any:
    """Documents in a filing, e.g. the EX-99.1 earnings press release attached to an 8-K."""
    return filings.filing_documents(url)


@tool()
def get_market_overview() -> Any:
    """Market regime: indices (SPY/QQQ/IWM/DIA/RSP), VIX & term structure, rates, credit, dollar, gold, oil,
    11 sector ETFs with returns and trend, sector leaders/laggards and regime notes."""
    return market_context.market_overview()


@tool()
def get_market_movers(top: Annotated[int, Field(ge=1, le=50)] = 10) -> Any:
    """Top gainers, losers and most-active stocks today (Alpaca screener)."""
    return market_context.movers(top)


@tool()
def get_research_brief(symbol: Symbol) -> Any:
    """Starting point for any single-stock analysis: quote, brief technicals (1d), key fundamentals,
    next earnings, analyst consensus, latest headlines, sector ETF context, and your prior analyses/notes
    on this symbol. Fetched in parallel."""
    sym = market.normalize_symbol(symbol)
    jobs = {
        "quote": lambda: market.get_quote(sym),
        "technical": lambda: technicals.technical_summary(sym, "1d", detail="brief"),
        "fundamentals": lambda: fundamentals.get_fundamentals(sym),
        "earnings": lambda: fundamentals.get_earnings(sym),
        "news": lambda: news_svc.get_news(sym, 8, 14),
        "prior_analyses": lambda: analyses.list_(sym, limit=5),
        "notes": lambda: workspace.list_notes(sym, limit=10, include_content=False),
    }
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futs = {k: pool.submit(fn) for k, fn in jobs.items()}
        for k, f in futs.items():
            try:
                results[k] = f.result()
            except Exception as e:
                results[k] = {"error": str(e)}
    f = results.get("fundamentals", {})
    e = results.get("earnings", {})
    brief: dict[str, Any] = {"symbol": sym, "quote": results["quote"], "technical": results["technical"]}
    if "error" not in f:
        brief["company"] = {**{k: f["profile"][k] for k in ("name", "sector", "industry")},
                            "market_cap": f["size"]["market_cap"],
                            "summary": (f["profile"]["summary"] or "")[:400]}
        brief["valuation"] = {k: f["valuation"][k] for k in ("trailing_pe", "forward_pe", "peg", "price_to_sales", "ev_to_ebitda", "fcf_yield_pct")}
        brief["quality_growth"] = {"gross_margin_pct": f["profitability"]["gross_margin_pct"],
                                   "operating_margin_pct": f["profitability"]["operating_margin_pct"],
                                   "roe_pct": f["profitability"]["roe_pct"],
                                   "revenue_growth_yoy_pct": f["growth"]["revenue_growth_yoy_pct"],
                                   "earnings_growth_yoy_pct": f["growth"]["earnings_growth_yoy_pct"],
                                   "debt_to_equity_pct": f["financial_health"]["debt_to_equity_pct"]}
        brief["analysts"] = f["analysts"]
        brief["short_interest_pct_float"] = f["short_interest_and_ownership"]["short_pct_of_float"]
        etf = market_context.sector_etf_for(f["profile"].get("sector"))
        if etf:
            try:
                rs = technicals._relative_strength(market.get_bars(sym, "1d", lookback="1y"), etf)
                brief["vs_sector_etf"] = rs
            except Exception:
                pass
    else:
        brief["fundamentals_error"] = f["error"]
    if "error" not in e:
        brief["earnings"] = {"next": e.get("next_earnings"), "beat_rate_pct": e.get("beat_rate_pct"),
                             "avg_surprise_pct": e.get("avg_surprise_pct"), "last": (e.get("history") or [None])[0]}
    n = results.get("news", {})
    brief["headlines"] = [{"date": (i.get("published_at") or "")[:10], "headline": i.get("headline"), "source": i.get("source")}
                          for i in n.get("items", [])][:8] if "error" not in n else n
    brief["prior_analyses"] = results["prior_analyses"]
    brief["notes"] = results["notes"]
    return brief


# ===================================================================================== chart control


@tool()
def get_chart_view() -> Any:
    """What the user's chart is showing: symbol, interval, visible range, indicators, and the layers the user
    keeps on this symbol (each layer groups price, volume, indicators, other symbols and drawings). A layer
    with `chart: true` has its own price chart; with an `interval` it is the user's setup for that interval
    (e.g. a daily layer and a 30m entry layer), and switching the chart's interval switches layers."""
    view = dict(workspace.get_view())
    layers = workspace.layers_summary(view.get("symbol", ""))
    if layers is not None:
        view["layers"] = layers
    return view


@tool()
def set_chart_view(
    symbol: Annotated[str | None, Field(description="Switch the chart to this symbol")] = None,
    interval: Annotated[str | None, Field(description="Switch interval")] = None,
    from_time: Annotated[str | None, Field(description="Visible range start (ISO)")] = None,
    to_time: Annotated[str | None, Field(description="Visible range end (ISO)")] = None,
    indicators: Annotated[list[IndicatorRequest] | None, Field(description="Replace the indicators shown")] = None,
) -> Any:
    """Change what the user's chart displays (updates live in the web UI)."""
    fields: dict[str, Any] = {k: v for k, v in dict(symbol=symbol, interval=interval, from_time=from_time, to_time=to_time).items() if v is not None}
    if indicators is not None:
        fields["indicators"] = [i.model_dump() for i in indicators]
    return workspace.set_view(ChartView(**fields), author=AUTHOR)


@tool()
def list_annotations(symbol: Symbol, interval: str | None = None, author: Literal["agent", "user"] | None = None,
                     tag: str | None = None) -> Any:
    """Drawings on a symbol's chart (both the user's and yours)."""
    return annotations.list_(symbol, interval, author, tag)


@tool()
def draw_annotations(
    symbol: Symbol,
    items: Annotated[list[AnnotationIn], Field(min_length=1, max_length=40, description="Drawings; see get_guide('annotations')")],
) -> Any:
    """Draw on the user's chart (trendlines, levels, zones, fib, channels, text, markers, trade plans).
    Appears live in the web UI. Returns created drawings with ids."""
    created = annotations.create(symbol, items, author=AUTHOR)
    return {"created": len(created), "ids": [c["id"] for c in created], "tags": sorted({c["tag"] for c in created if c["tag"]})}


@tool()
def update_annotation(
    annotation_id: str,
    points: list[Point] | None = None,
    text: str | None = None,
    style: Style | None = None,
    tag: str | None = None,
    visible: bool | None = None,
) -> Any:
    """Move/restyle/relabel/hide a drawing."""
    patch = AnnotationPatch(**{k: v for k, v in dict(points=points, text=text, style=style, tag=tag, visible=visible).items() if v is not None})
    return annotations.update(annotation_id, patch)


@tool()
def delete_annotations(
    ids: list[str] | None = None,
    symbol: str | None = None,
    tag: str | None = None,
    author: Annotated[Literal["agent", "user"] | None, Field(description="Use 'agent' to remove only your drawings")] = None,
    layer: Annotated[str | None, Field(description="Only drawings in this chart layer")] = None,
) -> Any:
    """Delete drawings by id, or by symbol/tag/author/layer filter (filters combine with AND). Do not delete the
    user's drawings unless asked."""
    return annotations.delete_(ids, symbol, tag, author, layer)


# ===================================================================================== research memory


@tool()
def save_analysis(analysis: AnalysisIn) -> Any:
    """Record a recommendation (action, horizon, calibrated confidence, entry zone, stop, targets, scores,
    risks, catalysts, invalidation, rationale). Price at creation is captured for outcome tracking."""
    return analyses.save(analysis, author=AUTHOR)


@tool()
def list_analyses(symbol: str | None = None, limit: Annotated[int, Field(ge=1, le=100)] = 10,
                  include_rationale: bool = False) -> Any:
    """Previously saved analyses, newest first."""
    return analyses.list_(symbol, limit, include_rationale)


@tool()
def get_analysis(analysis_id: str) -> Any:
    """One saved analysis with full rationale plus its current outcome evaluation."""
    a = analyses.get(analysis_id)
    a["evaluation"] = analyses.evaluate(analysis_id)
    return a


@tool()
def get_track_record(symbol: str | None = None, limit: Annotated[int, Field(ge=1, le=500)] = 100) -> Any:
    """Grade saved recommendations against subsequent prices: status (target_hit / stopped_out / open /
    expired), returns, R-multiple, alpha vs SPY, hit rate, calibration by confidence bucket, Brier score."""
    return analyses.track_record(symbol, limit)


@tool()
def save_note(title: str, content: Annotated[str, Field(description="Markdown")], symbol: str | None = None,
              tags: list[str] | None = None,
              note_id: Annotated[str | None, Field(description="Update an existing note instead of creating one")] = None) -> Any:
    """Persist research memory: theses, catalysts and dates, data quirks, lessons from past calls."""
    return workspace.save_note(NoteIn(title=title, content=content, symbol=symbol, tags=tags or []), note_id, author=AUTHOR)


@tool()
def search_notes(symbol: str | None = None, tag: str | None = None,
                 query: Annotated[str | None, Field(description="Text to find in title/content")] = None,
                 limit: Annotated[int, Field(ge=1, le=100)] = 20) -> Any:
    """Search your saved research notes by symbol, tag or text (read before re-analysing a name)."""
    return workspace.list_notes(symbol, tag, query, limit)


@tool()
def list_watchlists() -> Any:
    """Saved watchlists and their symbols."""
    return workspace.list_watchlists()


@tool()
def update_watchlist(name: str, add: list[str] | None = None, remove: list[str] | None = None,
                     replace: Annotated[list[str] | None, Field(description="Replace all symbols")] = None) -> Any:
    """Create a watchlist or add/remove symbols."""
    return workspace.update_watchlist(name, add, remove, replace)


@tool()
def scan_watchlist(name: str = "default", interval: IntervalArg = "1d",
                   sort_by: Literal["trend", "momentum", "change"] = "trend") -> Any:
    """Brief technical read of every symbol in a watchlist, ranked (trend/momentum scores, RSI, levels)."""
    wl = workspace.get_watchlist(name)
    rows, errors = [], {}

    def one(s):
        t = technicals.technical_summary(s, interval, detail="brief")
        return {"symbol": s, "price": t["price"], "change_pct": t["change_pct"],
                "trend_score": t["trend"]["score"], "momentum_score": t["momentum"]["score"],
                "rsi14": t["momentum"]["rsi14"], "ma_stack": t["trend"]["ma_stack"], "structure": t["trend"]["structure"],
                "returns_1m_pct": t["returns_pct"].get("1m"),
                "nearest_support": (t["levels"]["supports"] or [{}])[0].get("price"),
                "nearest_resistance": (t["levels"]["resistances"] or [{}])[0].get("price")}

    with ThreadPoolExecutor(max_workers=6) as pool:
        # entries starting with ### are section headers in the UI's watchlist
        futs = {s: pool.submit(one, s) for s in wl["symbols"] if not s.startswith("###")}
        for s, fut in futs.items():
            try:
                rows.append(fut.result())
            except Exception as e:
                errors[s] = str(e)
    key = {"trend": "trend_score", "momentum": "momentum_score", "change": "change_pct"}[sort_by]
    rows.sort(key=lambda r: -(r.get(key) if r.get(key) is not None else -1e9))
    return {"watchlist": name, "interval": interval, "ranked": rows, "errors": errors}


# ===================================================================================== screener, macro, alerts


@tool()
def screen_stocks(
    filters: Annotated[list[dict] | None, Field(description="[{field, op, value}], e.g. {'field':'pe','op':'<','value':20}. "
                                                              "Fields: get_guide('screener')")] = None,
    expression: Annotated[str | None, Field(description="Filter expression, e.g. 'roe > 15 and sales_qoq > 20 and pe < 30'")] = None,
    signal: Annotated[str | None, Field(description="Preset scan: top_gainers, top_losers, new_high, new_low, most_active, "
                                                    "unusual_volume, overbought, oversold, gap_up, gap_down, golden_trend, below_200")] = None,
    universe: Annotated[str, Field(description="'all' (~6,000 US stocks), 'sp500', or 'watchlist:<name>'")] = "all",
    sort: Annotated[str | None, Field(description="Field to sort by; prefix '-' for descending (default -market_cap)")] = None,
    columns: Annotated[list[str] | None, Field(description="Fields to return (default: a compact research set)")] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 25,
) -> Any:
    """FinViz-style stock screener over the whole US market: descriptive, valuation, financial, performance and
    technical fields (see get_guide('screener')). Use it for idea generation and relative comparisons, then verify
    finalists with the single-name tools. Returns total matches and the top rows."""
    cols = columns or ["symbol", "name", "sector", "market_cap", "price", "change_pct", "pe", "ps", "sales_qoq",
                       "eps_growth", "net_margin", "roe", "perf_1m", "perf_ytd", "rsi14", "sma200_pct",
                       "from_high_52w", "rel_volume"]
    res = screener.screen(filters, expression, signal, universe, sort, cols, None, 0, limit)
    if res.get("building") and not res["rows"]:
        return {"note": "The market universe is still being built (first run downloads ~6,000 symbols); retry in a minute.",
                "status": res["status"]}
    return {"total": res["total"], "sort": res.get("sort"), "rows": res["rows"]}


@tool()
def get_stock_snapshot(symbol: Symbol) -> Any:
    """Every screener field for one stock in one call (valuation, margins, growth, performance, technicals,
    52-week range, volume) – a FinViz-style quote table, handy for quick comparisons."""
    snap = screener.snapshot(symbol)
    if not snap.get("in_universe"):
        raise KeyError(f"{snap['symbol']} is not in the stock universe (ETFs, indices and non-US listings are not); "
                       "use get_fundamentals / get_technical_summary instead")
    return snap


@tool()
def get_market_breadth(universe: Annotated[str, Field(description="'all' or 'sp500'")] = "all") -> Any:
    """Market internals: advancers/decliners, up/down volume, new 52-week highs/lows, % of stocks above their
    20/50/200-day averages, overbought/oversold counts, cap-weighted sector performance (1d…1y), and the top
    names for each signal (gainers, losers, new highs, unusual volume, gaps …)."""
    b = screener.breadth(universe)
    b.pop("status", None)
    return b


@tool()
def get_sector_performance(
    period: Annotated[Literal["1d", "1w", "1m", "3m", "6m", "ytd", "1y"], Field(description="Return period")] = "1d",
    universe: Annotated[str, Field(description="'sp500' (GICS) or 'all'")] = "sp500",
    top_stocks: Annotated[int, Field(ge=0, le=10)] = 3,
) -> Any:
    """Heatmap data summarised: cap-weighted return per sector and industry for the period, with the largest
    names in each industry. Shows where money is rotating."""
    hm = screener.heatmap(universe, period)
    out = []
    for sec in hm.get("sectors", []):
        inds = []
        for ind in sec["industries"]:
            vals = [(x["value"], x["market_cap"]) for x in ind["stocks"] if x["value"] is not None]
            w = sum(m for _, m in vals)
            inds.append({"industry": ind["name"], "market_cap": ind["market_cap"],
                         "return_pct": round(sum(v * m for v, m in vals) / w, 2) if w else None,
                         "top": [{"symbol": x["symbol"], "return_pct": x["value"]} for x in ind["stocks"][:top_stocks]]})
        out.append({"sector": sec["name"], "return_pct": sec["value"], "market_cap": sec["market_cap"], "industries": inds})
    return {"period": period, "universe": universe, "sectors": out}


@tool()
def get_macro_dashboard() -> Any:
    """US macro backdrop: policy rate, Treasury yields and curve spreads, real yields, breakevens, CPI/core CPI/core
    PCE, unemployment, payrolls, claims, GDP, industrial production, retail sales, sentiment, housing, credit
    spreads, financial conditions, Fed balance sheet, M2, dollar. Each with latest value, prior, change and 1y-ago;
    plus the yield curve now / 1m / 1y ago."""
    d = macro.macro_dashboard()
    for g in d["groups"]:
        for i in g["indicators"]:
            i.pop("spark", None)
    return d


@tool()
def get_macro_series(series_id: Annotated[str, Field(description="FRED id, e.g. UNRATE, CPIAUCSL, DGS10, T10Y2Y")],
                     start: Annotated[str | None, Field(description="YYYY-MM-DD")] = None) -> Any:
    """History of one macro series (dashboard indicators come transformed, e.g. CPI as % y/y)."""
    d = macro.macro_series(series_id, start)
    pts = d["points"]
    if len(pts) > 400:
        pts = pts[:: len(pts) // 400 + 1] + [pts[-1]]
    d["points"] = [{"date": format_ts(p["time"], False), "value": p["value"]} for p in pts]
    return d


@tool()
def get_cross_asset_board(
    groups: Annotated[list[str] | None, Field(description="Subset of: US indices, Futures, Rates, Currencies, Crypto, "
                                                          "Global, Sectors, Factors, Bonds & credit")] = None) -> Any:
    """Cross-asset dashboard: last, daily change, 1w/1m/3m/YTD/1y returns for indices, futures (equity, energy,
    metals, grains), yields, FX, crypto, global indices, sector ETFs, factor ETFs and bond/credit ETFs."""
    b = macro.markets_board(groups)
    for g in b["groups"]:
        for i in g["items"]:
            i.pop("spark", None)
    return b


@tool()
def get_earnings_calendar(
    days: Annotated[int, Field(ge=1, le=31)] = 7,
    universe: Annotated[str, Field(description="'all', 'sp500' or 'watchlist:<name>'")] = "sp500",
    symbols: Annotated[list[str] | None, Field(description="Only these symbols")] = None,
    start: Annotated[str | None, Field(description="YYYY-MM-DD (default today)")] = None,
) -> Any:
    """Upcoming earnings reports (date, before/after market, EPS forecast, # estimates, last year's EPS) with
    sector and market cap. Check it before recommending positions that would be held through a report."""
    return macro.earnings_calendar(days, start, universe, symbols)


@tool()
def get_economic_calendar(days: Annotated[int, Field(ge=1, le=31)] = 7, start: str | None = None,
                          country: Annotated[str | None, Field(description="Default United States; null for all")] = "United States") -> Any:
    """Scheduled economic releases (time GMT, consensus, previous, actual once released): CPI, payrolls, FOMC,
    GDP, PMIs, auctions …"""
    return macro.economic_calendar(days, start, country)


@tool()
def create_alert(
    symbol: Symbol,
    kind: Annotated[Literal["above", "below", "crosses", "change_above", "change_below", "expression"],
                    Field(description="above/below/crosses a price; change_above/below today's % change; "
                                      "expression = indicator condition")],
    value: Annotated[float | None, Field(description="Price level, or % for change alerts")] = None,
    expression: Annotated[str | None, Field(description="For kind=expression, e.g. crosses_above(close, sma(50))")] = None,
    interval: IntervalArg = "1d",
    message: Annotated[str | None, Field(description="What the user should do/know when it fires")] = None,
    repeat: bool = False,
) -> Any:
    """Create an alert the user is notified about live in the web UI (e.g. entry zone reached, stop hit,
    breakout confirmed). Checked every ~20s for prices, every 5 min for indicator conditions."""
    return alerts_svc.create(symbol, kind, value, expression, interval, message, repeat, author=AUTHOR)


@tool()
def list_alerts(symbol: str | None = None,
                status: Annotated[Literal["active", "triggered", "paused"] | None, Field()] = None) -> Any:
    """Alerts with their status, last check and trigger details."""
    return alerts_svc.list_(symbol, status)


@tool()
def delete_alert(alert_id: str) -> Any:
    """Delete an alert by id (ids come from list_alerts or create_alert)."""
    return alerts_svc.delete(alert_id)


@tool()
def save_screen(name: str,
                filters: list[dict] | None = None, expression: str | None = None, signal: str | None = None,
                universe: str = "all", sort: str | None = None,
                view: Annotated[str, Field(description="overview | valuation | financial | performance | technical")] = "overview") -> Any:
    """Save a screen so the user can open it from the Screener page (and re-run it later)."""
    return screener.save_screen(name, {"filters": filters or [], "expression": expression, "signal": signal,
                                       "universe": universe, "sort": sort, "view": view})


@tool()
def show_in_ui(
    page: Annotated[Literal["chart", "screener", "heatmap", "markets", "calendar", "trading"], Field(description="Page to open")],
    symbol: Annotated[str | None, Field(description="For page=chart")] = None,
    screen: Annotated[dict | None, Field(description="For page=screener: {filters, expression, signal, universe, sort, view}")] = None,
    period: Annotated[str | None, Field(description="For page=heatmap: 1d 1w 1m 3m 6m ytd 1y")] = None,
) -> Any:
    """Switch the user's web UI to a page so they see what you are discussing (e.g. the screener with your
    filters applied, or the sector heatmap). Use set_chart_view for chart symbol/interval/indicators."""
    payload = {"page": page, "symbol": symbol.upper() if symbol else None, "screen": screen, "period": period}
    bus.publish("ui.navigate", payload)
    return {"ok": True, **payload}


@tool()
def get_guide(topic: Literal["workflow", "expressions", "annotations", "interpretation", "screener", "trading"]) -> Any:
    """Reference: research workflow, expression language, drawing types/conventions, interpretation notes,
    screener fields and syntax, and the trading workflow (sizing, brackets, approvals, limits)."""
    return {"topic": topic, "text": guides.GUIDES[topic]}


# ===================================================================================== trading


OrderSide = Annotated[Literal["buy", "sell"], Field(description="buy (long entry / cover short) or sell")]


def _spec(symbol, side, qty, order_type, limit_price, stop_price, stop_loss, take_profit, tif, rationale, analysis_id):
    return OrderSpec(symbol=symbol, side=side, qty=qty, order_type=order_type, limit_price=limit_price,
                     stop_price=stop_price, stop_loss=stop_loss, take_profit=take_profit, tif=tif,
                     rationale=rationale, analysis_id=analysis_id)


@tool()
def get_portfolio() -> Any:
    """The connected brokerage account: status (broker, paper/live, connected, halted, approval mode), net
    liquidation, cash vs invested capital, open risk to stops, positions (weight, P&L, stop/target), working
    orders, pending proposals and the risk limits your proposals must pass. Call this before proposing trades."""
    ov = trading.overview()
    ov["pending_tickets"] = trading.tickets(status="pending")
    ov["limits"] = trading.settings().model_dump()
    return ov


@tool()
def get_trade_history(symbol: Annotated[str | None, Field(description="Only this symbol")] = None,
                      status: Annotated[Literal["open", "closed"] | None, Field(description="Filter")] = None,
                      limit: Annotated[int, Field(ge=1, le=500)] = 50) -> Any:
    """Round-trip trades built from fills (entry/exit averages, net P&L after commission, return %, R multiple vs
    the planned stop, holding time) plus aggregate stats (win rate, profit factor, expectancy, avg R). Use it
    to review what worked before proposing new trades."""
    res = trading.trades(symbol, status, limit)
    for t in res["trades"]:
        t["fills"] = [{k: f[k] for k in ("time", "side", "qty", "price", "action")} for f in t["fills"]][:20]
    return res


@tool()
def preview_order(
    symbol: Symbol, side: OrderSide,
    qty: Annotated[int, Field(gt=0, description="Shares")],
    order_type: Annotated[Literal["limit", "stop", "stop_limit", "market"], Field(description="limit for entries (market only closes)")] = "limit",
    limit_price: float | None = None, stop_price: float | None = None,
    stop_loss: Annotated[float | None, Field(description="Protective stop (required for your entries)")] = None,
    take_profit: Annotated[float | None, Field(description="Profit target")] = None,
    tif: Literal["day", "gtc"] = "day",
) -> Any:
    """Dry-run an order against the account and the risk limits: errors that would block it, warnings, and the
    estimate (value, % of equity, position after, risk to stop in $ and % of equity, reward:risk). No side effects."""
    return trading.preview(_spec(symbol, side, qty, order_type, limit_price, stop_price, stop_loss, take_profit, tif,
                                 None, None), "agent")


@tool()
def propose_order(
    symbol: Symbol, side: OrderSide,
    qty: Annotated[int, Field(gt=0, description="Shares (size from risk: risk $ / (entry - stop))")],
    rationale: Annotated[str, Field(min_length=20, description="Why, the setup, invalidation and what you checked; "
                                                                "shown to the user when they approve")],
    order_type: Annotated[Literal["limit", "stop", "stop_limit", "market"], Field(description="limit for entries (market only closes)")] = "limit",
    limit_price: float | None = None, stop_price: float | None = None,
    stop_loss: Annotated[float | None, Field(description="Protective stop (required for entries)")] = None,
    take_profit: Annotated[float | None, Field(description="Profit target")] = None,
    tif: Literal["day", "gtc"] = "day",
    analysis_id: Annotated[str | None, Field(description="The save_analysis id this trade implements")] = None,
) -> Any:
    """Propose an order. It is risk-checked, then shown to the user as a ticket to approve or reject (approval
    mode), or sent at once when the user enabled auto mode on a paper account. Entries are sent as brackets:
    the stop-loss and take-profit orders activate when the entry fills. Returns the ticket; check its status
    later with get_order_tickets. You cannot approve tickets yourself."""
    t = trading.submit(_spec(symbol, side, qty, order_type, limit_price, stop_price, stop_loss, take_profit, tif,
                             rationale, analysis_id), "agent")
    return {**t, "next": "Waiting for the user's approval in the Trading page" if t["status"] == "pending"
            else f"Sent to the broker ({t['status']})"}


@tool()
def propose_close_position(
    symbol: Symbol,
    rationale: Annotated[str, Field(min_length=10, description="Why close now")],
    qty: Annotated[int | None, Field(gt=0, description="Shares to close (default: all)")] = None,
    order_type: Literal["market", "limit"] = "market",
    limit_price: float | None = None,
) -> Any:
    """Propose closing (part of) a position. Same approval flow as propose_order. Leftover stop-loss/take-profit
    orders are cancelled (or shrunk) automatically once the position is closed (or reduced)."""
    return trading.close_position(symbol, ClosePosition(qty=qty, order_type=order_type, limit_price=limit_price,
                                                        rationale=rationale), "agent")


@tool()
def get_order_tickets(status: Annotated[str | None, Field(description="pending, working, filled, rejected, expired, "
                                                                       "cancelled, error (comma-separate several)")] = None,
                      symbol: str | None = None, limit: Annotated[int, Field(ge=1, le=200)] = 30) -> Any:
    """Your and the user's order tickets with their outcome (approved and working/filled, rejected with the
    user's reason, expired, broker errors)."""
    return trading.tickets(status, symbol, limit)


@tool()
def cancel_order(
    order_id: Annotated[str | None, Field(description="Working broker order id (from get_portfolio)")] = None,
    ticket_id: Annotated[str | None, Field(description="Your pending proposal to withdraw")] = None,
    reason: str | None = None,
) -> Any:
    """Withdraw one of your pending proposals, or cancel a working order placed through Autobench (e.g. an entry
    that no longer makes sense). Stop-losses protecting an open position cannot be cancelled by you."""
    if ticket_id:
        return trading.reject(ticket_id, reason, "agent")
    if order_id:
        return trading.cancel_order(order_id, "agent")
    raise ValueError("Give order_id or ticket_id")


# ===================================================================================== prompts


@mcp.prompt()
def analyze_stock(symbol: str, horizon: str = "swing") -> str:
    """Full, evidence-based analysis of one stock ending in a recorded recommendation and chart drawings."""
    return (f"Perform a rigorous {horizon} analysis of {symbol.upper()} using the Autobench tools.\n\n{guides.WORKFLOW}\n"
            "Quantify claims, test technical hypotheses with run_signal_study/run_backtest before relying on them, "
            "draw the key levels and trade plan on the chart with one tag, save the analysis, then report: verdict, "
            "evidence (technical, fundamental, sentiment, risk), bull vs bear case, plan, and what would invalidate it.")


@mcp.prompt()
def review_watchlist(name: str = "default") -> str:
    """Scan a watchlist and shortlist the best setups."""
    return (f"Run scan_watchlist('{name}') and get_market_overview. Shortlist the 3 strongest and 3 weakest setups, "
            "explain why with numbers, and for the top candidate do a condensed analysis (multi-timeframe, levels, "
            "one signal study). Note anything to revisit with save_note.")


@mcp.prompt()
def review_track_record() -> str:
    """Audit past recommendations and extract lessons."""
    return ("Call get_track_record. Summarise hit rate, average R, alpha vs SPY and calibration. Identify systematic "
            "errors (e.g. overconfidence, stops too tight, fighting the trend, ignoring earnings dates) by examining "
            "the losing calls with get_analysis. Save the lessons with save_note(tags=['lessons']).")


@mcp.prompt()
def compare_stocks(symbols: str) -> str:
    """Compare several stocks (comma-separated) and rank them."""
    return (f"Compare {symbols} with compare_peers, get_correlations, get_multi_timeframe and get_risk_metrics for "
            "each. Rank them on valuation, quality, growth, momentum and risk; state which you would prefer and why.")


@mcp.resource("autobench://guide/{topic}")
def guide_resource(topic: str) -> str:
    """Autobench guides: workflow, expressions, annotations, interpretation, screener."""
    if topic not in guides.GUIDES:
        raise ValueError(f"Unknown guide '{topic}'")
    return guides.GUIDES[topic]
