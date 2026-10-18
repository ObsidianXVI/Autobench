---
name: technical-analysis
description: Rigorous multi-timeframe technical analysis of a stock's chart — trend, momentum, volatility, volume, support/resistance, trendlines, patterns and relative strength — using Autobench tools. Use when the user asks about a chart, levels, trend, setup, support/resistance, indicators (RSI, MACD, moving averages...), or entry/exit timing.
---

# Technical analysis

Goal: an objective read of supply/demand from price and volume, with exact levels, that is then
**tested** (see `quant-validation`). Indicators are lenses, not oracles; agreement of independent
lenses (trend + momentum + volume + levels) is what matters.

## Procedure
1. **Top-down timeframes** — `get_multi_timeframe(symbol, ["1wk","1d","1h"])`.
   - Higher timeframe = direction/context; lower = timing. Trade *with* the higher timeframe unless
     the call is explicitly a counter-trend reversal (demands stronger evidence).
   - "aligned bullish/bearish" is a strong context; "mixed" → reduce size/confidence or wait.
2. **Primary timeframe read** — `get_technical_summary(symbol, interval)`:
   - **Trend**: MA stack, price vs 50/200 SMA, SMA200 slope, ADX (+DI/-DI), linreg R² (trend quality),
     supertrend, cloud, swing structure (HH/HL vs LH/LL). Classify: strong up / weak up / range /
     weak down / strong down.
   - **Momentum**: RSI level *and* regime (bull ranges 40-80, bear 20-60), MACD histogram direction,
     stochastic, divergences (only meaningful at swing extremes, confirmed by structure).
   - **Volatility**: ATR% (stop sizing), BB bandwidth percentile / squeeze (compression → expansion),
     HV percentile.
   - **Volume**: rvol on key days, OBV vs price (confirming/diverging), CMF/MFI, up/down volume.
     Breakouts on low volume are suspect. Note: with the Alpaca IEX feed volume is partial — compare
     ratios, not absolute volume.
   - **Relative strength** vs SPY (and sector ETF from the brief): leaders outperform in uptrends.
3. **Exact levels** — `get_levels(symbol, interval, lookback)`:
   - Supports/resistances (clustered pivots; more touches + recent = stronger), volume-profile POC and
     value area (acceptance zones), LVNs (price moves fast through them), fib retracements of the
     dominant swing (0.382-0.618 = typical pullback zone in trends), classic pivots (intraday/swing).
   - Trendline candidates come with exact anchor points — use them verbatim when drawing.
   - Round numbers and prior gaps often act as levels too.
4. **Patterns & events** — `get_patterns`: candles only matter **at levels** and with confirmation;
   double tops/bottoms need neckline breaks; recent events (crosses, breakouts, gaps, 52w highs,
   volume spikes, squeeze releases) show what just changed.
5. **Indicator deep-dives** when needed — `get_indicators` (e.g. anchored VWAP from a key low/high
   or earnings gap: `{"name":"anchored_vwap","params":{"anchor":"2025-04-07"}}`), `evaluate_expression`
   to inspect a condition's recent values.
6. **Test** the 1-3 claims that drive your view (`quant-validation` skill) before relying on them.

## Setup taxonomy (name it explicitly)
- **Trend pullback**: uptrend intact (HH/HL, > rising 50/200), pullback to 20/50 MA / fib 0.382-0.618 /
  prior breakout level, momentum resets (RSI 40-50) then turns up. Stop below swing low.
- **Breakout**: tight base (consolidation `is_tight`, squeeze_on), resistance tested ≥2×, breakout close
  with rvol > 1.5. Stop below base mid/low. Beware failed breakouts back into the range.
- **Range trade**: ADX < 20, clear horizontal S/R; buy support/sell resistance, stops outside range.
- **Reversal**: requires structure change (break of last swing in the opposite direction) + divergence
  or climax volume + reclaim of a key MA. Lower confidence by default.
- **Breakdown / short**: mirror of breakout with lower highs and distribution volume.

## Common errors to avoid
- Treating overbought as a sell signal in a strong trend.
- Drawing levels from memory/eyeballing instead of `get_levels` output.
- Ignoring the higher timeframe or upcoming earnings.
- Counting correlated indicators (RSI, stochastic, CCI) as independent confirmations.
- Using intraday data for long-horizon conclusions (or the reverse).

## Output block
```
Timeframes: 1wk <state> | 1d <state> | 1h <state>  → alignment
Trend: … (ADX x, structure, MA stack)    Momentum: … (RSI x, MACD hist rising/falling, divergence?)
Volatility: ATR x (y%), regime …          Volume: rvol x, OBV confirming/diverging
Levels: R2 … | R1 … | price … | S1 … | S2 …   (source: clustered pivots / POC / fib)
Setup: <taxonomy name>, trigger …, invalidation …
Tested: <condition> → n, mean vs baseline, win%, t   (from quant-validation)
```
