---
name: market-regime
description: Assess the overall US market regime — trend, breadth, volatility, rates and the macro backdrop, credit, cross-asset moves, sector rotation, upcoming catalysts — to set the base rate for stock calls. Use when the user asks about the market, macro, the economy, sectors, risk-on/off, "how's the market", rotation, or before making several recommendations.
---

# Market regime

Most stocks move with the market and their sector; regime sets the base rate for every long or short.

## Procedure
1. `get_market_overview()` – read `regime_notes` first (index trend, VIX term structure, equal vs cap weight).
2. **Breadth** – `get_market_breadth()`: advancers/decliners, up/down volume, new highs vs lows, % of stocks
   above their 20/50/200-day averages, overbought/oversold counts. Divergence (index up, breadth down) is
   the classic late-trend warning.
3. **Rotation** – `get_sector_performance(period="1m")` and `"3m"` (cap-weighted GICS sectors and industries);
   cyclicals vs defensives; which industries lead inside the leading sector.
4. **Macro** – `get_macro_dashboard()`: policy rate, 2y/10y and curve spreads (10y-2y, 10y-3m), real yields,
   CPI/core PCE trend, unemployment and claims, credit spreads, financial conditions. Look at *direction*
   (latest vs prior and vs a year ago), not just levels. `get_macro_series(id)` for history when a turn matters.
5. **Cross-asset** – `get_cross_asset_board()`: futures (overnight tone), dollar, oil, gold, copper, yields,
   crypto, global indices. Rising yields + strong dollar tighten conditions; copper/oil confirm growth.
6. **Catalysts** – `get_economic_calendar(days=7)` (CPI, payrolls, FOMC, GDP …) and
   `get_earnings_calendar(days=7, universe="sp500")` (mega-cap reports move indices).
7. **Movers / news** – `get_market_movers()` and `get_news()` for what is driving today.
8. Optionally `show_in_ui(page="markets")` or `show_in_ui(page="heatmap", period="1m")` so the user sees it.

## Classify
| Regime | Typical evidence | Implication |
|---|---|---|
| Risk-on trend | SPY > rising 200d, >60% of stocks > 200d, highs ≫ lows, VIX < 18 contango, spreads tight | Favour longs/breakouts; full size |
| Narrow/late | indices up but breadth falling, RSP lags, highs shrinking, spreads widening | Selective; leaders only; tighter risk |
| Correction | SPY < 50d, VIX 20-30, defensives lead, lows > highs | Smaller size, stronger setups, hedges |
| Risk-off/bear | SPY < falling 200d, <30% > 200d, VIX > 30 or inverted, credit stress | Mostly cash/watch; defensives/shorts |
| Range/chop | SPY flat between levels, breadth ~50%, ADX low | Mean reversion > breakouts |

## Output block
```
Regime: <class> (as of date)
Trend: SPY … (vs 50/200), QQQ …, IWM … | Breadth: adv/dec …, % > 200d …, highs/lows …
Vol: VIX … (term …) | Rates: 2y …, 10y … (curve …), real 10y … | Inflation: core PCE … (trend)
Labor: unemployment …, claims … | Credit: HY spread … | USD … | Oil … | Copper …
Leaders: … | Laggards: … | Catalysts this week: …
Implications: sizing …, setup preference …, sectors to favour/avoid …
```
Save a dated `save_note(title="Market regime YYYY-MM-DD", tags=["regime"])` when it changes.
