// Canvas chart engine: candles + volume, indicator series in stacked panes, autoscaled price axes,
// exchange-time axis, crosshair, and zoom/pan by wheel, trackpad, drag and pinch.
//
// Coordinates: "logical" is a fractional bar index (0 = first loaded bar). Pixel coordinates are
// CSS pixels relative to the chart's top-left; the main pane starts at (0, 0), so its pixel
// coordinates are also host coordinates (used by drawings and the text prompt).
import 'dart:js_interop';
import 'dart:js_interop_unsafe';
import 'dart:math' as math;

import 'package:web/web.dart';

import '../core/format.dart';
import '../core/models.dart';
import '../core/ticks.dart';
import '../core/time.dart';

const upColor = '#22c55e';
const downColor = '#ef4444';
const _font = 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';

enum SeriesKind { line, dots, histogram }

/// An indicator series aligned to the bars (values[i] belongs to bar i).
class Series {
  final int pane; // 0 = main pane (overlay), 1.. = indicator panes
  final SeriesKind kind;
  final List<double?> values;
  final String color;
  final double width;
  final bool dashed;
  final bool lastValueLabel;
  final List<double> levels; // dotted reference lines (e.g. RSI 30/70)
  Series({
    required this.pane,
    required this.kind,
    required this.values,
    this.color = '#94a3b8',
    this.width = 1,
    this.dashed = false,
    this.lastValueLabel = false,
    this.levels = const [],
  });

  double? lastValue() {
    for (var i = values.length - 1; i >= 0; i--) {
      final v = values[i];
      if (v != null) return v;
    }
    return null;
  }
}

class _Pane {
  double top = 0, height = 0;
  bool auto = true;
  double lo = 0, hi = 1; // value range shown (after margins) – recomputed when auto
  double step = 1;
  List<double> ticks = const [];
  double y(double v) => top + (hi - v) / (hi - lo) * height;
  double value(double y) => hi - (y - top) / height * (hi - lo);
}

class _Theme {
  String bg = '#0a1120', text = '#8a99b4', grid = 'rgba(148,163,184,0.07)', border = '#1d2840';
  String crosshair = 'rgba(148,163,184,0.55)', labelBg = '#334155';
}

class ChartEngine {
  final HTMLElement host;
  final HTMLCanvasElement canvas = document.createElement('canvas') as HTMLCanvasElement;
  late final CanvasRenderingContext2D _ctx = canvas.getContext('2d') as CanvasRenderingContext2D;
  final _theme = _Theme();

  // data
  List<Bar> bars = const [];
  List<Civil> _civil = const [];
  bool intraday = false;
  List<Series> series = [];
  int paneCount = 1;
  final List<_Pane> _panes = [_Pane()];

  // time scale
  double spacing = 7; // px per bar
  double rightOffset = 8; // empty bars to the right of the last bar
  (double, double)? _pendingRange;

  // layout (CSS px)
  double width = 0, height = 0;
  double axisWidth = 60;
  static const timeAxisHeight = 26.0;
  double get plotWidth => math.max(0, width - axisWidth);
  double get mainHeight => _panes[0].height;

  // pointer state
  double? _mouseX, _mouseY;
  final Map<int, (double, double)> _pointers = {};
  (double, double)? _downAt;
  bool _dragging = false;
  String _dragMode = 'pan'; // pan | price-axis | time-axis
  int _dragPane = 0;
  double _pinchDist = 0;
  bool _scheduled = false;

  /// Called on a click (not a drag) inside the main pane, with pane coordinates.
  void Function(double x, double y)? onClick;

  /// Called as the pointer moves over the plot area (null logical when it leaves).
  void Function(double? x, double? y, double? logical)? onCrosshair;

  /// Draws on top of the main pane (annotations). Receives the context and the pane size.
  void Function(CanvasRenderingContext2D ctx, double w, double h)? overlay;

