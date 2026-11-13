---
name: quant-research
description: QBench quant research on a stock or a watchlist — regime diagnostics (trending vs mean-reverting), walk-forward testing of the quant strategy library, factor ranks (value, quality, growth, momentum, low vol), stat-arb pairs, risk-parity allocation, beta hedging, and combining quant evidence with FA/TA into a plan. Use when the user mentions QBench, quant, algos, systematic strategies, factors, momentum/mean-reversion strategies, pairs/arbitrage, hedging, portfolio weights, or asks which strategy fits a stock or which watchlist names score best.
---

# Quant research (QBench)

Read `get_guide("quant")` once per session: evidence standards, how to read the regime tests, the
strategy families and their real-world decay, sizing. The rule of the house still holds: **no edge
without a test**, and in QBench the test is *out-of-sample timing alpha deflated for every variant
tried* (`alpha_dsr`), not an in-sample Sharpe.

## Single stock: "which quant approach fits X?"
1. `search_notes(X)` (tag `qbench`) for earlier studies; reuse rather than repeat.
2. `get_quant_profile(X)` → regime verdict and favoured families, vol percentile, factor-ETF betas,
   tails, seasonality. State the regime with its evidence (Hurst, VR z-stats).
3. `get_factor_scores(X)` → percentiles market-wide and within sector.
4. `run_strategy_lab(X)` → read, for each strategy: verdict, `alpha_annual_pct`, `alpha_t`,
   `alpha_dsr`, OOS Sharpe vs buy-and-hold, max DD, `param_stability`, `entries`.
   - Survivors (robust / promising): inspect with `run_quant_strategy(X, id, params)` (yearly table,
     trades, worst year); rerun with `cost_bps` doubled.
   - Robustness: `run_strategy_lab` on 2-3 peers or the sector ETF with the same strategy ids. An
     effect unique to one ticker is probably noise.
   - "de-risks only" strategies (trend filters, vol targeting) are risk tools: say so.
   - If nothing survives, the honest answer is "no price-based timing edge"; move the focus to factor
     selection, sizing and hedging.
5. `get_quant_confluence(X)` → TA/FA/quant pillars and agreement, preferred plan (trend pullback /
   mean reversion / breakout), vol-target weight, Kelly bound.
6. Hand over to `trade-planning` for the executable plan: the quant plan levels are inputs, the
   structural stop and the user's tier rules decide. Cap size at the vol-target weight.
7. Record: `save_analysis` (rationale names strategy id, params, OOS stats) when it is a call;
   `save_note(title, content, symbol=X, tags=["qbench"])` for the study. `show_in_ui(page="qlab",
   symbol=X)` to show the Quant Lab.

## Watchlist: "which names should I focus on?"
1. `rank_watchlist_quant(watchlist)` → composite TA + FA + quant, regime, momentum rank.
2. `factor_screen(universe="watchlist:<name>", weights=…)` for a pure factor view; `run_factor_backtest`
   (momentum_12_1 / residual_momentum / low_vol) to see how the factor would have picked within the
   list vs its equal-weighted average (needs 15+ names to mean much).
3. Run the lab on the top 3-5 only. `show_in_ui(page="qsignals")`.

## Arbitrage, hedging, allocation
- Pairs: `find_pairs(symbols or watchlist)` → `analyze_pair(a, b)` (rolling OOS, stops, hedge-ratio
  stability). Same-industry, economically linked pairs only; note the short-leg requirement and that
  ~5% of tested pairs look cointegrated by chance. Single-name stat arb: `analyze_pair(X, sector ETF)`.
- Hedging: `get_hedge_plan()` (uses the broker positions) → dollar beta, SPY hedge, alternatives.
  Recommend hedges when the market-regime skill says hostile and theses are stock-specific.
- Allocation: `optimize_allocation(symbols, method="risk_parity" | "hrp" | "min_variance")` with
  `max_weight`; compare methods; explain risk contributions. Never optimise on expected returns.
- `show_in_ui(page="qarb")` to show pairs/hedge/allocation.

## Reporting
Verdict first, then a table (strategy, OOS Sharpe vs B&H, alpha %/yr, alpha_t, alpha_dsr, max DD,
trades, verdict). Name the number of variants tried, the OOS window, costs and the worst year. Cite
the library's evidence rating and decay note for any strategy you recommend
(`list_quant_strategies(strategy_id=…)`). Never present a myth (dividend capture) or a "contested"
effect as reliable.
