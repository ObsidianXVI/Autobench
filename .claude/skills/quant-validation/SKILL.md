---
name: quant-validation
description: Test trading hypotheses statistically before relying on them — signal/event studies, backtests, robustness across symbols and time, out-of-sample checks — with Autobench's expression language. Use whenever a technical claim ("RSI oversold bounces work here", "golden cross is bullish", "breakouts follow through") would influence a recommendation, or when the user asks to backtest, test, or validate a strategy/signal.
---

# Quant validation

Principle: **a pattern is a hypothesis until tested.** Markets drift up on average, so "it went up
afterwards" means nothing without a baseline. Your job is to estimate the conditional edge and how
much to trust it.

## 1. Formulate testable conditions
Write precise expressions (reference: `get_guide("expressions")`):

| Claim | Condition |
|---|---|
| Oversold in uptrend bounces | `rsi(14) < 35 and close > sma(200)` |
| Pullback to rising 50-day | `low <= sma(50) and close > sma(50) and rising(sma(50), 10)` |
| Breakout with volume | `close > highest(high, 20, 1) and rvol(20) > 1.5` |
| Squeeze fires up | `prev(squeeze().squeeze_on) == 1 and squeeze().squeeze_on == 0 and squeeze().momentum > 0` |
| Golden cross | `crosses_above(sma(50), sma(200))` |
| Mean reversion stretch | `zscore(20) < -2` |
| Gap and go | `open > prev(high) * 1.02 and close > open` |
| Bearish divergence proxy | `close >= highest(close, 20) and rsi(14) < highest(rsi(14), 20) - 8` |

Use `evaluate_expression` to sanity-check that a condition fires when you expect (look at the dates).

## 2. Event study first — `run_signal_study`
- `first_only=True` (default) to avoid overlapping samples.
- Horizons matching the trade horizon (swing: 5/10/20 bars; position: 20/60 on daily or 4/12 on weekly).
- Read: `samples`, `mean_pct` vs `baseline_mean_pct` → `edge_vs_baseline_pct`, `win_rate_pct` vs baseline,
  `t_stat_vs_baseline`, MAE/MFE (how much heat to expect → stop sizing).
- Add `symbols=[…]` (2-4 peers / sector ETF / SPY) for robustness.

## 3. Backtest a concrete rule — `run_backtest`
- Always include costs (defaults: 1 bp commission + 5 bp slippage per side; raise for illiquid names).
- Specify exits explicitly: exit condition and/or `stop_atr`/`target_atr`/`max_hold_bars`.
- Read: trades, win rate, expectancy, profit factor, t-stat, CAGR, Sharpe, max DD, exposure, and
  **in_sample vs out_of_sample**. Compare with buy-and-hold on a risk-adjusted basis (exposure!).
- Use `symbols=[…]` for cross-sectional robustness.

## 4. Judge the evidence (be strict)
| Evidence grade | Criteria |
|---|---|
| **Strong** | n ≥ 30, |t| ≥ 2.5, edge holds out-of-sample and on most peers, plausible mechanism |
| **Moderate** | n ≥ 20, |t| ≥ 2, OOS same sign, mixed peers |
| **Weak** | n < 20 or |t| < 2 or OOS reverses or only works on this symbol |
| **Contradicted** | edge negative / below baseline |

- Testing many variants and keeping the best is data-mining: if you tried k variants, mention it and
  demand higher t (≈ 2 + 0.5·log2(k)).
- Don't tune parameters to the decimal; robust edges survive neighbouring parameters (e.g. RSI 30/35/40).
- Regime dependence: check results in the recent period (OOS) — edges decay.
- A rule with fewer trades than years of data tells you little.

## 5. Report
```
Hypothesis: <plain English>
Condition: <expression>   (interval, lookback, first_only)
Result: n=…, fwd10 mean +x% vs baseline +y% (edge +z%, t=…), win …% vs …%, MAE …%
Robustness: peers A (+…, t…), B (…); OOS: …
Grade: Strong/Moderate/Weak/Contradicted → how it changes the recommendation
```
Always report negative or inconclusive results — they are information too. Save notable findings
with `save_note(tags=["signal-study", symbol])` so you don't re-run them blindly next time.
