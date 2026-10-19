// Canvas chart engine: price series (candles, hollow candles, OHLC bars, line, area, Heikin-Ashi)
// with volume, compare overlays, indicator series in resizable panes, linear/log/percent price
// scales, exchange-time axis, crosshair (with cross-chart sync), price lines, event markers, and
// zoom/pan by wheel, trackpad, drag, pinch and keyboard. Drawing tools hook in through the
// pointer-delegation callbacks and the main-pane overlay.
//
// Coordinates: "logical" is a fractional bar index (0 = first loaded bar). Pixel coordinates are
// CSS pixels relative to the chart's top-left; the main pane starts at (0, 0), so its pixel
// coordinates are also host coordinates.
import 'dart:js_interop';
import 'dart:js_interop_unsafe';
import 'dart:math' as math;

import 'package:web/web.dart';

import '../core/chartmath.dart';
import '../core/format.dart';
import '../core/models.dart';
import '../core/ticks.dart';
import '../core/time.dart';

const _font = 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';

enum SeriesKind { line, dots, histogram, volume, area, candles }

enum ChartType { candles, hollow, bars, line, area, heikin }

ChartType chartTypeFromName(String? n) =>
    ChartType.values.firstWhere((t) => t.name == n, orElse: () => ChartType.candles);

/// An indicator series aligned to the bars (values[i] belongs to bar i).
class Series {
  final String id;
  final int pane; // 0 = main pane (overlay), 1.. = indicator panes
  final SeriesKind kind;
  final List<double?> values;
  final String color;
  final double width;
  final bool dashed;
  final bool lastValueLabel;
  final List<double> levels; // dotted reference lines (e.g. RSI 30/70)
  final List<Bar?>? ohlc; // for candles (another symbol's bars aligned to ours)
  bool visible;
  Series({
    this.id = '',
    required this.pane,
    required this.kind,
    required this.values,
    this.color = '#94a3b8',
    this.width = 1,
    this.dashed = false,
    this.lastValueLabel = false,
    this.levels = const [],
    this.ohlc,
    this.visible = true,
  });

  double? lastValue() {
    for (var i = values.length - 1; i >= 0; i--) {
      final v = values[i];
      if (v != null) return v;
    }
    return null;
  }
}

/// A comparison symbol drawn as a line on the main pane (values aligned to the bars).
class CompareSeries {
  final String symbol;
  final String color;
  final List<double?> closes;
  CompareSeries(this.symbol, this.color, this.closes);
}

/// A horizontal line across the main pane with an axis label (alerts, positions).
class PriceLine {
  final double price;
  final String color;
  final String label;
  final bool dashed;
  const PriceLine(this.price, this.color, this.label, {this.dashed = true});
}

/// An event marker at the bottom of the main pane (earnings, dividends, splits).
class EventMarker {
  final int time;
  final String letter;
  final String color;
  final String tooltip;
  const EventMarker(this.time, this.letter, this.color, this.tooltip);
}

class PaneGeom {
  final double top, height;
  const PaneGeom(this.top, this.height);
}

class _Pane {
  double top = 0, height = 0;
  double weight = 1; // indicator panes share space by weight
  bool auto = true;
  double lo = 0, hi = 1; // axis range in transformed units
  double step = 1;
  List<double> ticks = const [];
  PriceTransform tf = const PriceTransform(ScaleMode.linear);
  double y(double price) => top + (hi - tf.to(price)) / (hi - lo) * height;
  double value(double y) => tf.from(hi - (y - top) / height * (hi - lo));
}

class ChartTheme {
  String bg = '#0a1120', text = '#8a99b4', grid = 'rgba(148,163,184,0.07)', border = '#1d2840';
  String crosshair = 'rgba(148,163,184,0.6)', labelBg = '#363f52';
  String up = '#22c55e', down = '#ef4444';
  String volUp = 'rgba(34,197,94,0.32)', volDown = 'rgba(239,68,68,0.32)';
  String line = '#38bdf8';
}

class ChartEngine {
  final HTMLElement host;
  final HTMLCanvasElement canvas = document.createElement('canvas') as HTMLCanvasElement;
  late final CanvasRenderingContext2D _ctx = canvas.getContext('2d') as CanvasRenderingContext2D;
  final theme = ChartTheme();

  // data
  List<Bar> rawBars = const [];
  List<Bar> bars = const []; // displayed (Heikin-Ashi transformed when chosen)
  List<Civil> _civil = const [];
  bool intraday = false;
  List<Series> series = [];
  List<CompareSeries> compares = [];
  List<PriceLine> priceLines = [];
  List<EventMarker> markers = [];
  ChartType chartType = ChartType.candles;
  ScaleMode scaleMode = ScaleMode.linear;
  bool showVolume = true; // volume overlaid at the bottom of the main pane
  bool showPrice = true;
  bool showLastPriceLine = true;
  bool showGrid = true;
  String? upColorOverride, downColorOverride;
  String? lastLabelExtra; // e.g. bar countdown, shown under the last price
  int paneCount = 1;
  final List<_Pane> _panes = [_Pane()];
  double? _subShare; // fraction of the height used by indicator panes (null = automatic)

  // time scale
  double spacing = 7;
  double rightOffset = 8;
  (double, double)? _pendingRange;

  // layout (CSS px)
  double width = 0, height = 0;
  double axisWidth = 62;
  static const timeAxisHeight = 26.0;
  double get plotWidth => math.max(0, width - axisWidth);
  double get mainHeight => _panes[0].height;

