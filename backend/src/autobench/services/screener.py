"""Stock screener over the market universe (FinViz-style): field metadata with preset filter
choices, structured filters, signals, a safe filter-expression language, column views, sorting,
saved screens. Also the heatmap tree and market breadth/movers built on the same table."""

from __future__ import annotations

import ast
import math
import operator
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from .. import db
from .events import bus
from .universe import universe

# ------------------------------------------------------------------------------- fields


@dataclass
class Field:
    key: str
    label: str
    group: str  # Descriptive | Valuation | Financial | Performance | Technical
    fmt: str  # text | price | pct | num | ratio | compact | int | bool
    desc: str = ""
    presets: list[dict] = field(default_factory=list)


def _under_over(values: list[float], unit: str = "", lo_label: str = "Under", hi_label: str = "Over") -> list[dict]:
    out = [{"label": f"{lo_label} {v:g}{unit}", "op": "<", "value": v} for v in values]
    out += [{"label": f"{hi_label} {v:g}{unit}", "op": ">", "value": v} for v in values]
    return out


def _pct_presets(values: list[float]) -> list[dict]:
    out = [{"label": "Positive (>0%)", "op": ">", "value": 0}, {"label": "Negative (<0%)", "op": "<", "value": 0}]
    out += [{"label": f"Up {v:g}%+", "op": ">", "value": v} for v in values]
    out += [{"label": f"Down {v:g}%+", "op": "<", "value": -v} for v in values]
    return out


