// Trading on the chart (TradingView-style): the position line (average cost, size, open P&L, close
// button), working order lines (draggable to a new price, × to cancel, P&L at stop/target), pending
// agent proposals (dashed, click to review), execution markers (▲ buys under the bar, ▼ sells above)
// and entry→exit connectors of closed trades.
import 'dart:js_interop';
import 'dart:math' as math;

import 'package:web/web.dart';

import '../core/format.dart';
import '../core/geometry.dart';
import '../core/trading.dart';
import 'engine.dart';

const agentLineColor = '#a78bfa';
const positionColor = '#2f7cf6';

/// A clickable/draggable region drawn this frame.
class TradeHit {
  final String kind; // order | order-x | position | position-x | ticket | fill
  final double x, y, w, hgt;
  final TOrder? order;
  final TTicket? ticket;
  final List<TFill>? fills;
  const TradeHit(this.kind, this.x, this.y, this.w, this.hgt, {this.order, this.ticket, this.fills});
  bool contains(double px, double py) => px >= x && px <= x + w && py >= y && py <= y + hgt;
}

class TradingOverlay {
  final ChartEngine engine;
  TChartData? data;
  bool show = true, showFills = true, showTradeLines = true;
  String upColor = '#12b886', downColor = '#f0414f';

  /// While an order line is dragged: its id and the price under the cursor.
  String? dragId;
  double? dragPrice;
  final List<TradeHit> _hits = [];
  /// This frame's label spots, worked out in [reserve] before the drawings are drawn.
  final List<(double, double, void Function(double right))> _planned = [];
  final List<(double, double, String, String)> _pills = [];

  TradingOverlay(this.engine);

  /// Regions drawn in the last frame (for tests).
  List<TradeHit> get hits => List.unmodifiable(_hits);

  List<TOrder> get _orders => [
        for (final o in data?.orders ?? const <TOrder>[])
          if (o.isOpen && o.linePrice != null) o,
      ];

  double? _priceOf(TOrder o) => o.id == dragId && dragPrice != null ? dragPrice : o.linePrice;

  String _orderColor(TOrder o) => switch (o.role) {
        'stop_loss' => downColor,
        'take_profit' => upColor,
        _ => o.isBuy ? upColor : downColor,
      };

  /// Lines (and price-axis labels) for the engine.
  List<PriceLine> priceLines() {
    final d = data;
    if (!show || d == null) return const [];
    return [
      if (d.position != null) PriceLine(d.position!.avgCost, positionColor, 'position', dashed: false),
      for (final o in _orders) PriceLine(_priceOf(o)!, _orderColor(o), o.roleLabel),
      for (final t in d.tickets) ...[
        if (t.isModify && t.entryPrice != null) PriceLine(t.entryPrice!, agentLineColor, 'proposed ${t.newStop != null ? 'stop' : 'price'}')
        else if (t.entryPrice != null && t.orderType != 'market') PriceLine(t.entryPrice!, agentLineColor, 'proposal'),
        if (t.stopLoss != null) PriceLine(t.stopLoss!, '${agentLineColor}aa', 'proposal stop'),
        if (t.takeProfit != null) PriceLine(t.takeProfit!, '${agentLineColor}aa', 'proposal target'),
      ],
    ];
  }

  /// P&L if [o] (a stop-loss / take-profit / exit) fills at [price]: against the position, or against
  /// its parent entry while the bracket is still waiting.
  double? pnlAt(TOrder o, double price) {
    final d = data;
    if (d == null) return null;
    final p = d.position;
    if (p != null && (p.isLong ? !o.isBuy : o.isBuy)) {
      return (price - p.avgCost) * math.min(o.remaining, p.qty.abs()) * (p.isLong ? 1 : -1);
    }
    if (o.parentId != null) {
      final parent = d.orders.where((x) => x.id == o.parentId).firstOrNull;
      final entry = parent?.linePrice;
      if (parent != null && entry != null) return (price - entry) * o.qty * (parent.isBuy ? 1 : -1);
    }
    return null;
  }

  // ---------------------------------------------------------------- drawing

  static const _font = '600 11px ui-sans-serif, system-ui, sans-serif';

