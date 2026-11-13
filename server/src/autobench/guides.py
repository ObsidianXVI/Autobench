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
- Your standing instructions (the Autobench system prompt, editable on the Claude page) are readable with
  get_system_prompt. When the user states a lasting preference about how you should work, you may rewrite it
  with update_system_prompt (full text + reason; keep their instructions). The user sees every change.
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
- QBench (quant workstation): get_quant_profile (regime: trending / mean-reverting / random walk), run_strategy_lab
  (walk-forward test of the strategy library on a stock), get_factor_scores / factor_screen (value, quality, growth,
  momentum, low-vol ranks over ~6,000 stocks), run_factor_backtest (rank a watchlist), find_pairs / analyze_pair
  (stat arb), optimize_allocation (risk parity, HRP), get_hedge_plan (beta hedges), get_quant_confluence and
  rank_watchlist_quant (TA + FA + quant in one score with quant-derived levels and sizing), list_quant_strategies
  (rules, papers, evidence, failure modes). Before using them read get_guide("quant"). Judge strategies by
  out-of-sample timing alpha deflated for the number of trials (alpha_dsr), never by in-sample Sharpe; a long-only
  rule on a rising stock earns a positive Sharpe for free. Most single-stock timing rules have no edge: say so.
- Financier is Autobench's second mode (the user's finance console: accounts, budgets, cashflow forecasts,
  the trading Portfolio and its risk plans), in the same vral-altos Firebase store. get_financier_summary
  shows which Portfolio and Virtual Accounts (e.g. L-Fund) the brokerage account backs and Financier's
  stop/target per open position; size against that capital and flag stops that disagree.
  show_in_ui(page="financier", tab="portfolio") opens it. It is read-only to you.
- Recommendations are research output, not financial advice; state key risks and uncertainty.
Guides: get_guide("workflow" | "expressions" | "annotations" | "interpretation" | "screener" | "trading" | "quant").
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

QUANT_WORKFLOW = """\
QBench study of one stock (quant-research skill):
1. Prior work: search_notes(symbol), list_analyses(symbol). Base rate: get_market_overview / market-regime skill.
2. get_quant_profile(symbol): regime verdict + favoured families, vol percentile, factor-ETF betas, tails, seasonality.
3. get_factor_scores(symbol): value / quality / growth / momentum / low-vol percentiles, market-wide and in-sector.
4. run_strategy_lab(symbol): which library strategies survive walk-forward with alpha_dsr ≥ 0.6? Read
   param_stability, entries (trade count) and the verdict; inspect a survivor with run_quant_strategy (yearly returns,
   trades). Cross-check robustness: run_strategy_lab on 2-3 peers or the sector ETF; a real effect should not be
   unique to one ticker.
5. get_quant_confluence(symbol): TA / FA / quant pillars, agreement, preferred plan, vol-target weight, Kelly bound.
6. Risk: correlation to current holdings (get_correlations, get_hedge_plan with the portfolio), position size from the
   stop (calculate_position_size) capped by the vol-target weight.
7. Decide and record: the quant view feeds the stock-research verdict (save_analysis with the strategy id, params and
   OOS numbers in the rationale); save_note the study (tag 'qbench'). Draw the plan levels with chart-annotation.
"""