  ChartEngine(this.host) {
    canvas.style
      ..display = 'block'
      ..width = '100%'
      ..height = '100%'
      ..setProperty('touch-action', 'pan-y');
    host.append(canvas);
    refreshTheme();
    ResizeObserver(((JSArray<JSObject> _, JSObject _) => _resize()).toJS).observe(host);
    _bindPointer();
    _resize();
  }

  // ---------------------------------------------------------------- public API

  void setBars(List<Bar> b, {required bool intraday}) {
    bars = b;
    this.intraday = intraday;
    _civil = [for (final x in b) civil(x.time, exchangeTime: intraday)];
    series = [];
    paneCount = 1;
    for (final p in _panes) {
      p.auto = true;
    }
    invalidate();
  }

  void setSeries(List<Series> s) {
    series = s;
    paneCount = 1 + s.fold<int>(0, (m, x) => math.max(m, x.pane));
    while (_panes.length < paneCount) {
      _panes.add(_Pane());
    }
    invalidate();
  }

  int get _n => bars.length;

  /// Shows logical bars [from]..[to] across the plot width.
  void setVisibleRange(double from, double to) {
    if (plotWidth <= 0 || _n == 0) {
      _pendingRange = (from, to);
      return;
    }
    _pendingRange = null;
    spacing = (plotWidth / math.max(1, to - from)).clamp(0.5, 60);
    rightOffset = to - _n + 0.5;
    _clampScroll();
    invalidate();
  }

  (double, double) get visibleRange => (xToLogical(0), xToLogical(plotWidth));

  /// Ensures at least [bars] empty bars to the right when the view is at the live edge, so
  /// drawings that extend into the future stay visible.
  void ensureRightSpace(double bars) {
    final atEdge = xToLogical(plotWidth) >= _n - 1;
    if (atEdge && rightOffset < bars) {
      rightOffset = bars;
      _clampScroll();
      invalidate();
    }
  }

  double logicalToX(double l) => plotWidth + (l - (_n - 1) - rightOffset) * spacing - spacing * 0.5;
  double xToLogical(double x) => (x - plotWidth + spacing * 0.5) / spacing + (_n - 1) + rightOffset;
  double? priceToY(double? p) => p == null || mainHeight <= 0 ? null : _panes[0].y(p);
  double? yToPrice(double y) => mainHeight <= 0 ? null : _panes[0].value(y);

  set cursor(String c) => canvas.style.cursor = c;

  void refreshTheme() {
    final s = window.getComputedStyle(document.documentElement!);
    String v(String name, String fallback) {
      final x = s.getPropertyValue(name).trim();
      return x.isEmpty ? fallback : x;
    }

    _theme
      ..bg = v('--chart-bg', _theme.bg)
      ..text = v('--text-muted', _theme.text)
      ..grid = v('--chart-grid', _theme.grid)
      ..border = v('--border', _theme.border);
    invalidate();
  }

  void invalidate() {
    if (_scheduled) return;
    _scheduled = true;
    window.requestAnimationFrame(((double _) {
      _scheduled = false;
      _draw();
    }).toJS);
  }

  /// Draws synchronously (tests and screenshots).
  void drawNow() => _draw();

  // ---------------------------------------------------------------- layout

  void _resize() {
    final r = host.getBoundingClientRect();
    width = r.width;
    height = r.height;
    final dpr = window.devicePixelRatio;
    canvas.width = (width * dpr).round();
    canvas.height = (height * dpr).round();
    final pending = _pendingRange;
    if (pending != null && plotWidth > 0 && _n > 0) setVisibleRange(pending.$1, pending.$2);
    _draw();
  }

  void _layout() {
    final total = math.max(0.0, height - timeAxisHeight);
    final subs = paneCount - 1;
    final subH = subs == 0 ? 0.0 : math.min(110.0, total * 0.45 / subs);
    final main = total - subs * subH;
    _panes[0]
      ..top = 0
      ..height = main;
    for (var i = 1; i < paneCount; i++) {
      _panes[i]
        ..top = main + (i - 1) * subH
        ..height = subH;
    }
  }

  (int, int) _visibleBars() {
    final (a, b) = visibleRange;
    return (math.max(0, a.floor()), math.min(_n - 1, b.ceil()));
  }