  /// Works out where this frame's labels go and reserves their boxes (and the fill markers) in the
  /// engine's label layout, so drawings place their labels around them.
  void reserve(CanvasRenderingContext2D ctx, double w, double h) {
    _planned.clear();
    _pills.clear();
    final d = data;
    if (d == null) return;
    ctx.save();
    ctx.font = _font;
    if (show) _planLabels(ctx, d, h);
    if (showFills) {
      for (final (x, y, _, _) in _fillSpots(d, h)) {
        engine.labels.reserve(Box(x - 7, y - 7, 14, 14));
      }
    }
    if (showTradeLines) {
      for (final (x, y, text, color) in _tradeLineSpots(d)) {
        final w = ctx.measureText(text).width + 10;
        final b = engine.labels.place(Box(x, y - 8, w, 16), maxShift: 60);
        _pills.add((b.l, b.t + 8, text, color));
      }
    }
    ctx.restore();
  }

  void draw(CanvasRenderingContext2D ctx, double w, double h) {
    _hits.clear();
    final d = data;
    if (d == null) return;
    ctx.save();
    ctx.font = _font;
    ctx.textBaseline = 'middle';
    if (showTradeLines) _drawTradeLines(ctx, d);
    if (showFills) _drawFills(ctx, d, h);
    if (show) {
      for (final (_, right, drawAt) in _planned) {
        drawAt(right);
      }
    }
    ctx.restore();
  }

  /// Entry→exit connectors of closed trades: (x0, y0, x1, y1, trade).
  List<(double, double, double, double, TTrade)> _tradeLines(TChartData d) {
    final bars = engine.rawBars;
    final out = <(double, double, double, double, TTrade)>[];
    if (bars.isEmpty) return out;
    for (final t in d.trades) {
      if (!t.closed || t.entryAvg == null || t.exitAvg == null || t.fills.isEmpty) continue;
      final entries = t.fills.where((f) => f.role != 'exit' && (t.isLong ? f.isBuy : !f.isBuy)).toList();
      final exits = t.fills.where((f) => t.isLong ? !f.isBuy : f.isBuy).toList();
      if (entries.isEmpty || exits.isEmpty) continue;
      final i0 = barIndexForTime(bars, entries.first.time), i1 = barIndexForTime(bars, exits.last.time);
      if (i0 < 0 || i1 < 0) continue;
      final x0 = engine.logicalToX(i0.toDouble()), x1 = engine.logicalToX(i1.toDouble());
      final y0 = engine.priceToY(t.entryAvg), y1 = engine.priceToY(t.exitAvg);
      if (y0 == null || y1 == null || (x1 < 0 && x0 < 0) || (x0 > engine.plotWidth && x1 > engine.plotWidth)) continue;
      out.add((x0, y0, x1, y1, t));
    }
    return out;
  }

  /// P&L pills at the exit end of each trade line: (left, y, text, color).
  List<(double, double, String, String)> _tradeLineSpots(TChartData d) => [
        for (final (_, _, x1, y1, t) in _tradeLines(d))
          (x1 + 6, y1, '${money(t.netPnl)}${t.rMultiple != null ? ' · ${n(t.rMultiple, 1)}R' : ''}',
              t.netPnl >= 0 ? upColor : downColor),
      ];

  void _drawTradeLines(CanvasRenderingContext2D ctx, TChartData d) {
    for (final (x0, y0, x1, y1, t) in _tradeLines(d)) {
      final color = t.netPnl >= 0 ? upColor : downColor;
      ctx.strokeStyle = color.toJS;
      ctx.globalAlpha = 0.75;
      ctx.lineWidth = 1.2;
      ctx.setLineDash(<JSNumber>[4.toJS, 3.toJS].toJS);
      ctx.beginPath();
      ctx.moveTo(x0, y0);
      ctx.lineTo(x1, y1);
      ctx.stroke();
      ctx.setLineDash(<JSNumber>[].toJS);
      ctx.globalAlpha = 1;
    }
    for (final (left, y, text, color) in _pills) {
      _pill(ctx, text, left, y, color, '#fff', alignRight: false);
    }
  }

  /// Execution markers: (x, y, buy?, fills) — buys under the bar's low, sells above its high.
  List<(double, double, bool, List<TFill>)> _fillSpots(TChartData d, double h) {
    final bars = engine.bars;
    final out = <(double, double, bool, List<TFill>)>[];
    if (bars.isEmpty || d.fills.isEmpty) return out;
    for (final e in fillsByBar(engine.rawBars, d.fills).entries) {
      final x = engine.logicalToX(e.key.toDouble());
      if (x < -10 || x > engine.plotWidth + 10 || e.key >= bars.length) continue;
      final bar = bars[e.key];
      final buys = e.value.where((f) => f.isBuy).toList(), sells = e.value.where((f) => !f.isBuy).toList();
      if (buys.isNotEmpty) out.add((x, (engine.priceToY(bar.low) ?? h) + 10, true, buys));
      if (sells.isNotEmpty) out.add((x, (engine.priceToY(bar.high) ?? 0) - 10, false, sells));
    }
    return out;
  }