QUANT = """\
QBench playbook: how to do quant research honestly with Autobench.

A. EVIDENCE STANDARDS (most backtests are false positives)
- Multiple testing: trying N variants inflates the best Sharpe. With ~45 configurations on 5 years of daily data
  the best in-sample Sharpe is ~1 by luck alone (Bailey & López de Prado, MinBTL). The lab reports dsr / alpha_dsr:
  the probability the result beats the best of all combinations tried by chance. ≥ 0.95 strong, 0.6-0.9 suggestive,
  < 0.5 indistinguishable from data mining. Count every variant you try, including ones you discard.
- Timing alpha, not Sharpe: a long-only rule on a rising stock inherits its drift. Skill = alpha in
  r_strategy = a + b·r_stock (alpha_annual_pct, alpha_t, alpha_dsr). "de-risks only" = same Sharpe, smaller drawdown:
  useful as risk control, not as an edge.
- Out-of-sample: the lab's walk-forward re-optimises on past data only. Low param_stability (< 0.5) = the "best"
  parameters jump around = fragile. A big drop from in_sample_best Sharpe to OOS Sharpe = overfit.
- Sample size: < 30 round trips is anecdotal; t-stat of the mean trade > 2; PSR > 0.95 for Sharpe > 0.
  MinTRL: a Sharpe of 0.5 needs roughly 10+ years of daily data to be distinguished from zero.
- Post-publication decay: anomalies lose ~35-55% of their return after publication (McLean & Pontiff 2016), more for
  liquid large caps. Expect live results well below the paper and below the backtest.
- Costs: 5 bps per unit of turnover is a liquid-large-cap floor; small caps 20-50 bps. Daily strategies (IBS, RSI(2),
  overnight) live or die on costs: rerun with cost_bps doubled before trusting them.
- Survivorship: watchlists and today's index members are survivors; compare a ranking strategy with the equal-weighted
  list (run_factor_backtest does), not with cash.
- Regime dependence: report the worst year (yearly table) and the max drawdown; ask whether today's regime resembles
  the periods that made the money.

B. DIAGNOSING A STOCK (get_quant_profile)
- Hurst (log prices, 2y): ~0.5 random walk; > 0.58 persistent (trend); < 0.42 anti-persistent (reversion). A random
  walk scatters with sd ≈ 0.04 over 2 years, so 0.45-0.55 means nothing; prefer the variance-ratio z-stats.
- Variance ratio VR(q) (Lo-MacKinlay, robust z): VR > 1 with z > 1.96 = momentum at horizon q; VR < 1 with z < -1.96 =
  reversion. VR(2) < 1 alone often reflects bid-ask bounce.
- ADF on log price: individual stocks almost never reject a unit root (p > 0.1 is normal). Mean reversion in a single
  stock is usually around a moving mean (half_life_vs_sma50_bars), not a fixed level. Spreads (pairs) are where ADF matters.
- Most liquid large caps test as "random walk": then price-only timing rules are unlikely to add alpha; the edge, if any,
  is in factor selection (which stocks), sizing (vol targeting) and risk control (trend filters, hedges).
- Vol percentile > 80: shrink size (vol-target weight falls automatically), widen stops in ATR terms, expect
  mean reversion of vol. Vol clusters: today's vol is the best forecast of next month's.
- Factor-ETF betas (proxies: IWM-SPY size, IWD-IWF value, MTUM/QUAL/USMV minus SPY): know what you really own; a
  "stock pick" with momentum beta 1.2 is largely a momentum-factor bet. Alpha_t > 2 over 2 years is rare.
- Overnight vs intraday: US stocks earn most of their drift overnight (Lou, Polk & Skouras 2019).

C. STRATEGY FAMILIES (list_quant_strategies for full cards with papers)
- Trend / time-series momentum (tsmom, ma_trend, donchian, dual_momentum, high52): strong evidence across a century and
  asset classes, mostly via diversified futures and volatility scaling (Kim-Tse-Wald). On single stocks: lower win
  rate, positive skew, whipsaw in ranges, losses at V-reversals. Main value for a stock trader: a regime/risk filter
  (stay out below the 200-day; cut drawdowns), not alpha.
- Cross-sectional momentum (12-1, residual momentum, 52-week high): among the most robust anomalies; residual momentum
  (Blitz-Huij-Martens) avoids most momentum-crash risk. Crashes happen in sharp junk rallies after bear markets
  (2009). Use to choose which watchlist names to favour, monthly.
- Short-term reversal / mean reversion (rsi2, ibs, bb_revert, reversal_1m): real but cost-sensitive and strongest in
  index ETFs and liquid large caps in bull markets; always with a trend filter (above SMA200) and an exit at the
  mean; on single stocks earnings gaps and news break it.
- Factors (value, quality/profitability, low volatility, momentum): long-horizon premia, multi-year drawdowns
  (value 2007-2020). Combine (quality + value, quality + momentum) rather than use one; factor_screen with
  sector_neutral=True avoids accidental sector bets.
- Volatility targeting: smoother risk; real-time return benefit is contested (Cederburg et al. 2020). Use for sizing.
- Seasonality (turn of month, day of week): weak on single names; never a stand-alone reason to trade.
- Stat arb / pairs: needs a short leg and an economic link; profits have declined since the 1990s (Do & Faff) mostly
  because more pairs fail to re-converge. find_pairs tests many pairs: expect 5% false positives. Require
  analyze_pair OOS results, a stable hedge ratio and same-industry economics. Without shorting, use the z-score to time
  buying the cheap leg, or hedge with the sector ETF.
- Event: PEAD has largely disappeared in large caps since ~2006 (Martineau 2022). Dividend capture is a myth.

D. COMBINING QUANT WITH FA / TA (get_quant_confluence, rank_watchlist_quant)
- Use factors to choose WHAT (rank the watchlist: quality + momentum, reasonable value), the regime and a tested
  strategy to choose HOW (pullback in trend vs reversion band vs breakout), TA levels to choose WHERE (entry zone,
  structural stop), and volatility to choose HOW MUCH.
- Pillars "conflicting" (spread > 40 points) → smaller size or wait. Strong FA + weak TA = watch for the trend to turn;
  strong TA + weak FA = trade the trend with tight risk, not a long-term hold.
- The lab's "position today" is a signal only for strategies whose verdict is robust/promising; otherwise ignore it.

E. SIZING AND RISK
- Fixed-fractional risk to the stop stays the primary rule (trade-planning skill and the user's tier rules).
- Cap the position at the vol-target weight (10% annual risk budget / stock vol): a 60%-vol stock gets ≤ ~17% of equity.
- Kelly f = μ/σ² overstates the optimal bet because μ is estimated with large error: never more than half Kelly,
  usually far less; Autobench caps it at 25%.
- Correlation: two positions with ρ > 0.7 are close to one bet; check get_correlations / optimize_allocation before
  adding. Portfolio beta: get_hedge_plan; hedge (SPY/sector ETF/puts) only when the thesis is stock-specific and the
  market regime is hostile.

F. REPORTING A QUANT VIEW
- Lead with the verdict and the strongest number with its uncertainty ("tsmom 252d: OOS Sharpe 0.8 vs buy-and-hold 0.7,
  alpha 1.2%/yr, alpha_t 0.6, alpha_dsr 0.41 → not significant; useful only as a drawdown filter").
- State the number of variants tried, the OOS window, costs, and the worst year. Say plainly when nothing works.
- Record: save_analysis rationale names the strategy id + params + OOS stats; save_note(tags=["qbench", symbol]).
"""

GUIDES = _Guides({"quant": QUANT, "trading": TRADING, "workflow": WORKFLOW, "expressions": EXPRESSIONS, "annotations": ANNOTATIONS,
                  "interpretation": INTERPRETATION})
