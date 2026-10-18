// The chart panel: bars, indicator series, drawings, drawing tools with an OHLC magnet, legend.
import 'dart:convert';
import 'dart:js_interop';
import 'dart:js_interop_unsafe';
import 'dart:math' as math;

import 'package:web/web.dart';

import '../core/format.dart';
import '../core/models.dart';
import '../core/time.dart';
import '../ui/api.dart';
import '../ui/dom.dart';
import 'annotations.dart';
import 'engine.dart';

const _palette = ['#f59e0b', '#38bdf8', '#e879f9', '#34d399', '#f87171', '#a3e635', '#fb923c', '#60a5fa'];
const _hiddenOverlayOutputs = {'bandwidth', 'percent_b', 'direction', 'slope_pct', 'r2'};
const _histogramOutputs = {'histogram', 'momentum', 'squeeze_on'};

/// Points a tool needs before it is committed (default 2).
const toolPoints = {
  'horizontal_line': 1, 'horizontal_ray': 1, 'vertical_line': 1, 'text': 1, 'trendline': 2, 'ray': 2,
  'rectangle': 2, 'fib_retracement': 2, 'arrow': 2, 'price_range': 2, 'long_position': 2, 'short_position': 2,
};

class _LegendSeries {
  final String label, color;
  final List<double?> values;
  _LegendSeries(this.label, this.color, this.values);
}

class ChartView {
  final HTMLElement root = h('div.chart-wrap');
  late final HTMLElement _box = h('div.chart');
  late final HTMLElement _legend = h('div.legend');
  late final ChartEngine engine;
  late final AnnotationLayer layer;

  final void Function(String? id) onSelect;
  final void Function(String type, List<Point> points, {String? text}) onDraw;
  final void Function(String message) onError;
  final void Function(String source) onLoaded;

  String symbol = '', interval = '1d';
  List<IndicatorRef> _indicators = const [];
  String _indicatorKey = '';
  List<Annotation> _annotations = const [];
  int? _fromTime, _toTime;
  String tool = 'select';
  bool _loading = false;
  int _loadSeq = 0, _indSeq = 0;
  int _futureBars = 0;
  List<_LegendSeries> _legendSeries = [];
  int? _legendIndex;
  HTMLElement? _prompt;

  ChartView({required this.onSelect, required this.onDraw, required this.onError, required this.onLoaded}) {
    root.append(_box);
    root.append(_legend);
    engine = ChartEngine(_box);
    layer = AnnotationLayer(engine);
    engine.onClick = _click;
    engine.onCrosshair = _move;
    window.addEventListener('keydown', ((KeyboardEvent e) {
      if (e.key == 'Escape') cancelDraft();
    }).toJS);
    window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', ((Event _) => engine.refreshTheme()).toJS);
    _exposeHook();
  }

  /// window.__autobench: lets end-to-end tests map times/prices to pixels and inspect the view.
  void _exposeHook() {
    final hook = JSObject();
    hook['visibleRange'] = (() => [engine.visibleRange.$1.toJS, engine.visibleRange.$2.toJS].toJS).toJS;
    hook['drawn'] = (() => layer.annotations.where((a) => a.visible).length.toJS).toJS;
    hook['bars'] = (() => bars.length.toJS).toJS;
    hook['redraw'] = (() => engine.drawNow()).toJS;
    hook['series'] = (() => engine.series.length.toJS).toJS;
    hook['panes'] = (() => engine.paneCount.toJS).toJS;
    hook['xOfTime'] = ((int t) => layer.x(t)?.toJS).toJS;
    hook['yOfPrice'] = ((double p) => layer.y(p)?.toJS).toJS;
    globalContext['__autobench'] = hook;
  }

  bool get intraday => isIntraday(interval);
  List<Bar> get bars => engine.bars;

  // ---------------------------------------------------------------- inputs

  void show(String symbol, String interval) {
    if (symbol == this.symbol && interval == this.interval) return;
    final symbolChanged = symbol != this.symbol;
    this.symbol = symbol;
    this.interval = interval;
    if (symbolChanged) cancelDraft();
    _load();
  }

  void setIndicators(List<IndicatorRef> refs) {
    final key = jsonEncode([for (final r in refs) r.toJson()]);
    if (key == _indicatorKey) return;
    _indicatorKey = key;
    _indicators = refs;
    if (bars.isNotEmpty) _loadIndicators();
  }