B = 1e9
FIELDS: list[Field] = [
    # descriptive
    Field("symbol", "Ticker", "Descriptive", "text"),
    Field("name", "Company", "Descriptive", "text"),
    Field("sector", "Sector", "Descriptive", "text", "GICS sector"),
    Field("industry", "Industry", "Descriptive", "text", "GICS sub-industry for S&P 500 members, else Nasdaq industry"),
    Field("country", "Country", "Descriptive", "text"),
    Field("exchange", "Exchange", "Descriptive", "text"),
    Field("index", "Index", "Descriptive", "text", "", [{"label": "S&P 500", "op": "==", "value": "S&P 500"}]),
    Field("market_cap", "Market Cap", "Descriptive", "compact", "Shares outstanding × price", [
        {"label": "Mega ($200B+)", "op": ">=", "value": 200 * B},
        {"label": "Large ($10B–$200B)", "op": "between", "value": [10 * B, 200 * B]},
        {"label": "Mid ($2B–$10B)", "op": "between", "value": [2 * B, 10 * B]},
        {"label": "Small ($300M–$2B)", "op": "between", "value": [0.3 * B, 2 * B]},
        {"label": "Micro ($50M–$300M)", "op": "between", "value": [0.05 * B, 0.3 * B]},
        {"label": "Nano (under $50M)", "op": "<", "value": 0.05 * B},
        {"label": "+Large (over $10B)", "op": ">=", "value": 10 * B},
        {"label": "+Mid (over $2B)", "op": ">=", "value": 2 * B},
        {"label": "+Small (over $300M)", "op": ">=", "value": 0.3 * B},
        {"label": "-Mid (under $10B)", "op": "<", "value": 10 * B},
        {"label": "-Small (under $2B)", "op": "<", "value": 2 * B},
    ]),
    Field("price", "Price", "Descriptive", "price", "", _under_over([1, 5, 10, 20, 50, 100], "$")),
    Field("change_pct", "Change", "Descriptive", "pct", "Today's change", _pct_presets([1, 2, 5, 10, 20])),
    Field("volume", "Volume", "Descriptive", "compact", "Today's volume (consolidated, may be delayed)",
          _under_over([50e3, 100e3, 500e3, 1e6, 2e6])),
    Field("avg_volume", "Avg Volume", "Descriptive", "compact", "3-month average daily volume",
          _under_over([50e3, 100e3, 200e3, 500e3, 750e3, 1e6, 2e6])),
    Field("rel_volume", "Rel Volume", "Descriptive", "ratio", "Today's volume / 20-day average",
          _under_over([0.5, 0.75, 1, 1.5, 2, 3, 5, 10])),
    Field("dollar_volume", "$ Volume", "Descriptive", "compact", "Average daily dollar volume",
          _under_over([1e6, 10e6, 50e6, 100e6, 1e9])),
    Field("ipo_year", "IPO Year", "Descriptive", "int", "", [
        {"label": "Last 1 year", "op": ">=", "value": pd.Timestamp.now().year - 1},
        {"label": "Last 3 years", "op": ">=", "value": pd.Timestamp.now().year - 3},
        {"label": "Last 5 years", "op": ">=", "value": pd.Timestamp.now().year - 5},
        {"label": "More than 10 years ago", "op": "<", "value": pd.Timestamp.now().year - 10},
    ]),
    # valuation
    Field("pe", "P/E", "Valuation", "num", "Price / diluted EPS (TTM)", [
        {"label": "Low (<15)", "op": "<", "value": 15}, {"label": "High (>50)", "op": ">", "value": 50},
        {"label": "Profitable (>0)", "op": ">", "value": 0}, *_under_over([5, 10, 20, 25, 30, 40])]),
    Field("ps", "P/S", "Valuation", "num", "Market cap / revenue (TTM)", _under_over([1, 2, 3, 5, 10])),
    Field("pb", "P/B", "Valuation", "num", "Market cap / book equity", _under_over([1, 2, 3, 5, 10])),
    Field("pfcf", "P/FCF", "Valuation", "num", "Market cap / free cash flow (last FY)", _under_over([10, 15, 20, 30, 50])),
    Field("ev_sales", "EV/Sales", "Valuation", "num", "", _under_over([1, 2, 5, 10])),
    Field("div_yield", "Dividend %", "Valuation", "pct", "TTM dividends per share / price", [
        {"label": "None (0%)", "op": "isnull", "value": None}, {"label": "Positive (>0%)", "op": ">", "value": 0},
        {"label": "High (>5%)", "op": ">", "value": 5}, {"label": "Very high (>10%)", "op": ">", "value": 10},
        *[{"label": f"Over {v}%", "op": ">", "value": v} for v in (1, 2, 3, 4)]]),
    Field("payout", "Payout %", "Valuation", "pct", "Dividends / EPS", _under_over([25, 50, 75, 100], "%")),
    # financial
    Field("revenue_ttm", "Revenue", "Financial", "compact", "Revenue (TTM)", _under_over([0.1 * B, 1 * B, 10 * B, 50 * B])),
    Field("net_income_ttm", "Net Income", "Financial", "compact", "Net income (TTM)",
          [{"label": "Positive", "op": ">", "value": 0}, {"label": "Negative", "op": "<", "value": 0}]),
    Field("eps_ttm", "EPS", "Financial", "num", "Diluted EPS (TTM)",
          [{"label": "Positive", "op": ">", "value": 0}, {"label": "Negative", "op": "<", "value": 0}]),
    Field("sales_qoq", "Sales Q/Q", "Financial", "pct", "Latest quarter revenue vs a year earlier", _pct_presets([5, 10, 20, 30, 50])),
    Field("eps_qoq", "EPS Q/Q", "Financial", "pct", "Latest quarter EPS vs a year earlier", _pct_presets([10, 20, 30, 50, 100])),
    Field("sales_growth", "Sales TTM Y/Y", "Financial", "pct", "TTM revenue vs the prior TTM", _pct_presets([5, 10, 20, 30])),
    Field("eps_growth", "EPS TTM Y/Y", "Financial", "pct", "TTM EPS vs the prior TTM", _pct_presets([10, 20, 30, 50])),
    Field("gross_margin", "Gross Margin", "Financial", "pct", "", _under_over([10, 20, 30, 40, 50, 60, 70], "%")),
    Field("oper_margin", "Oper. Margin", "Financial", "pct", "", _under_over([0, 10, 20, 30, 40], "%")),
    Field("net_margin", "Profit Margin", "Financial", "pct", "", _under_over([0, 5, 10, 20, 30], "%")),
    Field("roe", "ROE", "Financial", "pct", "Net income TTM / equity", _under_over([0, 5, 10, 15, 20, 30], "%")),
    Field("roa", "ROA", "Financial", "pct", "Net income TTM / assets", _under_over([0, 5, 10, 15], "%")),
    Field("debt_eq", "Debt/Eq", "Financial", "ratio", "Long-term debt / equity", _under_over([0.1, 0.5, 1, 2])),
    Field("current_ratio", "Current Ratio", "Financial", "ratio", "", _under_over([0.5, 1, 1.5, 2, 3])),
    Field("fcf", "Free Cash Flow", "Financial", "compact", "Operating cash flow − capex (last FY)",
          [{"label": "Positive", "op": ">", "value": 0}, {"label": "Negative", "op": "<", "value": 0}]),
    # performance
    Field("perf_today", "Perf Today", "Performance", "pct", "", _pct_presets([1, 2, 5, 10])),
    Field("perf_1w", "Perf Week", "Performance", "pct", "", _pct_presets([5, 10, 20, 30])),
    Field("perf_1m", "Perf Month", "Performance", "pct", "", _pct_presets([10, 20, 30, 50])),
    Field("perf_3m", "Perf Quarter", "Performance", "pct", "", _pct_presets([10, 20, 30, 50])),
    Field("perf_6m", "Perf Half", "Performance", "pct", "", _pct_presets([10, 20, 30, 50, 100])),
    Field("perf_ytd", "Perf YTD", "Performance", "pct", "", _pct_presets([10, 20, 30, 50, 100])),
    Field("perf_1y", "Perf Year", "Performance", "pct", "", _pct_presets([10, 20, 30, 50, 100])),
    # technical
    Field("rsi14", "RSI (14)", "Technical", "num", "", [
        {"label": "Overbought (>70)", "op": ">", "value": 70}, {"label": "Oversold (<30)", "op": "<", "value": 30},
        {"label": "Overbought (>80)", "op": ">", "value": 80}, {"label": "Oversold (<20)", "op": "<", "value": 20},
        {"label": "Not overbought (<60)", "op": "<", "value": 60}, {"label": "Not oversold (>40)", "op": ">", "value": 40}]),
    Field("sma20_pct", "SMA20 %", "Technical", "pct", "Price vs 20-day SMA", _pct_presets([2, 5, 10, 20])),
    Field("sma50_pct", "SMA50 %", "Technical", "pct", "Price vs 50-day SMA", _pct_presets([5, 10, 20, 30])),
    Field("sma200_pct", "SMA200 %", "Technical", "pct", "Price vs 200-day SMA", _pct_presets([5, 10, 20, 50])),
    Field("sma50_vs_sma200", "SMA50/200 %", "Technical", "pct", "50-day vs 200-day SMA",
          [{"label": "SMA50 above SMA200", "op": ">", "value": 0}, {"label": "SMA50 below SMA200", "op": "<", "value": 0}]),
    Field("from_high_52w", "From 52W High", "Technical", "pct", "Distance below the 52-week high", [
        {"label": "New high (0%)", "op": ">=", "value": -0.01},
        *[{"label": f"Within {v}% of high", "op": ">=", "value": -v} for v in (3, 5, 10, 20)],
        *[{"label": f"{v}%+ below high", "op": "<=", "value": -v} for v in (10, 20, 30, 50)]]),
    Field("from_low_52w", "From 52W Low", "Technical", "pct", "Distance above the 52-week low", [
        {"label": "New low (0%)", "op": "<=", "value": 0.01},
        *[{"label": f"Within {v}% of low", "op": "<=", "value": v} for v in (3, 5, 10)],
        *[{"label": f"{v}%+ above low", "op": ">=", "value": v} for v in (20, 50, 100)]]),
    Field("atr_pct", "ATR %", "Technical", "pct", "14-day ATR / price", _under_over([1, 2, 3, 5, 8], "%")),
    Field("atr14", "ATR", "Technical", "num", "14-day average true range"),
    Field("volatility_1m", "Volatility M", "Technical", "pct", "1-month annualized volatility", _under_over([20, 30, 50, 75, 100], "%")),
    Field("range_1w", "Range W", "Technical", "pct", "Average daily high-low range over the week", _under_over([1, 2, 3, 5], "%")),
    Field("beta", "Beta", "Technical", "num", "1-year beta vs SPY", _under_over([0.5, 1, 1.5, 2])),
    Field("gap_pct", "Gap", "Technical", "pct", "Today's open vs previous close", _pct_presets([1, 2, 5, 10])),
    Field("change_from_open", "Chg from Open", "Technical", "pct", "", _pct_presets([1, 2, 5, 10])),
    Field("streak", "Streak", "Technical", "int", "Consecutive up (+) or down (−) closes", [
        *[{"label": f"Up {v}+ days", "op": ">=", "value": v} for v in (3, 5, 7)],
        *[{"label": f"Down {v}+ days", "op": "<=", "value": -v} for v in (3, 5, 7)]]),
    Field("high_52w", "52W High", "Technical", "price"),
    Field("low_52w", "52W Low", "Technical", "price"),
]
FIELD_MAP = {f.key: f for f in FIELDS}