  void _drawFills(CanvasRenderingContext2D ctx, TChartData d, double h) {
    for (final (x, y, buy, fills) in _fillSpots(d, h)) {
      final color = buy ? upColor : downColor;
      _triangle(ctx, x, y, buy, color);
      _hits.add(TradeHit('fill', x - 7, y - 7, 14, 14, fills: fills));
      if (fills.length > 1) _count(ctx, x, y + (buy ? 11 : -11), fills.length, color);
    }
  }

  void _triangle(CanvasRenderingContext2D ctx, double x, double y, bool up, String color) {
    ctx.fillStyle = color.toJS;
    ctx.strokeStyle = 'rgba(0,0,0,0.35)'.toJS;
    ctx.lineWidth = 1;
    ctx.beginPath();
    if (up) {
      ctx.moveTo(x, y - 6);
      ctx.lineTo(x + 6, y + 5);
      ctx.lineTo(x - 6, y + 5);
    } else {
      ctx.moveTo(x, y + 6);
      ctx.lineTo(x + 6, y - 5);
      ctx.lineTo(x - 6, y - 5);
    }
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
  }

  void _count(CanvasRenderingContext2D ctx, double x, double y, int n, String color) {
    ctx.fillStyle = color.toJS;
    ctx.textAlign = 'center';
    ctx.font = '700 9px ui-sans-serif, system-ui, sans-serif';
    ctx.fillText('$n', x, y);
    ctx.font = '600 11px ui-sans-serif, system-ui, sans-serif';
  }

  /// Width of a label drawn by [_label].
  double _labelWidth(CanvasRenderingContext2D ctx, List<(String, String, String)> segs, bool close) =>
      segs.fold<double>(0, (a, s) => a + ctx.measureText(s.$1).width + 12) + (close ? 18 : 0);

  /// One label: colored segments right-aligned at [right]; returns the left edge.
  double _label(CanvasRenderingContext2D ctx, double right, double y, List<(String, String, String)> segs, String border,
      {String? hitKind, TOrder? order, TTicket? ticket, String? closeKind, double alpha = 1}) {
    const pad = 6.0, hgt = 18.0;
    final widths = [for (final s in segs) ctx.measureText(s.$1).width + pad * 2];
    const xw = 18.0;
    final total = widths.fold<double>(0, (a, b) => a + b) + (closeKind != null ? xw : 0);
    var x = right - total;
    final top = y - hgt / 2;
    ctx.globalAlpha = alpha;
    ctx.fillStyle = engine.theme.bg.toJS;
    ctx.beginPath();
    ctx.roundRect(x, top, total, hgt, 3.toJS);
    ctx.fill();
    final left = x;
    for (var i = 0; i < segs.length; i++) {
      final (text, bg, fg) = segs[i];
      ctx.fillStyle = bg.toJS;
      ctx.fillRect(x, top, widths[i], hgt);
      ctx.fillStyle = fg.toJS;
      ctx.textAlign = 'center';
      ctx.fillText(text, x + widths[i] / 2, y + 0.5);
      x += widths[i];
    }
    if (hitKind != null) _hits.add(TradeHit(hitKind, left, top, x - left, hgt, order: order, ticket: ticket));
    if (closeKind != null) {
      ctx.fillStyle = engine.theme.bg.toJS;
      ctx.fillRect(x, top, xw, hgt);
      ctx.strokeStyle = engine.theme.text.toJS;
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      ctx.moveTo(x + 6, top + 6);
      ctx.lineTo(x + 12, top + 12);
      ctx.moveTo(x + 12, top + 6);
      ctx.lineTo(x + 6, top + 12);
      ctx.stroke();
      _hits.add(TradeHit(closeKind, x, top, xw, hgt, order: order, ticket: ticket));
      x += xw;
    }
    ctx.strokeStyle = border.toJS;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.roundRect(left + 0.5, top + 0.5, total - 1, hgt - 1, 3.toJS);
    ctx.stroke();
    ctx.globalAlpha = 1;
    return left;
  }

