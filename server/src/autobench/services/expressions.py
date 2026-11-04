"""A small, safe expression language over bar data, used for backtests, signal studies and scans.

Examples:
    close > sma(200) and rsi(14) < 30
    crosses_above(ema(20), ema(50))
    macd(12, 26, 9).histogram > 0 and rising(macd().histogram, 3)
    close < bbands(20, 2).lower and rvol(20) > 1.5
    close > highest(high, 20, 1)          # breakout over the prior 20-bar high
    pct_change(close, 5) < -8             # down more than 8% in 5 bars

Names:     open high low close volume hl2 hlc3 ohlc4
Indicators: any registered indicator, positional args in declared order or keywords;
           multi-output indicators need an attribute: macd().signal, bbands().upper, adx().plus_di
Helpers:   crosses_above(a,b) crosses_below(a,b) prev(x,n=1) highest(x,n,offset=0) lowest(x,n,offset=0)
           mean(x,n) stdev(x,n) pct_change(x,n=1) rising(x,n=1) falling(x,n=1) abs(x) min(a,b) max(a,b)
           bars_since(cond)
Operators: + - * / % **, comparisons (chainable), and / or / not.

The expression is parsed with Python's ast module and only whitelisted node types are evaluated —
no attribute access on Python objects, no imports, no builtins.
"""

from __future__ import annotations

import ast
from typing import Any

import numpy as np
import pandas as pd

from . import indicators as ind

_NAMES = {"open": "o", "high": "h", "low": "l", "close": "c", "volume": "v"}
_DERIVED = {"hl2", "hlc3", "ohlc4"}

_HELP = (
    "Names: open high low close volume hl2 hlc3 ohlc4. Indicators: any from list_indicators (e.g. sma(50), "
    "rsi(14), macd(12,26,9).histogram, bbands(20,2).lower). Helpers: crosses_above crosses_below prev highest "
    "lowest mean stdev pct_change rising falling abs min max bars_since."
)


class ExpressionError(ValueError):
    pass


class _MultiOutput:
    def __init__(self, name: str, frame: pd.DataFrame):
        self.name, self.frame = name, frame


def _series(x: Any, index: pd.Index) -> pd.Series:
    if isinstance(x, pd.Series):
        return x
    if isinstance(x, _MultiOutput):
        raise ExpressionError(
            f"{x.name}() has several outputs; pick one with an attribute: "
            + ", ".join(f"{x.name}().{c}" for c in x.frame.columns)
        )
    if isinstance(x, (int, float, bool, np.number)):
        return pd.Series(float(x), index=index)
    raise ExpressionError(f"Unsupported value {x!r}")


def _int_arg(v: Any, what: str) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v or v < 0:
        raise ExpressionError(f"{what} must be a non-negative integer constant")
    return int(v)


