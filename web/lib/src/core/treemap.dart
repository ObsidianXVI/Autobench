// Squarified treemap layout (Bruls, Huizing & van Wijk): rectangles whose areas are proportional
// to the weights, with aspect ratios kept close to 1.
import 'dart:math' as math;

class Rect {
  final double x, y, w, h;
  const Rect(this.x, this.y, this.w, this.h);
  double get area => w * h;
  bool contains(double px, double py) => px >= x && px < x + w && py >= y && py < y + h;
  Rect inset(double l, double t, double r, double b) =>
      Rect(x + l, y + t, math.max(0, w - l - r), math.max(0, h - t - b));
  @override
  String toString() => 'Rect(${x.toStringAsFixed(1)}, ${y.toStringAsFixed(1)}, '
      '${w.toStringAsFixed(1)}, ${h.toStringAsFixed(1)})';
}

/// Lays out [weights] (any order, non-negative) inside [bounds]. Returns one rect per weight, in
/// the input order. Zero weights get empty rects.
List<Rect> squarify(List<double> weights, Rect bounds) {
  final out = List<Rect>.filled(weights.length, Rect(bounds.x, bounds.y, 0, 0));
  final total = weights.fold<double>(0, (a, b) => a + math.max(0, b));
  if (total <= 0 || bounds.w <= 0 || bounds.h <= 0) return out;
  final scale = bounds.area / total;
  // largest first gives the best aspect ratios
  final order = [for (var i = 0; i < weights.length; i++) if (weights[i] > 0) i]
    ..sort((a, b) => weights[b].compareTo(weights[a]));
  var rect = bounds;
  var row = <int>[];

  double worst(List<int> r, double side) {
    if (r.isEmpty) return double.infinity;
    var sum = 0.0, mx = 0.0, mn = double.infinity;
    for (final i in r) {
      final a = weights[i] * scale;
      sum += a;
      mx = math.max(mx, a);
      mn = math.min(mn, a);
    }
    final s2 = side * side, sum2 = sum * sum;
    return math.max(s2 * mx / sum2, sum2 / (s2 * mn));
  }

  void layoutRow(List<int> r) {
    final sum = r.fold<double>(0, (a, i) => a + weights[i] * scale);
    if (rect.w >= rect.h) {
      // place a column on the left
      final colW = rect.h > 0 ? sum / rect.h : 0.0;
      var y = rect.y;
      for (final i in r) {
        final h = colW > 0 ? weights[i] * scale / colW : 0.0;
        out[i] = Rect(rect.x, y, colW, h);
        y += h;
      }
      rect = Rect(rect.x + colW, rect.y, math.max(0, rect.w - colW), rect.h);
    } else {
      final rowH = rect.w > 0 ? sum / rect.w : 0.0;
      var x = rect.x;
      for (final i in r) {
        final w = rowH > 0 ? weights[i] * scale / rowH : 0.0;
        out[i] = Rect(x, rect.y, w, rowH);
        x += w;
      }
      rect = Rect(rect.x, rect.y + rowH, rect.w, math.max(0, rect.h - rowH));
    }
  }

  for (final i in order) {
    final side = math.min(rect.w, rect.h);
    if (row.isEmpty || worst([...row, i], side) <= worst(row, side)) {
      row.add(i);
    } else {
      layoutRow(row);
      row = [i];
    }
  }
  if (row.isNotEmpty) layoutRow(row);
  return out;
}