VIEWS = {
    "overview": ["symbol", "name", "sector", "industry", "country", "market_cap", "pe", "price", "change_pct", "volume"],
    "valuation": ["symbol", "market_cap", "pe", "ps", "pb", "pfcf", "ev_sales", "div_yield", "payout", "eps_ttm",
                  "eps_growth", "sales_growth", "price", "change_pct"],
    "financial": ["symbol", "market_cap", "revenue_ttm", "net_income_ttm", "gross_margin", "oper_margin", "net_margin",
                  "roe", "roa", "debt_eq", "current_ratio", "sales_qoq", "eps_qoq", "fcf", "price", "change_pct"],
    "performance": ["symbol", "perf_1w", "perf_1m", "perf_3m", "perf_6m", "perf_ytd", "perf_1y", "volatility_1m",
                    "rel_volume", "avg_volume", "price", "change_pct", "volume"],
    "technical": ["symbol", "beta", "atr_pct", "sma20_pct", "sma50_pct", "sma200_pct", "from_high_52w", "from_low_52w",
                  "rsi14", "gap_pct", "change_from_open", "streak", "price", "change_pct", "volume"],
}

SIGNALS = {
    "top_gainers": ("Top Gainers", "change_pct > 0 and price >= 1 and avg_volume >= 100000", "-change_pct"),
    "top_losers": ("Top Losers", "change_pct < 0 and price >= 1 and avg_volume >= 100000", "change_pct"),
    "new_high": ("New 52W High", "from_high_52w >= -0.01 and avg_volume >= 100000 and price >= 1", "-change_pct"),
    "new_low": ("New 52W Low", "from_low_52w <= 0.01 and avg_volume >= 100000 and price >= 1", "change_pct"),
    "most_active": ("Most Active", "volume > 0 and price >= 1", "-volume"),
    "most_volatile": ("Most Volatile", "avg_volume >= 100000", "-range_1w"),
    "unusual_volume": ("Unusual Volume", "rel_volume >= 2 and avg_volume >= 100000", "-rel_volume"),
    "overbought": ("Overbought", "rsi14 > 70 and avg_volume >= 100000", "-rsi14"),
    "oversold": ("Oversold", "rsi14 < 30 and avg_volume >= 100000", "rsi14"),
    "gap_up": ("Gap Up", "gap_pct >= 3 and avg_volume >= 100000", "-gap_pct"),
    "gap_down": ("Gap Down", "gap_pct <= -3 and avg_volume >= 100000", "gap_pct"),
    "golden_trend": ("Strong Uptrend", "sma20_pct > 0 and sma50_pct > 0 and sma200_pct > 0 and sma50_vs_sma200 > 0",
                     "-perf_3m"),
    "below_200": ("Below SMA200", "sma200_pct < 0 and avg_volume >= 100000", "sma200_pct"),
}