  void _clampScroll() {
    if (_n == 0 || plotWidth <= 0) return;
    final visibleBars = plotWidth / spacing;
    // keep at least 3 bars on screen on either side
    rightOffset = rightOffset.clamp(3.0 - _n - 0.5, visibleBars - 3.0);
  }

  void _autoscale(int pane, int from, int to) {
    final p = _panes[pane];
    if (!p.auto) return;
    var lo = double.infinity, hi = double.negativeInfinity;
    void take(double? v) {
      if (v == null || !v.isFinite) return;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }

    if (pane == 0) {
      for (var i = from; i <= to; i++) {
        take(bars[i].low);
        take(bars[i].high);
      }
    }
    for (final s in series) {
      if (s.pane != pane) continue;
      for (var i = from; i <= to && i < s.values.length; i++) {
        take(s.values[i]);
      }
      if (s.kind == SeriesKind.histogram) take(0);
      if (pane > 0) s.levels.forEach(take);
    }
    if (!lo.isFinite) {
      lo = 0;
      hi = 1;
    }
    if (hi - lo < 1e-9) {
      final pad = lo.abs() * 0.01 + 1e-6;
      lo -= pad;
      hi += pad;
    }
    final (top, bottom) = pane == 0 ? (0.1, 0.16) : (0.1, 0.08);
    final span = (hi - lo) / (1 - top - bottom);
    p.hi = hi + span * top;
    p.lo = lo - span * bottom;
  }

  void _computeTicks(int pane) {
    final p = _panes[pane];
    final t = niceTicks(p.lo, p.hi, math.max(2, (p.height / 28).floor()));
    p.step = t.step;
    p.ticks = t.values;
  }

  // ---------------------------------------------------------------- drawing

  void _draw() {
    final ctx = _ctx;
    final dpr = window.devicePixelRatio;
    ctx.setTransform(dpr.toJS, 0, 0, dpr, 0, 0);
    ctx.fillStyle = _theme.bg.toJS;
    ctx.fillRect(0, 0, width, height);
    if (width <= 0 || height <= 0) return;
    _layout();
    if (_n == 0) return;

    final (from, to) = _visibleBars();
    for (var i = 0; i < paneCount; i++) {
      _autoscale(i, from, math.max(from, to));
      _computeTicks(i);
    }
    // price axis wide enough for its labels
    ctx.font = '11px $_font';
    var widest = 0.0;
    for (var i = 0; i < paneCount; i++) {
      final p = _panes[i];
      for (final v in [p.lo, p.hi]) {
        widest = math.max(widest, ctx.measureText(axisPrice(v, p.step)).width);
      }
    }
    final wantAxis = (widest + 18).clamp(52.0, 110.0);
    if ((wantAxis - axisWidth).abs() > 4) axisWidth = wantAxis;

    final ticks = timeTicks(_civil, from, to, math.max(1, 76 / spacing), intraday);
    for (var i = 0; i < paneCount; i++) {
      _drawPane(ctx, i, from, to, ticks);
    }
    _drawPriceAxes(ctx);
    _drawTimeAxis(ctx, ticks);
    _drawCrosshair(ctx);
  }

  void _dash(CanvasRenderingContext2D ctx, List<num> d) => ctx.setLineDash([for (final v in d) v.toJS].toJS);

  void _drawPane(CanvasRenderingContext2D ctx, int index, int from, int to, List<TimeTick> ticks) {
    final p = _panes[index];
    final w = plotWidth;
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, p.top, w, p.height);
    ctx.clip();

    // grid
    ctx.strokeStyle = _theme.grid.toJS;
    ctx.lineWidth = 1;
    _dash(ctx, const []);
    ctx.beginPath();
    for (final v in p.ticks) {
      final y = _crisp(p.y(v));
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
    }
    for (final t in ticks) {
      final x = _crisp(logicalToX(t.index.toDouble()));
      ctx.moveTo(x, p.top);
      ctx.lineTo(x, p.top + p.height);
    }
    ctx.stroke();

