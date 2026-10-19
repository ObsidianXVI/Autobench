// One chart of the workbench: bars + live updates, indicators with per-pane legends, compares,
// alert lines, earnings markers, drawing tools (create, select, drag, style), context menus and
// cross-chart crosshair sync.
import 'dart:async';
import 'dart:convert';
import 'dart:js_interop';
import 'dart:js_interop_unsafe';
import 'dart:math' as math;

import 'package:web/web.dart';

import '../core/chartmath.dart';
import '../core/format.dart';
import '../core/models.dart';
import '../ui/api.dart';
import '../ui/dom.dart';
import '../ui/kit/icons.dart';
import '../ui/kit/overlay.dart';
import '../ui/kit/state.dart';
import '../ui/store.dart';
import 'annotations.dart';
import 'drawing_ops.dart';
import 'engine.dart';

const palette = ['#f59e0b', '#38bdf8', '#e879f9', '#34d399', '#f87171', '#a3e635', '#fb923c', '#60a5fa'];
const compareColors = ['#f472b6', '#facc15', '#2dd4bf', '#c084fc', '#fb7185'];
const drawColors = ['#38bdf8', '#a78bfa', '#22c55e', '#ef4444', '#f59e0b', '#e2e8f0', '#64748b', '#ec4899'];
const _hiddenOverlayOutputs = {'bandwidth', 'percent_b', 'direction', 'slope_pct', 'r2'};
const _histogramOutputs = {'histogram', 'momentum', 'squeeze_on'};

/// Points a tool needs before it is committed (default 2).
const toolPoints = {
  'horizontal_line': 1, 'horizontal_ray': 1, 'vertical_line': 1, 'text': 1, 'marker': 1, 'trendline': 2, 'ray': 2,
  'extended_line': 2, 'rectangle': 2, 'fib_retracement': 2, 'arrow': 2, 'price_range': 2, 'long_position': 2,
  'short_position': 2, 'callout': 2, 'parallel_channel': 3,
};

/// Crosshair sync between charts: the chart under the mouse publishes a time.
final crosshairTime = Obs<(int, int?)>((-1, null)); // (source chart index, time)
final syncedRange = Obs<(int, double, double)?>(null); // (source chart index, from-time, to-time)

String indicatorKey(IndicatorRef r) => jsonEncode(r.toJson());

class _LegendRow {
  final String key, label;
  final List<(String, String, List<double?>)> outputs; // (name, color, values)
  final int pane;
  _LegendRow(this.key, this.label, this.outputs, this.pane);
}

class _Drag {
  final Annotation original;
  final int? handle; // point index, or null when moving the whole drawing
  final double startX, startY;
  _Drag(this.original, this.handle, this.startX, this.startY);
}

class ChartPanel {
  final ChartState state;
  final int index;
  final HTMLElement root = h('div.chart-cell');
  final HTMLElement _host = h('div.chart');
  final HTMLElement _legend = h('div.legend');
  final HTMLElement _paneLegends = h('div.pane-legends');
  final HTMLElement _status = h('div.chart-status');
  final HTMLElement _toLatest = h('button.to-latest', icon('chev-right', size: 16),
      {'type': 'button', 'aria-label': 'Scroll to the latest bar', 'data-tip': 'Scroll to the latest bar', 'data-tip-pos': 'left'});
  final HTMLElement _floating = h('div.draw-toolbar');
  late final ChartEngine engine;
  late final AnnotationLayer layer;
  final Scope _scope = Scope();

  bool _loading = false;
  String _loadedKey = '';
  int _loadSeq = 0, _indSeq = 0, _cmpSeq = 0;
  int _futureBars = 0;
  List<_LegendRow> _legendRows = [];
  int? _legendIndex;
  HTMLElement? _prompt;
  _Drag? _drag;
  Timer? _liveTimer, _clock;
  String? _error;
  final Map<String, List<double?>> _compareData = {};
  final void Function(ChartPanel) onActivate;

  ChartPanel(this.state, this.index, {required this.onActivate}) {
    root.append(_host);
    append(root, [_legend, _paneLegends, _status, _toLatest, _floating]);
    _floating.isHidden = true;
    _toLatest.isHidden = true;
    engine = ChartEngine(_host);
    layer = AnnotationLayer(engine);
    engine.onClick = _click;
    engine.onDoubleClick = _doubleClick;
    engine.onCrosshair = _move;
    engine.onContextMenu = _contextMenu;
    engine.onPointerDown = _pointerDown;
    engine.onPointerDrag = _pointerDrag;
    engine.onPointerUp = _pointerUp;
    engine.onRangeChanged = _rangeChanged;
    engine.afterDraw = _afterDraw;
    root.addEventListener('pointerdown', ((Event _) => onActivate(this)).toJS, true.toJS);
    _toLatest.addEventListener('click', ((Event _) => engine.scrollToRealtime()).toJS);

    _scope.listen(state.symbol, (_) => _load());
    _scope.listen(state.interval, (_) => _load());
    _scope.listen(state.indicators, (_) => _loadIndicators());
    _scope.listen(state.hidden, (_) => _applyHidden());
    _scope.listen(state.chartType, (t) => engine.setChartType(chartTypeFromName(t)));
    _scope.listen(state.scale, (s) => engine.setScaleMode(ScaleMode.values.byName(s)));
    _scope.listen(state.compares, (_) => _loadCompares());
    _scope.listen(store.drawingsVersion, (_) => _applyDrawings());
    _scope.listen(store.selected, (_) => _applyDrawings());
    _scope.listen(store.drawingsHidden, (h) {
      layer.hidden = h;
      layer.update();
    });
    _scope.listen(store.tool, (_) => cancelDraft());
    _scope.listen(store.alerts, (_) => _applyAlerts());
    _scope.listen(crosshairTime, (v) {
      if (v.$1 != index) engine.setSyncedCrosshair(store.syncCrosshair.value ? v.$2 : null);
    });
    _scope.listen(syncedRange, (v) {
      if (v == null || v.$1 == index || !store.syncRange.value) return;
      final from = layer.mapper.logical(v.$2.round()), to = layer.mapper.logical(v.$3.round());
      if (from != null && to != null) engine.setVisibleRange(from, to);
    });
    engine.setChartType(chartTypeFromName(state.chartType.value));
    engine.setScaleMode(ScaleMode.values.byName(state.scale.value));
    _clock = Timer.periodic(const Duration(seconds: 1), (_) => _tickCountdown());
    _load();
  }