def meta() -> dict:
    return {"fields": [asdict(f) for f in FIELDS], "views": VIEWS,
            "signals": {k: v[0] for k, v in SIGNALS.items()}, "screens": list_screens(),
            "sectors": _distinct("sector"), "countries": _distinct("country"), "industries": _distinct("industry"),
            "exchanges": _distinct("exchange"), "status": universe.status()}


def _distinct(col: str) -> list[str]:
    t = universe.table()
    if t.empty or col not in t.columns:
        return []
    return sorted(v for v in t[col].dropna().unique() if isinstance(v, str))


# ------------------------------------------------------------------------------- filter expressions

_CMP = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
        ast.Eq: operator.eq, ast.NotEq: operator.ne}
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
_FUNCS = {"abs": lambda x: x.abs() if isinstance(x, pd.Series) else abs(x)}


def eval_expression(expr: str, df: pd.DataFrame) -> pd.Series:
    """Evaluate a filter like `pe < 20 and sales_qoq > 10 and sector == "Energy"` against the table.
    Only field names, numbers, strings, lists, comparisons, arithmetic, and/or/not, `in` and abs()."""
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as e:
        raise ValueError(f"Invalid filter expression: {e.msg}") from None

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.BoolOp):
            vals = [_as_bool(ev(v), df) for v in node.values]
            out = vals[0]
            for v in vals[1:]:
                out = (out & v) if isinstance(node.op, ast.And) else (out | v)
            return out
        if isinstance(node, ast.UnaryOp):
            v = ev(node.operand)
            if isinstance(node.op, ast.Not):
                return ~_as_bool(v, df)
            if isinstance(node.op, ast.USub):
                return -v
            raise ValueError("unsupported operator")
        if isinstance(node, ast.Compare):
            left = ev(node.left)
            result = None
            for op, comp in zip(node.ops, node.comparators):
                right = ev(comp)
                if isinstance(op, (ast.In, ast.NotIn)):
                    if not isinstance(right, list):
                        raise ValueError("'in' needs a list, e.g. sector in [\"Energy\", \"Utilities\"]")
                    part = left.isin(right) if isinstance(left, pd.Series) else pd.Series(left in right, index=df.index)
                    if isinstance(op, ast.NotIn):
                        part = ~part
                elif type(op) in _CMP:
                    part = _CMP[type(op)](left, right)
                    if isinstance(part, pd.Series):
                        part = part.fillna(False).astype(bool)
                else:
                    raise ValueError("unsupported comparison")
                result = part if result is None else (result & part)
                left = right
            return result
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            return _BIN[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.Name):
            key = node.id
            if key in ("True", "true"):
                return True
            if key in ("False", "false"):
                return False
            if key == "symbol":
                return pd.Series(df.index, index=df.index)
            if key not in df.columns or key not in FIELD_MAP and key not in ("sp500",):
                raise ValueError(f"Unknown field '{key}'. Fields: {', '.join(f.key for f in FIELDS)}")
            return df[key]
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str)):
            return node.value
        if isinstance(node, ast.List):
            return [ev(e) for e in node.elts]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and len(node.args) == 1:
            return _FUNCS[node.func.id](ev(node.args[0]))
        raise ValueError(f"Unsupported syntax in filter: {ast.dump(node)[:60]}")

    return _as_bool(ev(tree), df)


