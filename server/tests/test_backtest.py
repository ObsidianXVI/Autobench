"""Backtest/signal-study correctness on hand-built price paths where every fill is known."""

import numpy as np
import pytest

from autobench.services import backtest
from conftest import make_bars


def _path():
    # flat 100 for 70 bars, then +1/bar for 30 bars, then -1/bar for 30 bars
    c = np.r_[np.full(70, 100.0), 100 + np.arange(1, 31), 130 - np.arange(1, 31)]
    opens = np.r_[c[0], c[:-1]]  # open = previous close (no gaps)
    return make_bars(c, opens=opens, spread=0.0)


def test_entry_fills_next_open_and_no_lookahead(patch_bars):
    df = _path()
    patch_bars(df)
    # signal on the first close above 100 (bar 70) -> fill at bar 71 open (= close of bar 70 = 101)
    r = backtest.run_backtest("TEST", "crosses_above(close, 100.5)", "close < prev(close)", lookback="max",
                              commission_bps=0, slippage_bps=0, oos_fraction=0)
    t = r["recent_trades"][0]
    assert t["entry"] == "2024-04-10"  # bar 71 (signal on bar 70's close)
    first_trade_entry_px = 101.0
    # exit signal: first down close is bar 100 (129 < 130) -> fill at bar 101 open = 129
    assert t["return_pct"] == pytest.approx((129 / first_trade_entry_px - 1) * 100, abs=0.01)
    assert r["performance"]["trades"] == 1
    assert r["exit_reasons"] == {"signal": 1}


def test_costs_reduce_returns(patch_bars):
    patch_bars(_path())
    free = backtest.run_backtest("T", "crosses_above(close, 100.5)", "close < prev(close)", lookback="max",
                                 commission_bps=0, slippage_bps=0, oos_fraction=0)
    costly = backtest.run_backtest("T", "crosses_above(close, 100.5)", "close < prev(close)", lookback="max",
                                   commission_bps=10, slippage_bps=40, oos_fraction=0)
    gross = free["recent_trades"][0]["return_pct"] / 100
    net = costly["recent_trades"][0]["return_pct"] / 100
    assert net == pytest.approx((129 * (1 - 0.005)) / (101 * (1 + 0.005)) - 1, abs=1e-4)
    assert net < gross


def test_equity_matches_trade_return_for_single_trade(patch_bars):
    patch_bars(_path())
    r = backtest.run_backtest("T", "crosses_above(close, 100.5)", "close < prev(close)", lookback="max",
                              commission_bps=0, slippage_bps=0, oos_fraction=0)
    assert r["performance"]["total_return_pct"] == pytest.approx(r["recent_trades"][0]["return_pct"], abs=0.01)


def test_stop_loss_intrabar_and_gap(patch_bars):
    c = np.r_[np.full(60, 100.0), [101, 100, 99, 97, 90, 89]]
    opens = np.r_[c[0], c[:-1]]
    opens[-2] = 92  # gap down through the stop on bar -2
    df = make_bars(c, opens=opens, spread=0.0)
    patch_bars(df)
    r = backtest.run_backtest("T", "crosses_above(close, 100.5)", None, lookback="max", stop_loss_pct=5,
                              commission_bps=0, slippage_bps=0, oos_fraction=0)
    t = r["recent_trades"][0]
    assert t["reason"] == "stop"
    # entry at open after the 101 close = 101; stop = 95.95; bar opens at 92 (gap) -> fill 92
    assert t["return_pct"] == pytest.approx((92 / 101 - 1) * 100, abs=0.01)


def test_take_profit_and_max_hold(patch_bars):
    patch_bars(_path())
    r = backtest.run_backtest("T", "crosses_above(close, 100.5)", None, lookback="max", take_profit_pct=10,
                              commission_bps=0, slippage_bps=0, oos_fraction=0)
    assert r["recent_trades"][0]["reason"] == "target"
    assert r["recent_trades"][0]["return_pct"] == pytest.approx(10.0, abs=0.01)
    r2 = backtest.run_backtest("T", "crosses_above(close, 100.5)", None, lookback="max", max_hold_bars=5,
                               commission_bps=0, slippage_bps=0, oos_fraction=0)
    assert r2["exit_reasons"].get("max_hold", 0) >= 1


def test_short_side(patch_bars):
    patch_bars(_path())
    r = backtest.run_backtest("T", "close < prev(close) and close < 129.5", "close > prev(close)", side="short",
                              lookback="max", commission_bps=0, slippage_bps=0, oos_fraction=0)
    # short entered at 129 on the first down close and still open; price fell to 100
    assert r["performance"]["trades"] == 0
    assert r["open_trade"]["entry_price"] == pytest.approx(129.0)
    assert r["open_trade"]["unrealized_pct"] == pytest.approx((1 - 100 / 129) * 100, abs=0.01)


def test_validation(patch_bars):
    patch_bars(_path())
    with pytest.raises(ValueError, match="exit"):
        backtest.run_backtest("T", "close > 1")
    with pytest.raises(ValueError, match="side"):
        backtest.run_backtest("T", "close > 1", "close < 1", side="sideways")


def test_signal_study_forward_returns(patch_bars):
    df = _path()
    patch_bars(df)
    r = backtest.signal_study("T", "close > 100.5", lookback="max", horizons=[5])
    # fires once at bar 70 (close 101); 5 bars later close 106
    assert r["occurrences"] == 1
    fr = r["forward_returns"]["5_bars"]
    assert fr["mean_pct"] == pytest.approx((106 / 101 - 1) * 100, abs=0.01)
    assert r["condition_true_now"] is False  # last close is 100
    assert any("anecdotal" in w for w in r["warnings"])
    every = backtest.signal_study("T", "close > 100.5", lookback="max", horizons=[5], first_only=False)
    assert every["occurrences"] == 59  # 30 rising bars + 29 falling bars still above 100.5


def test_backtest_on_fake_provider_end_to_end():
    r = backtest.run_backtest("AAPL", "crosses_above(ema(20), ema(50))", "crosses_below(ema(20), ema(50))",
                              lookback="5y", stop_atr=2)
    assert r["performance"]["trades"] > 0
    assert {"in_sample", "out_of_sample", "buy_and_hold", "warnings"} <= r.keys()
    s = backtest.signal_study("MSFT", "rsi(14) < 30", lookback="10y")
    assert s["occurrences"] > 0 and "baseline_mean_pct" in s["forward_returns"]["10_bars"]


def test_scan():
    r = backtest.scan(["AAPL", "MSFT", "NVDA"], "close > 0")
    assert {m["symbol"] for m in r["matches"]} == {"AAPL", "MSFT", "NVDA"}
    r2 = backtest.scan(["AAPL"], "close < 0")
    assert r2["non_matches"] == ["AAPL"]