  String get symbol => state.symbol.value;
  String get interval => state.interval.value;
  bool get intraday => isIntraday(interval);
  List<Bar> get bars => engine.rawBars;

  void dispose() {
    _scope.dispose();
    _liveTimer?.cancel();
    _clock?.cancel();
    root.remove();
  }

  set active(bool v) => root.classList.toggle('active', v);

  // ---------------------------------------------------------------- loading

  Future<void> _load() async {
    final key = '$symbol|$interval';
    if (key == _loadedKey) return;
    final symbolChanged = !_loadedKey.startsWith('$symbol|');
    _loadedKey = key;
    final seq = ++_loadSeq;
    _loading = true;
    _error = null;
    cancelDraft();
    if (symbolChanged) {
      store.selected.value = null;
      engine.markers = [];
    }
    _renderLegend();
    _renderStatus();
    store.loadDrawings(symbol).catchError((Object e) => toast('Drawings: $e', kind: 'error'));
    try {
      final res = await api.bars(symbol, interval);
      if (seq != _loadSeq) return;
      final b = res.bars;
      engine.setBars(b, intraday: intraday, keepView: true);
      engine.setSeries(const []);
      layer.mapper.set([for (final x in b) x.time], intervalSeconds[interval] ?? 86400, intraday);
      _applyDrawings();
      _applyAlerts();
      final show = intraday ? 160 : interval == '1d' ? 180 : 150;
      engine.setVisibleRange(math.max(0, b.length - show).toDouble(), b.length + math.max(8, _futureBars + 4).toDouble());
      _loading = false;
      if (b.isEmpty) _error = 'No data for $symbol ($interval)';
      _renderLegend();
      _renderStatus();
      if (res.source.isNotEmpty) store.provider.value = res.source;
      await Future.wait([_loadIndicators(), _loadCompares(), if (symbolChanged) _loadMarkers()]);
      _scheduleLive();
    } catch (e) {
      if (seq != _loadSeq) return;
      _loading = false;
      _error = '$e';
      _renderStatus();
      _renderLegend();
    }
  }

  void _renderStatus() {
    replace(_status, [
      if (_loading) h('div.spinner'),
      if (_error != null && !_loading) h('div.chart-error', [icon('info', size: 16), ' ', _error]),
    ]);
  }

  /// Keeps the chart current: daily+ bars update from quotes, intraday bars reload every minute.
  void _scheduleLive() {
    _liveTimer?.cancel();
    _liveTimer = Timer.periodic(Duration(seconds: intraday ? 60 : 20), (_) => _refreshLive());
  }

  Future<void> _refreshLive() async {
    if (marketSession(DateTime.now().toUtc()).state == 'closed' || bars.isEmpty || document.hidden) return;
    final seq = _loadSeq;
    try {
      if (intraday) {
        final res = await api.bars(symbol, interval);
        if (seq != _loadSeq || res.bars.isEmpty) return;
        final atEdge = !engine.scrolledBack;
        engine.setBars(res.bars, intraday: true, keepView: true);
        layer.mapper.set([for (final x in res.bars) x.time], intervalSeconds[interval] ?? 60, true);
        if (atEdge) engine.scrollToRealtime(right: engine.rightOffset);
      } else {
        final q = await api.quote(symbol);
        if (seq != _loadSeq) return;
        final last = bars.last;
        final updated = Bar(last.time, last.open, math.max(last.high, q.price), math.min(last.low, q.price), q.price,
            last.volume);
        final list = [...bars.sublist(0, bars.length - 1), updated];
        engine.setBars(list, intraday: false, keepView: true);
      }
      _renderLegend();
    } catch (_) {
      // transient: try again next tick
    }
  }

  void _tickCountdown() {
    if (!intraday || bars.isEmpty) {
      if (engine.lastLabelExtra != null) {
        engine.lastLabelExtra = null;
        engine.invalidate();
      }
      return;
    }
    final secs = intervalSeconds[interval] ?? 60;
    final now = DateTime.now().millisecondsSinceEpoch ~/ 1000;
    final session = marketSession(DateTime.now().toUtc());
    String? label;
    if (session.state == 'open') {
      final end = bars.last.time + secs;
      var left = end - now;
      if (left < 0) left = secs - ((now - bars.last.time) % secs);
      final m = left ~/ 60, s = left % 60;
      label = left >= 3600 ? '${left ~/ 3600}h ${(left % 3600) ~/ 60}m' : '${m.toString().padLeft(2, '0')}:${s.toString().padLeft(2, '0')}';
    }
    if (label != engine.lastLabelExtra) {
      engine.lastLabelExtra = label;
      engine.invalidate();
    }
  }

