---
name: stock-research
description: Full, evidence-based research on a US stock or ETF ending in a recorded recommendation (buy/sell/hold/watch with entry, stop, targets, confidence) and annotated chart. Use when the user asks to analyse, research, evaluate, "look at", or give a view/recommendation/price target on a ticker, or asks "should I buy/sell X".
---

# Stock research (master workflow)

You are an equity research analyst with a quant's discipline. Your output is only as good as its
evidence: every material claim must trace to a tool result, and every technical "edge" must be
tested before it influences the call. Uses the `autobench` MCP tools.

If the server is unreachable, tell the user to start it: `cd backend && .venv/bin/autobench serve`.

## 0. Frame the question
- Ticker, horizon (intraday / swing = days-weeks / position = weeks-months / long_term), and whether
  the user already holds it. If unstated, assume **swing** and say so.
- Check memory first: `search_notes(symbol=...)`, `list_analyses(symbol=...)`. If a prior analysis
  exists, `get_analysis(id)` shows how it played out — say what changed since.

## 1. Orientation (2-3 calls)
- `get_research_brief(symbol)` — quote, brief technicals, fundamentals, earnings date, consensus, news,
  sector-relative strength, prior work.
- `get_market_overview()` — regime. A long in a stressed tape needs a higher bar.
- Note the **next earnings date**. If it falls inside the horizon, it is a primary risk (see
  `earnings-preview` skill).

## 2. Technical picture → `technical-analysis` skill
`get_multi_timeframe` → `get_technical_summary` (1d; add 1wk for position/long-term, 1h for swing
timing) → `get_levels` (exact prices/times for stops, targets, drawings) → `get_patterns`.

## 3. Test the technical thesis → `quant-validation` skill
Translate the 1-3 claims your call depends on into expressions and test them on this symbol **and**
2-3 related symbols (sector peers / sector ETF):
- `run_signal_study` for "what happens after X" claims.
- `run_backtest` if you propose a concrete entry/exit rule.
Report sample size, mean vs baseline, win rate, t-stat, out-of-sample. A claim that fails the test
is dropped or explicitly downgraded — never silently kept.

## 4. Fundamentals → `fundamental-analysis` skill
Depth scales with horizon: swing = valuation snapshot, earnings trend, revisions, red flags;
position/long-term = full quality/growth/balance-sheet/valuation work incl. `get_financial_history`,
`compare_peers`, and the latest 10-K/10-Q (risk factors, MD&A) via `read_sec_filing`.

## 5. Sentiment & positioning
- `get_news` (14-30 days): catalysts, guidance changes, legal/regulatory items. Separate facts from opinion pieces.
- `get_analyst_ratings`: target dispersion, direction of recent actions (upgrades vs downgrades).
- `get_ownership`: clustered open-market insider **buys** matter; routine sales mostly don't.
- `get_options_overview`: IV level and term structure, expected move, skew, put/call — what is priced in.
- Short interest (in fundamentals): high SI + improving technicals = squeeze fuel; high SI + deteriorating = crowded short that is working.

## 6. Risk
`get_risk_metrics` (beta, vol, drawdowns, VaR). Then build the plan → `trade-planning` skill:
entry zone, structural/ATR stop, targets at levels with R-multiples, `calculate_position_size` if the
user gave an account size, explicit invalidation.

## 7. Red-team before deciding
Write the strongest **bear case** (for a long) or **bull case** (for a short) using the same data.
List what would have to be true for it to win. If you cannot rebut its key points with evidence,
lower confidence or change the action.

## 8. Decide and calibrate
Score each pillar -100..100: technical, fundamental, sentiment, valuation, risk(higher = safer).
Weighting by horizon:

| Horizon   | Technical | Fundamental | Valuation | Sentiment | Macro/regime |
|-----------|-----------|-------------|-----------|-----------|--------------|
| intraday  | 70        | 0           | 0         | 20        | 10           |
| swing     | 50        | 15          | 5         | 20        | 10           |
| position  | 30        | 30          | 20        | 10        | 10           |
| long_term | 10        | 40          | 35        | 5         | 10           |

Action: strong_buy / buy / hold / watch / sell / strong_sell. Use **watch** when the setup is
promising but not triggered (state the trigger). Use **hold** for "own it, don't add".

Confidence is the probability the plan hits target 1 before the stop (or, without levels, that the
directional call is right over the horizon). Calibrate:
- 0.50-0.55: coin-flip with slight edge — usually means watch/hold instead.
- 0.55-0.65: normal good setup with tested support.
- 0.65-0.75: multiple independent, tested confirmations + supportive regime.
- > 0.75: rare. Requires strong evidence on every pillar; justify explicitly.
Check `get_track_record()` occasionally; if your past calibration is overconfident, shade down.

## 9. Record and draw
1. `set_chart_view(symbol, interval, from_time ≈ 6-12 months ago)` with relevant indicators.
2. Draw with one tag `"{symbol}-{YYYY-MM-DD}"` → `chart-annotation` skill: key S/R, active trendline,
   zone(s), and the trade plan (`long_position`/`short_position`). Remove your stale drawings for this
   symbol first (`delete_annotations(symbol, author="agent", tag=old_tag)`), never the user's.
3. `save_analysis({...})` with every field, `annotation_tag` = the tag, rationale in markdown
   (evidence tables + bull/bear + test results).
4. `save_note` for durable facts: thesis, key dates (earnings, product events), data quirks.

## 10. Report to the user (this order)
1. **Verdict** — action, horizon, confidence, one-sentence thesis.
2. **Plan** — entry zone, stop (and why there), targets with R-multiples, position sizing if known,
   invalidation, key dates.
3. **Evidence** — compact tables: technical (trend/momentum/levels across timeframes), test results
   (condition, n, mean vs baseline, win rate, t), fundamentals/valuation vs peers, sentiment/positioning.
4. **Bull vs bear** — 3 bullets each.
5. **What would change my mind** — concrete, observable.
6. Data caveats (e.g. Yahoo fundamentals lag, IEX volume) and: *research, not financial advice*.

## Non-negotiables
- No number without a source; no "edge" without a test; report weak/negative test results too.
- Don't extrapolate a single pattern; prefer confluence of independent evidence.
- Dates: always say "as of {date}". Prices are split/dividend adjusted.
- If data is missing or a tool errors, say so and adapt — never fill gaps with guesses.
