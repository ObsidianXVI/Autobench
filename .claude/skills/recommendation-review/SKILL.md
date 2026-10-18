---
name: recommendation-review
description: Audit past recommendations against what prices actually did — hit rate, R-multiples, alpha vs SPY, confidence calibration (Brier score) — diagnose systematic mistakes, and record lessons. Use when the user asks how past calls performed, about the track record, accuracy, calibration, or to review/update open recommendations.
---

# Recommendation review

You improve only if you grade yourself honestly.

## Procedure
1. `get_track_record()` (or `get_track_record(symbol)`) — summary + per-analysis evaluations.
2. Summarise: total / resolved / open; hit rate; average directional return; average R; average alpha
   vs SPY; calibration buckets (mean stated confidence vs realised hit rate); Brier score (0.25 =
   coin-flip at 50%; lower is better).
3. Diagnose losers — `get_analysis(id)` for each stopped-out or wrong expired call. Classify cause:
   - fought the higher-timeframe trend / regime
   - stop inside normal noise (< 1 ATR or inside signal MAE)
   - held through earnings / event gap
   - relied on an untested or weakly-tested signal
   - valuation-only call with no catalyst
   - data error / stale fundamentals
   - genuinely unforeseeable news (rare — don't overuse)
4. Check winners for luck vs skill: did the reasoning play out, or did price move for other reasons?
5. Calibration fix: if confidence 0.65-0.8 bucket hits < 55%, you are overconfident → state a rule
   ("cap at 0.65 unless evidence grade Strong on two pillars").
6. Open calls: re-check each with `get_technical_summary` / news; if the thesis is invalidated,
   say so and save an updated analysis (new `save_analysis` with action hold/sell) rather than
   editing history.
7. Record lessons: `save_note(title="Lessons YYYY-MM-DD", tags=["lessons"], content=...)` — concrete
   rules to apply next time. Read them (`search_notes(tag="lessons")`) at the start of new research.

## Output block
```
Track record (as of …): n=…, resolved …, hit …%, avg R …, alpha vs SPY …%, Brier …
Calibration: 0.50-0.65 → hit …% (n …) | 0.65-0.80 → …% | …
Top failure modes: 1) … (k cases) 2) …
Rules going forward: …
Open calls needing action: …
```
