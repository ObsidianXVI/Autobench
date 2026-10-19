// Interactive line chart for the Portfolio page: dated series on a time axis with labelled value and date
// axes, a hover crosshair with a tooltip (also from the keyboard), and drag-to-zoom into a date range.
// Lines are SVG stretched to the plot; labels, crosshair and tooltip are HTML so text never distorts.
import 'dart:js_interop';
import 'dart:math' as math;

import 'package:web/web.dart';

import '../../core/chart_ticks.dart';
import '../../core/ticks.dart';
import '../dom.dart';

const _svgNs = 'http://www.w3.org/2000/svg';

Element svgEl(String tag, Map<String, String> attrs) {
  final el = document.createElementNS(_svgNs, tag);
  attrs.forEach((k, v) => el.setAttribute(k, v));
  return el;
}

/// One line: [values] align with the chart's dates (null = no value that day).
class ChartLine {
  final String label;
  final String cls;
  final List<double?> values;
  const ChartLine(this.label, this.cls, this.values);
}

/// A filled band between two lines (e.g. account value vs money put in = the gain).
class ChartBand {
  final int upper, lower;
  const ChartBand(this.upper, this.lower);
}

/// The chart. [tip] builds the tooltip rows for point i; [onRange] is called after a drag across the plot.
class PerfChart {
  final List<String> dates;
  final List<ChartLine> lines;
  final ChartBand? band;
  final String Function(double v) axisLabel;
  final List<(String, String, String?)> Function(int i) tip; // (label, value, class)
  final void Function(String start, String end)? onRange;
  final void Function(int? i)? onHover;
  final bool includeZero;
  final double height;

  late final HTMLElement root;
  late final HTMLElement _plot, _cross, _tip, _sel;
  late final List<HTMLElement> _dots;
  late final List<double> _t; // x of each point, 0..1
  late double _lo, _hi;
  int? _at;
  double? _dragFrom;

  PerfChart({
    required this.dates,
    required this.lines,
    required this.axisLabel,
    required this.tip,
    this.band,
    this.onRange,
    this.onHover,
    this.includeZero = false,
    this.height = 220,
  }) {
    final t0 = dayUtc(dates.first), t1 = dayUtc(dates.last);
    final span = math.max(1, t1.difference(t0).inDays);
    _t = [for (final d in dates) dayUtc(d).difference(t0).inDays / span];
    final all = [for (final l in lines) ...l.values.whereType<double>()];
    _lo = all.isEmpty ? 0 : all.reduce(math.min);
    _hi = all.isEmpty ? 1 : all.reduce(math.max);
    if (includeZero) {
      _lo = math.min(_lo, 0);
      _hi = math.max(_hi, 0);
    }
    final pad = (_hi - _lo) * 0.08;
    _lo -= _lo >= 0 && _lo - pad < 0 && !includeZero ? _lo : pad;
    _hi += pad;
    if (_hi - _lo < 1e-9) {
      _hi += 1;
      _lo -= 1;
    }
    _build(t0, t1);
  }

  double _y(double v) => (_hi - v) / (_hi - _lo); // 0 top .. 1 bottom

  String _pt(int i, double v) => '${(_t[i] * 1000).toStringAsFixed(2)},${(_y(v) * 1000).toStringAsFixed(2)}';

  void _build(DateTime t0, DateTime t1) {
    final svg = svgEl('svg', {'viewBox': '0 0 1000 1000', 'preserveAspectRatio': 'none', 'class': 'pc-svg'});
    final ticks = niceTicks(_lo, _hi, math.max(3, (height / 34).round())).values;
    final yAxis = h('div.pc-yaxis');
    for (final v in ticks) {
      final y = _y(v) * 1000;
      svg.append(svgEl('line', {'x1': '0', 'x2': '1000', 'y1': '$y', 'y2': '$y', 'class': v == 0 ? 'pc-grid zero' : 'pc-grid'}));
      yAxis.append(h('span', axisLabel(v), {'style': 'top:${(_y(v) * 100).toStringAsFixed(2)}%'}));
    }
    final xAxis = h('div.pc-xaxis');
    final span = math.max(1, t1.difference(t0).inDays);
    for (final (d, label) in dateTicks(t0, t1)) {
      final x = d.difference(t0).inDays / span;
      svg.append(svgEl('line', {'x1': '${x * 1000}', 'x2': '${x * 1000}', 'y1': '0', 'y2': '1000', 'class': 'pc-grid v'}));
      xAxis.append(h('span', label, {'style': 'left:${(x * 100).toStringAsFixed(2)}%'}));
    }
    final b = band;
    if (b != null) {
      final up = lines[b.upper].values, low = lines[b.lower].values;
      final idx = [for (var i = 0; i < dates.length; i++) if (up[i] != null && low[i] != null) i];
      if (idx.length > 1) {
        // split where the band changes sign, so above/below are coloured separately
        var run = <int>[idx.first];
        void flush() {
          if (run.length < 2) return;
          final pos = run.map((i) => up[i]! - low[i]!).fold(0.0, (a, v) => a + v) >= 0;
          final pts = [for (final i in run) _pt(i, up[i]!), for (final i in run.reversed) _pt(i, low[i]!)].join(' ');
          svg.append(svgEl('polygon', {'points': pts, 'class': 'pf-gain ${pos ? 'up' : 'down'}'}));
        }

        for (final i in idx.skip(1)) {
          final prev = run.last;
          final flip = (up[prev]! - low[prev]!).sign * (up[i]! - low[i]!).sign < 0;
          run.add(i);
          if (flip) {
            flush();
            run = [i];
          }
        }
        flush();
      }
    }
    for (final l in lines) {
      final pts = [for (var i = 0; i < dates.length; i++) if (l.values[i] != null) _pt(i, l.values[i]!)];
      svg.append(svgEl('polyline', {'points': pts.join(' '), 'class': l.cls}));
    }
    _cross = h('div.pc-cross', null, {'hidden': ''});
    _dots = [for (final l in lines) h('div.pc-dot.${l.cls}', null, {'hidden': ''})];
    _tip = h('div.pc-tip', null, {'hidden': ''});
    _sel = h('div.pc-sel', null, {'hidden': ''});
    _plot = h('div.pc-plot', [svg, _sel, _cross, ..._dots, _tip], {
      'tabindex': '0',
      'role': 'img',
      'aria-label': '${lines.map((l) => l.label).join(', ')} from ${dates.first} to ${dates.last}. Arrow keys move the cursor.',
      'style': 'height:${height}px',
    });
    root = h('div.pc', [_plot, yAxis, xAxis, if (onRange != null) h('div.pc-hint.muted.tiny', 'Drag across the chart to zoom in')]);
    _wire();
  }