  Future<void> _loadIndicators() async {
    final refs = state.indicators.value;
    final seq = ++_indSeq;
    if (refs.isEmpty || bars.isEmpty) {
      engine.setSeries(const []);
      _legendRows = [];
      _renderLegend();
      return;
    }
    try {
      final res = await api.indicators(symbol, interval, refs);
      if (seq != _indSeq) return;
      final idx = {for (var i = 0; i < bars.length; i++) bars[i].time: i};
      List<double?> align(List<double?> vals) {
        final out = List<double?>.filled(bars.length, null);
        for (var i = 0; i < res.time.length && i < vals.length; i++) {
          final k = idx[res.time[i]];
          if (k != null) out[k] = vals[i];
        }
        return out;
      }

      final series = <Series>[];
      final rows = <_LegendRow>[];
      var pane = 0, colorIdx = 0;
      for (var r = 0; r < res.results.length && r < refs.length; r++) {
        final result = res.results[r];
        final key = indicatorKey(refs[r]);
        final target = result.overlay ? 0 : ++pane;
        final outs = <(String, String, List<double?>)>[];
        var first = true;
        for (final out in result.outputs) {
          if (result.overlay && _hiddenOverlayOutputs.contains(out)) continue;
          if (!result.overlay && out == 'direction') continue;
          final vals = align(result.values[out] ?? const []);
          final color = palette[colorIdx++ % palette.length];
          final hist = !result.overlay && _histogramOutputs.contains(out);
          series.add(Series(
            id: key,
            pane: target,
            kind: hist ? SeriesKind.histogram : result.name == 'psar' ? SeriesKind.dots : SeriesKind.line,
            values: vals,
            color: color,
            width: result.overlay ? 1.4 : 1.8,
            dashed: out == 'middle' || out == 'signal',
            lastValueLabel: !result.overlay && !hist,
            levels: first && !result.overlay ? result.levels : const [],
            visible: !state.hidden.value.contains(key),
          ));
          first = false;
          outs.add((out, color, vals));
        }
        rows.add(_LegendRow(key, result.label, outs, target));
      }
      engine.setSeries(series);
      _legendRows = rows;
      _renderLegend();
    } catch (e) {
      if (seq == _indSeq) toast('Indicators: $e', kind: 'error');
    }
  }

  void _applyHidden() {
    final hidden = state.hidden.value.toSet();
    for (final s in engine.series) {
      s.visible = !hidden.contains(s.id);
    }
    engine.invalidate();
    _renderLegend();
  }

  Future<void> _loadCompares() async {
    final syms = state.compares.value;
    final seq = ++_cmpSeq;
    if (syms.isEmpty || bars.isEmpty) {
      engine.setCompares(const []);
      _renderLegend();
      return;
    }
    final out = <CompareSeries>[];
    for (var i = 0; i < syms.length; i++) {
      final sym = syms[i];
      try {
        final res = await api.bars(sym, interval);
        if (seq != _cmpSeq) return;
        final byTime = {for (final b in res.bars) b.time: b.close};
        final aligned = [for (final b in bars) byTime[b.time]];
        _compareData[sym] = aligned;
        out.add(CompareSeries(sym, compareColors[i % compareColors.length], aligned));
      } catch (e) {
        toast('Compare $sym: $e', kind: 'error');
      }
    }
    engine.setCompares(out);
    _renderLegend();
  }

  Future<void> _loadMarkers() async {
    final sym = symbol;
    try {
      final e = await api.earnings(sym);
      if (sym != symbol) return;
      final markers = <EventMarker>[];
      for (final h in e['history'].list) {
        final d = DateTime.tryParse(h['date'].s ?? '');
        if (d == null) continue;
        final t = DateTime.utc(d.year, d.month, d.day).millisecondsSinceEpoch ~/ 1000;
        final rep = h['Reported EPS'].d, est = h['EPS Estimate'].d, sur = h['Surprise(%)'].d;
        markers.add(EventMarker(t, 'E', (sur ?? 0) >= 0 ? '#22a06b' : '#d1453b',
            'Earnings ${d.toIso8601String().substring(0, 10)}: EPS ${n(rep)} vs est ${n(est)}${sur != null ? ' (${pct(sur)})' : ''}'));
      }
      for (final next in e['next_earnings'].list) {
        final d = DateTime.tryParse(next.s ?? '');
        if (d == null) continue;
        markers.add(EventMarker(DateTime.utc(d.year, d.month, d.day).millisecondsSinceEpoch ~/ 1000, 'E', '#64748b',
            'Next earnings ${next.s}'));
      }
      engine.markers = markers;
      engine.invalidate();
    } catch (_) {
      // ETFs and indices have no earnings
    }
  }

  void _applyDrawings() {
    final all = store.drawingsFor(symbol);
    final shown = [for (final a in all) if (a.interval == null || a.interval == interval) a];
    layer.annotations = shown;
    layer.selectedId = store.selected.value;
    final n = bars.length;
    if (n > 0) {
      var maxLg = (n - 1).toDouble();
      for (final a in shown) {
        if (!a.visible) continue;
        for (final p in a.points) {
          final t = p.time;
          if (t == null) continue;
          final lg = layer.mapper.logical(t);
          if (lg != null) maxLg = math.max(maxLg, lg);
        }
      }
      _futureBars = math.min(76, math.max(0, (maxLg - (n - 1)).ceil()));
      engine.ensureRightSpace(math.max(8, _futureBars + 4).toDouble());
    }
    layer.update();
    _renderFloating();
  }

  void _applyAlerts() {
    engine.priceLines = [
      for (final a in store.alerts.value)
        if (a.symbol == symbol && a.status == 'active' && a.value != null && a.kind != 'change_above' &&
            a.kind != 'change_below' && a.kind != 'expression')
          PriceLine(a.value!, '#f59e0b', '⏰ ${a.description}'),
    ];
    engine.invalidate();
  }

  // ---------------------------------------------------------------- view helpers

  /// Applies a range preset from the bottom bar (1D … All).
  void showRange(String range) {
    if (bars.isEmpty) return;
    final start = rangeStart(range, bars.last.time);
    final from = start == null ? 0 : firstIndexAtOrAfter(bars, start);
    engine.setVisibleRange(from.toDouble(), bars.length + 4.0, notify: true);
  }

  void setVisibleTimes(int from, int? to) {
    final lf = layer.mapper.logical(from);
    final lt = to != null ? layer.mapper.logical(to) : bars.length - 1 + math.max(8, _futureBars + 4).toDouble();
    if (lf != null && lt != null) engine.setVisibleRange(lf, lt);
  }

  void resetView() {
    engine.resetPriceScales();
    final n = bars.length;
    final show = intraday ? 160 : interval == '1d' ? 180 : 150;
    engine.setVisibleRange(math.max(0, n - show).toDouble(), n + math.max(8, _futureBars + 4).toDouble(), notify: true);
  }