  // pointer state
  double? _mouseX, _mouseY;
  int? _syncTime; // crosshair time pushed from another chart
  final Map<int, (double, double)> _pointers = {};
  (double, double)? _downAt;
  bool _dragging = false;
  bool _delegated = false;
  String _dragMode = 'pan'; // pan | price-axis | time-axis | separator
  int _dragPane = 0;
  double _pinchDist = 0;
  bool _scheduled = false;

  /// Click (not drag) inside the main pane, in pane coordinates.
  void Function(double x, double y, MouseEvent e)? onClick;
  void Function(double x, double y)? onDoubleClick;

  /// Right click anywhere on the chart; `price` is set when over the main pane.
  void Function(double x, double y, double? price, MouseEvent e)? onContextMenu;

  /// Pointer moves over the plot area (null logical when it leaves).
  void Function(double? x, double? y, double? logical)? onCrosshair;

  /// Return true to take over a pointer drag starting at (x, y) in the main pane (drawing edits).
  bool Function(double x, double y, PointerEvent e)? onPointerDown;
  void Function(double x, double y, PointerEvent e)? onPointerDrag;
  void Function(double x, double y, PointerEvent e)? onPointerUp;

  /// Visible logical range changed by the user (zoom/pan), for range sync.
  void Function(double from, double to)? onRangeChanged;

  /// Draws on top of the main pane (drawings). Receives the context and the pane size.
  void Function(CanvasRenderingContext2D ctx, double w, double h)? overlay;

  /// Called after each frame (DOM legends reposition themselves from [paneGeometry]).
  void Function()? afterDraw;

  ChartEngine(this.host) {
    canvas.style
      ..display = 'block'
      ..width = '100%'
      ..height = '100%'
      ..setProperty('touch-action', 'pan-y');
    canvas.tabIndex = 0;
    host.append(canvas);
    refreshTheme();
    ResizeObserver(((JSArray<JSObject> _, JSObject _) => _resize()).toJS).observe(host);
    _bindPointer();
    _resize();
  }

  // ---------------------------------------------------------------- data API

  void setBars(List<Bar> b, {required bool intraday, bool keepView = false}) {
    rawBars = b;
    this.intraday = intraday;
    bars = chartType == ChartType.heikin ? heikinAshi(b) : b;
    _civil = [for (final x in b) civil(x.time, exchangeTime: intraday)];
    if (!keepView) {
      series = [];
      compares = [];
      paneCount = 1;
      for (final p in _panes) {
        p.auto = true;
      }
    }
    invalidate();
  }

  void setChartType(ChartType t) {
    chartType = t;
    bars = t == ChartType.heikin ? heikinAshi(rawBars) : rawBars;
    invalidate();
  }

  void setScaleMode(ScaleMode m) {
    scaleMode = m;
    _panes[0].auto = true;
    invalidate();
  }

  void setSeries(List<Series> s) {
    series = s;
    final want = 1 + s.fold<int>(0, (m, x) => math.max(m, x.pane));
    while (_panes.length < want) {
      _panes.add(_Pane());
    }
    paneCount = want;
    invalidate();
  }

  void setCompares(List<CompareSeries> c) {
    compares = c;
    _panes[0].auto = true;
    invalidate();
  }

  int get _n => bars.length;

  // ---------------------------------------------------------------- view API

  void setVisibleRange(double from, double to, {bool notify = false}) {
    if (plotWidth <= 0 || _n == 0) {
      _pendingRange = (from, to);
      return;
    }
    _pendingRange = null;
    spacing = (plotWidth / math.max(1, to - from)).clamp(0.5, 60);
    rightOffset = to - _n + 0.5;
    _clampScroll();
    invalidate();
    if (notify) _rangeChanged();
  }

  (double, double) get visibleRange => (xToLogical(0), xToLogical(plotWidth));

  /// Ensures at least [bars] empty bars to the right when the view is at the live edge.
  void ensureRightSpace(double bars) {
    final atEdge = xToLogical(plotWidth) >= _n - 1;
    if (atEdge && rightOffset < bars) {
      rightOffset = bars;
      _clampScroll();
      invalidate();
    }
  }

  /// True when the latest bar is off-screen to the right (show a "scroll to latest" button).
  bool get scrolledBack => _n > 0 && xToLogical(plotWidth) < _n - 1.5;

  void scrollToRealtime({double right = 8}) {
    rightOffset = right;
    _clampScroll();
    invalidate();
    _rangeChanged();
  }

  void scrollBars(double n) {
    rightOffset -= n;
    _clampScroll();
    invalidate();
    _rangeChanged();
  }

  void zoom(double factor, {double? atX}) => _zoomAt(atX ?? plotWidth, factor);

  void resetPriceScales() {
    for (final p in _panes) {
      p.auto = true;
    }
    invalidate();
  }

  double logicalToX(double l) => plotWidth + (l - (_n - 1) - rightOffset) * spacing - spacing * 0.5;
  double xToLogical(double x) => (x - plotWidth + spacing * 0.5) / spacing + (_n - 1) + rightOffset;
  double? priceToY(double? p) => p == null || mainHeight <= 0 ? null : _panes[0].y(p);
  double? yToPrice(double y) => mainHeight <= 0 ? null : _panes[0].value(y);

  /// Pane geometry for DOM overlays (legends).
  List<PaneGeom> get paneGeometry => [for (var i = 0; i < paneCount; i++) PaneGeom(_panes[i].top, _panes[i].height)];

  /// Bar index under a logical position, clamped to the data.
  int barIndexAt(double logical) => logical.round().clamp(0, math.max(0, _n - 1));

  set cursor(String c) => canvas.style.cursor = c;