  void _pill(CanvasRenderingContext2D ctx, String text, double x, double y, String bg, String fg, {bool alignRight = true}) {
    final w = ctx.measureText(text).width + 10;
    final left = alignRight ? x - w : x;
    ctx.fillStyle = bg.toJS;
    ctx.beginPath();
    ctx.roundRect(left, y - 8, w, 16, 3.toJS);
    ctx.fill();
    ctx.fillStyle = fg.toJS;
    ctx.textAlign = 'center';
    ctx.fillText(text, left + w / 2, y + 0.5);
  }

  void _planLabels(CanvasRenderingContext2D ctx, TChartData d, double h) {
    // (y, width, draw at right edge)
    final items = <(double, double, void Function(double right))>[];
    final p = d.position;
    if (p != null) {
      final y = engine.priceToY(p.avgCost);
      if (y != null) {
        final pnl = p.unrealizedPnl ?? (p.marketPrice != null ? p.pnlAt(p.marketPrice!) : null);
        final segs = [
          (p.isLong ? 'LONG ${qtyText(p.qty.abs())}' : 'SHORT ${qtyText(p.qty.abs())}', positionColor, '#fff'),
          (pnl == null ? '–' : money(pnl), engine.theme.bg, (pnl ?? 0) >= 0 ? upColor : downColor),
        ];
        items.add((y, _labelWidth(ctx, segs, true), (right) {
          _label(ctx, right, y, segs, positionColor, hitKind: 'position', closeKind: 'position-x');
        }));
      }
    }
    for (final o in _orders) {
      final price = _priceOf(o)!;
      final y = engine.priceToY(price);
      if (y == null) continue;
      final color = _orderColor(o);
      final pnl = o.isProtective || o.role == 'exit' ? pnlAt(o, price) : null;
      final dragging = o.id == dragId;
      final segs = [
        ('${o.roleLabel} ${o.typeLabel}', color, '#fff'),
        (qtyText(o.remaining), engine.theme.bg, engine.theme.text),
        if (dragging) (fmtPrice(price), engine.theme.bg, color),
        if (pnl != null) (money(pnl), engine.theme.bg, pnl >= 0 ? upColor : downColor),
      ];
      items.add((y, _labelWidth(ctx, segs, true), (right) {
        _label(ctx, right, y, segs, color, hitKind: 'order', order: o, closeKind: 'order-x',
            alpha: o.held && !dragging ? 0.6 : 1);
      }));
    }
    for (final t in d.tickets) {
      final price = t.entryPrice;
      final y = price == null ? null : engine.priceToY(price);
      if (y == null) continue;
      final segs = [
        (t.isModify ? '✦ MOVE ${(t.spec['role'].s ?? 'order').replaceAll('_', ' ')}' : '✦ ${t.side.toUpperCase()} ${qtyText(t.qty)}',
            agentLineColor, '#fff'),
        ('Review', engine.theme.bg, agentLineColor),
      ];
      items.add((y, _labelWidth(ctx, segs, false), (right) {
        _label(ctx, right, y, segs, agentLineColor, hitKind: 'ticket', ticket: t);
      }));
    }
    // labels stay on their price line; ones that would overlap move left along it
    items.sort((a, b) => a.$1.compareTo(b.$1));
    final row = LabelLayout(gap: 6);
    for (final (y, w, drawAt) in items) {
      if (y < -10 || y > h + 10) continue;
      final b = row.place(Box(engine.plotWidth - 8 - w, y - 9, w, 18), vertical: false, dir: -1,
          maxShift: engine.plotWidth);
      engine.labels.reserve(b);
      _planned.add((y, b.r, drawAt));
    }
  }

  // ---------------------------------------------------------------- hit testing

  /// The label, × button, line or fill marker under (x, y), most specific first.
  TradeHit? hitAt(double x, double y) {
    for (final k in ['order-x', 'position-x', 'order', 'position', 'ticket', 'fill']) {
      for (final hit in _hits.reversed) {
        if (hit.kind == k && hit.contains(x, y)) return hit;
      }
    }
    if (!show || x > engine.plotWidth) return null;
    for (final o in _orders) {
      final py = engine.priceToY(_priceOf(o));
      if (py != null && (py - y).abs() <= 4) return TradeHit('order', x, py, 0, 0, order: o);
    }
    return null;
  }
}
