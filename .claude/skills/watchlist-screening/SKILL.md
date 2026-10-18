---
name: watchlist-screening
description: Screen and rank many stocks — scan a watchlist or ticker list for setups, filter by technical conditions, and triage the best candidates for deeper research. Use when the user asks to scan, screen, rank, find setups/opportunities, review their watchlist, or compare a list of tickers.
---

# Watchlist screening

Breadth first, depth on the few that matter.

## Procedure
1. Universe: `list_watchlists()` / the user's list. Create or update with `update_watchlist(name, add=[...])`.
   For sector screens, include the sector ETF as a benchmark.
2. Regime check: `get_market_overview()` (one call) — it changes which setups to favour.
3. Rank: `scan_watchlist(name, sort_by="trend"|"momentum"|"change")` → trend/momentum scores, RSI,
   MA stack, structure, nearest levels for every symbol.
4. Filter with explicit conditions via `scan_symbols(condition, watchlist=name)`, e.g.
   - Trend pullback: `close > sma(200) and close > sma(50) and rsi(14) < 45`
   - Near breakout: `close > highest(high, 50, 1) * 0.97 and squeeze().squeeze_on == 1`
   - Breakout today: `close > highest(high, 20, 1) and rvol(20) > 1.5`
   - Oversold quality: `rsi(14) < 30 and close > sma(200)`
   - Breakdown risk: `close < sma(50) and falling(sma(50), 5)`
5. Shortlist 3-5 names. For each: `get_multi_timeframe`, `get_levels`, one `run_signal_study` of the
   setup's condition on that symbol, and the next earnings date (`get_earnings` or the brief).
6. Present a ranked table, then offer full `stock-research` on the top 1-2.

## Output block
```
Universe: <name> (n) · Regime: …
| Rank | Symbol | Setup | Trend | Mom | RSI | Key level | Earnings | Tested edge |
|------|--------|-------|-------|-----|-----|-----------|----------|-------------|
Avoid / weakest: …
Next: full research on X?
```
Note screened ideas worth revisiting with `save_note(tags=["screen", "<date>"])`.
