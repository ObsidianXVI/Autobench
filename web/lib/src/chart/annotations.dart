// Renders server-side annotations (agent + user drawings) on the main chart pane.
import 'dart:js_interop';
import 'dart:math' as math;

import 'package:web/web.dart';

import '../core/format.dart';
import '../core/geometry.dart';
import '../core/json.dart';
import '../core/mapper.dart';
import '../core/models.dart';
import 'engine.dart';

const agentColor = '#a78bfa';
const userColor = '#38bdf8';
const _fib = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0];
const _fibColors = ['#94a3b8', '#f87171', '#fb923c', '#facc15', '#4ade80', '#22d3ee', '#94a3b8'];
const _font = 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif';

/// An in-progress drawing (the last point follows the cursor).
class Draft {
  final String type;
  final List<Point> points;
  Draft(this.type, this.points);
}

class AnnotationLayer {
  final ChartEngine chart;
  final mapper = TimeMapper();
  List<Annotation> annotations = const [];
  Draft? draft;
  String? selectedId, hoverId;
  bool hidden = false;
  /// Standalone text and callouts (the toolbar's "Text" checkbox). Text attached to other drawings
  /// only shows while that drawing is hovered or selected.
  bool showText = true;
  /// Layers whose drawings cannot be dragged (null/'' = the main layer).
  Set<String> lockedLayers = const {};
  /// Drawings being dragged: shown instead of the stored version until the edit is committed.
  final Map<String, Annotation> previews = {};
  double _w = 10000, _h = 10000;
  /// Label boxes drawn last frame per drawing (hit testing follows labels that were moved aside).
  final Map<String, List<Box>> _boxes = {};
  String? _cur;
  bool _pinned = false;
  Map<String, List<Box>> get labelBoxes => _boxes;

  AnnotationLayer(this.chart) {
    chart.overlay = _drawAll;
  }

  void update() => chart.invalidate();

  // ---------------------------------------------------------------- coordinates

  double? x(int? t) {
    if (t == null) return null;
    final lg = mapper.logical(t);
    return lg == null ? null : chart.logicalToX(lg);
  }

  double? y(double? p) => chart.priceToY(p);

  XY? pt(Point p) {
    final xx = x(p.time), yy = y(p.price);
    return xx == null || yy == null ? null : XY(xx, yy);
  }

  // ---------------------------------------------------------------- drawing

  static bool isText(Annotation a) => a.type == 'text' || a.type == 'callout';

  bool _shown(Annotation a) => a.visible && (showText || !isText(a) || a.id == selectedId);

  void _drawAll(CanvasRenderingContext2D ctx, double w, double h) {
    _w = w;
    _h = h;
    _boxes.clear();
    if (!hidden) {
      // the hovered and selected drawings go last: on top, and their revealed text doesn't push the
      // labels of the others around
      bool front(Annotation a) => a.id == hoverId || a.id == selectedId;
      for (final stored in [...annotations.where((a) => !front(a)), ...annotations.where(front)]) {
        final a = previews[stored.id] ?? stored;
        if (!_shown(a)) continue;
        final sel = a.id == selectedId;
        _drawOne(ctx, a, w, h, sel, a.id == hoverId);
        if (sel) _handles(ctx, [for (final hd in handles(a)) hd.$2], _baseColor(a), locked: isLocked(a));
      }
    }
    final d = draft;
    if (d != null && d.points.isNotEmpty) {
      final style = J(const {'line_style': 'dashed'});
      _drawOne(ctx, Annotation(id: '__draft', symbol: '', type: d.type, points: d.points, style: style), w, h, true,
          false);
    }
  }

  void _dash(CanvasRenderingContext2D ctx, List<num> d) => ctx.setLineDash([for (final v in d) v.toJS].toJS);

  void _lineStyle(CanvasRenderingContext2D ctx, String? style) =>
      _dash(ctx, style == 'dashed' ? const [6, 4] : style == 'dotted' ? const [2, 3] : const []);

  /// Claims a spot for a box anchored at [want]: kept as is for the drawing being edited, otherwise
  /// moved clear of labels already on the chart (all layers, orders and fills).
  Box _claim(Box want, {bool vertical = true, int dir = 0}) {
    final b = _pinned
        ? (chart.labels..reserve(want)).boxes.last
        : chart.labels.place(want, vertical: vertical, dir: dir, lo: 0, hi: vertical ? _h : _w, maxShift: vertical ? 120 : _w);
    (_boxes[_cur ?? ''] ??= []).add(b);
    return b;
  }

