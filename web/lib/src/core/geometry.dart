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