  void _rangeChanged(double from, double to) {
    _toLatest.isHidden = !engine.scrolledBack;
    if (!store.syncRange.value || bars.isEmpty) return;
    final tf = layer.mapper.time(from), tt = layer.mapper.time(to);
    if (tf != null && tt != null) syncedRange.value = (index, tf.toDouble(), tt.toDouble());
  }

  // ---------------------------------------------------------------- pointer: drawing tools

  String get tool => store.tool.value;

  Point? _toPoint(double x, double y, {bool snap = true}) {
    final lg = engine.xToLogical(x);
    final idx = lg.round();
    var time = layer.mapper.time(lg);
    var price = engine.yToPrice(y);
    if (idx >= 0 && idx < bars.length) {
      final bar = engine.bars[idx];
      time = bars[idx].time;
      if (snap && store.magnet.value) {
        var bestD = 9.0;
        for (final v in [bar.open, bar.high, bar.low, bar.close]) {
          final vy = engine.priceToY(v);
          if (vy != null && (vy - y).abs() < bestD) {
            bestD = (vy - y).abs();
            price = v;
          }
        }
      }
    }
    if (price == null || time == null || !price.isFinite) return null;
    return Point(time, double.parse(price.toStringAsFixed(price.abs() >= 1 ? 4 : 6)));
  }

  void _click(double x, double y, MouseEvent e) {
    if (tool == 'select') {
      final hit = layer.hit(x, y);
      store.selected.value = hit;
      return;
    }
    final pt = _toPoint(x, y);
    if (pt == null) return;
    if (tool == 'text' || tool == 'callout' && (layer.draft?.points.length ?? 0) >= 2) {
      if (tool == 'text') {
        _showPrompt(x, y, [pt]);
        return;
      }
    }
    final need = toolPoints[tool] ?? 2;
    final draft = layer.draft;
    final pts = [...?draft?.points.sublist(0, draft.points.length - 1), pt];
    if (pts.length >= need) {
      layer.draft = null;
      layer.update();
      if (tool == 'callout') {
        _showPrompt(x, y, pts);
        return;
      }
      _commitDrawing(tool, pts);
    } else {
      layer.draft = Draft(tool, [...pts, pt]);
      layer.update();
    }
  }

  Future<void> _commitDrawing(String type, List<Point> points, {String? text}) async {
    var pts = points;
    if ((type == 'long_position' || type == 'short_position') && points.length == 2) {
      // two clicks: entry then target; the stop defaults to half the target distance (2R)
      final e = points[0], t = points[1];
      final ep = e.price!, tp = t.price!;
      final ok = type == 'long_position' ? tp > ep : tp < ep;
      if (!ok) {
        toast(type == 'long_position' ? 'Long: click the target above the entry' : 'Short: click the target below the entry',
            kind: 'warn');
        return;
      }
      final stop = ep - (tp - ep) / 2;
      pts = [e, Point(t.time, double.parse(stop.toStringAsFixed(4))), Point(t.time, tp)];
    }
    final created = await createDrawing(symbol, type, pts, text: text);
    if (created != null) {
      store.selected.value = created.id;
      if (!store.stayInDrawMode.value) store.tool.value = 'select';
    }
  }

  void cancelDraft() {
    layer.draft = null;
    layer.update();
    _prompt?.remove();
    _prompt = null;
  }

  bool _pointerDown(double x, double y, PointerEvent e) {
    if (tool != 'select') return false;
    final handle = layer.hitHandle(x, y);
    if (handle != null) {
      final a = store.drawingById(handle.$1);
      if (a == null || layer.isLocked(a) || store.drawingsLocked.value) return false;
      _drag = _Drag(a, handle.$2, x, y);
      return true;
    }
    final id = layer.hit(x, y);
    if (id == null) return false;
    store.selected.value = id;
    final a = store.drawingById(id);
    if (a == null || layer.isLocked(a) || store.drawingsLocked.value) return true; // select, but no drag
    _drag = _Drag(a, null, x, y);
    return true;
  }

  void _pointerDrag(double x, double y, PointerEvent e) {
    final d = _drag;
    if (d == null) return;
    final a = d.original;
    List<Point> pts;
    if (d.handle != null) {
      final p = _toPoint(x, y, snap: !e.altKey);
      if (p == null) return;
      pts = [...a.points];
      final i = d.handle!;
      if (a.type == 'horizontal_line') {
        pts[0] = Point(a.points[0].time, p.price);
      } else if (a.type == 'vertical_line') {
        pts[0] = Point(p.time, a.points[0].price);
      } else if ((a.type == 'long_position' || a.type == 'short_position') && i > 0) {
        // stop/target handles: price from the cursor, the right edge from the cursor time
        pts[i] = Point(p.time, p.price);
        final other = i == 1 ? 2 : 1;
        pts[other] = Point(p.time, a.points[other].price);
      } else {
        pts[i] = p;
      }
    } else {
      final dLogical = engine.xToLogical(x) - engine.xToLogical(d.startX);
      final dy = y - d.startY;
      pts = [
        for (final p in a.points)
          Point(
            p.time == null ? null : layer.mapper.time((layer.mapper.logical(p.time!) ?? 0) + dLogical),
            p.price == null ? null : _shiftPrice(p.price!, dy),
          ),
      ];
      if (a.type == 'horizontal_line') pts = [Point(a.points[0].time, pts[0].price)];
    }
    layer.previews[a.id] = Annotation(
        id: a.id, symbol: a.symbol, interval: a.interval, type: a.type, points: pts, text: a.text, style: a.style,
        author: a.author, tag: a.tag, visible: a.visible);
    engine.cursor = 'grabbing';
    layer.update();
  }

  double _shiftPrice(double price, double dy) {
    final y0 = engine.priceToY(price);
    if (y0 == null) return price;
    final v = engine.yToPrice(y0 + dy) ?? price;
    return double.parse(v.toStringAsFixed(v.abs() >= 1 ? 4 : 6));
  }