  void _label(CanvasRenderingContext2D ctx, String text, double x, double y, String color,
      {String align = 'left', String? bg, String? fg, double size = 11, bool vertical = true, int dir = 0}) {
    ctx.font = '500 ${size}px $_font';
    final w = ctx.measureText(text).width + 10;
    final h = size + 7;
    var left = x;
    if (align == 'right') left = x - w;
    if (align == 'center') left = x - w / 2;
    left = math.max(2, math.min(left, _w - w - 2));
    final want = Box(left, y - h / 2, w, h);
    final box = _claim(want, vertical: vertical, dir: dir);
    left = box.l;
    _dash(ctx, const []);
    if ((box.t - want.t).abs() > 3) {
      // moved aside: a faint leader back to where the label belongs
      ctx.strokeStyle = hexAlpha(color, 0.5).toJS;
      ctx.lineWidth = 1;
      _line(ctx, XY(x.clamp(box.l, box.r), y), XY(x.clamp(box.l, box.r), box.t > want.t ? box.t : box.b));
    }
    y = box.t + h / 2;
    ctx.fillStyle = (bg ?? 'rgba(15,23,42,0.85)').toJS;
    ctx.strokeStyle = color.toJS;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.roundRect(left, y - h / 2, w, h, 4.toJS);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = (fg ?? '#e2e8f0').toJS;
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    ctx.fillText(text, left + 5, y + 0.5);
  }

  void _line(CanvasRenderingContext2D ctx, XY p, XY q) {
    ctx.beginPath();
    ctx.moveTo(p.x, p.y);
    ctx.lineTo(q.x, q.y);
    ctx.stroke();
  }

