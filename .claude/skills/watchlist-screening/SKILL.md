---
name: watchlist-screening
description: Screen the whole US market or a watchlist for ideas — FinViz-style filters on valuation, growth, quality, performance and technicals — then rank and triage the best candidates for deeper research. Use when the user asks to scan, screen, find stocks/setups/opportunities, rank a list, review their watchlist, or compare many tickers.
---

# Screening

Breadth first, depth on the few that matter. Screens generate hypotheses; they are not evidence.

## Procedure
1. **Backdrop** (one call): `get_market_breadth()` – advance/decline, % above 50/200-day, new highs vs lows,
   sector performance. It decides which setups to favour (breakouts in strong breadth, mean reversion in chop).
2. **Universe**:
   - Whole market: `screen_stocks(universe="all" | "sp500", …)` (~6,000 US common stocks; fields in
     `get_guide("screener")`).
   - The user's list: `screen_stocks(universe="watchlist:<name>")` for fields, `scan_watchlist(name)` for
     trend/momentum scores and levels. Create/extend lists with `update_watchlist(name, add=[...])`.
3. **Screen** with explicit, explainable criteria – combine a quality/valuation filter with a technical one:
   - Quality growth at a fair price: `expression="market_cap > 2e9 and sales_growth > 15 and roe > 15 and pe > 0 and pe < 35"`
   - Momentum leaders: `expression="avg_volume > 5e5 and sma50_pct > 0 and sma200_pct > 0 and from_high_52w > -5 and perf_3m > 15", sort="-perf_3m"`
   - Pullback in an uptrend: `expression="sma200_pct > 0 and rsi14 < 40 and perf_1y > 20"`
   - Value with a catalyst check: `expression="pe > 0 and pe < 12 and pb < 1.5 and current_ratio > 1.5"`
   - Preset scans: `signal="unusual_volume" | "new_high" | "gap_up" | "oversold" | …`
   Always add a liquidity floor (`avg_volume > 300000`, `price > 5`) unless the user wants small caps.
   Report how many names passed (`total`) – a screen returning 400 names is not selective.
4. **Shortlist 3-5.** For each: `get_stock_snapshot` (all fields in one call), `get_multi_timeframe`,
   `get_levels`, and one `run_signal_study` of the setup condition on that symbol – the screen found the
   pattern; the study says whether it has paid on this stock. Check `get_earnings_calendar(symbols=[...])`:
   a report inside the holding period changes the trade.
5. **Show the user**: `save_screen(name, …)` and `show_in_ui(page="screener", screen={...})` so they can
   browse the full result set; add finalists with `update_watchlist`.
6. Offer full `stock-research` on the top 1-2.

## Data caveats (say them when they matter)
- Fundamentals are TTM from SEC XBRL filings; new registrants and many foreign issuers have none and drop
  out of fundamental screens.
- Quotes are live during the session; technicals use daily bars through the last session.

## Output block
```
Backdrop: breadth … (adv/dec, % > 200d) · leading sectors …
Screen: <criteria> → N matches (universe …)
| Rank | Symbol | Why it passed | Val (P/E, P/S) | Growth | Trend/RSI | Key level | Earnings | Tested edge |
|------|--------|---------------|----------------|--------|-----------|-----------|----------|-------------|
Avoid / weakest: …
Saved screen: "<name>" (open the Screener page) · Next: full research on X?
```
Note ideas worth revisiting with `save_note(tags=["screen", "<date>"])`.
