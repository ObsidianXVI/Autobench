import 'dart:math' as math;

import 'time.dart';

/// Evenly spaced "nice" values (steps of 1, 2 or 5 x 10^k) covering [lo, hi] with about [target]
/// ticks.
({double step, List<double> values}) niceTicks(double lo, double hi, int target) {
  if (!(hi > lo) || target < 1) return (step: 0, values: const []);
  final raw = (hi - lo) / target;
  final mag = math.pow(10, (math.log(raw) / math.ln10).floor()).toDouble();
  final norm = raw / mag;
  final step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag; // round up: never denser than asked
  final values = <double>[];
  for (var v = (lo / step).ceil() * step; v <= hi + step * 1e-9; v += step) {
    values.add((v / step).round() * step); // avoid accumulated float drift
  }
  return (step: step, values: values);
}

/// A label on the time axis at bar [index].
class TimeTick {
  final int index;
  final TickWeight weight;
  final String label;
  const TimeTick(this.index, this.weight, this.label);
}

/// Picks time-axis labels for bars [from]..[to] (inclusive) given each bar's wall-clock time,
/// preferring more significant boundaries (year > month > day > hour) and keeping labels at least
/// [minBars] bars apart.
List<TimeTick> timeTicks(List<Civil> bars, int from, int to, double minBars, bool intraday) {
  if (bars.isEmpty) return const [];
  final lo = math.max(0, from), hi = math.min(bars.length - 1, to);
  if (hi < lo) return const [];
  final candidates = <TimeTick>[];
  Civil? prev = lo > 0 ? bars[lo - 1] : null;
  for (var i = lo; i <= hi; i++) {
    final c = bars[i];
    var w = prev == null ? TickWeight.day : tickWeight(prev, c);
    // intraday minute/hour labels only on round times so they read as a regular grid
    if (w == TickWeight.minute && c.minute % 15 != 0) w = TickWeight.none;
    if (w != TickWeight.none) candidates.add(TimeTick(i, w, tickLabel(c, w, intraday)));
    prev = c;
  }
  candidates.sort((a, b) => b.weight.index != a.weight.index
      ? b.weight.index - a.weight.index
      : a.index - b.index);
  final picked = <TimeTick>[];
  for (final c in candidates) {
    if (picked.every((p) => (p.index - c.index).abs() >= minBars)) picked.add(c);
  }
  picked.sort((a, b) => a.index - b.index);
  return picked;
}