  void _drawOne(CanvasRenderingContext2D ctx, Annotation a, double W, double H, bool selected, bool hover) {
    final st = a.style;
    final base = st['color'].s ?? (a.author == 'agent' ? agentColor : userColor);
    final lw = (st['line_width'].d ?? (a.type == 'horizontal_line' ? 1.5 : 2)) + (selected || hover ? 1 : 0);
    ctx.save();
    ctx.strokeStyle = base.toJS;
    ctx.fillStyle = base.toJS;
    ctx.lineWidth = lw;
    _lineStyle(ctx, st['line_style'].s);
    _cur = a.id;
    _pinned = selected && (a.id == '__draft' || previews.containsKey(a.id));
    final pts = [for (final p in a.points) pt(p)];
    XY? at(int i) => i < pts.length ? pts[i] : null;
    double? price(int i) => i < a.points.length ? a.points[i].price : null;
    // text attached to a line or shape waits for hover/selection; standalone text always shows
    final text = isText(a) || selected || hover ? (a.text ?? '') : '';
    final badge = a.author == 'agent' ? '✦ ' : '';

    switch (a.type) {
      case 'trendline' || 'ray' || 'extended_line' || 'arrow':
        final p = at(0), q = at(1);
        if (p == null || q == null) break;
        var s = p, e = q;
        if (a.type == 'ray' || a.type == 'extended_line' || st['extend_right'].b == true) {
          e = lineAt(p, q, q.x >= p.x ? W : 0);
        }
        if (a.type == 'extended_line' || st['extend_left'].b == true) s = lineAt(p, q, q.x >= p.x ? 0 : W);
        _line(ctx, s, e);
        if (a.type == 'arrow') {
          final ang = math.atan2(q.y - p.y, q.x - p.x);
          _dash(ctx, const []);
          ctx.beginPath();
          ctx.moveTo(q.x, q.y);
          ctx.lineTo(q.x - 10 * math.cos(ang - 0.4), q.y - 10 * math.sin(ang - 0.4));
          ctx.lineTo(q.x - 10 * math.cos(ang + 0.4), q.y - 10 * math.sin(ang + 0.4));
          ctx.closePath();
          ctx.fill();
        }
        if (text.isNotEmpty) _label(ctx, badge + text, (p.x + q.x) / 2, (p.y + q.y) / 2 - 12, base, align: 'center');
      case 'horizontal_line' || 'horizontal_ray':
        final yy = y(price(0));
        if (yy == null) break;
        final x0 = a.type == 'horizontal_ray' ? (x(a.points[0].time) ?? 0.0) : 0.0;
        _line(ctx, XY(x0, yy), XY(W, yy));
        _label(ctx, '$badge${text.isNotEmpty ? '$text · ' : ''}${fmtPrice(price(0)!)}', W - 60, yy, base,
            align: 'right', vertical: false, dir: -1);
      case 'vertical_line':
        final xx = x(a.points.isEmpty ? null : a.points[0].time);
        if (xx == null) break;
        _line(ctx, XY(xx, 0), XY(xx, H));
        if (text.isNotEmpty) _label(ctx, badge + text, xx + 4, H - 14, base);
      case 'rectangle':
        final p = at(0), q = at(1);
        if (p == null || q == null) break;
        ctx.fillStyle = (st['fill_color'].s ?? hexAlpha(base, 0.12)).toJS;
        final l = math.min(p.x, q.x), t = math.min(p.y, q.y), w = (q.x - p.x).abs(), h = (q.y - p.y).abs();
        ctx.fillRect(l, t, w, h);
        ctx.strokeRect(l, t, w, h);
        if (text.isNotEmpty) _label(ctx, badge + text, l + 4, t + 12, base);
      case 'parallel_channel':
        final p = at(0), q = at(1), r = at(2);
        if (p == null || q == null || r == null) break;
        final off = r.y - lineAt(p, q, r.x).y;
        final p2 = XY(p.x, p.y + off), q2 = XY(q.x, q.y + off);
        ctx.fillStyle = (st['fill_color'].s ?? hexAlpha(base, 0.08)).toJS;
        ctx.beginPath();
        ctx.moveTo(p.x, p.y);
        ctx.lineTo(q.x, q.y);
        ctx.lineTo(q2.x, q2.y);
        ctx.lineTo(p2.x, p2.y);
        ctx.closePath();
        ctx.fill();
        _line(ctx, p, q);
        _line(ctx, p2, q2);
        _dash(ctx, const [4, 4]);
        _line(ctx, XY(p.x, (p.y + p2.y) / 2), XY(q.x, (q.y + q2.y) / 2));
        if (text.isNotEmpty) _label(ctx, badge + text, q.x + 4, q.y, base);
      case 'fib_retracement':
        final p = at(0), q = at(1);
        final p0 = price(0), p1 = price(1);
        if (p == null || q == null || p0 == null || p1 == null) break;
        final left = math.min(p.x, q.x);
        for (var i = 0; i < _fib.length; i++) {
          final r = _fib[i];
          final lvl = p1 - r * (p1 - p0);
          final yy = y(lvl);
          if (yy == null) continue;
          ctx.strokeStyle = _fibColors[i].toJS;
          _dash(ctx, r == 0 || r == 1 ? const [] : const [4, 3]);
          _line(ctx, XY(left, yy), XY(W, yy));
          if (st['show_labels'].b != false) {
            ctx.font = '11px $_font';
            ctx.fillStyle = _fibColors[i].toJS;
            ctx.textBaseline = 'bottom';
            ctx.textAlign = 'left';
            ctx.fillText('${r.toStringAsFixed(3)} (${fmtPrice(lvl)})', left + 4, yy - 2);
          }
        }
        ctx.strokeStyle = base.toJS;
        _dash(ctx, const [2, 3]);
        _line(ctx, p, q);
        if (text.isNotEmpty) _label(ctx, badge + text, left + 4, math.min(p.y, q.y) - 14, base);
      case 'price_range':
        final p = at(0), q = at(1);
        final p0 = price(0), p1 = price(1);
        if (p == null || q == null || p0 == null || p1 == null) break;
        final d = p1 - p0;
        final pc = d / p0 * 100;
        final col = d >= 0 ? chart.theme.up : chart.theme.down;
        ctx.fillStyle = hexAlpha(col, 0.12).toJS;
        ctx.fillRect(math.min(p.x, q.x), math.min(p.y, q.y), (q.x - p.x).abs(), (q.y - p.y).abs());
        ctx.strokeStyle = col.toJS;
        _line(ctx, XY(q.x, p.y), XY(q.x, q.y));
        _label(ctx, '${d >= 0 ? '+' : ''}${fmtPrice(d)} (${pct(pc)})${text.isNotEmpty ? ' · $text' : ''}', q.x,
            q.y + (d >= 0 ? -14 : 14), col,
            align: 'center');
      case 'polyline':
        final ok = pts.whereType<XY>().toList();
        if (ok.length < 2) break;
        ctx.beginPath();
        ctx.moveTo(ok.first.x, ok.first.y);
        for (final p in ok.skip(1)) {
          ctx.lineTo(p.x, p.y);
        }
        ctx.stroke();
        if (text.isNotEmpty) _label(ctx, badge + text, ok.last.x + 4, ok.last.y, base);
      case 'text':
        final p = at(0);
        if (p == null) break;
        _label(ctx, badge + text, p.x, p.y, base, size: st['font_size'].d ?? 12, fg: st['text_color'].s);
      case 'callout':
        final p = at(0), q = at(1);
        if (p == null || q == null) break;
        _dash(ctx, const []);
        _line(ctx, p, q);
        ctx.beginPath();
        ctx.arc(p.x, p.y, 3, 0, math.pi * 2);
        ctx.fill();
        _label(ctx, badge + text, q.x, q.y, base, size: st['font_size'].d ?? 12, fg: st['text_color'].s);
      case 'marker':
        final p = at(0);
        if (p == null) break;
        final shape = st['shape'].s ?? 'circle';
        final pos = st['position'].s ??
            (shape == 'arrow_up'
                ? 'below'
                : shape == 'arrow_down'
                    ? 'above'
                    : 'at');
        // markers on the same bar stack outward instead of covering each other
        final yy = _claim(Box(p.x - 6, p.y + (pos == 'below' ? 14 : pos == 'above' ? -14 : 0) - 7, 12, 14),
                    dir: pos == 'below' ? 1 : pos == 'above' ? -1 : 0)
                .t +
            7;
        _dash(ctx, const []);
        ctx.beginPath();
        if (shape == 'arrow_up') {
          ctx.moveTo(p.x, yy - 7);
          ctx.lineTo(p.x - 6, yy + 5);
          ctx.lineTo(p.x + 6, yy + 5);
        } else if (shape == 'arrow_down') {
          ctx.moveTo(p.x, yy + 7);
          ctx.lineTo(p.x - 6, yy - 5);
          ctx.lineTo(p.x + 6, yy - 5);
        } else if (shape == 'square') {
          ctx.rect(p.x - 5, yy - 5, 10, 10);
        } else {
          ctx.arc(p.x, yy, 5, 0, math.pi * 2);
        }
        ctx.closePath();
        ctx.fill();
        if (text.isNotEmpty) {
          _label(ctx, text, p.x, yy + (pos == 'above' ? -16 : 16), base, align: 'center', size: 10,
              dir: pos == 'above' ? -1 : 1);
        }
      case 'long_position' || 'short_position':
        final e = at(0), s = at(1), t = at(2);
        final entry = price(0), stop = price(1), tgt = price(2);
        if (e == null || s == null || t == null || entry == null || stop == null || tgt == null) break;
        final x1 = e.x;
        final x2 = math.max(s.x, x1 + 30);
        final rr = (tgt - entry).abs() / (entry - stop).abs();
        _dash(ctx, const []);
        ctx.fillStyle = 'rgba(34,197,94,0.18)'.toJS;
        ctx.fillRect(x1, math.min(e.y, t.y), x2 - x1, (t.y - e.y).abs());
        ctx.fillStyle = 'rgba(239,68,68,0.18)'.toJS;
        ctx.fillRect(x1, math.min(e.y, s.y), x2 - x1, (s.y - e.y).abs());
        ctx.strokeStyle = '#94a3b8'.toJS;
        ctx.lineWidth = 1;
        _line(ctx, XY(x1, e.y), XY(x2, e.y));
        String rel(double v) => pct((v - entry) / entry * 100);
        _label(ctx, '${badge}Target ${fmtPrice(tgt)} (${rel(tgt)}) · ${rr.toStringAsFixed(2)}R', x1 + 4, t.y, chart.theme.up);
        _label(ctx, 'Stop ${fmtPrice(stop)} (${rel(stop)})', x1 + 4, s.y, chart.theme.down);
        _label(ctx,
            '${a.type == 'long_position' ? 'Long' : 'Short'} ${fmtPrice(entry)}${text.isNotEmpty ? ' · $text' : ''}',
            x1 + 4, e.y, '#94a3b8');
    }
    ctx.restore();
  }

