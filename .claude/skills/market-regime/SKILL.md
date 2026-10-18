---
name: market-regime
description: Assess the overall US market regime — trend, breadth, volatility, rates, credit, sector rotation, risk appetite — to set the backdrop for stock calls. Use when the user asks about the market, macro, sectors, risk-on/off, "how's the market", rotation, or before making several recommendations.
---

# Market regime

Most stocks move with the market and their sector; regime sets the base rate for every long or short.

## Procedure
1. `get_market_overview()` — read `regime_notes` first, then the tables.
2. **Trend**: SPY/QQQ/IWM vs 50/200-day SMAs; 1m/3m returns. `get_multi_timeframe("SPY")` for
   alignment; `get_technical_summary("SPY")` if the index sits at a key level.
3. **Breadth**: equal-weight RSP vs SPY (narrow vs broad leadership), sector ETFs above 50-day, small
   caps vs large.
4. **Volatility**: VIX level (<15 complacent, 15-20 normal, 20-30 elevated, >30 stressed) and term
   structure (VIX > VIX3M = inverted = near-term fear).
5. **Rates/credit/dollar**: ^TNX direction (rising yields pressure long-duration growth), HYG vs IEF
   (credit risk appetite), UUP (dollar strength tightens conditions).
6. **Rotation**: sector leaders/laggards 1m vs 3m; cyclicals vs defensives. `compare_peers` or
   `get_correlations` on sector ETFs when relevant.
7. **Movers**: `get_market_movers()` (Alpaca) for the day's notable stocks.
8. News: `get_news()` (market-wide, Alpaca) for macro catalysts (Fed, CPI, earnings season).

## Classify
| Regime | Typical evidence | Implication |
|---|---|---|
| Risk-on trend | SPY > rising 200d, breadth broad, VIX < 18 contango, credit firm | Favour longs/breakouts; full size |
| Narrow/late | indices up but RSP lags, few sectors > 50d, credit softening | Selective; leaders only; tighter risk |
| Correction | SPY < 50d, VIX 20-30, defensives lead | Smaller size, require stronger setups, consider hedges |
| Risk-off/bear | SPY < falling 200d, VIX > 30 or inverted, credit weak | Mostly cash/watch; shorts or defensives |
| Range/chop | SPY flat between levels, ADX low | Mean-reversion > breakouts |

## Output block
```
Regime: <class> (as of date)
Trend: SPY … (vs 50/200), QQQ …, IWM … | Breadth: RSP−SPY 3m …, sectors >50d x/11
Vol: VIX … (term …) | Rates: 10Y … (trend) | Credit: HYG−IEF 1m … | USD …
Leaders: … | Laggards: …
Implications: sizing …, setup preference …, sectors to favour/avoid …
```
Save a dated `save_note(title="Market regime YYYY-MM-DD", tags=["regime"])` when it changes.
