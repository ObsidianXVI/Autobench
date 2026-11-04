import 'dart:math' as math;

class XY {
  final double x, y;
  const XY(this.x, this.y);
}

/// Distance from [p] to the segment [a]-[b].
double segDist(XY p, XY a, XY b) {
  final dx = b.x - a.x, dy = b.y - a.y;
  final len = dx * dx + dy * dy;
  final t = len == 0 ? 0.0 : (((p.x - a.x) * dx + (p.y - a.y) * dy) / len).clamp(0.0, 1.0);
  return math.sqrt(math.pow(p.x - (a.x + t * dx), 2) + math.pow(p.y - (a.y + t * dy), 2));
}

/// The point on the line through [a] and [b] at [x] (keeping slope).
XY lineAt(XY a, XY b, double x) => b.x == a.x ? XY(x, b.y) : XY(x, a.y + (b.y - a.y) * (x - a.x) / (b.x - a.x));

/// "#rrggbb" -> "rgba(r,g,b,alpha)"; other colors are returned unchanged.
String hexAlpha(String color, double alpha) {
  final m = RegExp(r'^#([0-9a-fA-F]{6})$').firstMatch(color);
  if (m == null) return color;
  final v = int.parse(m.group(1)!, radix: 16);
  return 'rgba(${(v >> 16) & 255},${(v >> 8) & 255},${v & 255},$alpha)';
}

/// An axis-aligned box in pixels.
class Box {
  final double l, t, w, h;
  const Box(this.l, this.t, this.w, this.h);
  double get r => l + w;
  double get b => t + h;
  Box shift(double dx, double dy) => Box(l + dx, t + dy, w, h);
  bool overlaps(Box o, [double gap = 0]) => l < o.r + gap && o.l < r + gap && t < o.b + gap && o.t < b + gap;
  bool contains(double x, double y) => x >= l && x <= r && y >= t && y <= b;
  @override
  String toString() => 'Box($l, $t, $w, $h)';
}

/// Keeps labels drawn in one frame from covering each other: [place] moves a box along one axis to
/// the nearest spot that is clear of every box placed or reserved before it.
class LabelLayout {
  final List<Box> boxes = [];
  final double gap;
  LabelLayout({this.gap = 2});

  void clear() => boxes.clear();

  /// Marks [b] as taken without moving it (labels pinned to a price line, markers).
  void reserve(Box b) => boxes.add(b);

  /// The clear position nearest to [want], moving only vertically (or horizontally when [vertical] is
  /// false) by at most [maxShift] and staying within [lo]..[hi] on that axis. [dir] limits the
  /// direction: -1 only up/left, 1 only down/right, 0 either. When nothing is clear it takes the
  /// candidate covering the least label area. The result is reserved.
  Box place(Box want, {bool vertical = true, double maxShift = 120, int dir = 0, double? lo, double? hi}) {
    final size = vertical ? want.h : want.w;
    final start = vertical ? want.t : want.l;
    final offsets = <double>{0};
    for (final o in boxes) {
      // only boxes in the same lane can block a move along the axis
      final sameLane = vertical ? (want.l < o.r + gap && o.l < want.r + gap) : (want.t < o.b + gap && o.t < want.b + gap);
      if (!sameLane) continue;
      final (oLo, oHi) = vertical ? (o.t, o.b) : (o.l, o.r);
      offsets.add(oLo - gap - size - start);
      offsets.add(oHi + gap - start);
    }
    final sorted = offsets.where((d) => d.abs() <= maxShift && (dir == 0 || d == 0 || d.sign == dir)).toList()
      ..sort((a, b) => a.abs().compareTo(b.abs()));
    var best = want;
    var bestArea = _covered(want);
    for (final d in sorted) {
      final pos = start + d;
      if (lo != null && pos < lo) continue;
      if (hi != null && pos + size > hi) continue;
      final c = vertical ? want.shift(0, d) : want.shift(d, 0);
      final area = _covered(c);
      if (area == 0) {
        best = c;
        break;
      }
      if (area < bestArea) (best, bestArea) = (c, area);
    }
    boxes.add(best);
    return best;
  }

  double _covered(Box c) {
    var sum = 0.0;
    for (final o in boxes) {
      if (!o.overlaps(c, gap - 0.01)) continue;
      // touching within the gap still counts a little, so a clear spot always wins
      final w = math.min(c.r, o.r) - math.max(c.l, o.l), h = math.min(c.b, o.b) - math.max(c.t, o.t);
      sum += math.max(w, 0.5) * math.max(h, 0.5);
    }
    return sum;
  }
}