  void setAnnotations(List<Annotation> all, String? selectedId) {
    _annotations = all;
    layer.selectedId = selectedId;
    _applyAnnotations();
  }

  /// Visible range requested by the shared view (agent or another client).
  void setRange(int? from, int? to) {
    _fromTime = from;
    _toTime = to;
    _applyRange();
  }

  set currentTool(String t) {
    if (t == tool) return;
    tool = t;
    cancelDraft();
  }

  void cancelDraft() {
    layer.draft = null;
    layer.update();
    _prompt?.remove();
    _prompt = null;
  }

  // ---------------------------------------------------------------- loading

  Future<void> _load() async {
    final seq = ++_loadSeq;
    _loading = true;
    _renderLegend();
    try {
      final res = await api.bars(symbol, interval);
      if (seq != _loadSeq) return;
      final b = res.bars;
      engine.setBars(b, intraday: intraday);
      _legendSeries = [];
      layer.mapper.set([for (final x in b) x.time], intervalSeconds[interval] ?? 86400, intraday);
      _applyAnnotations();
      if (_fromTime == null) {
        final show = intraday ? 160 : interval == '1d' ? 180 : 150;
        engine.setVisibleRange(math.max(0, b.length - show).toDouble(), b.length + math.max(6, _futureBars + 4).toDouble());
      } else {
        _applyRange();
      }
      _loading = false;
      _renderLegend();
      onLoaded(res.source);
      if (_indicators.isNotEmpty) await _loadIndicators();
    } catch (e) {
      if (seq == _loadSeq) onError('Bars: $e');
    } finally {
      if (seq == _loadSeq) {
        _loading = false;
        _renderLegend();
      }
    }
  }

  Future<void> _loadIndicators() async {
    final seq = ++_indSeq;
    if (_indicators.isEmpty) {
      engine.setSeries([]);
      _legendSeries = [];
      _renderLegend();
      return;
    }
    try {
      final res = await api.indicators(symbol, interval, _indicators);
      if (seq != _indSeq) return;
      // align by time to the loaded bars
      final index = {for (var i = 0; i < bars.length; i++) bars[i].time: i};
      List<double?> align(List<double?> vals) {
        final out = List<double?>.filled(bars.length, null);
        for (var i = 0; i < res.time.length && i < vals.length; i++) {
          final k = index[res.time[i]];
          if (k != null) out[k] = vals[i];
        }
        return out;
      }

      final series = <Series>[];
      final legend = <_LegendSeries>[];
      var pane = 0, colorIdx = 0;
      for (final r in res.results) {
        final target = r.overlay ? 0 : ++pane;
        var first = true;
        for (final out in r.outputs) {
          if (r.overlay && _hiddenOverlayOutputs.contains(out)) continue;
          if (!r.overlay && out == 'direction') continue;
          final vals = align(r.values[out] ?? const []);
          final color = _palette[colorIdx++ % _palette.length];
          final hist = !r.overlay && _histogramOutputs.contains(out);
          series.add(Series(
            pane: target,
            kind: hist ? SeriesKind.histogram : r.name == 'psar' ? SeriesKind.dots : SeriesKind.line,
            values: vals,
            color: color,
            width: r.overlay ? 1 : 2,
            dashed: out == 'middle' || out == 'signal',
            lastValueLabel: !r.overlay && !hist,
            levels: first && !r.overlay ? r.levels : const [],
          ));
          first = false;
          legend.add(_LegendSeries('${r.label}${r.outputs.length > 1 ? '.$out' : ''}', color, vals));
        }
      }
      engine.setSeries(series);
      _legendSeries = legend;
      _renderLegend();
    } catch (e) {
      if (seq == _indSeq) onError('Indicators: $e');
    }
  }

  void _applyAnnotations() {
    final shown = [for (final a in _annotations) if (a.interval == null || a.interval == interval) a];
    layer.annotations = shown;
    // leave room on the right for drawings that extend into the future (trade plans, event lines)
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
  }

  void _applyRange() {
    final from = _fromTime;
    if (from == null || bars.isEmpty) return;
    final lf = layer.mapper.logical(from);
    final to = _toTime;
    final lt = to != null ? layer.mapper.logical(to) : bars.length - 1 + math.max(8, _futureBars + 4).toDouble();
    if (lf != null && lt != null) engine.setVisibleRange(lf, lt);
  }

