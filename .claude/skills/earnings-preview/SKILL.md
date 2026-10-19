---
name: earnings-preview
description: Pre- or post-earnings analysis for a stock — beat/miss history, consensus and revision trends, options-implied expected move, positioning, historical post-earnings reactions, and the 8-K press release once reported. Use when earnings are upcoming (inside the trade horizon), or the user asks about an earnings report, guidance, or "what's priced in".
---

# Earnings preview / review

## Before the report
0. Who reports when: `get_earnings_calendar(days=7, universe="sp500")` (BMO/AMC, consensus EPS) – peers
   reporting first often move the stock (read-through).
1. `get_earnings(symbol)` — next date; last 8 quarters: estimate vs actual, surprise %, beat rate;
   consensus EPS/revenue for 0q with low/high range (dispersion = uncertainty); `eps_trend` (7/30/60/90
   day drift) and `eps_revisions` (up vs down counts). Rising estimates into the print = bar is higher
   but fundamentals improving; falling = lowered bar.
2. `get_options_overview(symbol)` — the first expiry after the earnings date is flagged `spans_earnings`.
   Variances add, so the implied earnings-day move ≈ √(EM_after² − EM_normal²), where EM_after is that
   expiry's `expected_move_pct` and EM_normal is the non-event move over the same days, scaled from a
   pre-earnings weekly by √(days ratio). Example: EM_after 12.2% (19d), weekly 6.25% (5d) → EM_normal ≈
   6.25·√(19/5) ≈ 12.2% → little extra priced; if EM_after were 15% → √(15²−12.2²) ≈ 8.7% earnings move.
   Also: IV term structure kink around the date, skew (downside demand), put/call.
3. Historical reactions — find past earnings dates from `get_earnings.history` and measure the
   next-session move with `get_price_history(symbol, "1d", start=<date-5d>, end=<date+5d>)` for the
   last 4-8 reports. Compare average |move| with today's implied move: implied ≫ realised history →
   options rich; implied ≪ → cheap.
4. Positioning/sentiment — `get_analyst_ratings` (recent target changes), short interest, `get_news`
   for previews, guidance from the last call (prior 8-K EX-99.1 via `list_sec_filings(forms=["8-K"])`
   → `get_filing_documents` → `read_sec_filing`).
5. Technical setup into the print — run-up into earnings (`returns_pct`), distance from key levels:
   a stock extended far above support has more air below on disappointment.

## Decision framing
- Is the call about the **business** (hold through, position/long-term) or the **event** (trade the
  reaction)? Event trades have low reliability; keep confidence ≤ 0.6 unless evidence is unusual.
- For swing trades, the default is: reduce/exit before earnings or size for a full expected-move gap.

## After the report
- 8-K item 2.02 + EX-99.1: revenue/EPS vs consensus, guidance vs consensus, margins, segment detail,
  any change in tone. `get_news` for reaction coverage.
- Reaction quality: gap direction, close location in the day's range, rvol, whether it held key levels
  (next day's `get_technical_summary`). Post-earnings drift tends to follow strong surprises with
  strong volume.
- Update notes: `save_note(symbol, title="Q? FY?? earnings", tags=["earnings"])` with the key numbers and
  guidance; re-evaluate open analyses (`list_analyses(symbol)`).

## Output block
```
Earnings: <date> (<before/after market>) | Beat rate 7/8, avg surprise +4.1%
Consensus 0q EPS 1.97 (range 1.93-2.07), rev 113.6B | Revisions 30d: EPS −0.1%, 2 up / 1 down
Implied move ±5.6% (front expiry) vs avg realised ±4.2% last 6 → options rich
Setup into print: +12% in 1m, 1.8 ATR above S1 → asymmetric downside on a miss
Plan: trim before / hold through with half size / no new position until after
```
