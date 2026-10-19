---
name: trade-planning
description: Turn an analysis into an executable, risk-managed trade plan — entry zone and trigger, structural/ATR stop, targets with R-multiples, position size, event risk, invalidation and management rules. Use when the user asks how to trade/enter/exit a stock, where to put a stop or target, how much to buy, risk/reward, or position sizing.
---

# Trade planning & risk

A recommendation without a plan is an opinion. Plans are built from **levels** (get_levels) and
**volatility** (ATR), never from round percentages alone.

## 1. Entry
- **Zone**, not a point: e.g. pullback into S1/POC/50-SMA confluence ("entry 178-181").
- **Trigger** for watch setups: "daily close above 187.2 on rvol > 1.5", "reclaim of 20-EMA".
- Chasing: if price is > 1.5 ATR beyond the ideal entry, prefer *watch* with a pullback entry.

## 2. Stop (invalidation)
- **Structural**: just beyond the level whose break invalidates the thesis (last swing low for a
  long, base low for a breakout), plus a buffer of 0.25-0.5 × ATR(14) to avoid noise.
- **Volatility check**: stop distance should be ≥ ~1 × ATR (else noise stops you) and preferably
  ≤ 3 × ATR for swing trades. Use the signal study's average MAE as a reality check.
- Write *why*: "below 171.5 = under the August swing low and the 0.618 retracement".

## 3. Targets
- T1 at the next meaningful resistance (or support for shorts); T2 at the following level, measured
  move, or fib extension (1.272/1.618).
- Compute **R-multiples**: (target − entry) / (entry − stop). Require T1 ≥ 1.5R for swing,
  ≥ 2R for position trades; otherwise the setup is "watch for better price".
- Options `expected_move_pct` gives a market-implied range for the horizon — targets far outside it
  need a catalyst.

## 4. Size
- `calculate_position_size(account_size, entry, stop, risk_pct, max_position_pct, targets)` when the
  user gives an account size. Default risk 0.5-1% of equity per trade; cap single-name exposure
  (default 25%). Halve size for: earnings inside the horizon, mixed timeframe alignment, VIX > 25,
  or evidence grade "Weak".
- Correlation: if the user holds related names, `get_correlations` — two 0.9-correlated positions
  are one bet.

## 5. Event risk
- Earnings inside the horizon: either plan to exit before, size for the gap (expected move), or make
  the plan explicitly an earnings play (see `earnings-preview`). Stops do not protect against gaps.
- Ex-dividend dates, index rebalances, FOMC/CPI for high-beta names.

- Check `get_earnings_calendar(symbols=[symbol])` and `get_economic_calendar(days=<horizon>)` for dated
  catalysts rather than relying on memory.

## 6. Management rules (state them)
- Partial at T1, move stop to breakeven (or below the new higher low).
- Time stop: exit if no progress within ~½ of the horizon.
- Thesis invalidation beats price stops: if the reason for the trade disappears (e.g. guidance cut),
  exit even if the stop isn't hit.
- Put the levels on alert (`create_alert`): entry trigger, stop, first target. Alerts fire in the user's UI.

## 7. Execution
With a connected broker, hand the plan to the `trade-execution` skill: `get_portfolio` for equity
and limits, then `preview_order` / `propose_order` as a bracket (stop_loss + take_profit). The user
approves proposals in the UI.

## Output block
```
Plan (swing, long): entry 178-181 (or trigger: close > 187.2 on rvol>1.5)
Stop 171.5 — under Aug swing low + 0.4 ATR (dist 5.0%, 1.9 ATR)
T1 192.0 (1.9R) prior high · T2 204 (3.2R) 1.272 ext · expected move (30d) ±9.8%
Size: 1% risk → 210 shares ($37.8k, 18% of account) · halved due to earnings 11-19
Manage: ½ off at T1, stop → BE; time stop 15 sessions; invalidate on close < 50-SMA with distribution volume
```