def _as_bool(v, df: pd.DataFrame) -> pd.Series:
    if isinstance(v, pd.Series):
        return v.fillna(False).astype(bool)
    return pd.Series(bool(v), index=df.index)


# ------------------------------------------------------------------------------- structured filters


def _apply_filter(df: pd.DataFrame, f: dict) -> pd.Series:
    key, op, value = f.get("field"), f.get("op", "=="), f.get("value")
    if key == "symbol":
        col = pd.Series(df.index, index=df.index)
    elif key not in df.columns:
        raise ValueError(f"Unknown field '{key}'")
    else:
        col = df[key]
    if op == "isnull":
        return col.isna() | (col == 0)
    if op == "notnull":
        return col.notna()
    if op == "between":
        lo, hi = value
        return (col >= lo) & (col <= hi)
    if op == "in":
        return col.isin(value if isinstance(value, list) else [value])
    if op == "contains":
        return col.astype(str).str.contains(str(value), case=False, na=False, regex=False)
    if op not in ("<", "<=", ">", ">=", "==", "!="):
        raise ValueError(f"Unknown operator '{op}'")
    fn = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge, "==": operator.eq, "!=": operator.ne}[op]
    return fn(col, value).fillna(False).astype(bool)


def _universe_mask(df: pd.DataFrame, which: str | None) -> pd.Series:
    if not which or which == "all":
        return pd.Series(True, index=df.index)
    if which == "sp500":
        return df.get("sp500", pd.Series(False, index=df.index)).fillna(False).astype(bool)
    if which.startswith("watchlist:"):
        from .workspace import get_watchlist

        syms = set(get_watchlist(which.split(":", 1)[1])["symbols"])
        return pd.Series(df.index.isin(syms), index=df.index)
    raise ValueError("universe must be 'all', 'sp500' or 'watchlist:<name>'")