  Future<void> _pointerUp(double x, double y, PointerEvent e) async {
    final d = _drag;
    _drag = null;
    if (d == null) return;
    final preview = layer.previews[d.original.id];
    if (preview == null) return; // a click without movement: selection only
    final moved = jsonEncode([for (final p in preview.points) p.toJson()]) !=
        jsonEncode([for (final p in d.original.points) p.toJson()]);
    if (moved) {
      // show the new position until the server confirms
      store.upsertDrawing(preview);
      await updateDrawing(d.original, {'points': [for (final p in preview.points) p.toJson()]},
          label: d.handle == null ? 'Move drawing' : 'Edit drawing');
    }
    layer.previews.remove(d.original.id);
    layer.update();
  }

  void _doubleClick(double x, double y) {
    final id = layer.hit(x, y);
    final a = id == null ? null : store.drawingById(id);
    if (a != null) editDrawingText(a);
  }

  void _move(double? x, double? y, double? logical) {
    if (x == null || y == null || logical == null) {
      _legendIndex = null;
      _renderLegend();
      crosshairTime.value = (index, null);
      return;
    }
    final draft = layer.draft;
    if (draft != null) {
      final pt = _toPoint(x, y);
      if (pt != null) {
        draft.points[draft.points.length - 1] = pt;
        layer.update();
      }
    }
    if (_drag == null) {
      final onHandle = tool == 'select' && layer.hitHandle(x, y) != null;
      final hov = tool == 'select' && y < engine.mainHeight ? layer.hit(x, y) : null;
      if (hov != layer.hoverId) {
        layer.hoverId = hov;
        layer.update();
      }
      final marker = engine.markerAt(x, y);
      root.setAttribute('data-marker', marker?.tooltip ?? '');
      engine.cursor = tool != 'select'
          ? 'crosshair'
          : onHandle
              ? 'move'
              : hov != null
                  ? 'pointer'
                  : marker != null
                      ? 'help'
                      : 'crosshair';
      _renderMarkerTip(marker, x, y);
    }
    final idx = engine.barIndexAt(logical);
    _legendIndex = idx >= 0 && idx < bars.length ? idx : null;
    _renderLegend();
    if (_legendIndex != null) crosshairTime.value = (index, bars[_legendIndex!].time);
  }

  HTMLElement? _markerTip;
  void _renderMarkerTip(EventMarker? m, double x, double y) {
    if (m == null) {
      _markerTip?.remove();
      _markerTip = null;
      return;
    }
    _markerTip ??= h('div.marker-tip');
    _markerTip!.textContent = m.tooltip;
    if (!root.contains(_markerTip)) root.append(_markerTip!);
    _markerTip!.style
      ..left = '${(x + 12).clamp(0, engine.plotWidth - 220)}px'
      ..top = '${y - 40}px';
  }

  void _showPrompt(double x, double y, List<Point> pts) {
    _prompt?.remove();
    final type = tool;
    final input = h('input', null, {'placeholder': 'Label… (Enter)', 'aria-label': 'Text label'}) as HTMLInputElement;
    final form = h('form.text-prompt', input, null, {
      'submit': (e) {
        e.preventDefault();
        final v = input.value.trim();
        _prompt?.remove();
        _prompt = null;
        if (v.isNotEmpty) _commitDrawing(type, pts, text: v);
      },
    });
    input.addEventListener('keydown', ((KeyboardEvent e) {
      if (e.key == 'Escape') cancelDraft();
      e.stopPropagation();
    }).toJS);
    form.style
      ..left = '${x}px'
      ..top = '${y}px';
    root.append(form);
    _prompt = form;
    input.focus();
  }

  // ---------------------------------------------------------------- selection toolbar

  void _renderFloating() {
    final id = store.selected.value;
    final a = id == null ? null : layer.annotations.where((x) => x.id == id).firstOrNull;
    _floating.isHidden = a == null;
    if (a == null) return;
    final color = a.style['color'].s ?? (a.author == 'agent' ? agentColor : userColor);
    final width = a.style['line_width'].i ?? 2;
    final lineStyle = a.style['line_style'].s ?? 'solid';
    final locked = layer.isLocked(a);
    HTMLElement btn(String ic, String tip, void Function(Event) fn, {bool on = false, Object? content}) =>
        h('button.tb-btn${on ? '.on' : ''}', content ?? icon(ic, size: 16),
            {'type': 'button', 'data-tip': tip, 'aria-label': tip}, {'click': fn});
    replace(_floating, [
      h('span.tb-label', [
        if (a.author == 'agent') h('span.badge-agent', '✦ AI'),
        a.type.replaceAll('_', ' '),
      ]),
      h('span.tb-sep'),
      btn('', 'Color', (e) => _colorMenu(a, e.currentTarget as Element),
          content: h('span.swatch', null, {'style': 'background:$color'})),
      btn('', 'Line width', (e) {
        showMenu([
          for (final w in [1, 2, 3, 4])
            MenuItem('${w}px', checked: w == width, onSelect: () => _style(a, {'line_width': w})),
        ], anchor: e.currentTarget as Element);
      }, content: h('span.tb-text', '${width}px')),
      btn('', 'Line style', (e) {
        showMenu([
          for (final s in ['solid', 'dashed', 'dotted'])
            MenuItem(s, checked: s == lineStyle, onSelect: () => _style(a, {'line_style': s})),
        ], anchor: e.currentTarget as Element);
      }, content: h('span.tb-text', lineStyle == 'solid' ? '——' : lineStyle == 'dashed' ? '– –' : '···')),
      btn('text', 'Edit text', (_) => editDrawingText(a)),
      if (a.type == 'horizontal_line' || a.type == 'horizontal_ray')
        btn('alarm', 'Add alert on this line', (_) => addLineAlert(a)),
      btn(locked ? 'lock' : 'unlock', locked ? 'Unlock' : 'Lock', (_) => _style(a, {'locked': !locked}), on: locked),
      btn('copy', 'Clone', (_) => cloneDrawing(a)),
      btn('eye-off', 'Hide', (_) => updateDrawing(a, {'visible': false}, label: 'Hide drawing')),
      btn('trash', 'Delete (Del)', (_) => deleteDrawing(a)),
      btn('more', 'More', (e) => _drawingMenu(a, anchor: e.currentTarget as Element)),
    ]);
  }