  void _handles(CanvasRenderingContext2D ctx, List<XY> pts, String color, {bool locked = false}) {
    _dash(ctx, const []);
    for (final p in pts) {
      ctx.beginPath();
      if (locked) {
        ctx.rect(p.x - 3, p.y - 3, 6, 6);
      } else {
        ctx.arc(p.x, p.y, 4.5, 0, math.pi * 2);
      }
      ctx.fillStyle = chart.theme.bg.toJS;
      ctx.fill();
      ctx.strokeStyle = color.toJS;
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
  }

  String _baseColor(Annotation a) => a.style['color'].s ?? (a.author == 'agent' ? agentColor : userColor);

  bool isLocked(Annotation a) =>
      a.style['locked'].b == true || lockedLayers.contains((a.layer == null || a.layer!.isEmpty) ? 'main' : a.layer);

  /// Editable points of a drawing: (index into points, pixel position). Horizontal lines expose a
  /// single handle near the right edge; positions expose entry, stop and target.
  List<(int, XY)> handles(Annotation a) {
    final pts = [for (final p in a.points) pt(p)];
    XY? at(int i) => i < pts.length ? pts[i] : null;
    switch (a.type) {
      case 'horizontal_line':
        final yy = y(a.points.isEmpty ? null : a.points[0].price);
        return yy == null ? const [] : [(0, XY(_w - 30, yy))];
      case 'horizontal_ray':
        final p = at(0);
        return p == null ? const [] : [(0, p)];
      case 'vertical_line':
        final xx = x(a.points.isEmpty ? null : a.points[0].time);
        return xx == null ? const [] : [(0, XY(xx, 24))];
      case 'long_position' || 'short_position':
        final e = at(0), s = at(1), t = at(2);
        if (e == null || s == null || t == null) return const [];
        final x2 = math.max(s.x, e.x + 30);
        return [(0, e), (1, XY(x2, s.y)), (2, XY(x2, t.y))];
      default:
        return [for (var i = 0; i < pts.length; i++) if (pts[i] != null) (i, pts[i]!)];
    }
  }

  /// The selected drawing's handle under (px, py): (annotation id, point index).
  (String, int)? hitHandle(double px, double py, {double tol = 7}) {
    final id = selectedId;
    if (id == null || hidden) return null;
    for (final a in annotations) {
      if (a.id != id || !a.visible) continue;
      for (final (i, p) in handles(previews[id] ?? a)) {
        if ((p.x - px).abs() <= tol && (p.y - py).abs() <= tol) return (id, i);
      }
    }
    return null;
  }

  /// Time for an x coordinate (interpolated/extrapolated, no snapping).
  int? timeAtX(double px) => mapper.time(chart.xToLogical(px));

  // ---------------------------------------------------------------- hit testing

  /// Id of the topmost visible annotation within [tol] px of ([px], [py]).
  String? hit(double px, double py, {double tol = 6}) {
    if (hidden) return null;
    final P = XY(px, py);
    final W = _w;
    for (final a in annotations.reversed) {
      if (!_shown(a)) continue;
      // a shown label (moved aside or revealed by hover) counts as part of its drawing
      if (_boxes[a.id]?.any((b) => b.contains(px, py)) == true) return a.id;
      final pts = [for (final p in a.points) pt(p)];
      XY? at(int i) => i < pts.length ? pts[i] : null;
      var d = double.infinity;
      switch (a.type) {
        case 'horizontal_line' || 'horizontal_ray':
          final yy = y(a.points.isEmpty ? null : a.points[0].price);
          if (yy != null) d = (yy - py).abs();
        case 'vertical_line':
          final xx = x(a.points.isEmpty ? null : a.points[0].time);
          if (xx != null) d = (xx - px).abs();
        case 'rectangle' || 'price_range' || 'fib_retracement':
          final p = at(0), q = at(1);
          if (p != null && q != null) {
            final right = math.max(math.max(p.x, q.x), a.type == 'fib_retracement' ? W : 0);
            final inside = px >= math.min(p.x, q.x) - tol &&
                px <= right + tol &&
                py >= math.min(p.y, q.y) - tol &&
                py <= math.max(p.y, q.y) + tol;
            d = inside ? 0 : double.infinity;
          }
        case 'long_position' || 'short_position':
          final e = at(0), s = at(1), t = at(2);
          if (e != null && s != null && t != null) {
            final inside = px >= e.x - tol &&
                px <= math.max(s.x, e.x + 30) + tol &&
                py >= math.min(s.y, t.y) - tol &&
                py <= math.max(s.y, t.y) + tol;
            d = inside ? 0 : double.infinity;
          }
        case 'text' || 'marker' || 'callout':
          final p = at(a.type == 'callout' ? 1 : 0);
          if (p != null) d = math.sqrt(math.pow(p.x - px, 2) + math.pow(p.y - py, 2)) - 10;
        default:
          final ok = pts.whereType<XY>().toList();
          for (var k = 0; k + 1 < ok.length; k++) {
            var s = ok[k], e = ok[k + 1];
            if (a.type == 'ray' || a.type == 'extended_line') e = lineAt(s, e, e.x >= s.x ? W : 0);
            if (a.type == 'extended_line') s = lineAt(ok[0], ok[1], ok[1].x >= ok[0].x ? 0 : W);
            d = math.min(d, segDist(P, s, e));
          }
          if (a.type == 'parallel_channel' && ok.length == 3) {
            final off = ok[2].y - lineAt(ok[0], ok[1], ok[2].x).y;
            d = math.min(d, segDist(P, XY(ok[0].x, ok[0].y + off), XY(ok[1].x, ok[1].y + off)));
          }
      }
      if (d <= tol) return a.id;
    }
    return null;
  }
}
