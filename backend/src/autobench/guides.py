"""Reference text served to agents (MCP instructions, get_guide tool, prompts)."""

from .schemas import ANNOTATION_GUIDE

SERVER_INSTRUCTIONS = """\
Autobench: US-equity research workbench. You can read market data, fundamentals, filings, news,
options and macro context; run quantitative tests; draw on the user's live chart; and keep a
persistent research memory (analyses, notes, watchlists).

Working rules:
- Start a single-name analysis with get_research_brief(symbol); drill down from there.
- Never assert a technical "edge" without evidence: test it with run_signal_study or run_backtest
  (check sample size, t-stat, out-of-sample), and report the numbers including the weak ones.
- Separate facts (tool output) from interpretation. Cite figures with dates.
- Prices are split/dividend adjusted. Daily bars are dated by session; intraday times are US/Eastern.
- Before drawing, call get_chart_view / set_chart_view so the user sees the right symbol/interval.
  Tag every drawing from one analysis with the same tag (e.g. "nvda-2025-10-18") and reuse that tag
  in save_analysis(annotation_tag=...). Clean up stale drawings with delete_annotations(tag=...).
- Record every recommendation with save_analysis (stop, targets, confidence 0-1 calibrated honestly,
  invalidation). Check get_track_record periodically and learn from misses.
- Save durable findings (thesis, catalysts, key dates, data quirks) with save_note; read
  search_notes(symbol) before re-analysing a name.
- Recommendations are research output, not financial advice; state key risks and uncertainty.
Guides: get_guide("workflow" | "expressions" | "annotations" | "interpretation").
"""

EXPRESSIONS = """\
Strategy/condition expressions (run_backtest, run_signal_study, scan_symbols, evaluate_expression):

Names: open high low close volume hl2 hlc3 ohlc4
Indicators (positional args in declared order, or keywords; see list_indicators):
  sma(50)  ema(21)  rsi(14)  atr(14)  adx(14)  natr(14)  rvol(20)  zscore(20)  cmf(20)  mfi(14)
  multi-output need an attribute: macd(12,26,9).histogram  bbands(20,2).lower  adx(14).plus_di
  stoch(14,3,3).k  supertrend(10,3).direction  ichimoku().kijun  donchian(20).upper  squeeze().squeeze_on
Helpers: crosses_above(a,b) crosses_below(a,b) prev(x,n=1) highest(x,n,offset=0) lowest(x,n,offset=0)
  mean(x,n) stdev(x,n) pct_change(x,n=1) rising(x,n=1) falling(x,n=1) abs(x) min(a,b) max(a,b) bars_since(cond)
Operators: + - * / % ** < <= > >= == != (chainable: 30 < rsi(14) < 70), and / or / not, parentheses.

Examples:
  Trend pullback:   close > sma(200) and rsi(14) < 40 and rising(sma(50), 5)
  Breakout:         close > highest(high, 20, 1) and rvol(20) > 1.5
  Golden cross:     crosses_above(sma(50), sma(200))
  Squeeze fire:     prev(squeeze().squeeze_on) == 1 and squeeze().squeeze_on == 0 and squeeze().momentum > 0
  Mean reversion:   zscore(20) < -2 and close > sma(200)
  Exit examples:    crosses_below(close, ema(21))   |   rsi(14) > 70   |   close < supertrend().supertrend
Backtest execution: signal on bar close, fill next bar open, costs default 6 bps/side; stops/targets
intrabar (stop_loss_pct, take_profit_pct, stop_atr, target_atr), max_hold_bars. Indicator args must be
constants; to smooth a series use mean(x, n).
"""

ANNOTATIONS = "Drawing types (draw_annotations):\n" + "\n".join(f"  {k}: {v}" for k, v in ANNOTATION_GUIDE.items()) + """

Points: {"time": "2025-03-14" | "2025-03-14T10:30-04:00" | epoch, "price": 123.45}
Optional per drawing: text, style {color, line_width 1-6, line_style solid|dashed|dotted, fill_color,
text_color, font_size, extend_left, extend_right, shape, position, show_labels}, interval (limit to
one timeframe), tag (group).

Colour conventions (keep consistent so the user can read the chart at a glance):
  support / bullish  #22c55e (zones rgba(34,197,94,0.12))     resistance / bearish  #ef4444 (rgba(239,68,68,0.12))
  trendlines         #3b82f6                                  targets #a855f7, stops #f97316 dashed
  neutral / notes    #eab308 text                             avoid more than ~12 drawings per analysis
Use time/price values from tool output (swing points, levels) rather than estimating them.
"""

WORKFLOW = """\
Rigorous single-stock workflow:
1. Context: get_research_brief(symbol) (+ search_notes, list_analyses for prior work).
   get_market_overview for regime; sector ETF relative strength.
2. Technicals: get_multi_timeframe -> get_technical_summary (1d, and 1wk for position trades)
   -> get_levels / get_patterns for exact levels. Intraday plans: 1h and 15m.
3. Evidence: turn each technical claim into a testable condition and run_signal_study on this symbol
   (and 2-3 peers to check robustness). Backtest any proposed entry/exit rule; report trades, win rate,
   expectancy, max DD, t-stat, out-of-sample vs in-sample, buy-and-hold comparison.
4. Fundamentals: get_fundamentals, get_financial_statements (quarterly trends), get_financial_history
   (5-10y), get_earnings (beat history, revisions), compare_peers. Read the latest 10-K/10-Q risk
   factors and MD&A via list_sec_filings + read_sec_filing when the thesis depends on them.
5. Sentiment/positioning: get_news, get_analyst_ratings, get_ownership (insiders), get_options_overview
   (IV, expected move into earnings, skew, put/call).
6. Risk: get_risk_metrics (beta, vol, drawdowns); define invalidation; calculate_position_size.
7. Chart: set_chart_view, draw key levels/trendlines/zones and the trade plan (long_position /
   short_position) with one tag.
8. Decide: bull case vs bear case, weigh evidence, action + horizon + entry zone + stop + targets +
   confidence. save_analysis(...) and save_note for anything to remember.
9. Report: concise verdict first, then evidence tables, risks, what would change your mind.
"""

INTERPRETATION = """\
Interpretation notes:
- Mechanical scores in get_technical_summary are a starting point (60% trend, 40% momentum), not a verdict.
- RSI: in strong uptrends 40-50 often acts as support (pullback zone); >70 alone is not a sell signal.
- ADX < 20: trend signals unreliable, favour range tactics; ADX > 25 with +DI > -DI: trend-following favoured.
- Volume: breakouts on rvol > 1.5 are more reliable; OBV diverging from price warns of a weak move.
- Bollinger bandwidth percentile < 20 / squeeze_on: volatility compression, expect expansion (direction unknown).
- Relative strength vs SPY and sector matters: leaders tend to keep leading in uptrends.
- Signal studies: overlapping forward windows inflate significance; prefer first_only=True and |t| > 2,
  compare with baseline drift (stocks rise on average), check several symbols.
- Backtests: < 30 trades is anecdotal; an edge that disappears out-of-sample is likely overfit.
- Valuation is relative: compare with the company's own history (get_financial_history) and peers.
- Earnings: options expected_move_pct shows what the market prices; consensus revisions (eps_trend) lead price.
- Insiders: open-market buys by several insiders are meaningful; routine sales are not.
- Data caveats: Yahoo fundamentals can lag or be wrong; cross-check important figures against SEC filings.
"""

GUIDES = {"workflow": WORKFLOW, "expressions": EXPRESSIONS, "annotations": ANNOTATIONS, "interpretation": INTERPRETATION}