class Evaluator:
    def __init__(self, df: pd.DataFrame):
        self.df = df
        self.index = df.index
        self._cache: dict[str, Any] = {}

    # helpers -------------------------------------------------------------------------------
    def _helpers(self):
        idx = self.index
        S = lambda x: _series(x, idx)  # noqa: E731

        def crosses_above(a, b):
            a, b = S(a), S(b)
            return (a > b) & (a.shift(1) <= b.shift(1))

        def crosses_below(a, b):
            a, b = S(a), S(b)
            return (a < b) & (a.shift(1) >= b.shift(1))

        def prev(x, n=1):
            return S(x).shift(_int_arg(n, "prev() n"))

        def highest(x, n, offset=0):
            n = _int_arg(n, "highest() n")
            return S(x).rolling(n, min_periods=n).max().shift(_int_arg(offset, "offset"))

        def lowest(x, n, offset=0):
            n = _int_arg(n, "lowest() n")
            return S(x).rolling(n, min_periods=n).min().shift(_int_arg(offset, "offset"))

        def mean(x, n):
            n = _int_arg(n, "mean() n")
            return S(x).rolling(n, min_periods=n).mean()

        def stdev(x, n):
            n = _int_arg(n, "stdev() n")
            return S(x).rolling(n, min_periods=n).std(ddof=0)

        def pct_change(x, n=1):
            x = S(x)
            return (x / x.shift(_int_arg(n, "pct_change() n")) - 1) * 100

        def rising(x, n=1):
            x = S(x)
            return x > x.shift(_int_arg(n, "rising() n"))

        def falling(x, n=1):
            x = S(x)
            return x < x.shift(_int_arg(n, "falling() n"))

        def bars_since(cond):
            c = S(cond).fillna(False).astype(bool).to_numpy()
            out = np.full(len(c), np.nan)
            last = -1
            for i, v in enumerate(c):
                if v:
                    last = i
                if last >= 0:
                    out[i] = i - last
            return pd.Series(out, index=idx)

        return {
            "crosses_above": crosses_above, "crosses_below": crosses_below, "prev": prev,
            "highest": highest, "lowest": lowest, "mean": mean, "stdev": stdev, "pct_change": pct_change,
            "rising": rising, "falling": falling, "bars_since": bars_since,
            "abs": lambda x: S(x).abs(),
            "min": lambda a, b: np.minimum(S(a), S(b)),
            "max": lambda a, b: np.maximum(S(a), S(b)),
        }

    # evaluation ----------------------------------------------------------------------------
    def eval(self, expr: str) -> pd.Series:
        try:
            tree = ast.parse(expr.strip(), mode="eval")
        except SyntaxError as e:
            raise ExpressionError(f"Syntax error in expression: {e.msg} (at col {e.offset})") from e
        self.helpers = self._helpers()
        res = self._eval(tree.body)
        return _series(res, self.index)

    def _eval(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float, str)) and not isinstance(node.value, bool):
                return node.value
            if isinstance(node.value, bool):
                return node.value
            raise ExpressionError(f"Unsupported constant {node.value!r}")
        if isinstance(node, ast.Name):
            n = node.id.lower()
            if n in _NAMES:
                return self.df[_NAMES[n]]
            if n in _DERIVED:
                return ind.price_source(self.df, n)
            if n in ("true", "false"):
                return n == "true"
            raise ExpressionError(f"Unknown name '{node.id}'. {_HELP}")
        if isinstance(node, ast.UnaryOp):
            v = self._eval(node.operand)
            if isinstance(node.op, ast.USub):
                return -_series(v, self.index) if not isinstance(v, (int, float)) else -v
            if isinstance(node.op, ast.UAdd):
                return v
            if isinstance(node.op, ast.Not):
                return ~_series(v, self.index).fillna(False).astype(bool)
            raise ExpressionError("Unsupported unary operator")
        if isinstance(node, ast.BinOp):
            a, b = self._eval(node.left), self._eval(node.right)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                return self._binop(node.op, a, b)
            return self._binop(node.op, _series(a, self.index), _series(b, self.index))
        if isinstance(node, ast.BoolOp):
            vals = [_series(self._eval(v), self.index).fillna(False).astype(bool) for v in node.values]
            out = vals[0]
            for v in vals[1:]:
                out = (out & v) if isinstance(node.op, ast.And) else (out | v)
            return out
        if isinstance(node, ast.Compare):
            left = _series(self._eval(node.left), self.index)
            result = pd.Series(True, index=self.index)
            for op, comp in zip(node.ops, node.comparators):
                right = _series(self._eval(comp), self.index)
                result &= self._compare(op, left, right)
                left = right
            return result
        if isinstance(node, ast.Call):
            return self._call(node)
        if isinstance(node, ast.Attribute):
            base = self._eval(node.value)
            if not isinstance(base, _MultiOutput):
                raise ExpressionError(f"'.{node.attr}' can only follow a multi-output indicator such as macd() or bbands()")
            if node.attr not in base.frame.columns:
                raise ExpressionError(f"{base.name}() has no output '{node.attr}'. Outputs: {', '.join(base.frame.columns)}")
            return base.frame[node.attr]
        raise ExpressionError(f"Unsupported syntax: {type(node).__name__}. {_HELP}")

    @staticmethod
    def _binop(op, a, b):
        if isinstance(op, ast.Add):
            return a + b
        if isinstance(op, ast.Sub):
            return a - b
        if isinstance(op, ast.Mult):
            return a * b
        if isinstance(op, ast.Div):
            if isinstance(b, pd.Series):
                return a / b.replace(0, np.nan)
            return a / b if b != 0 else np.nan
        if isinstance(op, ast.Mod):
            return a % b
        if isinstance(op, ast.Pow):
            return a**b
        raise ExpressionError("Unsupported arithmetic operator")

    @staticmethod
    def _compare(op, a: pd.Series, b: pd.Series) -> pd.Series:
        valid = a.notna() & b.notna()
        if isinstance(op, ast.Gt):
            r = a > b
        elif isinstance(op, ast.GtE):
            r = a >= b
        elif isinstance(op, ast.Lt):
            r = a < b
        elif isinstance(op, ast.LtE):
            r = a <= b
        elif isinstance(op, ast.Eq):
            r = a == b
        elif isinstance(op, ast.NotEq):
            r = a != b
        else:
            raise ExpressionError("Unsupported comparison")
        return r & valid

    def _call(self, node: ast.Call) -> Any:
        if not isinstance(node.func, ast.Name):
            raise ExpressionError("Only plain function calls like sma(20) are allowed")
        fname = node.func.id.lower()
        args = [self._eval(a) for a in node.args]
        kwargs = {k.arg: self._eval(k.value) for k in node.keywords if k.arg}
        if fname in self.helpers:
            try:
                return self.helpers[fname](*args, **kwargs)
            except TypeError as e:
                raise ExpressionError(f"Bad arguments for {fname}(): {e}") from e
        if fname in ind.REGISTRY:
            for a in [*args, *kwargs.values()]:
                if isinstance(a, (pd.Series, _MultiOutput)):
                    raise ExpressionError(
                        f"Indicator arguments must be constants (got a series in {fname}()). "
                        "To smooth a series use mean(x, n)."
                    )
            key = f"{fname}|{args}|{sorted(kwargs.items())}"
            if key not in self._cache:
                try:
                    self._cache[key] = ind.compute(self.df, fname, kwargs, args)
                except ValueError as e:
                    raise ExpressionError(str(e)) from e
            frame = self._cache[key]
            if frame.shape[1] == 1:
                return frame.iloc[:, 0]
            return _MultiOutput(fname, frame)
        raise ExpressionError(f"Unknown function '{node.func.id}'. {_HELP}")


def evaluate(df: pd.DataFrame, expr: str) -> pd.Series:
    return Evaluator(df).eval(expr)


def evaluate_bool(df: pd.DataFrame, expr: str) -> pd.Series:
    s = evaluate(df, expr)
    if s.dtype != bool:
        uniq = set(pd.unique(s.dropna()))
        if not uniq <= {0.0, 1.0, True, False}:
            raise ExpressionError("Condition must evaluate to true/false (use a comparison such as rsi(14) < 30)")
    return s.fillna(False).astype(bool)


def max_lookback_hint(expr: str) -> int:
    """Rough warm-up requirement: 3x the largest integer argument passed to any function call
    (thresholds such as `close > 100` are not lookbacks and are ignored)."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return 0
    nums = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for a in [*node.args, *(k.value for k in node.keywords)]:
                if isinstance(a, ast.Constant) and isinstance(a.value, int) and not isinstance(a.value, bool):
                    nums.append(a.value)
    return int(max([v for v in nums if v < 1000] or [0]) * 3)