  /// Crosshair driven by another chart (time sync). null hides it.
  void setSyncedCrosshair(int? time) {
    if (time == _syncTime) return;
    _syncTime = time;
    invalidate();
  }

  void refreshTheme() {
    final s = window.getComputedStyle(document.documentElement!);
    String v(String name, String fallback) {
      final x = s.getPropertyValue(name).trim();
      return x.isEmpty ? fallback : x;
    }

    theme
      ..bg = v('--chart-bg', theme.bg)
      ..text = v('--text-muted', theme.text)
      ..grid = v('--chart-grid', theme.grid)
      ..border = v('--border', theme.border)
      ..labelBg = v('--chart-label', theme.labelBg)
      ..crosshair = v('--chart-crosshair', theme.crosshair)
      ..up = v('--up', theme.up)
      ..down = v('--down', theme.down)
      ..line = v('--accent', theme.line);
    if (upColorOverride != null) theme.up = upColorOverride!;
    if (downColorOverride != null) theme.down = downColorOverride!;
    theme
      ..volUp = _alpha(theme.up, 0.32)
      ..volDown = _alpha(theme.down, 0.32);
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

  void drawNow() => _draw();

  /// PNG data URL of the current chart (for screenshots).
  String snapshot() {
    _draw();
    return canvas.toDataURL('image/png');
  }

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
    // indicator panes take up to ~45% of the height, split by their weights
    final subTotal = subs == 0
        ? 0.0
        : _subShare != null
            ? total * _subShare!
            : math.min(subs * 110.0, total * 0.45);
    final weights = [for (var i = 1; i < paneCount; i++) _panes[i].weight];
    final wsum = weights.fold<double>(0, (a, b) => a + b);
    final main = total - subTotal;
    _panes[0]
      ..top = 0
      ..height = main;
    var top = main;
    for (var i = 1; i < paneCount; i++) {
      final h = subTotal * _panes[i].weight / wsum;
      _panes[i]
        ..top = top
        ..height = h;
      top += h;
    }
  }

  (int, int) _visibleBars() {
    final (a, b) = visibleRange;
    return (math.max(0, a.floor()), math.min(_n - 1, b.ceil()));
  }

  void _clampScroll() {
    if (_n == 0 || plotWidth <= 0) return;
    final visibleBars = plotWidth / spacing;
    rightOffset = rightOffset.clamp(3.0 - _n - 0.5, visibleBars - 3.0);
  }

  void _rangeChanged() {
    final cb = onRangeChanged;
    if (cb != null && _n > 0) {
      final (a, b) = visibleRange;
      cb(a, b);
    }
  }

  double _firstVisibleClose(int from, int to) {
    for (var i = from; i <= to; i++) {
      final c = bars[i].close;
      if (c > 0) return c;
    }
    return bars.isEmpty ? 1 : bars.last.close;
  }