def screen(filters: list[dict] | None = None, expression: str | None = None, signal: str | None = None,
           universe_name: str | None = "all", sort: str | None = None, columns: list[str] | None = None,
           view: str | None = "overview", offset: int = 0, limit: int = 50, search: str | None = None) -> dict:
    universe.ensure()  # refresh stale layers in the background
    df = universe.table()
    status = universe.status()
    if df.empty:
        return {"total": 0, "rows": [], "columns": columns or VIEWS.get(view or "overview"), "status": status,
                "building": True}
    mask = _universe_mask(df, universe_name)
    for f in filters or []:
        mask &= _apply_filter(df, f)
    if signal:
        if signal not in SIGNALS:
            raise ValueError(f"Unknown signal '{signal}'. Options: {', '.join(SIGNALS)}")
        _, sig_expr, sig_sort = SIGNALS[signal]
        mask &= eval_expression(sig_expr, df)
        sort = sort or sig_sort
    if expression and expression.strip():
        mask &= eval_expression(expression, df)
    if search:
        s = search.strip().upper()
        mask &= pd.Series(df.index.str.startswith(s), index=df.index) | df["name"].fillna("").str.upper().str.contains(s, regex=False)
    hits = df[mask]
    sort = sort or "-market_cap"
    desc = sort.startswith("-")
    key = sort.lstrip("-+")
    if key == "symbol":
        hits = hits.sort_index(ascending=not desc)
    elif key in hits.columns:
        hits = hits.sort_values(key, ascending=not desc, na_position="last", kind="stable")
    cols = columns or VIEWS.get(view or "overview", VIEWS["overview"])
    unknown = [c for c in cols if c not in FIELD_MAP]
    if unknown:
        raise ValueError(f"Unknown columns: {', '.join(unknown)}")
    page = hits.iloc[offset:offset + limit]
    rows = [_row(sym, r, cols) for sym, r in page.iterrows()]
    return {"total": int(len(hits)), "offset": offset, "rows": rows, "columns": cols, "sort": sort,
            "status": status, "building": any(v["refreshing"] for v in status.values())}