    if (index == 0) {
      _drawVolume(ctx, from, to);
      _drawCandles(ctx, from, to);
    }
    for (final s in series) {
      if (s.pane != index) continue;
      for (final lv in s.levels) {
        ctx.strokeStyle = 'rgba(148,163,184,0.5)'.toJS;
        ctx.lineWidth = 1;
        _dash(ctx, const [2, 3]);
        final y = _crisp(p.y(lv));
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(w, y);
        ctx.stroke();
      }
      switch (s.kind) {
        case SeriesKind.histogram:
          _drawHistogram(ctx, s, p, from, to);
        case SeriesKind.line:
          _drawLine(ctx, s, p, from, to);
        case SeriesKind.dots:
          _drawDots(ctx, s, p, from, to);
      }
    }
    if (index == 0) {
      // last price line
      final last = bars.last;
      final up = _n < 2 || last.close >= bars[_n - 2].close;
      ctx.strokeStyle = (up ? upColor : downColor).toJS;
      ctx.lineWidth = 1;
      _dash(ctx, const [1, 3]);
      final y = _crisp(p.y(last.close));
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
      ctx.stroke();
      _dash(ctx, const []);
      final o = overlay;
      if (o != null) {
        ctx.save();
        o(ctx, w, p.height);
        ctx.restore();
      }
    }
    ctx.restore();

