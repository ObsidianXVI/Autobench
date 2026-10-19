// Small canvas sparklines / mini area charts (screener previews, market boards, macro cards).
import 'dart:js_interop';
import 'dart:math' as math;

import 'package:web/web.dart';

import '../../core/models.dart';
import '../api.dart';

/// Draws [values] as a line (and soft area) filling the canvas. Colors by first vs last value
/// unless [color] is given. Optional [baseline] draws a dotted reference line.
void drawSpark(HTMLCanvasElement canvas, List<double?> values,
    {String? color, double? baseline, bool area = true, double lineWidth = 1.5}) {
  final dpr = window.devicePixelRatio;
  final r = canvas.getBoundingClientRect();
  final w = r.width > 0 ? r.width : (double.tryParse(canvas.getAttribute('width') ?? '') ?? 100);
  final hgt = r.height > 0 ? r.height : (double.tryParse(canvas.getAttribute('height') ?? '') ?? 30);
  canvas.width = (w * dpr).round();
  canvas.height = (hgt * dpr).round();
  final ctx = canvas.getContext('2d') as CanvasRenderingContext2D;
  ctx.setTransform(dpr.toJS, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, hgt);
  final pts = [for (final v in values) if (v != null && v.isFinite) v];
  if (pts.length < 2) return;
  var lo = pts.reduce(math.min), hi = pts.reduce(math.max);
  if (baseline != null) {
    lo = math.min(lo, baseline);
    hi = math.max(hi, baseline);
  }
  if (hi - lo < 1e-12) {
    hi += 1;
    lo -= 1;
  }
  final pad = 2.0;
  double x(int i) => pad + i / (values.length - 1) * (w - pad * 2);
  double y(double v) => pad + (hi - v) / (hi - lo) * (hgt - pad * 2);
  final styles = window.getComputedStyle(document.documentElement!);
  final up = styles.getPropertyValue('--up').trim().isEmpty ? '#22c55e' : styles.getPropertyValue('--up').trim();
  final down = styles.getPropertyValue('--down').trim().isEmpty ? '#ef4444' : styles.getPropertyValue('--down').trim();
  final c = color ?? (pts.last >= pts.first ? up : down);
  if (baseline != null) {
    ctx.strokeStyle = 'rgba(148,163,184,0.5)'.toJS;
    ctx.setLineDash([2.toJS, 3.toJS].toJS);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, y(baseline));
    ctx.lineTo(w, y(baseline));
    ctx.stroke();
    ctx.setLineDash(<JSNumber>[].toJS);
  }
  ctx.beginPath();
  var started = false;
  var firstX = 0.0, lastX = 0.0;
  for (var i = 0; i < values.length; i++) {
    final v = values[i];
    if (v == null || !v.isFinite) continue;
    if (!started) {
      ctx.moveTo(x(i), y(v));
      firstX = x(i);
      started = true;
    } else {
      ctx.lineTo(x(i), y(v));
    }
    lastX = x(i);
  }
  ctx.strokeStyle = c.toJS;
  ctx.lineWidth = lineWidth;
  ctx.lineJoin = 'round';
  ctx.stroke();
  if (area) {
    ctx.lineTo(lastX, hgt);
    ctx.lineTo(firstX, hgt);
    ctx.closePath();
    final grad = ctx.createLinearGradient(0, 0, 0, hgt);
    grad.addColorStop(0, _alpha(c, 0.28));
    grad.addColorStop(1, _alpha(c, 0.0));
    ctx.fillStyle = grad;
    ctx.fill();
  }
}

String _alpha(String c, double a) {
  final m = RegExp(r'^#([0-9a-fA-F]{6})$').firstMatch(c.trim());
  if (m == null) return c;
  final v = int.parse(m.group(1)!, radix: 16);
  return 'rgba(${(v >> 16) & 255},${(v >> 8) & 255},${v & 255},$a)';
}

/// Daily closes for previews, cached per symbol for the session.
final Map<String, Future<List<Bar>>> _barCache = {};

Future<List<Bar>> previewBars(String symbol, {String lookback = '6mo'}) =>
    _barCache.putIfAbsent('$symbol|$lookback', () => api.barsRange(symbol, '1d', lookback).catchError((Object _) {
          _barCache.remove('$symbol|$lookback');
          return <Bar>[];
        }));