  void _autoscale(int pane, int from, int to) {
    final p = _panes[pane];
    if (pane == 0) {
      final base = scaleMode == ScaleMode.percent ? _firstVisibleClose(from, to) : 1.0;
      p.tf = PriceTransform(scaleMode, base);
    } else {
      p.tf = const PriceTransform(ScaleMode.linear);
    }
    if (!p.auto) return;
    var lo = double.infinity, hi = double.negativeInfinity;
    void take(double? v) {
      if (v == null || !v.isFinite) return;
      final t = p.tf.to(v);
      if (!t.isFinite) return;
      if (t < lo) lo = t;
      if (t > hi) hi = t;
    }

    if (pane == 0 && showPrice) {
      final lineOnly = chartType == ChartType.line || chartType == ChartType.area;
      for (var i = from; i <= to; i++) {
        if (lineOnly) {
          take(bars[i].close);
        } else {
          take(bars[i].low);
          take(bars[i].high);
        }
      }
      // compares are scaled to the main series' first visible close (percent-like overlay)
      for (final c in compares) {
        final base = _compareBase(c, from, to);
        if (base == null) continue;
        final mainBase = _firstVisibleClose(from, to);
        for (var i = from; i <= to && i < c.closes.length; i++) {
          final v = c.closes[i];
          if (v != null) take(mainBase * v / base);
        }
      }
    }
    for (final s in series) {
      if (s.pane != pane || !s.visible) continue;
      if (s.kind == SeriesKind.candles && s.ohlc != null) {
        for (var i = from; i <= to && i < s.ohlc!.length; i++) {
          take(s.ohlc![i]?.low);
          take(s.ohlc![i]?.high);
        }
      } else {
        for (var i = from; i <= to && i < s.values.length; i++) {
          take(s.values[i]);
        }
      }
      if (s.kind == SeriesKind.histogram || s.kind == SeriesKind.volume) take(0);
      if (pane > 0) s.levels.forEach(take);
    }
    if (pane == 0 && !lo.isFinite) {
      // price hidden and nothing else in the main pane: keep the price scale so drawings stay put
      for (var i = from; i <= to; i++) {
        take(bars[i].low);
        take(bars[i].high);
      }
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
    final (top, bottom) = pane == 0 ? (0.1, showVolume ? 0.16 : 0.08) : (0.1, 0.08);
    final span = (hi - lo) / (1 - top - bottom);
    p.hi = hi + span * top;
    p.lo = lo - span * bottom;
  }

  double? _compareBase(CompareSeries c, int from, int to) {
    for (var i = from; i <= to && i < c.closes.length; i++) {
      final v = c.closes[i];
      if (v != null && v > 0) return v;
    }
    return null;
  }

  void _computeTicks(int pane) {
    final p = _panes[pane];
    final count = math.max(2, (p.height / 28).floor());
    final t = niceTicks(p.lo, p.hi, count);
    p.step = t.step;
    p.ticks = t.values;
  }

  String _axisText(_Pane p, double transformed) {
    switch (p.tf.mode) {
      case ScaleMode.percent:
        return '${transformed >= 0 ? '+' : ''}${transformed.toStringAsFixed(p.step < 1 ? 2 : 1)}%';
      case ScaleMode.log:
        final price = p.tf.from(transformed);
        return axisPrice(price, price.abs() >= 100 ? 1 : price.abs() >= 1 ? 0.01 : 0.0001);
      case ScaleMode.linear:
        return axisPrice(transformed, p.step);
    }
  }

  String _priceText(_Pane p, double price) {
    switch (p.tf.mode) {
      case ScaleMode.percent:
        final v = p.tf.to(price);
        return '${v >= 0 ? '+' : ''}${v.toStringAsFixed(2)}%';
      case ScaleMode.log:
      case ScaleMode.linear:
        return price.toStringAsFixed(price.abs() >= 1 ? 2 : 4);
    }
  }

  // ---------------------------------------------------------------- drawing

  void _draw() {
    final ctx = _ctx;
    final dpr = window.devicePixelRatio;
    ctx.setTransform(dpr.toJS, 0, 0, dpr, 0, 0);
    ctx.fillStyle = theme.bg.toJS;
    ctx.fillRect(0, 0, width, height);
    if (width <= 0 || height <= 0) return;
    _layout();
    if (_n == 0) {
      afterDraw?.call();
      return;
    }

    final (from, to) = _visibleBars();
    for (var i = 0; i < paneCount; i++) {
      _autoscale(i, from, math.max(from, to));
      _computeTicks(i);
    }
    ctx.font = '11px $_font';
    var widest = 0.0;
    for (var i = 0; i < paneCount; i++) {
      final p = _panes[i];
      for (final v in [p.lo, p.hi]) {
        widest = math.max(widest, ctx.measureText(_axisText(p, v)).width);
      }
    }
    final wantAxis = (widest + 20).clamp(56.0, 120.0);
    if ((wantAxis - axisWidth).abs() > 4) axisWidth = wantAxis;

    final ticks = timeTicks(_civil, from, to, math.max(1, 80 / spacing), intraday);
    for (var i = 0; i < paneCount; i++) {
      _drawPane(ctx, i, from, to, ticks);
    }
    _drawPriceAxes(ctx);
    _drawTimeAxis(ctx, ticks);
    _drawCrosshair(ctx);
    afterDraw?.call();
  }

  void _dash(CanvasRenderingContext2D ctx, List<num> d) => ctx.setLineDash([for (final v in d) v.toJS].toJS);

  double _crisp(double v) => v.floorToDouble() + 0.5;

  void _drawPane(CanvasRenderingContext2D ctx, int index, int from, int to, List<TimeTick> ticks) {
    final p = _panes[index];
    final w = plotWidth;
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, p.top, w, p.height);
    ctx.clip();

    if (showGrid) {
      ctx.strokeStyle = theme.grid.toJS;
      ctx.lineWidth = 1;
      _dash(ctx, const []);
      ctx.beginPath();
      for (final v in p.ticks) {
        final y = _crisp(p.top + (p.hi - v) / (p.hi - p.lo) * p.height);
        ctx.moveTo(0, y);
        ctx.lineTo(w, y);
      }
      for (final t in ticks) {
        final x = _crisp(logicalToX(t.index.toDouble()));
        ctx.moveTo(x, p.top);
        ctx.lineTo(x, p.top + p.height);
      }
      ctx.stroke();
    }

    if (index == 0) {
      if (showVolume) _drawVolume(ctx, from, to);
      if (showPrice) _drawPrice(ctx, from, to);
      _drawCompares(ctx, from, to);
    }
    for (final s in series) {
      if (s.pane != index || !s.visible) continue;
      for (final lv in s.levels) {
        ctx.strokeStyle = 'rgba(148,163,184,0.45)'.toJS;
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
          _drawLine(ctx, s.values, s.color, s.width, s.dashed, p, from, to);
        case SeriesKind.dots:
          _drawDots(ctx, s, p, from, to);
        case SeriesKind.volume:
          _drawVolumeSeries(ctx, s, p, from, to);
        case SeriesKind.area:
          _drawAreaSeries(ctx, s, p, from, to);
        case SeriesKind.candles:
          _drawCandleSeries(ctx, s, p, from, to);
      }
    }
    if (index == 0) {
      final last = bars.last;
      final up = _n < 2 || last.close >= bars[_n - 2].close;
      if (showPrice && showLastPriceLine) {
        ctx.strokeStyle = (up ? theme.up : theme.down).toJS;
        ctx.lineWidth = 1;
        _dash(ctx, const [1, 3]);
        final y = _crisp(p.y(last.close));
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(w, y);
        ctx.stroke();
      }
      for (final pl in priceLines) {
        final py = p.y(pl.price);
        if (!py.isFinite) continue;
        ctx.strokeStyle = pl.color.toJS;
        _dash(ctx, pl.dashed ? const [5, 4] : const []);
        ctx.beginPath();
        ctx.moveTo(0, _crisp(py));
        ctx.lineTo(w, _crisp(py));
        ctx.stroke();
      }
      _dash(ctx, const []);
      _drawMarkers(ctx, p);
      final o = overlay;
      if (o != null) {
        ctx.save();
        o(ctx, w, p.height);
        ctx.restore();
      }
    }
    ctx.restore();

    if (index > 0) {
      ctx.fillStyle = theme.border.toJS;
      ctx.fillRect(0, p.top, width, 1);
    }
  }