    // separator above indicator panes
    if (index > 0) {
      ctx.fillStyle = _theme.border.toJS;
      ctx.fillRect(0, p.top, width, 1);
    }
  }

  double _crisp(double v) => v.floorToDouble() + 0.5;

  void _drawCandles(CanvasRenderingContext2D ctx, int from, int to) {
    final p = _panes[0];
    final body = math.max(1.0, (spacing * 0.72).floorToDouble());
    for (final up in [true, false]) {
      ctx.fillStyle = (up ? upColor : downColor).toJS;
      ctx.beginPath();
      for (var i = from; i <= to; i++) {
        final b = bars[i];
        if ((b.close >= b.open) != up) continue;
        final x = logicalToX(i.toDouble()).roundToDouble();
        final yh = p.y(b.high), yl = p.y(b.low);
        ctx.rect(x - 0.5, yh, 1, math.max(1, yl - yh)); // wick
        if (body > 2) {
          final yo = p.y(b.open), yc = p.y(b.close);
          final top = math.min(yo, yc);
          ctx.rect(x - (body / 2).floorToDouble(), top, body, math.max(1, (yo - yc).abs()));
        }
      }
      ctx.fill();
    }
  }

  void _drawVolume(CanvasRenderingContext2D ctx, int from, int to) {
    final p = _panes[0];
    var maxVol = 0.0;
    for (var i = from; i <= to; i++) {
      maxVol = math.max(maxVol, bars[i].volume);
    }
    if (maxVol <= 0) return;
    final areaH = p.height * 0.18;
    final base = p.top + p.height;
    final w = math.max(1.0, (spacing * 0.8).floorToDouble());
    for (final up in [true, false]) {
      ctx.fillStyle = (up ? 'rgba(34,197,94,0.35)' : 'rgba(239,68,68,0.35)').toJS;
      ctx.beginPath();
      for (var i = from; i <= to; i++) {
        final b = bars[i];
        if ((b.close >= b.open) != up) continue;
        final x = logicalToX(i.toDouble()).roundToDouble();
        final h = b.volume / maxVol * areaH;
        ctx.rect(x - (w / 2).floorToDouble(), base - h, w, h);
      }
      ctx.fill();
    }
  }

  void _drawLine(CanvasRenderingContext2D ctx, Series s, _Pane p, int from, int to) {
    ctx.strokeStyle = s.color.toJS;
    ctx.lineWidth = s.width;
    ctx.lineJoin = 'round';
    _dash(ctx, s.dashed ? const [6, 4] : const []);
    ctx.beginPath();
    var pen = false;
    final lo = math.max(0, from - 1), hi = math.min(s.values.length - 1, to + 1);
    for (var i = lo; i <= hi; i++) {
      final v = s.values[i];
      if (v == null) {
        pen = false;
        continue;
      }
      final x = logicalToX(i.toDouble()), y = p.y(v);
      if (pen) {
        ctx.lineTo(x, y);
      } else {
        ctx.moveTo(x, y);
        pen = true;
      }
    }
    ctx.stroke();
    _dash(ctx, const []);
  }

  void _drawDots(CanvasRenderingContext2D ctx, Series s, _Pane p, int from, int to) {
    ctx.fillStyle = s.color.toJS;
    ctx.beginPath();
    for (var i = from; i <= to && i < s.values.length; i++) {
      final v = s.values[i];
      if (v == null) continue;
      final x = logicalToX(i.toDouble()), y = p.y(v);
      ctx.moveTo(x + 1.5, y);
      ctx.arc(x, y, 1.5, 0, math.pi * 2);
    }
    ctx.fill();
  }

  void _drawHistogram(CanvasRenderingContext2D ctx, Series s, _Pane p, int from, int to) {
    final base = p.y(0);
    final w = math.max(1.0, (spacing * 0.6).floorToDouble());
    for (final pos in [true, false]) {
      ctx.fillStyle = (pos ? 'rgba(34,197,94,0.6)' : 'rgba(239,68,68,0.6)').toJS;
      ctx.beginPath();
      for (var i = from; i <= to && i < s.values.length; i++) {
        final v = s.values[i];
        if (v == null || (v >= 0) != pos) continue;
        final x = logicalToX(i.toDouble()).roundToDouble();
        final y = p.y(v);
        ctx.rect(x - (w / 2).floorToDouble(), math.min(y, base), w, math.max(1, (y - base).abs()));
      }
      ctx.fill();
    }
  }

  void _axisLabel(CanvasRenderingContext2D ctx, String text, double y, String bg, String fg) {
    final x0 = plotWidth;
    final h = 18.0;
    final top = (y - h / 2).clamp(0.0, height - timeAxisHeight - h);
    ctx.fillStyle = bg.toJS;
    ctx.beginPath();
    ctx.roundRect(x0 + 1, top, axisWidth - 2, h, 3.toJS);
    ctx.fill();
    ctx.fillStyle = fg.toJS;
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    ctx.fillText(text, x0 + 7, top + h / 2 + 0.5);
  }

  void _drawPriceAxes(CanvasRenderingContext2D ctx) {
    final x0 = plotWidth;
    ctx.fillStyle = _theme.bg.toJS;
    ctx.fillRect(x0, 0, axisWidth, height);
    ctx.fillStyle = _theme.border.toJS;
    ctx.fillRect(x0, 0, 1, height - timeAxisHeight);
    ctx.font = '11px $_font';
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    for (var i = 0; i < paneCount; i++) {
      final p = _panes[i];
      ctx.fillStyle = _theme.text.toJS;
      for (final v in p.ticks) {
        final y = p.y(v);
        if (y < p.top + 8 || y > p.top + p.height - 8) continue;
        ctx.fillText(axisPrice(v, p.step), x0 + 7, y);
      }
    }
    // last values: close for the main pane, latest value for indicator-pane lines
    final last = bars.last;
    final up = _n < 2 || last.close >= bars[_n - 2].close;
    _axisLabel(ctx, axisPrice(last.close, _panes[0].step), _panes[0].y(last.close), up ? upColor : downColor, '#fff');
    for (final s in series) {
      if (s.pane == 0 || !s.lastValueLabel) continue;
      final v = s.lastValue();
      if (v == null) continue;
      final p = _panes[s.pane];
      final y = p.y(v);
      if (y >= p.top && y <= p.top + p.height) _axisLabel(ctx, axisPrice(v, p.step), y, s.color, '#0b1220');
    }
  }

  void _drawTimeAxis(CanvasRenderingContext2D ctx, List<TimeTick> ticks) {
    final top = height - timeAxisHeight;
    ctx.fillStyle = _theme.bg.toJS;
    ctx.fillRect(0, top, width, timeAxisHeight);
    ctx.fillStyle = _theme.border.toJS;
    ctx.fillRect(0, top, width, 1);
    ctx.font = '11px $_font';
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'center';
    for (final t in ticks) {
      final x = logicalToX(t.index.toDouble());
      if (x < 12 || x > plotWidth - 12) continue;
      final strong = t.weight == TickWeight.year || (intraday && t.weight == TickWeight.day);
      ctx.font = '${strong ? '600 ' : ''}11px $_font';
      ctx.fillStyle = _theme.text.toJS;
      ctx.fillText(t.label, x, top + timeAxisHeight / 2);
    }
  }

  void _drawCrosshair(CanvasRenderingContext2D ctx) {
    final mx = _mouseX, my = _mouseY;
    if (mx == null || my == null || mx > plotWidth || my > height - timeAxisHeight || _dragging) return;
    final idx = xToLogical(mx).round().clamp(0, _n - 1);
    final x = _crisp(logicalToX(idx.toDouble()));
    ctx.strokeStyle = _theme.crosshair.toJS;
    ctx.lineWidth = 1;
    _dash(ctx, const [4, 4]);
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, height - timeAxisHeight);
    ctx.moveTo(0, _crisp(my));
    ctx.lineTo(plotWidth, _crisp(my));
    ctx.stroke();
    _dash(ctx, const []);
    final pane = _paneAt(my);
    if (pane != null) {
      final p = _panes[pane];
      _axisLabel(ctx, axisPrice(p.value(my), p.step / 10), my, _theme.labelBg, '#fff');
    }
    // time label
    final label = formatBarTime(bars[idx].time, intraday);
    ctx.font = '11px $_font';
    final w = ctx.measureText(label).width + 14;
    final left = (x - w / 2).clamp(0.0, plotWidth - w);
    final top = height - timeAxisHeight + 3;
    ctx.fillStyle = _theme.labelBg.toJS;
    ctx.beginPath();
    ctx.roundRect(left, top, w, timeAxisHeight - 6, 3.toJS);
    ctx.fill();
    ctx.fillStyle = '#fff'.toJS;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(label, left + w / 2, top + (timeAxisHeight - 6) / 2 + 0.5);
  }

  int? _paneAt(double y) {
    for (var i = 0; i < paneCount; i++) {
      final p = _panes[i];
      if (y >= p.top && y < p.top + p.height) return i;
    }
    return null;
  }

  // ---------------------------------------------------------------- interaction

  (double, double) _local(MouseEvent e) {
    final r = canvas.getBoundingClientRect();
    // read as doubles: package:web types clientX/Y as int but browsers report fractional pixels
    final o = e as JSObject;
    return ((o['clientX'] as JSNumber).toDartDouble - r.left, (o['clientY'] as JSNumber).toDartDouble - r.top);
  }

  void _bindPointer() {
    canvas.addEventListener('pointerdown', ((PointerEvent e) {
      final (x, y) = _local(e);
      _pointers[e.pointerId] = (x, y);
      if (_pointers.length == 2) {
        final [a, b] = _pointers.values.toList();
        _pinchDist = math.sqrt(math.pow(a.$1 - b.$1, 2) + math.pow(a.$2 - b.$2, 2));
        _dragging = true;
        return;
      }
      _downAt = (x, y);
      _dragging = false;
      _dragMode = x > plotWidth ? 'price-axis' : y > height - timeAxisHeight ? 'time-axis' : 'pan';
      _dragPane = _paneAt(y) ?? 0;
      canvas.setPointerCapture(e.pointerId);
    }).toJS);

    canvas.addEventListener('pointermove', ((PointerEvent e) {
      final (x, y) = _local(e);
      final prev = _pointers[e.pointerId];
      if (prev != null) _pointers[e.pointerId] = (x, y);
      if (_pointers.length == 2 && prev != null) {
        final [a, b] = _pointers.values.toList();
        final d = math.sqrt(math.pow(a.$1 - b.$1, 2) + math.pow(a.$2 - b.$2, 2));
        if (_pinchDist > 0 && d > 0) _zoomAt((a.$1 + b.$1) / 2, d / _pinchDist);
        _pinchDist = d;
        return;
      }
      final down = _downAt;
      if (down != null && prev != null) {
        if (!_dragging && ((x - down.$1).abs() > 4 || (y - down.$2).abs() > 4)) _dragging = true;
        if (_dragging) {
          final dx = x - prev.$1, dy = y - prev.$2;
          switch (_dragMode) {
            case 'price-axis':
              _scalePrice(_dragPane, dy);
            case 'time-axis':
              _zoomAt(plotWidth, math.exp(-dx * 0.006));
            default:
              rightOffset -= dx / spacing;
              _clampScroll();
              final p = _panes[_dragPane];
              if (!p.auto) {
                final shift = dy / p.height * (p.hi - p.lo);
                p.hi += shift;
                p.lo += shift;
              }
          }
          invalidate();
        }
      }
      _hover(x, y);
    }).toJS);

    void up(PointerEvent e) {
      final wasPointer = _pointers.remove(e.pointerId) != null;
      if (_pointers.isNotEmpty) return;
      final down = _downAt;
      _downAt = null;
      final dragged = _dragging;
      _dragging = false;
      if (wasPointer && !dragged && down != null && e.type == 'pointerup') {
        final (x, y) = _local(e);
        if (x <= plotWidth && y < mainHeight) onClick?.call(x, y);
      }
      invalidate();
    }

    canvas.addEventListener('pointerup', up.toJS);
    canvas.addEventListener('pointercancel', up.toJS);
    canvas.addEventListener('pointerleave', ((PointerEvent e) {
      if (_pointers.isNotEmpty) return;
      _mouseX = _mouseY = null;
      onCrosshair?.call(null, null, null);
      invalidate();
    }).toJS);

    canvas.addEventListener(
        'wheel',
        ((WheelEvent e) {
          if (_n == 0) return;
          e.preventDefault();
          final unit = e.deltaMode == 1 ? 16.0 : e.deltaMode == 2 ? 400.0 : 1.0;
          final dx = e.deltaX * unit, dy = e.deltaY * unit;
          final (x, _) = _local(e);
          if (dx.abs() > dy.abs()) {
            rightOffset += dx / spacing;
            _clampScroll();
            invalidate();
          } else {
            // ctrlKey is set for trackpad pinch gestures, whose deltas are much smaller
            _zoomAt(math.min(x, plotWidth), math.exp(-dy * (e.ctrlKey ? 0.01 : 0.0015)));
          }
        }).toJS,
        AddEventListenerOptions(passive: false));

    canvas.addEventListener('dblclick', ((MouseEvent e) {
      final (x, y) = _local(e);
      if (x > plotWidth) {
        final p = _paneAt(y);
        if (p != null) _panes[p].auto = true;
        invalidate();
      }
    }).toJS);
  }

  void _hover(double x, double y) {
    _mouseX = x;
    _mouseY = y;
    if (x > plotWidth) {
      cursor = 'ns-resize';
    } else if (y > height - timeAxisHeight) {
      cursor = 'ew-resize';
    }
    final inPlot = x <= plotWidth && y <= height - timeAxisHeight;
    onCrosshair?.call(inPlot ? x : null, inPlot ? y : null, inPlot ? xToLogical(x) : null);
    invalidate();
  }

  void _zoomAt(double x, double factor) {
    final anchor = xToLogical(x);
    spacing = (spacing * factor).clamp(0.5, 60);
    rightOffset = anchor - (x - plotWidth + spacing * 0.5) / spacing - (_n - 1);
    _clampScroll();
    invalidate();
  }

  void _scalePrice(int pane, double dy) {
    final p = _panes[pane];
    p.auto = false;
    final mid = (p.hi + p.lo) / 2;
    final half = (p.hi - p.lo) / 2 * math.exp(dy * 0.005);
    p.hi = mid + half;
    p.lo = mid - half;
  }
}