  void _style(Annotation a, Map<String, Object?> patch) {
    final style = {if (a.style.v is Map) ...Map<String, Object?>.from(a.style.v as Map), ...patch};
    updateDrawing(a, {'style': style}, label: 'Style drawing');
  }

  void _colorMenu(Annotation a, Element anchor) {
    late Popover pop;
    pop = Popover.open(
        h('div.swatches', [
          for (final c in drawColors)
            h('button.swatch-btn', h('span.swatch', null, {'style': 'background:$c'}),
                {'type': 'button', 'aria-label': c}, {
              'click': (_) {
                pop.close();
                _style(a, {'color': c});
              },
            }),
        ]),
        anchor: anchor);
  }

  void _drawingMenu(Annotation a, {Element? anchor, double? x, double? y}) {
    final locked = layer.isLocked(a);
    showMenu([
      MenuItem('Edit text…', iconName: 'text', onSelect: () => editDrawingText(a)),
      if (a.type == 'horizontal_line' || a.type == 'horizontal_ray')
        MenuItem('Add alert on line', iconName: 'alarm', onSelect: () => addLineAlert(a)),
      MenuItem('Color', iconName: 'pie', submenu: [
        for (final c in drawColors) MenuItem(c, onSelect: () => _style(a, {'color': c})),
      ]),
      MenuItem(a.interval == null ? 'Show only on $interval' : 'Show on all intervals', iconName: 'clock',
          onSelect: () => updateDrawing(a, {'interval': a.interval == null ? interval : ''}, label: 'Drawing visibility')),
      MenuItem(locked ? 'Unlock' : 'Lock', iconName: locked ? 'unlock' : 'lock', onSelect: () => _style(a, {'locked': !locked})),
      MenuItem('Clone', iconName: 'copy', onSelect: () => cloneDrawing(a)),
      MenuItem('Hide', iconName: 'eye-off', onSelect: () => updateDrawing(a, {'visible': false}, label: 'Hide drawing')),
      const MenuItem.divider(),
      MenuItem('Delete', iconName: 'trash', shortcut: 'Del', danger: true, onSelect: () => deleteDrawing(a)),
    ], anchor: anchor, x: x, y: y);
  }

  // ---------------------------------------------------------------- context menu

  void _contextMenu(double x, double y, double? price, MouseEvent e) {
    final cx = e.clientX.toDouble(), cy = e.clientY.toDouble();
    final id = y < engine.mainHeight ? layer.hit(x, y) : null;
    final a = id == null ? null : store.drawingById(id);
    if (a != null) {
      store.selected.value = a.id;
      _drawingMenu(a, x: cx, y: cy);
      return;
    }
    final p = price == null ? null : double.parse(price.toStringAsFixed(price.abs() >= 1 ? 2 : 4));
    final t = state.chartType.value;
    showMenu([
      if (p != null) ...[
        MenuItem('Add alert at ${fmtPrice(p)}', iconName: 'alarm', onSelect: () => showAlertDialog(symbol, price: p)),
        MenuItem('Horizontal line at ${fmtPrice(p)}', iconName: 'hline',
            onSelect: () => _commitDrawing('horizontal_line', [Point(bars.isEmpty ? null : bars.last.time, p)])),
        MenuItem('Copy price ${fmtPrice(p)}', iconName: 'copy',
            onSelect: () => window.navigator.clipboard.writeText(fmtPrice(p))),
        const MenuItem.divider(),
      ],
      MenuItem('Reset chart view', iconName: 'refresh', shortcut: 'Alt+R', onSelect: resetView),
      MenuItem('Chart type', iconName: 'candles', submenu: [
        for (final (k, label) in chartTypes) MenuItem(label, checked: t == k, onSelect: () => state.chartType.value = k),
      ]),
      MenuItem('Price scale', iconName: 'log', submenu: [
        for (final s in ['linear', 'log', 'percent'])
          MenuItem(s == 'linear' ? 'Regular' : s == 'log' ? 'Logarithmic' : 'Percent',
              checked: state.scale.value == s, onSelect: () => state.scale.value = s),
      ]),
      MenuItem(engine.showVolume ? 'Hide volume' : 'Show volume', iconName: 'chart', onSelect: () {
        engine.showVolume = !engine.showVolume;
        engine.invalidate();
      }),
      MenuItem(engine.showGrid ? 'Hide grid' : 'Show grid', iconName: 'table', onSelect: () {
        engine.showGrid = !engine.showGrid;
        engine.invalidate();
      }),
      const MenuItem.divider(),
      MenuItem(store.drawingsHidden.value ? 'Show drawings' : 'Hide drawings', iconName: 'eye',
          onSelect: () => store.drawingsHidden.value = !store.drawingsHidden.value),
      MenuItem('Remove drawings', iconName: 'trash', submenu: [
        MenuItem('Mine on $symbol', onSelect: () => removeDrawings(symbol, 'user')),
        MenuItem("Agent's on $symbol", onSelect: () => removeDrawings(symbol, 'agent')),
        MenuItem('All on $symbol', danger: true, onSelect: () => removeDrawings(symbol, null)),
      ]),
      const MenuItem.divider(),
      MenuItem('Take screenshot', iconName: 'camera', onSelect: () => screenshot()),
    ], x: cx, y: cy);
  }

  void screenshot() {
    final url = engine.snapshot();
    final a = document.createElement('a') as HTMLAnchorElement;
    a.href = url;
    a.download = '${symbol}_${interval}_${DateTime.now().toIso8601String().substring(0, 19).replaceAll(':', '-')}.png';
    a.click();
    toast('Chart image saved', kind: 'success', ms: 2000);
  }

  // ---------------------------------------------------------------- legend

