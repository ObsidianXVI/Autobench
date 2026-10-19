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
- For idea generation use screen_stocks (FinViz-style screener over ~6,000 US stocks: valuation,
  growth, margins, performance, technicals) and get_market_breadth; then verify finalists with the
  single-name tools. Macro backdrop: get_macro_dashboard (FRED/Treasury/BLS), get_cross_asset_board,
  get_economic_calendar; earnings dates: get_earnings_calendar.
- Alerts (create_alert) notify the user live; use them for entry/stop levels of a recorded plan.
- show_in_ui(page=...) switches the user's screen (chart + a side panel, screener with your filters,
  heatmap, markets, calendar, a Trading or Journal tab) so they can see what you are describing.
- The user's chart setup is yours to arrange when asked: get_chart_layers, edit_chart_add (indicators,
  other symbols, volume; per-output colors/width/line style), edit_chart_item, edit_chart_remove,
  edit_chart_layer (layers, e.g. a 30m entry layer with its own price chart), set_chart_view
  (chart_type, scale), set_chart_layout / manage_layouts (multi-chart grid). Watchlists:
  update_watchlist (sections, order, rename, show), delete_watchlist. Ask before removing the user's
  own items, layers, watchlists or notes.
- get_journal shows the user's trades with what price did after each exit, aggregate performance,
  recent drawings and notes; add a lesson to a trade with save_note(tags=["trade:<id>"]).
- get_account_history covers the whole account from imported IBKR statements: current state, deposits
  and withdrawals, dividends/interest/fees, and returns. Keep growth (which includes the user's
  deposits) apart from investment gain and returns (TWR = how the investments did; MWR = the user's
  own result including the timing of their deposits). A deposit is never a profit.
- Trading (connected broker: IBKR paper/live or the simulator): get_portfolio first, then
  preview_order and propose_order with a stop_loss and a written rationale. Proposals wait for the
  user's approval unless they enabled auto mode on a paper account; risk limits are enforced on the
  server. You cannot approve, change limits or arm live trading. Read rejections (get_order_tickets)
  and do not re-propose the same order unchanged. Review results with get_trade_history.
- Recommendations are research output, not financial advice; state key risks and uncertainty.
Guides: get_guide("workflow" | "expressions" | "annotations" | "interpretation" | "screener" | "trading").
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
one timeframe), tag (group). Text on a line, shape or marker is shown only while the user hovers or
selects that drawing; put anything they must see at a glance in a text or callout drawing, which
stays visible (unless the user unticks "Text" in the chart toolbar). Labels that would overlap are
moved aside automatically, so keep them short.

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

def _screener_guide() -> str:
    from .services.screener import FIELDS, SIGNALS, VIEWS

    groups: dict[str, list[str]] = {}
    for f in FIELDS:
        groups.setdefault(f.group, []).append(f"{f.key}" + (f" ({f.desc})" if f.desc else ""))
    lines = ["Screener (screen_stocks) over every US-listed common stock (~6,000; S&P 500 via universe='sp500').",
             "", "Fields by group (units: pct fields are percent numbers, e.g. 12.5 = 12.5%; market_cap in $):"]
    for g, keys in groups.items():
        lines.append(f"  {g}: " + "; ".join(keys))
    lines += [
        "", "Filters: [{field, op, value}] with op in < <= > >= == != between(value=[lo,hi]) in(value=[...]) contains isnull notnull.",
        "Expression (combined with filters by AND): python-like, field names only, e.g.",
        "  pe < 20 and sales_qoq > 15 and roe > 15 and sector in [\"Information Technology\", \"Health Care\"]",
        "  sma50_pct > 0 and sma200_pct > 0 and from_high_52w > -5 and rel_volume > 1.5",
        "  abs(gap_pct) > 4 and avg_volume > 500000",
        "Signals (preset scans): " + ", ".join(f"{k} ({v[0]})" for k, v in SIGNALS.items()),
        "Sort: field name, '-' prefix for descending (default -market_cap). Views (column sets): " + ", ".join(VIEWS),
        "",
        "Data notes: fundamentals are TTM from SEC XBRL filings (latest annual + quarters since, minus the same",
        "quarters a year earlier); 'Q/Q' growth = latest quarter vs the same quarter a year earlier. Prices are live",
        "(Alpaca) when markets are open; technicals use daily bars through the last session. Companies without",
        "recent XBRL filings (new registrants, many foreign issuers) have empty fundamentals – screens that filter",
        "on them exclude those names, so say so when it matters.",
    ]
    return "\n".join(lines)


class _Guides(dict):
    def __missing__(self, key):
        if key == "screener":
            return _screener_guide()
        raise KeyError(key)

    def __contains__(self, key):
        return key == "screener" or dict.__contains__(self, key)


TRADING = """\
Trading with Autobench (the user's broker account: IBKR via IB Gateway/TWS, or the built-in simulator).

1. Context: get_portfolio -> status (paper/live, connected, halted, mode), net liquidation, cash vs
   invested, open risk, positions with their working stop/target, working orders, pending proposals,
   and the risk limits. If not connected or halted, do not propose; tell the user.
2. Plan first (trade-planning skill): entry from levels, structural stop, targets in R, event risk.
   Link the plan: save_analysis, then propose_order(analysis_id=...).
3. Size from the account: shares = floor(equity x risk% / |entry - stop|), within limits
   max_risk_pct (risk to stop), max_position_pct (including working entries) and max_order_value.
   Round down. preview_order shows exactly what the server will check.
4. propose_order: limit (or stop for breakouts) entries with stop_loss (required) and usually
   take_profit -> a bracket: both exits activate when the entry fills and cancel each other. TIF day
   for intraday ideas, gtc for swing entries. Rationale: setup, evidence, invalidation, what would
   make you cancel. The user sees it when approving.
5. Follow up: get_order_tickets(status="pending,rejected,expired,error"). A rejection reason is
   feedback; address it or drop the idea. Cancel stale entries you proposed with cancel_order.
6. Manage open positions: propose_close_position for exits (market or limit), partial closes via qty;
   propose_order_change to trail a stop or move a limit (always waits for the user's approval).
   You cannot cancel a stop protecting an open position; leftover stops/targets are cancelled
   automatically once a position is closed.
7. Review: get_trade_history -> win rate, expectancy, avg R, per-trade R vs the planned stop.
   Feed lessons into save_note and get_track_record.

Rules the server enforces: no shorting / market entries / outside-RTH orders unless enabled, daily
loss and order-count limits, symbol allow/block lists, live accounts read-only unless the user armed
them, and the kill switch (halt) blocks everything except the user's closing orders.
"""

GUIDES = _Guides({"trading": TRADING, "workflow": WORKFLOW, "expressions": EXPRESSIONS, "annotations": ANNOTATIONS,
                  "interpretation": INTERPRETATION})