def _clean(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else round(f, 4)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def _row(sym: str, r: pd.Series, cols: list[str]) -> dict:
    out = {"symbol": sym}
    for c in cols:
        if c != "symbol":
            out[c] = _clean(r.get(c))
    return out


def snapshot(symbol: str) -> dict:
    """Every universe field for one symbol (the FinViz-style quote table)."""
    df = universe.table()
    sym = symbol.upper()
    if df.empty or sym not in df.index:
        # ETFs, indices, futures etc. are not in the stock universe: not an error for the UI
        return {"symbol": sym, "in_universe": False}
    r = df.loc[sym]
    out = {"symbol": sym, "in_universe": True}
    for f in FIELDS:
        if f.key != "symbol":
            out[f.key] = _clean(r.get(f.key))
    for extra in ("fiscal_period_end", "bar_date", "shares", "sma20", "sma50", "sma200", "avg_volume_20", "ev"):
        out[extra] = _clean(r.get(extra))
    return out


# ------------------------------------------------------------------------------- saved screens


def list_screens() -> list[dict]:
    data = db.state_get("screens", {"items": {}}) or {"items": {}}
    return [{"name": k, **v} for k, v in sorted(data["items"].items())]


def save_screen(name: str, spec: dict) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("name is required")
    allowed = {"filters", "expression", "signal", "universe", "sort", "view", "columns"}
    spec = {k: v for k, v in spec.items() if k in allowed}
    if spec.get("expression"):
        eval_expression(spec["expression"], universe.table() if not universe.table().empty else pd.DataFrame(
            columns=[f.key for f in FIELDS]))
    data = db.state_get("screens", {"items": {}}) or {"items": {}}
    data["items"][name] = spec
    db.state_set("screens", data)
    bus.publish("screens.updated", {"name": name})
    return {"name": name, **spec}


def delete_screen(name: str) -> dict:
    data = db.state_get("screens", {"items": {}}) or {"items": {}}
    if name not in data["items"]:
        raise KeyError(f"No saved screen '{name}'")
    del data["items"][name]
    db.state_set("screens", data)
    bus.publish("screens.updated", {"name": name})
    return {"deleted": name}


# ------------------------------------------------------------------------------- heatmap & breadth

PERIOD_FIELD = {"1d": "change_pct", "1w": "perf_1w", "1m": "perf_1m", "3m": "perf_3m", "6m": "perf_6m",
                "ytd": "perf_ytd", "1y": "perf_1y", "rel_volume": "rel_volume", "pe": "pe", "rsi": "rsi14"}


def heatmap(universe_name: str = "sp500", period: str = "1d", min_cap: float = 0, limit: int = 1500) -> dict:
    """Sector -> industry -> stock tree sized by market cap and colored by the period's change."""
    universe.ensure()
    df = universe.table()
    if df.empty:
        return {"sectors": [], "building": True, "status": universe.status()}
    metric = PERIOD_FIELD.get(period)
    if metric is None:
        raise ValueError(f"period must be one of {', '.join(PERIOD_FIELD)}")
    d = df[_universe_mask(df, universe_name)]
    d = d[d["market_cap"].notna() & (d["market_cap"] > min_cap) & d["sector"].notna()]
    d = d.sort_values("market_cap", ascending=False).head(limit)
    sectors = []
    for sector, g in d.groupby("sector"):
        industries = []
        for industry, gi in g.groupby(g["industry"].fillna("Other")):
            industries.append({"name": industry, "market_cap": float(gi["market_cap"].sum()), "stocks": [
                {"symbol": s, "name": r["name"], "market_cap": float(r["market_cap"]), "value": _clean(r.get(metric)),
                 "price": _clean(r.get("price")), "change_pct": _clean(r.get("change_pct"))}
                for s, r in gi.iterrows()]})
        industries.sort(key=lambda x: -x["market_cap"])
        w = g["market_cap"]
        vals = g[metric]
        ok = vals.notna()
        avg = float((vals[ok] * w[ok]).sum() / w[ok].sum()) if ok.any() else None
        sectors.append({"name": sector, "market_cap": float(w.sum()), "value": _clean(avg), "industries": industries})
    sectors.sort(key=lambda x: -x["market_cap"])
    return {"universe": universe_name, "period": period, "metric": metric, "sectors": sectors,
            "count": int(len(d)), "status": universe.status()}


def breadth(universe_name: str = "all") -> dict:
    """Advance/decline, highs/lows, trend participation, sector performance and FinViz-style
    signal lists (top gainers, losers, new highs, unusual volume …)."""
    universe.ensure()
    df = universe.table()
    if df.empty:
        return {"building": True, "status": universe.status()}
    d = df[_universe_mask(df, universe_name)]
    liquid = d[(d["price"] >= 1) & (d.get("avg_volume", 0) >= 50_000)] if "avg_volume" in d.columns else d
    ch = liquid["change_pct"]
    vol = liquid.get("volume", pd.Series(dtype=float))

    def pct(mask):
        n = mask.notna().sum() if hasattr(mask, "notna") else len(mask)
        return round(float(mask.fillna(False).sum()) / max(1, len(liquid)) * 100, 1)

    out: dict[str, Any] = {
        "universe": universe_name, "count": int(len(liquid)),
        "advancing": int((ch > 0).sum()), "declining": int((ch < 0).sum()), "unchanged": int((ch == 0).sum()),
        "up_volume": _clean(vol[ch > 0].sum()) if len(vol) else None,
        "down_volume": _clean(vol[ch < 0].sum()) if len(vol) else None,
        "new_highs": int((liquid.get("from_high_52w", pd.Series(dtype=float)) >= -0.01).sum()),
        "new_lows": int((liquid.get("from_low_52w", pd.Series(dtype=float)) <= 0.01).sum()),
        "above_sma20_pct": pct(liquid.get("sma20_pct", pd.Series(dtype=float)) > 0),
        "above_sma50_pct": pct(liquid.get("sma50_pct", pd.Series(dtype=float)) > 0),
        "above_sma200_pct": pct(liquid.get("sma200_pct", pd.Series(dtype=float)) > 0),
        "overbought": int((liquid.get("rsi14", pd.Series(dtype=float)) > 70).sum()),
        "oversold": int((liquid.get("rsi14", pd.Series(dtype=float)) < 30).sum()),
    }
    sectors = []
    for sector, g in liquid[liquid["sector"].notna()].groupby("sector"):
        w = g["market_cap"].fillna(0)
        row = {"name": sector, "count": int(len(g)), "market_cap": _clean(w.sum())}
        for p, col in (("1d", "change_pct"), ("1w", "perf_1w"), ("1m", "perf_1m"), ("3m", "perf_3m"),
                       ("ytd", "perf_ytd"), ("1y", "perf_1y")):
            if col in g.columns:
                ok = g[col].notna() & (w > 0)
                row[p] = _clean((g.loc[ok, col] * w[ok]).sum() / w[ok].sum()) if ok.any() else None
        row["advancing_pct"] = round(float((g["change_pct"] > 0).mean() * 100), 1)
        sectors.append(row)
    out["sectors"] = sorted(sectors, key=lambda r: -(r.get("1d") or -1e9))
    lists = {}
    for key in ("top_gainers", "top_losers", "new_high", "new_low", "most_active", "unusual_volume", "overbought",
                "oversold", "gap_up", "gap_down"):
        res = screen(signal=key, universe_name=universe_name, limit=8,
                     columns=["symbol", "name", "price", "change_pct", "volume", "rel_volume", "market_cap"])
        lists[key] = {"label": SIGNALS[key][0], "total": res["total"], "rows": res["rows"]}
    out["signals"] = lists
    out["status"] = universe.status()
    return out