  void _afterDraw() {
    final geom = engine.paneGeometry;
    // indicator-pane legends sit at the top of their pane
    final children = _paneLegends.children;
    for (var i = 0; i < children.length; i++) {
      final el = children.item(i) as HTMLElement;
      final pane = int.tryParse(el.getAttribute('data-pane') ?? '') ?? 0;
      if (pane < geom.length) el.style.top = '${geom[pane].top + 4}px';
    }
    final scrolled = engine.scrolledBack;
    if (_toLatest.isHidden == scrolled) _toLatest.isHidden = !scrolled;
    _toLatest.style.right = '${engine.axisWidth + 10}px';
  }

  void _renderLegend() {
    final b = bars;
    final idx = _legendIndex ?? (b.isEmpty ? null : b.length - 1);
    final bar = idx == null ? null : engine.bars[idx];
    final raw = idx == null ? null : b[idx];
    final prev = idx != null && idx > 0 ? b[idx - 1] : null;
    final chg = raw != null && prev != null ? (raw.close - prev.close) / prev.close * 100 : null;
    final cls = chg == null ? '' : chg >= 0 ? '.up' : '.down';
    replace(_legend, [
      h('div.legend-row.legend-main', [
        h('button.legend-symbol', symbol, {'type': 'button', 'data-tip': 'Change symbol (or just start typing)'},
            {'click': (_) => window.dispatchEvent(CustomEvent('open-symbol-search'))}),
        h('span.legend-interval', intervalLabel(interval)),
        if (bar != null)
          h('span.ohlc', [
            h('span.muted', 'O'), h('b$cls', fmt(bar.open)),
            h('span.muted', 'H'), h('b$cls', fmt(bar.high)),
            h('span.muted', 'L'), h('b$cls', fmt(bar.low)),
            h('span.muted', 'C'), h('b$cls', fmt(bar.close)),
            if (chg != null) h('b$cls', '${signed(raw!.close - prev!.close)} (${signed(chg)}%)'),
            h('span.muted', 'Vol'), h('b', compact(raw!.volume)),
          ]),
        if (_loading) h('span.muted', 'loading…'),
      ]),
      for (final (i, sym) in state.compares.value.indexed)
        h('div.legend-row.small', [
          h('span.dot', null, {'style': 'background:${compareColors[i % compareColors.length]}'}),
          h('span', sym),
          h('span.muted', _compareValue(sym, idx)),
          h('button.legend-x', icon('x', size: 12), {'type': 'button', 'aria-label': 'Remove $sym'}, {
            'click': (_) => state.compares.value = [...state.compares.value]..remove(sym),
          }),
        ]),
      for (final r in _legendRows.where((r) => r.pane == 0)) _legendRow(r, idx),
    ]);
    replace(_paneLegends, [
      for (final r in _legendRows.where((r) => r.pane > 0))
        h('div.pane-legend', _legendRow(r, idx), {'data-pane': '${r.pane}'}),
    ]);
    _afterDraw();
  }

  String _compareValue(String sym, int? idx) {
    final data = _compareData[sym];
    if (data == null || idx == null || idx >= data.length || data[idx] == null) return '';
    final (from, _) = engine.visibleRange;
    double? base;
    for (var i = math.max(0, from.floor()); i < data.length; i++) {
      if (data[i] != null) {
        base = data[i];
        break;
      }
    }
    final v = data[idx]!;
    return '${fmt(v)}${base != null ? '  ${pct((v / base - 1) * 100)}' : ''}';
  }

  HTMLElement _legendRow(_LegendRow r, int? idx) {
    final hidden = state.hidden.value.contains(r.key);
    return h('div.legend-row.small.ind-row${hidden ? '.dim' : ''}', [
      h('span.ind-name', r.label),
      for (final (_, color, values) in r.outputs)
        h('span.ind-val', fmt(idx != null && idx < values.length ? values[idx] : null), {'style': 'color:$color'}),
      h('span.ind-actions', [
        h('button.legend-x', icon(hidden ? 'eye-off' : 'eye', size: 13), {'type': 'button', 'aria-label': 'Toggle ${r.label}'}, {
          'click': (_) {
            final hs = [...state.hidden.value];
            hs.contains(r.key) ? hs.remove(r.key) : hs.add(r.key);
            state.hidden.value = hs;
          },
        }),
        h('button.legend-x', icon('settings', size: 13), {'type': 'button', 'aria-label': 'Settings for ${r.label}'},
            {'click': (_) => editIndicator(state, r.key)}),
        h('button.legend-x', icon('x', size: 13), {'type': 'button', 'aria-label': 'Remove ${r.label}'}, {
          'click': (_) => state.indicators.value = [
                for (final x in state.indicators.value)
                  if (indicatorKey(x) != r.key) x
              ],
        }),
      ]),
    ]);
  }

  // test hook
  JSObject hook() {
    final o = JSObject();
    o['visibleRange'] = (() => [engine.visibleRange.$1.toJS, engine.visibleRange.$2.toJS].toJS).toJS;
    o['drawn'] = (() => layer.annotations.where((a) => a.visible).length.toJS).toJS;
    o['bars'] = (() => bars.length.toJS).toJS;
    o['series'] = (() => engine.series.length.toJS).toJS;
    o['panes'] = (() => engine.paneCount.toJS).toJS;
    o['compares'] = (() => engine.compares.length.toJS).toJS;
    o['priceLines'] = (() => engine.priceLines.length.toJS).toJS;
    o['chartType'] = (() => engine.chartType.name.toJS).toJS;
    o['scale'] = (() => engine.scaleMode.name.toJS).toJS;
    o['xOfTime'] = ((int t) => layer.x(t)?.toJS).toJS;
    o['yOfPrice'] = ((double p) => layer.y(p)?.toJS).toJS;
    o['handles'] = (() {
      final id = store.selected.value;
      final a = id == null ? null : layer.annotations.where((x) => x.id == id).firstOrNull;
      if (a == null) return <JSAny>[].toJS;
      return [for (final (_, p) in layer.handles(a)) [p.x.toJS, p.y.toJS].toJS].toJS;
    }).toJS;
    return o;
  }
}

