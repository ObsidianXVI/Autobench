---
name: trade-execution
description: Turn a trade plan into orders on the user's connected brokerage account (IBKR paper/live or the simulator) — check the portfolio and limits, size from equity and the stop, preview, propose bracket orders for approval, follow up on rejections, manage and close positions, and review results. Use when the user asks to buy, sell, enter, exit, place/cancel an order, manage a position, or review their trades.
---

# Trade execution

You **propose**; the user **approves** (unless they turned on auto mode for a paper account). The
server enforces the risk limits — design orders that pass them, don't probe them.

## 1. Check the account — `get_portfolio`
- `status`: connected? paper or **live**? `halted`? `mode` (confirm / auto)? If not connected or
  halted: stop and tell the user (IB Gateway not running, kill switch on).
- Capital: net liquidation, cash, invested %, `open_risk`, `unprotected_positions` (flag these).
- Existing exposure in the same name or highly correlated names (`get_correlations`) — adding to a
  correlated bet counts as concentration.
- `limits`: max_risk_pct, max_position_pct, max_order_value, allow_short, allow_market_entries.

## 2. Plan → order (needs a plan from `trade-planning`)
| Plan element | Order field |
|---|---|
| Entry zone (pullback) | `order_type: limit`, `limit_price` = top of zone for longs |
| Breakout trigger | `order_type: stop` (or `stop_limit` with a cap), `stop_price` = trigger |
| Invalidation | `stop_loss` (required for entries) |
| T1 | `take_profit` (for T2, propose a partial close later) |
| Horizon | `tif`: `day` for intraday, `gtc` for swing |

## 3. Size
`shares = floor(net_liquidation × risk% ÷ |entry − stop|)`, risk% = 0.5–1% (≤ `max_risk_pct`),
then cap by `max_position_pct` and `max_order_value`; halve for earnings inside the horizon or
weak evidence. Whole shares, round down.

## 4. Preview, then propose
1. `preview_order(...)` — fix every error; read warnings (limit through the market, wide spread).
2. `save_analysis(...)` for the thesis if not done; pass its id as `analysis_id`.
3. `propose_order(..., rationale=...)`. Rationale (the user reads it to decide): setup, evidence
   (numbers), stop logic, target logic, size logic, what would make you cancel.
4. Tell the user it is waiting in the Trading page / chart (✦ proposal line) for approval.

## 5. Follow up
- `get_order_tickets(status="pending,rejected,expired,error")`. A **rejected** ticket carries the
  user's reason — adapt or drop the idea; never re-propose the same order unchanged.
- Entry no longer valid (thesis broken, gap away)? `cancel_order(ticket_id=...)` for a pending
  proposal, `cancel_order(order_id=...)` for a working entry.

## 6. Manage
- Exits: `propose_close_position(symbol, rationale, qty?, order_type?)` — partial at T1, full on
  invalidation. Leftover stop/target orders are cleaned up automatically.
- You cannot cancel or loosen a stop that protects a position. To move a stop, explain the new
  level and ask the user to drag the stop line on the chart (or propose closing instead).
- Earnings ahead on an open position: say so explicitly with the expected move.

## 7. Review
`get_trade_history()` → per-trade R vs the planned stop, win rate, expectancy, avg R, hold time.
Compare with `get_track_record`; record lessons with `save_note` (tag "journal").

Never: propose without a stop, average down a losing position without a new plan, exceed limits by
splitting orders, or present a proposal as already executed.
