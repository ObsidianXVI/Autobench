# Trade planning: two tiers (set by the user 2026-11-09)

Label every proposed ticket **Tier A** or **Tier B** and say why in the rationale.

## Tier A: high-confidence big swing
Use only when ALL of these hold:
- Calibrated confidence ≥ 0.6 (as recorded in save_analysis).
- A tested edge on the stock itself: run_signal_study t ≳ 1.5 with ≥ 15 occurrences, or consistently positive across horizons and peers.
- Clean structure, the trend aligned across timeframes, no earnings inside the hold, and a market regime that isn't hostile.

Rules:
- Target an **8-20% move** at **≥ 1.8R** (2R+ preferred). Skip the setup if 1.8R needs a target above 20% or beyond realistic structure.
- **The stop-loss order is optional** (user's decision 2026-11-09). Either attach a wide stop (~2.5-3 × ATR(14), beyond the study's average MAE and beyond structure) or send the entry with a take-profit only.
- Always define the invalidation level, even with no stop order:
  - Size the position as if the stop sat at that level.
  - Create an alert there with create_alert.
  - State in the rationale that no stop order is attached.
- Without a stop, the server cannot measure or cap risk, so self-enforce the limits: risk to invalidation ≤ **1.5% equity**, position ≤ **25% NAV**.

## Tier B: everything else
This covers lower reward, an untested setup or unknown target, and confidence < 0.6. Use the original trade-planning defaults:
- **A stop-loss is required.** Use a structural stop + 0.25-0.5 × ATR buffer, 1-3 × ATR from entry, checked against MAE.
- T1 ≥ 1.5R for a swing trade.
- Risk **0.5-1% of equity**, position ≤ **20% NAV**. Halve the size for earnings inside the hold, mixed timeframe alignment or weak evidence.

## Order mechanics (IBKR)
- Bracket children (stop-loss / take-profit) are in an **OCA group and cannot be modified**: IBKR rejects the change, and a failed modify can leave the bracket without its stop.
- Get the stop and target right when first proposing. Re-check them against these rules before every propose_order.
- To change a bracket whose entry hasn't filled: cancel the entry (its children go with it) and propose a fresh bracket.
- To change the exits of a filled position: propose closing the position and reopening it, or tell the user to edit it in TWS. Do not use propose_order_change on OCA children.
- After any order change, verify with get_portfolio that the stop and target orders still exist.

## Both tiers
- Keep total open risk (to stops or invalidation levels) ≲ 6% of equity.
- Server limits are set to the Tier A ceiling (max_risk_pct 1.5, max_position_pct 25, max_order_value $5,000, max_daily_loss_pct 3, require_stop_for_agent off). Self-enforce the lower Tier B caps and the Tier B stop requirement; the server won't.
- You cannot change limits. If a plan needs different limits, tell the user (Trading → Risk).

# QBench: quant workstation (added 2026-11-12)

The user can show QBench pages (Quant Lab, Q-Signals, Arb & Hedge, Strategies) with "Show QBench Controls". Use the `quant-research` skill and read `get_guide("quant")` once per session before quant work.

## Evidence standard
- Judge a strategy by **out-of-sample timing alpha vs holding the stock, deflated for every variant tried** (`alpha_dsr` in run_strategy_lab), never by in-sample Sharpe. A long-only rule on a rising stock earns a positive Sharpe for free.
- Verdicts: robust (alpha_dsr ≥ 0.9) > promising (≥ 0.6) > de-risks only (risk tool, not an edge) > not significant / no edge. Confirm survivors on 2-3 peers or the sector ETF and rerun with costs doubled.
- Expect published anomalies to deliver well below the paper (post-publication decay ~35-55%). Most liquid large caps test as a random walk: then say there is no price-timing edge and focus on factor selection, sizing and risk control.
- Report the number of variants tried, the OOS window, costs and the worst year. Never present a "contested" effect as reliable or a "myth" (dividend capture) at all.

## Using quant with the tiers
- QBench results support, but do not replace, the Tier A "tested edge" requirement above; cite strategy id, params and OOS numbers in the rationale.
- Cap position size at the **vol-target weight** from get_quant_confluence (10% annual risk budget / stock vol), in addition to the tier risk limits. Never size above half Kelly.
- Conflicting TA/FA/quant pillars (confluence "conflicting") → Tier B at most, or wait.
- Use factors to choose what (rank_watchlist_quant, factor_screen), regime and a tested strategy to choose how (pullback vs reversion vs breakout), TA levels to choose where, volatility to choose how much.
- Pairs need a short leg: only propose them if shorting is enabled; otherwise use the z-score to time the cheap leg. Beta hedges (get_hedge_plan) only when the market regime is hostile and theses are stock-specific; ask the user first.
- Save quant studies with save_note(tags=["qbench", symbol]).