const chartTypes = [
  ('candles', 'Candles'), ('hollow', 'Hollow candles'), ('bars', 'Bars'), ('line', 'Line'), ('area', 'Area'),
  ('heikin', 'Heikin Ashi'),
];

String intervalLabel(String iv) => switch (iv) {
      '1m' => '1m', '5m' => '5m', '15m' => '15m', '30m' => '30m', '1h' => '1H', '1d' => '1D', '1wk' => '1W',
      '1mo' => '1M', _ => iv,
    };

// ------------------------------------------------------------------------------- shared dialogs

Future<void> editDrawingText(Annotation a) async {
  final text = await promptDialog('Drawing text', label: 'Label shown with the drawing', initial: a.text ?? '');
  if (text == null) return;
  await updateDrawing(a, {'text': text}, label: 'Edit text');
}

Future<void> addLineAlert(Annotation a) async {
  final price = a.points.isEmpty ? null : a.points[0].price;
  if (price == null) return;
  await showAlertDialog(a.symbol, price: price, annotationId: a.id, kind: 'crosses');
}

Future<void> removeDrawings(String symbol, String? author) async {
  final who = author == null ? 'all' : author == 'agent' ? "the agent's" : 'your';
  if (!await confirmDialog('Remove drawings', 'Remove $who drawings on $symbol?', confirm: 'Remove', danger: true)) {
    return;
  }
  try {
    final n = await api.deleteAnnotations(symbol, author: author);
    await store.loadDrawings(symbol, force: true);
    toast('Removed $n drawing${n == 1 ? '' : 's'}', kind: 'success');
  } catch (e) {
    toast('$e', kind: 'error');
  }
}

/// Create-alert dialog (price above/below/crossing, daily change, or indicator condition).
Future<void> showAlertDialog(String symbol, {double? price, String? annotationId, String kind = 'crosses'}) async {
  final kindSel = h('select.input', [
    for (final (k, label) in [
      ('crosses', 'Price crosses'), ('above', 'Price at or above'), ('below', 'Price at or below'),
      ('change_above', 'Day change ≥ (%)'), ('change_below', 'Day change ≤ (%)'), ('expression', 'Indicator condition'),
    ])
      h('option', label, {'value': k}),
  ]) as HTMLSelectElement;
  kindSel.value = kind;
  final valueIn = h('input.input', null, {'type': 'number', 'step': 'any', 'value': price == null ? '' : fmtPrice(price)})
      as HTMLInputElement;
  final exprIn = h('input.input', null, {'placeholder': 'e.g. crosses_above(close, sma(50))  or  rsi(14) < 30'})
      as HTMLInputElement;
  final msgIn = h('input.input', null, {'placeholder': 'Message (optional)'}) as HTMLInputElement;
  final repeat = h('input', null, {'type': 'checkbox'}) as HTMLInputElement;
  final valueRow = h('label.field-col', [h('span', 'Value'), valueIn]);
  final exprRow = h('label.field-col', [h('span', 'Condition (daily bars)'), exprIn]);
  void sync() {
    final isExpr = kindSel.value == 'expression';
    valueRow.isHidden = isExpr;
    exprRow.isHidden = !isExpr;
  }

  kindSel.addEventListener('change', ((Event _) => sync()).toJS);
  sync();
  Dialog.open('Create alert on $symbol', h('div.form-grid', [
    h('label.field-col', [h('span', 'Condition'), kindSel]),
    valueRow,
    exprRow,
    h('label.field-col', [h('span', 'Message'), msgIn]),
    h('label.check-row', [repeat, ' Repeat (re-arm after each trigger)']),
    if (annotationId != null) h('div.muted.small', 'Linked to the drawing: moving the line moves the alert.'),
  ]), actions: [
    const DialogAction('Cancel'),
    DialogAction('Create', primary: true, onPressed: () async {
      final isExpr = kindSel.value == 'expression';
      final v = double.tryParse(valueIn.value);
      if (!isExpr && v == null) {
        toast('Enter a value', kind: 'warn');
        return false;
      }
      try {
        await request('/api/alerts', method: 'POST', body: {
          'symbol': symbol, 'kind': kindSel.value, if (!isExpr) 'value': v, if (isExpr) 'expression': exprIn.value,
          'message': msgIn.value.isEmpty ? null : msgIn.value, 'repeat': repeat.checked, 'annotation_id': annotationId,
        });
        await store.loadAlerts();
        toast('Alert created', kind: 'success', ms: 2500);
        return true;
      } catch (e) {
        toast('$e', kind: 'error');
        return false;
      }
    }),
  ]);
}

/// Indicator settings dialog: edit the parameters of one indicator on a chart.
Future<void> editIndicator(ChartState chart, String key) async {
  final refs = chart.indicators.value;
  final i = refs.indexWhere((r) => indicatorKey(r) == key);
  if (i < 0) return;
  final ref = refs[i];
  final spec = store.specs.value.where((s) => s.name == ref.name).firstOrNull;
  if (spec == null) return;
  final inputs = <String, HTMLInputElement>{};
  Dialog.open('${spec.title} settings', h('div.form-grid', [
    h('div.muted.small', spec.description),
    for (final e in spec.params.entries)
      h('label.field-col', [
        h('span', e.key),
        inputs[e.key] = h('input.input', null, {'value': '${ref.params[e.key] ?? e.value ?? ''}'}) as HTMLInputElement,
      ]),
  ]), actions: [
    DialogAction('Defaults', onPressed: () {
      for (final e in spec.params.entries) {
        inputs[e.key]!.value = '${e.value ?? ''}';
      }
      return false;
    }),
    const DialogAction('Cancel'),
    DialogAction('Apply', primary: true, onPressed: () {
      final p = <String, Object>{};
      for (final e in spec.params.entries) {
        final raw = inputs[e.key]!.value.trim();
        if (raw.isEmpty || raw == '${e.value}') continue;
        p[e.key] = e.value is num ? (num.tryParse(raw) ?? e.value!) : raw;
      }
      final next = [...refs];
      next[i] = IndicatorRef(ref.name, p);
      chart.indicators.value = next;
      return true;
    }),
  ]);
}
