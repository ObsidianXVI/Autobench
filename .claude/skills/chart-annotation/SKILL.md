---
name: chart-annotation
description: Draw analysis on the user's live Autobench chart — support/resistance, trendlines, zones, Fibonacci, channels, labels, markers and trade plans — with consistent styling and tagging, and manage/clean up drawings. Use when the user asks to draw, mark, annotate, highlight or show something on the chart, or at the end of an analysis to visualise levels and the plan.
---

# Chart annotation

Drawings are server-side objects that appear live in the user's browser. They are part of your
deliverable: a clear chart lets the user verify your reasoning at a glance.

## Workflow
1. `get_chart_view()` — what the user is looking at. Then `set_chart_view(symbol, interval,
   from_time, to_time, indicators)` to frame the analysis (e.g. last 6-12 months on 1d; include the
   indicators you reference such as `sma 50`, `sma 200`, `rsi`). Don't hijack the view mid-task
   without reason; mention that you switched it.
2. Get exact coordinates from tools — `get_levels` (supports/resistances, swing_points, trendlines,
   fibonacci, volume_profile), `get_patterns` (double tops/bottoms points). Never eyeball prices.
3. Choose one tag per analysis: `"{symbol}-{YYYY-MM-DD}"` (lower-case symbol). Remove your previous
   drawings for that symbol if superseded: `list_annotations(symbol, author="agent")` →
   `delete_annotations(symbol=..., tag=old_tag, author="agent")`. **Never delete user drawings**
   unless asked.
4. `draw_annotations(symbol, items=[...])` in one batch (≤ 12 items for one analysis).
5. Pass the tag to `save_analysis(annotation_tag=...)`.

## Conventions
| Meaning | Type | Style |
|---|---|---|
| Support level | `horizontal_line` or `horizontal_ray` from the first touch | `#22c55e`, width 2, text "S1 123.40 (4 touches)" |
| Resistance level | same | `#ef4444` |
| Demand / supply zone | `rectangle` (from zone start to a future date) | fill `rgba(34,197,94,0.12)` / `rgba(239,68,68,0.12)` |
| Active trendline | `ray` through the two anchor points | `#3b82f6`, width 2 |
| Channel | `parallel_channel` (p1,p2 base line; p3 on the opposite side) | `#3b82f6`, fill `rgba(59,130,246,0.06)` |
| Fib of dominant swing | `fib_retracement` (swing start → swing end) | `#a855f7`, `show_labels: true` |
| Value area / POC | `rectangle` VAL..VAH, `horizontal_line` POC | `#eab308` dashed |
| Event (earnings, news) | `vertical_line` | `#64748b` dotted, text "Q3 earnings" |
| Signal occurrence | `marker` shape `arrow_up`/`arrow_down`, position below/above | green / red |
| Trade plan | `long_position` / `short_position` (entry at signal time; end time ≈ horizon) | defaults |
| Commentary | `callout` (anchor → label) or `text` | `#eab308` |

Labels: short and quantitative — "R1 187.20 · 3 touches", "Stop 172.9 (1.5×ATR below swing low)",
"T1 196 (2.1R)". Use `interval` on a drawing only if it is meaningless on other timeframes (e.g.
intraday VWAP levels).

## Examples
```json
[
 {"type":"horizontal_line","points":[{"price":182.4}],"text":"S1 182.40 · 4 touches","tag":"nvda-2025-10-18",
  "style":{"color":"#22c55e","line_width":2}},
 {"type":"ray","points":[{"time":"2025-04-07","price":86.6},{"time":"2025-08-01","price":167.9}],
  "text":"Rising support","tag":"nvda-2025-10-18","style":{"color":"#3b82f6"}},
 {"type":"rectangle","points":[{"time":"2025-09-02","price":168},{"time":"2025-11-28","price":174}],
  "text":"Demand zone","tag":"nvda-2025-10-18","style":{"fill_color":"rgba(34,197,94,0.12)","color":"#22c55e"}},
 {"type":"long_position","points":[{"time":"2025-10-17","price":180},{"time":"2025-11-21","price":171.5},
  {"time":"2025-11-21","price":198}],"text":"Swing long 2.1R","tag":"nvda-2025-10-18"}
]
```
Positions: p1 = (entry time, entry price), p2 = (end time, stop), p3 = (end time, target). For a long
stop < entry < target; for a short target < entry < stop.

## Maintenance
- `update_annotation(id, ...)` to move a level after new pivots form instead of stacking duplicates.
- Hide rather than delete when the user may want history: `update_annotation(id, visible=false)`.
- Keep the chart readable: ≤ 3 levels per side, 1-2 trendlines, 1 plan.
