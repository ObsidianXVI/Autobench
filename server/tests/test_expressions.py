import numpy as np
import pytest

from autobench.services.expressions import ExpressionError, evaluate, evaluate_bool, max_lookback_hint
from conftest import make_bars


@pytest.fixture
def df():
    c = np.r_[np.linspace(100, 80, 30), np.linspace(80, 130, 50)]
    return make_bars(c)


def test_arithmetic_and_names(df):
    s = evaluate(df, "(close - open) / open * 100")
    assert np.isclose(s.iloc[-1], (df.c.iloc[-1] - df.o.iloc[-1]) / df.o.iloc[-1] * 100)
    assert np.isclose(evaluate(df, "hl2").iloc[-1], (df.h.iloc[-1] + df.l.iloc[-1]) / 2)
    assert np.isclose(evaluate(df, "-close + 2 ** 2").iloc[-1], -df.c.iloc[-1] + 4)


def test_comparisons_chain_and_bool_ops(df):
    s = evaluate_bool(df, "30 < rsi(14) < 101 and not (close < 0)")
    assert s.iloc[-1] and not s.iloc[0]  # warm-up bars are False (NaN-safe)
    assert evaluate_bool(df, "close > 1000 or close > 0").all()


def test_crosses(df):
    s = evaluate_bool(df, "crosses_above(close, sma(10))")
    assert s.sum() >= 1
    i = int(np.flatnonzero(s.to_numpy())[0])
    sma = df.c.rolling(10).mean()
    assert df.c.iloc[i] > sma.iloc[i] and df.c.iloc[i - 1] <= sma.iloc[i - 1]
    assert not evaluate_bool(df, "crosses_above(close, close)").any()


def test_multi_output_and_kwargs(df):
    hist = evaluate(df, "macd(12, 26, 9).histogram")
    assert hist.notna().iloc[-1]
    lower_kw = evaluate(df, "bbands(length=20, std=2).lower")
    lower_pos = evaluate(df, "bbands(20, 2).lower")
    assert np.allclose(lower_kw.dropna(), lower_pos.dropna())
    with pytest.raises(ExpressionError, match="several outputs"):
        evaluate(df, "macd() > 0")
    with pytest.raises(ExpressionError, match="no output"):
        evaluate(df, "macd().nope")


def test_helpers(df):
    assert np.isclose(evaluate(df, "highest(high, 5, 1)").iloc[-1], df.h.iloc[-6:-1].max())
    assert np.isclose(evaluate(df, "prev(close, 2)").iloc[-1], df.c.iloc[-3])
    assert np.isclose(evaluate(df, "pct_change(close, 5)").iloc[-1], (df.c.iloc[-1] / df.c.iloc[-6] - 1) * 100)
    assert evaluate_bool(df, "rising(close, 3)").iloc[-1]
    bs = evaluate(df, "bars_since(close < 81)")
    assert bs.iloc[-1] == len(df) - 1 - int(np.flatnonzero((df.c < 81).to_numpy())[-1])
    assert np.isclose(evaluate(df, "max(close, 120)").iloc[0], 120)


def test_rejects_unsafe_or_invalid_syntax(df):
    for bad in ["__import__('os')", "close.__class__", "[1,2]", "lambda x: x", "close[0]", "open('/etc/passwd')",
                "a = 1", "x if close else y"]:
        with pytest.raises(ExpressionError):
            evaluate(df, bad)
    with pytest.raises(ExpressionError, match="Unknown name"):
        evaluate(df, "foo > 1")
    with pytest.raises(ExpressionError, match="Unknown function"):
        evaluate(df, "bogus(3)")
    with pytest.raises(ExpressionError, match="constants"):
        evaluate(df, "sma(rsi(14))")
    with pytest.raises(ExpressionError, match="Syntax"):
        evaluate(df, "close >")
    with pytest.raises(ExpressionError, match="true/false"):
        evaluate_bool(df, "close + 1")


def test_lookback_hint():
    assert max_lookback_hint("sma(200) > close") == 600
    assert max_lookback_hint("close > 100.5 and rsi(14) < 30") == 42
    assert max_lookback_hint("garbage((") == 0