  void _drawPrice(CanvasRenderingContext2D ctx, int from, int to) {
    final p = _panes[0];
    switch (chartType) {
      case ChartType.line:
        _drawLine(ctx, [for (final b in bars) b.close], theme.line, 2, false, p, from, to);
      case ChartType.area:
        _drawArea(ctx, p, from, to);
      case ChartType.bars:
        _drawOhlcBars(ctx, p, from, to);
      case ChartType.candles || ChartType.hollow || ChartType.heikin:
        _drawCandles(ctx, p, from, to, hollow: chartType == ChartType.hollow);
    }
  }

  void _drawCandles(CanvasRenderingContext2D ctx, _Pane p, int from, int to, {bool hollow = false}) {
    final body = math.max(1.0, (spacing * 0.72).floorToDouble());
    for (final up in [true, false]) {
      final color = (up ? theme.up : theme.down).toJS;
      ctx.fillStyle = color;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      final hollowRects = <(double, double, double, double)>[];
      for (var i = from; i <= to; i++) {
        final b = bars[i];
        // hollow candles: color by close vs previous close, hollow body when close >= open
        final isUp = hollow && i > 0 ? b.close >= bars[i - 1].close : b.close >= b.open;
        if (isUp != up) continue;
        final x = logicalToX(i.toDouble()).roundToDouble();
        final yh = p.y(b.high), yl = p.y(b.low);
        final yo = p.y(b.open), yc = p.y(b.close);
        final top = math.min(yo, yc), bot = math.max(yo, yc);
        final left = x - (body / 2).floorToDouble();
        if (hollow && b.close >= b.open && body > 2) {
          ctx.rect(x - 0.5, yh, 1, math.max(0, top - yh));
          ctx.rect(x - 0.5, bot, 1, math.max(0, yl - bot));
          hollowRects.add((left + 0.5, top + 0.5, body - 1, math.max(1, bot - top - 1)));
          continue;
        }
        ctx.rect(x - 0.5, yh, 1, math.max(1, yl - yh));
        if (body > 2) ctx.rect(left, top, body, math.max(1, bot - top));
      }
      ctx.fill();
      for (final (x, y, w, h) in hollowRects) {
        ctx.strokeRect(x, y, w, h);
      }
    }
  }

  void _drawOhlcBars(CanvasRenderingContext2D ctx, _Pane p, int from, int to) {
    final tick = math.max(1.0, (spacing * 0.35).floorToDouble());
    ctx.lineWidth = spacing > 6 ? 1.5 : 1;
    for (final up in [true, false]) {
      ctx.strokeStyle = (up ? theme.up : theme.down).toJS;
      ctx.beginPath();
      for (var i = from; i <= to; i++) {
        final b = bars[i];
        if ((b.close >= b.open) != up) continue;
        final x = _crisp(logicalToX(i.toDouble()));
        ctx.moveTo(x, p.y(b.high));
        ctx.lineTo(x, p.y(b.low));
        ctx.moveTo(x - tick, _crisp(p.y(b.open)));
        ctx.lineTo(x, _crisp(p.y(b.open)));
        ctx.moveTo(x, _crisp(p.y(b.close)));
        ctx.lineTo(x + tick, _crisp(p.y(b.close)));
      }
      ctx.stroke();
    }
  }

  void _drawArea(CanvasRenderingContext2D ctx, _Pane p, int from, int to) {
    final lo = math.max(0, from - 1), hi = math.min(_n - 1, to + 1);
    if (hi < lo) return;
    final grad = ctx.createLinearGradient(0, p.top, 0, p.top + p.height);
    grad.addColorStop(0, _alpha(theme.line, 0.35));
    grad.addColorStop(1, _alpha(theme.line, 0.02));
    ctx.beginPath();
    ctx.moveTo(logicalToX(lo.toDouble()), p.top + p.height);
    for (var i = lo; i <= hi; i++) {
      ctx.lineTo(logicalToX(i.toDouble()), p.y(bars[i].close));
    }
    ctx.lineTo(logicalToX(hi.toDouble()), p.top + p.height);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();
    _drawLine(ctx, [for (final b in bars) b.close], theme.line, 2, false, p, from, to);
  }

  void _drawCompares(CanvasRenderingContext2D ctx, int from, int to) {
    if (compares.isEmpty) return;
    final p = _panes[0];
    final mainBase = _firstVisibleClose(from, to);
    for (final c in compares) {
      final base = _compareBase(c, from, to);
      if (base == null) continue;
      final scaled = [for (final v in c.closes) v == null ? null : mainBase * v / base];
      _drawLine(ctx, scaled, c.color, 1.6, false, p, from, to);
    }
  }

  void _drawVolume(CanvasRenderingContext2D ctx, int from, int to) {
    final p = _panes[0];
    var maxVol = 0.0;
    for (var i = from; i <= to; i++) {
      maxVol = math.max(maxVol, rawBars[i].volume);
    }
    if (maxVol <= 0) return;
    final areaH = p.height * 0.18;
    final base = p.top + p.height;
    final w = math.max(1.0, (spacing * 0.8).floorToDouble());
    for (final up in [true, false]) {
      ctx.fillStyle = (up ? theme.volUp : theme.volDown).toJS;
      ctx.beginPath();
      for (var i = from; i <= to; i++) {
        final b = rawBars[i];
        if ((b.close >= b.open) != up) continue;
        final x = logicalToX(i.toDouble()).roundToDouble();
        final h = b.volume / maxVol * areaH;
        ctx.rect(x - (w / 2).floorToDouble(), base - h, w, h);
      }
      ctx.fill();
    }
  }