  // ---------------------------------------------------------------- pointer

  Point? _toPoint(double x, double y) {
    final lg = engine.xToLogical(x);
    final idx = lg.round();
    var time = layer.mapper.time(lg);
    var price = engine.yToPrice(y);
    // magnet: snap to the bar's time and to its OHLC when the cursor is within 8px
    if (idx >= 0 && idx < bars.length) {
      final bar = bars[idx];
      time = bar.time;
      var bestD = 8.0;
      for (final v in [bar.open, bar.high, bar.low, bar.close]) {
        final vy = engine.priceToY(v);
        if (vy != null && (vy - y).abs() < bestD) {
          bestD = (vy - y).abs();
          price = v;
        }
      }
    }
    if (price == null || time == null) return null;
    return Point(time, double.parse(price.toStringAsFixed(4)));
  }

  void _click(double x, double y) {
    if (tool == 'select') {
      onSelect(layer.hit(x, y));
      return;
    }
    final pt = _toPoint(x, y);
    if (pt == null) return;
    if (tool == 'text') {
      _showPrompt(x, y, pt);
      return;
    }
    final need = toolPoints[tool] ?? 2;
    final draft = layer.draft;
    final pts = [...?draft?.points.sublist(0, draft.points.length - 1), pt];
    if (pts.length >= need) {
      layer.draft = null;
      layer.update();
      onDraw(tool, pts);
    } else {
      layer.draft = Draft(tool, [...pts, pt]);
      layer.update();
    }
  }

  void _move(double? x, double? y, double? logical) {
    if (x == null || y == null || logical == null) {
      _legendIndex = null;
      _renderLegend();
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
    final hov = tool == 'select' && y < engine.mainHeight ? layer.hit(x, y) : null;
    if (hov != layer.hoverId) {
      layer.hoverId = hov;
      layer.update();
    }
    engine.cursor = tool != 'select' ? 'crosshair' : hov != null ? 'pointer' : 'default';
    final idx = logical.round();
    _legendIndex = idx >= 0 && idx < bars.length ? idx : null;
    _renderLegend();
  }

  void _showPrompt(double x, double y, Point pt) {
    _prompt?.remove();
    final input = h('input', null, {'name': 't', 'placeholder': 'Label… (Enter)', 'aria-label': 'Text label'})
        as HTMLInputElement;
    final form = h('form.text-prompt', input, null, {
      'submit': (e) {
        e.preventDefault();
        final v = input.value.trim();
        _prompt?.remove();
        _prompt = null;
        if (v.isNotEmpty) onDraw('text', [pt], text: v);
      },
    });
    input.addEventListener('keydown', ((KeyboardEvent e) {
      if (e.key == 'Escape') cancelDraft();
    }).toJS);
    form.style
      ..left = '${x}px'
      ..top = '${y}px';
    root.append(form);
    _prompt = form;
    input.focus();
  }

  // ---------------------------------------------------------------- legend

  void _renderLegend() {
    final b = bars;
    final idx = _legendIndex ?? (b.isEmpty ? null : b.length - 1);
    final bar = idx == null ? null : b[idx];
    final prev = idx != null && idx > 0 ? b[idx - 1] : null;
    final chg = bar != null && prev != null ? (bar.close - prev.close) / prev.close * 100 : null;
    replace(_legend, [
      h('div.legend-row', [
        h('span.legend-symbol', symbol),
        h('span.muted', interval),
        if (bar != null)
          h('span.ohlc', [
            '${formatBarTime(bar.time, intraday)} O ',
            h('b', fmt(bar.open)),
            ' H ',
            h('b', fmt(bar.high)),
            ' L ',
            h('b', fmt(bar.low)),
            ' C ',
            h('b', fmt(bar.close)),
            if (chg != null) h(chg >= 0 ? 'b.up' : 'b.down', ' ${signed(chg)}%'),
            h('span.muted', ' Vol ${compact(bar.volume)}'),
          ]),
        if (_loading) h('span.muted', 'loading…'),
      ]),
      for (final s in _legendSeries)
        h('div.legend-row.small', [
          h('span.dot', null, {'style': 'background:${s.color}'}),
          h('span.muted', s.label),
          ' ',
          h('span', fmt(idx != null && idx < s.values.length ? s.values[idx] : null)),
        ]),
    ]);
  }
}