  void _wire() {
    void on(String type, void Function(Event e) fn) => _plot.addEventListener(type, fn.toJS);
    double frac(Event e) {
      final r = _plot.getBoundingClientRect();
      return (clientXY(e).$1 - r.left) / math.max(1, r.width);
    }

    on('pointermove', (e) {
      final f = frac(e).clamp(0.0, 1.0);
      final from = _dragFrom;
      if (from != null) {
        final a = math.min(from, f), b = math.max(from, f);
        _sel
          ..removeAttribute('hidden')
          ..style.left = '${a * 100}%'
          ..style.width = '${(b - a) * 100}%';
      }
      _setAt(_nearest(f), notify: true);
    });
    on('pointerleave', (_) {
      if (_dragFrom == null) _setAt(null, notify: true);
    });
    on('pointerdown', (e) {
      if (onRange == null || (e as MouseEvent).button != 0) return;
      _dragFrom = frac(e).clamp(0.0, 1.0);
      _plot.setPointerCapture((e as PointerEvent).pointerId);
    });
    on('pointerup', (e) {
      final from = _dragFrom;
      _dragFrom = null;
      _sel.setAttribute('hidden', '');
      if (from == null) return;
      final to = frac(e).clamp(0.0, 1.0);
      final r = _plot.getBoundingClientRect();
      if ((to - from).abs() * r.width < 8) return;
      final i0 = _nearest(math.min(from, to)), i1 = _nearest(math.max(from, to));
      if (i1 > i0) onRange!(dates[i0], dates[i1]);
    });
    on('keydown', (e) {
      final k = (e as KeyboardEvent).key;
      final step = e.shiftKey ? 10 : 1;
      final i = switch (k) {
        'ArrowLeft' => math.max(0, (_at ?? dates.length) - step),
        'ArrowRight' => math.min(dates.length - 1, (_at ?? -1) + step),
        'Home' => 0,
        'End' => dates.length - 1,
        'Escape' => -1,
        _ => null,
      };
      if (i == null) return;
      e.preventDefault();
      _setAt(i < 0 ? null : i, notify: true);
    });
    on('blur', (_) => _setAt(null, notify: true));
  }

  int _nearest(double f) {
    var lo = 0, hi = _t.length - 1;
    while (hi - lo > 1) {
      final mid = (lo + hi) >> 1;
      if (_t[mid] < f) {
        lo = mid;
      } else {
        hi = mid;
      }
    }
    return (f - _t[lo]).abs() <= (_t[hi] - f).abs() ? lo : hi;
  }

  /// Shows the cursor at point [i] (null hides it); used to keep several charts in step.
  void showAt(int? i) => _setAt(i, notify: false);

  void _setAt(int? i, {required bool notify}) {
    if (i == _at) return;
    _at = i;
    if (notify) onHover?.call(i);
    if (i == null) {
      _cross.setAttribute('hidden', '');
      _tip.setAttribute('hidden', '');
      for (final d in _dots) {
        d.setAttribute('hidden', '');
      }
      return;
    }
    final x = _t[i] * 100;
    _cross
      ..removeAttribute('hidden')
      ..style.left = '$x%';
    for (var k = 0; k < lines.length; k++) {
      final v = lines[k].values[i];
      if (v == null) {
        _dots[k].setAttribute('hidden', '');
      } else {
        _dots[k]
          ..removeAttribute('hidden')
          ..style.left = '$x%'
          ..style.top = '${_y(v) * 100}%';
      }
    }
    replace(_tip, [
      h('div.pc-tip-date', longDate(dates[i])),
      for (final (l, v, c) in tip(i)) h('div.pc-tip-row', [h('span.muted', l), h('b.num${c == null ? '' : '.$c'}', v)]),
    ]);
    _tip.removeAttribute('hidden');
    // keep the tooltip inside the plot: to the right of the cursor, or to the left past the middle
    if (_t[i] > 0.55) {
      _tip.style
        ..left = ''
        ..right = '${100 - x}%';
    } else {
      _tip.style
        ..right = ''
        ..left = '$x%';
    }
  }
}