  void _drawLine(CanvasRenderingContext2D ctx, List<double?> values, String color, double width, bool dashed,
      _Pane p, int from, int to) {
    ctx.strokeStyle = color.toJS;
    ctx.lineWidth = width;
    ctx.lineJoin = 'round';
    _dash(ctx, dashed ? const [6, 4] : const []);
    ctx.beginPath();
    var pen = false;
    final lo = math.max(0, from - 1), hi = math.min(values.length - 1, to + 1);
    for (var i = lo; i <= hi; i++) {
      final v = values[i];
      if (v == null) {
        pen = false;
        continue;
      }
      final x = logicalToX(i.toDouble()), y = p.y(v);
      if (!y.isFinite) {
        pen = false;
        continue;
      }
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
      ctx.fillStyle = (pos ? _alpha(theme.up, 0.6) : _alpha(theme.down, 0.6)).toJS;
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

  void _drawVolumeSeries(CanvasRenderingContext2D ctx, Series s, _Pane p, int from, int to) {
    final base = p.y(0);
    final w = math.max(1.0, (spacing * 0.8).floorToDouble());
    for (final up in [true, false]) {
      ctx.fillStyle = (up ? _alpha(theme.up, 0.55) : _alpha(theme.down, 0.55)).toJS;
      ctx.beginPath();
      for (var i = from; i <= to && i < s.values.length && i < rawBars.length; i++) {
        final v = s.values[i];
        final b = rawBars[i];
        if (v == null || (b.close >= b.open) != up) continue;
        final x = logicalToX(i.toDouble()).roundToDouble();
        final y = p.y(v);
        ctx.rect(x - (w / 2).floorToDouble(), y, w, math.max(1, base - y));
      }
      ctx.fill();
    }
  }

  void _drawAreaSeries(CanvasRenderingContext2D ctx, Series s, _Pane p, int from, int to) {
    final lo = math.max(0, from - 1), hi = math.min(s.values.length - 1, to + 1);
    final grad = ctx.createLinearGradient(0, p.top, 0, p.top + p.height);
    grad.addColorStop(0, _alpha(s.color, 0.3));
    grad.addColorStop(1, _alpha(s.color, 0.02));
    ctx.beginPath();
    double? firstX, lastX;
    for (var i = lo; i <= hi; i++) {
      final v = s.values[i];
      if (v == null) continue;
      final x = logicalToX(i.toDouble()), y = p.y(v);
      if (firstX == null) {
        ctx.moveTo(x, p.top + p.height);
        firstX = x;
      }
      ctx.lineTo(x, y);
      lastX = x;
    }
    if (firstX != null && lastX != null) {
      ctx.lineTo(lastX, p.top + p.height);
      ctx.closePath();
      ctx.fillStyle = grad;
      ctx.fill();
    }
    _drawLine(ctx, s.values, s.color, s.width, false, p, from, to);
  }

  void _drawCandleSeries(CanvasRenderingContext2D ctx, Series s, _Pane p, int from, int to) {
    final data = s.ohlc;
    if (data == null) return;
    final body = math.max(1.0, (spacing * 0.72).floorToDouble());
    for (final up in [true, false]) {
      ctx.fillStyle = (up ? theme.up : theme.down).toJS;
      ctx.beginPath();
      for (var i = from; i <= to && i < data.length; i++) {
        final b = data[i];
        if (b == null || (b.close >= b.open) != up) continue;
        final x = logicalToX(i.toDouble()).roundToDouble();
        final yh = p.y(b.high), yl = p.y(b.low), yo = p.y(b.open), yc = p.y(b.close);
        ctx.rect(x - 0.5, yh, 1, math.max(1, yl - yh));
        if (body > 2) ctx.rect(x - (body / 2).floorToDouble(), math.min(yo, yc), body, math.max(1, (yo - yc).abs()));
      }
      ctx.fill();
    }
  }

  void _drawMarkers(CanvasRenderingContext2D ctx, _Pane p) {
    if (markers.isEmpty || _n == 0) return;
    final y = p.top + p.height - 11;
    ctx.font = '600 10px $_font';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    for (final m in markers) {
      final x = logicalToX(_indexOfTime(m.time));
      if (x < -10 || x > plotWidth + 10) continue;
      ctx.fillStyle = m.color.toJS;
      ctx.beginPath();
      ctx.arc(x, y, 8, 0, math.pi * 2);
      ctx.fill();
      ctx.fillStyle = '#fff'.toJS;
      ctx.fillText(m.letter, x, y + 0.5);
    }
  }

  /// Logical index for a time (fractional; extrapolated beyond the data).
  double _indexOfTime(int t) {
    final i = firstIndexAtOrAfter(rawBars, t);
    if (i >= _n) {
      final step = _n > 1 ? (rawBars[_n - 1].time - rawBars[_n - 2].time).clamp(1, 1 << 30) : 86400;
      return (_n - 1) + (t - rawBars[_n - 1].time) / step;
    }
    return i.toDouble();
  }

  /// The marker under (x, y), if any (for tooltips).
  EventMarker? markerAt(double x, double y) {
    final p = _panes[0];
    final my = p.top + p.height - 11;
    for (final m in markers) {
      final mx = logicalToX(_indexOfTime(m.time));
      if ((mx - x).abs() <= 9 && (my - y).abs() <= 9) return m;
    }
    return null;
  }

  void _axisLabel(CanvasRenderingContext2D ctx, String text, double y, String bg, String fg, {String? second}) {
    final x0 = plotWidth;
    final h = second == null ? 18.0 : 30.0;
    final top = (y - 9).clamp(0.0, math.max(0.0, height - timeAxisHeight - h));
    ctx.fillStyle = bg.toJS;
    ctx.beginPath();
    ctx.roundRect(x0 + 1, top, axisWidth - 2, h, 3.toJS);
    ctx.fill();
    ctx.fillStyle = fg.toJS;
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    ctx.font = '11px $_font';
    ctx.fillText(text, x0 + 7, top + 9.5);
    if (second != null) {
      ctx.font = '10px $_font';
      ctx.fillText(second, x0 + 7, top + 22);
    }
  }

  void _drawPriceAxes(CanvasRenderingContext2D ctx) {
    final x0 = plotWidth;
    ctx.fillStyle = theme.bg.toJS;
    ctx.fillRect(x0, 0, axisWidth, height);
    ctx.fillStyle = theme.border.toJS;
    ctx.fillRect(x0, 0, 1, height - timeAxisHeight);
    ctx.font = '11px $_font';
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    for (var i = 0; i < paneCount; i++) {
      final p = _panes[i];
      ctx.fillStyle = theme.text.toJS;
      for (final v in p.ticks) {
        final y = p.top + (p.hi - v) / (p.hi - p.lo) * p.height;
        if (y < p.top + 8 || y > p.top + p.height - 8) continue;
        ctx.fillText(_axisText(p, v), x0 + 7, y);
      }
    }
    final main = _panes[0];
    for (final pl in priceLines) {
      final y = main.y(pl.price);
      if (y.isFinite && y >= 0 && y <= main.height) _axisLabel(ctx, _priceText(main, pl.price), y, pl.color, '#fff');
    }
    final (from, to) = _visibleBars();
    for (final c in compares) {
      final v = c.closes.lastWhere((x) => x != null, orElse: () => null);
      final base = _compareBase(c, from, to);
      if (v == null || base == null) continue;
      final price = _firstVisibleClose(from, to) * v / base;
      _axisLabel(ctx, scaleMode == ScaleMode.percent ? _priceText(main, price) : c.symbol, main.y(price), c.color,
          '#fff');
    }
    for (final s in series) {
      if (s.pane == 0 || !s.lastValueLabel || !s.visible) continue;
      final v = s.kind == SeriesKind.candles ? s.ohlc?.lastWhere((b) => b != null, orElse: () => null)?.close : s.lastValue();
      if (v == null) continue;
      final p = _panes[s.pane];
      final y = p.y(v);
      if (y >= p.top && y <= p.top + p.height) _axisLabel(ctx, axisPrice(v, p.step), y, s.color, '#0b1220');
    }
    if (showPrice) {
      final last = bars.last;
      final up = _n < 2 || last.close >= bars[_n - 2].close;
      _axisLabel(ctx, _priceText(main, last.close), main.y(last.close), up ? theme.up : theme.down, '#fff',
          second: lastLabelExtra);
    }
  }

  void _drawTimeAxis(CanvasRenderingContext2D ctx, List<TimeTick> ticks) {
    final top = height - timeAxisHeight;
    ctx.fillStyle = theme.bg.toJS;
    ctx.fillRect(0, top, width, timeAxisHeight);
    ctx.fillStyle = theme.border.toJS;
    ctx.fillRect(0, top, width, 1);
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'center';
    for (final t in ticks) {
      final x = logicalToX(t.index.toDouble());
      if (x < 14 || x > plotWidth - 14) continue;
      final strong = t.weight == TickWeight.year || (intraday && t.weight == TickWeight.day);
      ctx.font = '${strong ? '600 ' : ''}11px $_font';
      ctx.fillStyle = theme.text.toJS;
      ctx.fillText(t.label, x, top + timeAxisHeight / 2);
    }
  }

  void _drawCrosshair(CanvasRenderingContext2D ctx) {
    final mx = _mouseX, my = _mouseY;
    final own = mx != null && my != null && mx <= plotWidth && my <= height - timeAxisHeight &&
        !(_dragging && !_delegated);
    int? idx;
    if (own) {
      idx = barIndexAt(xToLogical(mx));
    } else if (_syncTime != null) {
      final i = firstIndexAtOrAfter(rawBars, _syncTime!);
      if (i < _n) idx = i;
    }
    if (idx == null) return;
    final x = _crisp(logicalToX(idx.toDouble()));
    if (x < 0 || x > plotWidth) return;
    ctx.strokeStyle = theme.crosshair.toJS;
    ctx.lineWidth = 1;
    _dash(ctx, const [4, 4]);
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, height - timeAxisHeight);
    if (own) {
      ctx.moveTo(0, _crisp(my));
      ctx.lineTo(plotWidth, _crisp(my));
    }
    ctx.stroke();
    _dash(ctx, const []);
    if (own) {
      final pane = _paneAt(my);
      if (pane != null) {
        final p = _panes[pane];
        final v = p.value(my);
        _axisLabel(ctx, pane == 0 ? _priceText(p, v) : axisPrice(v, p.step / 10), my, theme.labelBg, '#fff');
      }
    }
    final label = formatBarTime(rawBars[idx].time, intraday);
    ctx.font = '11px $_font';
    final w = ctx.measureText(label).width + 14;
    final left = (x - w / 2).clamp(0.0, math.max(0.0, plotWidth - w));
    final top = height - timeAxisHeight + 3;
    ctx.fillStyle = theme.labelBg.toJS;
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

  /// Index of the separator (pane i's top edge, i ≥ 1) within 4px of y.
  int? _separatorAt(double y) {
    for (var i = 1; i < paneCount; i++) {
      if ((y - _panes[i].top).abs() <= 4) return i;
    }
    return null;
  }

  // ---------------------------------------------------------------- interaction

  (double, double) local(MouseEvent e) {
    final r = canvas.getBoundingClientRect();
    // read as doubles: package:web types clientX/Y as int but browsers report fractional pixels
    final o = e as JSObject;
    return ((o['clientX'] as JSNumber).toDartDouble - r.left, (o['clientY'] as JSNumber).toDartDouble - r.top);
  }

  void _bindPointer() {
    canvas.addEventListener('pointerdown', ((PointerEvent e) {
      if (e.button != 0 && e.pointerType == 'mouse') return;
      final (x, y) = local(e);
      _pointers[e.pointerId] = (x, y);
      if (_pointers.length == 2) {
        final [a, b] = _pointers.values.toList();
        _pinchDist = math.sqrt(math.pow(a.$1 - b.$1, 2) + math.pow(a.$2 - b.$2, 2));
        _dragging = true;
        return;
      }
      _downAt = (x, y);
      _dragging = false;
      _delegated = false;
      final sep = x <= plotWidth ? _separatorAt(y) : null;
      if (sep != null) {
        _dragMode = 'separator';
        _dragPane = sep;
      } else {
        _dragMode = x > plotWidth ? 'price-axis' : y > height - timeAxisHeight ? 'time-axis' : 'pan';
        _dragPane = _paneAt(y) ?? 0;
        if (_dragMode == 'pan' && y < mainHeight && (onPointerDown?.call(x, y, e) ?? false)) {
          _delegated = true;
        }
      }
      canvas.setPointerCapture(e.pointerId);
    }).toJS);

    canvas.addEventListener('pointermove', ((PointerEvent e) {
      final (x, y) = local(e);
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
        if (!_dragging && ((x - down.$1).abs() > 3 || (y - down.$2).abs() > 3)) _dragging = true;
        if (_dragging) {
          if (_delegated) {
            _mouseX = x;
            _mouseY = y;
            onPointerDrag?.call(x, y, e);
            invalidate();
            return;
          }
          final dx = x - prev.$1, dy = y - prev.$2;
          switch (_dragMode) {
            case 'separator':
              _dragSeparator(_dragPane, dy);
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
              _rangeChanged();
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
      final (x, y) = local(e);
      if (_delegated) {
        _delegated = false;
        onPointerUp?.call(x, y, e);
        invalidate();
        return;
      }
      if (wasPointer && !dragged && down != null && e.type == 'pointerup') {
        if (x <= plotWidth && y < mainHeight) onClick?.call(x, y, e);
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
          final (x, _) = local(e);
          if (dx.abs() > dy.abs() || e.shiftKey) {
            rightOffset += (dx.abs() > dy.abs() ? dx : dy) / spacing;
            _clampScroll();
            invalidate();
            _rangeChanged();
          } else {
            _zoomAt(math.min(x, plotWidth), math.exp(-dy * (e.ctrlKey ? 0.01 : 0.0015)));
          }
        }).toJS,
        AddEventListenerOptions(passive: false));

    canvas.addEventListener('dblclick', ((MouseEvent e) {
      final (x, y) = local(e);
      if (x > plotWidth) {
        final p = _paneAt(y);
        if (p != null) _panes[p].auto = true;
        invalidate();
      } else if (y > height - timeAxisHeight) {
        scrollToRealtime();
      } else if (y < mainHeight) {
        onDoubleClick?.call(x, y);
      }
    }).toJS);

    canvas.addEventListener('contextmenu', ((MouseEvent e) {
      e.preventDefault();
      final (x, y) = local(e);
      final pane = _paneAt(y);
      onContextMenu?.call(x, y, pane == 0 ? yToPrice(y) : null, e);
    }).toJS);
  }

  void _hover(double x, double y) {
    _mouseX = x;
    _mouseY = y;
    if (_dragging && !_delegated) {
      // keep the drag cursor
    } else if (x > plotWidth) {
      cursor = 'ns-resize';
    } else if (y > height - timeAxisHeight) {
      cursor = 'ew-resize';
    } else if (_separatorAt(y) != null) {
      cursor = 'row-resize';
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
    _rangeChanged();
  }

  void _scalePrice(int pane, double dy) {
    final p = _panes[pane];
    p.auto = false;
    final mid = (p.hi + p.lo) / 2;
    final half = (p.hi - p.lo) / 2 * math.exp(dy * 0.005);
    p.hi = mid + half;
    p.lo = mid - half;
  }

  /// Drags the top edge of pane [pane] by dy, trading height with the pane above.
  void _dragSeparator(int pane, double dy) {
    if (pane == 1) {
      // between the main pane and the indicator panes: change their share of the height
      final total = math.max(1.0, height - timeAxisHeight);
      final sub = total - _panes[0].height;
      _subShare = ((sub - dy) / total).clamp(0.1, 0.8);
      return;
    }
    final a = _panes[pane - 1], b = _panes[pane];
    final sum = a.weight + b.weight;
    final ha = math.max(30.0, a.height + dy), hb = math.max(30.0, b.height - dy);
    a.weight = sum * ha / (ha + hb);
    b.weight = sum * hb / (ha + hb);
  }
}

String _alpha(String color, double a) {
  final m = RegExp(r'^#([0-9a-fA-F]{6})$').firstMatch(color.trim());
  if (m == null) return color;
  final v = int.parse(m.group(1)!, radix: 16);
  return 'rgba(${(v >> 16) & 255},${(v >> 8) & 255},${v & 255},$a)';
}
