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
import '../core/layers.dart';
import '../core/format.dart';
import '../core/models.dart';
import '../core/trading.dart';
import '../ui/api.dart';
import '../ui/dom.dart';
import '../ui/kit/icons.dart';
import '../ui/kit/overlay.dart';
import '../ui/kit/state.dart';
import '../ui/store.dart';
import '../ui/trading_state.dart';
import '../ui/trading_ui.dart';
import 'annotations.dart';
import 'drawing_ops.dart';
import 'engine.dart';
import 'trading_overlay.dart';

const palette = ['#f59e0b', '#38bdf8', '#e879f9', '#34d399', '#f87171', '#a3e635', '#fb923c', '#60a5fa'];
const compareColors = ['#f472b6', '#facc15', '#2dd4bf', '#c084fc', '#fb7185'];
const drawColors = ['#38bdf8', '#a78bfa', '#22c55e', '#ef4444', '#f59e0b', '#e2e8f0', '#64748b', '#ec4899'];
const _hiddenOverlayOutputs = {'bandwidth', 'percent_b', 'direction', 'slope_pct', 'r2'};
const _histogramOutputs = {'histogram', 'momentum', 'squeeze_on'};

/// The automatic look of an indicator output (used unless the user styled it).
String autoPlot(String indicator, String output, bool ownPane) =>
    ownPane && _histogramOutputs.contains(output) ? 'histogram' : indicator == 'psar' ? 'dots' : 'line';
String autoLine(String output) => output == 'middle' || output == 'signal' ? 'dashed' : 'solid';
double autoWidth(bool ownPane) => ownPane ? 1.8 : 1.4;

/// Outputs an indicator draws (the rest are values for the agent, e.g. Bollinger %B).
bool drawnOutput(String output, bool overlay) =>
    !(overlay && _hiddenOverlayOutputs.contains(output)) && !(!overlay && output == 'direction');

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
  final String key, label; // key = layer item id
  final List<(String, String, List<double?>)> outputs; // (name, color, values)
  final int pane;
  final ItemKind kind;
  _LegendRow(this.key, this.label, this.outputs, this.pane, this.kind);
}

/// Computed values of one indicator item.
class _IndData {
  final String label;
  final bool overlay;
  final List<String> outputs;
  final List<double> levels;
  final Map<String, List<double?>> values;
  _IndData(this.label, this.overlay, this.outputs, this.levels, this.values);
}

class _Drag {
  final Annotation original;
  final int? handle; // point index, or null when moving the whole drawing
  final double startX, startY;
  _Drag(this.original, this.handle, this.startX, this.startY);
}

class _TradeDrag {
  final TradeHit hit;
  final double startY;
  bool moved = false;
  _TradeDrag(this.hit, this.startY);
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
  int _loadSeq = 0, _indSeq = 0;
  SymbolConfig? cfg;
  String _indKey = '';
  final Map<String, _IndData> _indData = {};
  final Map<String, List<Bar?>> _cmpBars = {}; // "<symbol>|<interval>" -> bars aligned to ours
  final Set<String> _cmpLoading = {};
  int _futureBars = 0;
  List<_LegendRow> _legendRows = [];
  int? _legendIndex;
  HTMLElement? _prompt;
  _Drag? _drag;
  _TradeDrag? _tdrag;
  late final TradingOverlay trading;
  List<PriceLine> _alertLines = const [];
  final _tradeReload = Debouncer(200);
  int _tradeSeq = 0;
  Timer? _liveTimer, _clock;
  String? _error;
  final void Function(ChartPanel) onActivate;

  ChartPanel(this.state, this.index, {required this.onActivate}) {
    root.append(_host);
    append(root, [_legend, _paneLegends, _status, _toLatest, _floating]);
    _floating.isHidden = true;
    _toLatest.isHidden = true;
    engine = ChartEngine(_host);
    layer = AnnotationLayer(engine);
    trading = TradingOverlay(engine);
    engine.topOverlay = trading.draw;
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
    _scope.listen(state.interval, (_) {
      _followLayer();
      _load();
    });
    _scope.listen(store.configVersion, (_) {
      if (store.lastConfigChanged == symbol) _applyConfig();
    });
    _scope.listen(store.specs, (_) => _applyConfig());
    _scope.listen(store.prefs, (_) => _applyPrefs());
    _scope.listen(store.drawingsVersion, (_) {
      _ensureDrawingLayers();
      _applyDrawings();
    });
    _scope.listen(store.selected, (_) => _applyDrawings());
    _scope.listen(store.drawingsHidden, (h) {
      layer.hidden = h;
      layer.update();
    });
    _scope.listen(store.tool, (_) => cancelDraft());
    _scope.listen(store.alerts, (_) => _applyAlerts());
    _scope.listen(trade.version, (_) => _tradeReload(_loadTrading));
    _scope.listen(trade.status, (_) => _tradeReload(_loadTrading));
    _scope.listen(crosshairTime, (v) {
      if (v.$1 != index) engine.setSyncedCrosshair(store.syncCrosshair.value ? v.$2 : null);
    });
    _scope.listen(syncedRange, (v) {
      if (v == null || v.$1 == index || !store.syncRange.value) return;
      final from = layer.mapper.logical(v.$2.round()), to = layer.mapper.logical(v.$3.round());
      if (from != null && to != null) engine.setVisibleRange(from, to);
    });
    _applyPrefs();
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
      trading.data = null;
    }
    if (symbolChanged) cfg = null;
    _renderLegend();
    _renderStatus();
    store.loadDrawings(symbol).catchError((Object e) => toast('Drawings: $e', kind: 'error'));
    final cfgFuture = store.config(symbol);
    try {
      final res = await api.bars(symbol, interval);
      if (seq != _loadSeq) return;
      final b = res.bars;
      cfg = await cfgFuture;
      if (seq != _loadSeq) return;
      engine.setBars(b, intraday: intraday, keepView: true);
      engine.setSeries(const []);
      engine.setCompares(const []);
      _indKey = '';
      _indData.clear();
      layer.mapper.set([for (final x in b) x.time], intervalSeconds[interval] ?? 86400, intraday);
      _ensureDrawingLayers();
      _applyDrawings();
      _applyAlerts();
      _loadTrading();
      _showDefaultRange();
      _loading = false;
      if (b.isEmpty) _error = 'No data for $symbol ($interval)';
      _applyConfig();
      _renderStatus();
      if (res.source.isNotEmpty) store.provider.value = res.source;
      if (symbolChanged && store.pref<bool>('chart.markers')) await _loadMarkers();
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
    if (!intraday || bars.isEmpty || !store.pref<bool>('chart.countdown')) {
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

  // ---------------------------------------------------------------- layers → series

  bool _isOverlay(String indicator) {
    for (final sp in store.specs.value) {
      if (sp.name == indicator) return sp.overlay;
    }
    return false;
  }

  /// Rebuilds everything the symbol's layers describe. Indicator values and other symbols' bars
  /// are fetched once and cached, so toggling visibility, moving panes or re-ordering is instant.
  void _applyConfig() {
    final c = cfg;
    if (c == null || bars.isEmpty) {
      _renderLegend();
      return;
    }
    engine.setChartType(chartTypeFromName(c.priceAt(interval).chartType));
    engine.showPrice = c.itemShownAt(c.priceAt(interval), interval);
    engine.setScaleMode(ScaleMode.values.firstWhere((m) => m.name == c.scale, orElse: () => ScaleMode.linear));
    final indicators = c.indicatorItemsAt(interval);
    final key = jsonEncode([for (final (item, r) in indicators) [item.id, r.toJson()]]);
    if (key != _indKey) {
      _indKey = key;
      _fetchIndicators(indicators);
    }
    for (final i in c.allAt(interval)) {
      if (i.kind == ItemKind.compare && i.symbol.isNotEmpty) _fetchCompare(i.symbol);
    }
    _rebuildSeries();
    _applyDrawings();
  }

  Future<void> _fetchIndicators(List<(LayerItem, IndicatorRef)> items) async {
    final seq = ++_indSeq;
    if (items.isEmpty) {
      _indData.clear();
      _rebuildSeries();
      return;
    }
    try {
      final res = await api.indicators(symbol, interval, [for (final (_, r) in items) r]);
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

      _indData.clear();
      for (var k = 0; k < res.results.length && k < items.length; k++) {
        final r = res.results[k];
        _indData[items[k].$1.id] = _IndData(r.label, r.overlay, r.outputs, r.levels,
            {for (final out in r.outputs) out: align(r.values[out] ?? const [])});
      }
      _rebuildSeries();
    } catch (e) {
      if (seq == _indSeq) toast('Indicators: $e', kind: 'error');
    }
  }

  Future<void> _fetchCompare(String sym) async {
    final key = '$sym|$interval';
    if (_cmpBars.containsKey(key) || _cmpLoading.contains(key)) return;
    _cmpLoading.add(key);
    try {
      final res = await api.bars(sym, interval);
      final byTime = {for (final b in res.bars) b.time: b};
      _cmpBars[key] = [for (final b in bars) byTime[b.time]];
      _rebuildSeries();
    } catch (e) {
      toast('$sym: $e', kind: 'error');
    } finally {
      _cmpLoading.remove(key);
    }
  }

  void _rebuildSeries() {
    final c = cfg;
    if (c == null || bars.isEmpty) return;
    final shown = c.shownAt(interval);
    final panes = assignPanes(shown, (name) => _indData.values.isNotEmpty
        ? (_indDataByName(name)?.overlay ?? _isOverlay(name))
        : _isOverlay(name));
    // pane indexes must be contiguous for the engine
    final used = (panes.values.toSet()..add(0)).toList()..sort();
    final remap = {for (var i = 0; i < used.length; i++) used[i]: i};
    final series = <Series>[];
    final compares = <CompareSeries>[];
    final rows = <_LegendRow>[];
    var colorIdx = 0;
    var cmpIdx = 0;
    engine.showVolume = false;
    for (final item in shown) {
      final pane = remap[panes[item.id] ?? 0] ?? 0;
      switch (item.kind) {
        case ItemKind.price:
          break;
        case ItemKind.volume:
          if (pane == 0) {
            engine.showVolume = true;
          } else {
            final vals = [for (final b in bars) b.volume];
            series.add(Series(id: item.id, pane: pane, kind: SeriesKind.volume, values: vals, color: '#64748b'));
            rows.add(_LegendRow(item.id, 'Volume', [('volume', '#64748b', vals)], pane, item.kind));
          }
        case ItemKind.indicator:
          final d = _indData[item.id];
          if (d == null) continue;
          final outs = <(String, String, List<double?>)>[];
          var first = true;
          for (final out in d.outputs) {
            if (d.overlay && _hiddenOverlayOutputs.contains(out)) continue;
            if (!d.overlay && out == 'direction') continue;
            final st = item.styles[out];
            final auto = palette[colorIdx++ % palette.length]; // consumed either way so autos stay stable
            if (st != null && !st.visible) continue;
            final vals = d.values[out] ?? const [];
            final color = st?.color ?? (first && item.color != null ? item.color! : auto);
            final plot = st?.plot ?? autoPlot(item.name, out, pane > 0);
            final hist = plot == 'histogram';
            final line = st?.line ?? autoLine(out);
            series.add(Series(
              id: item.id,
              pane: pane,
              kind: hist ? SeriesKind.histogram : plot == 'dots' ? SeriesKind.dots : SeriesKind.line,
              values: vals,
              color: color,
              width: st?.width ?? autoWidth(pane > 0),
              dashed: line == 'dashed',
              dotted: line == 'dotted',
              lastValueLabel: pane > 0 && !hist,
              levels: first && pane > 0 ? (item.levels ?? d.levels) : const [],
            ));
            first = false;
            outs.add((out, color, vals));
          }
          rows.add(_LegendRow(item.id, d.label, outs, pane, item.kind));
        case ItemKind.compare:
          final data = _cmpBars['${item.symbol}|$interval'];
          final color = item.color ?? compareColors[cmpIdx++ % compareColors.length];
          if (data == null) continue;
          final closes = [for (final b in data) b?.close];
          if (pane == 0) {
            compares.add(CompareSeries(item.symbol, color, closes));
          } else {
            series.add(Series(
              id: item.id,
              pane: pane,
              kind: item.display == 'candles' ? SeriesKind.candles : item.display == 'area' ? SeriesKind.area : SeriesKind.line,
              values: closes,
              ohlc: item.display == 'candles' ? data : null,
              color: color,
              width: 1.6,
              lastValueLabel: true,
            ));
          }
          rows.add(_LegendRow(item.id, item.symbol, [('close', color, closes)], pane, item.kind));
      }
    }
    engine.setSeries(series);
    engine.setCompares(compares);
    _legendRows = rows;
    _renderLegend();
  }

  _IndData? _indDataByName(String name) {
    final c = cfg;
    if (c == null) return null;
    for (final (item, _) in c.indicatorItemsAt(interval)) {
      if (item.name == name && _indData[item.id] != null) return _indData[item.id];
    }
    return null;
  }

  void _applyPrefs() {
    engine.showGrid = store.pref<bool>('chart.grid');
    engine.showLastPriceLine = store.pref<bool>('chart.lastPriceLine');
    engine.upColorOverride = store.pref<String?>('chart.upColor');
    engine.downColorOverride = store.pref<String?>('chart.downColor');
    if (!store.pref<bool>('chart.markers')) engine.markers = [];
    engine.refreshTheme();
    trading
      ..show = store.pref<bool>('chart.trading')
      ..showFills = store.pref<bool>('chart.fills')
      ..showTradeLines = store.pref<bool>('chart.tradeLines')
      ..upColor = engine.upColorOverride ?? engine.theme.up
      ..downColor = engine.downColorOverride ?? engine.theme.down;
    _syncLines();
  }

  // ---------------------------------------------------------------- trading

  Future<void> _loadTrading() async {
    final sym = symbol;
    final seq = ++_tradeSeq;
    final j = await attempt(trade.chart(sym));
    if (seq != _tradeSeq || sym != symbol) return;
    trading.data = j == null ? null : TChartData.fromJson(j);
    _syncLines();
  }

  void _syncLines() {
    engine.priceLines = [..._alertLines, ...trading.priceLines()];
    engine.invalidate();
  }

  void _openTicket(OrderDraft d) {
    trade.openTicket(d);
    store.panel.value = 'trade';
  }

  double _roundPrice(double v) => double.parse(v.toStringAsFixed(v.abs() >= 1 ? 2 : 4));

  Future<void> _tradeClick(TradeHit hit, double cx, double cy) async {
    final d = trading.data;
    final o = hit.order;
    switch (hit.kind) {
      case 'order-x':
        if (o != null) await cancelOrder(o, protectsPosition: d?.position != null);
      case 'position-x':
        if (d?.position != null) await closePositionDialog(d!.position!);
      case 'ticket':
        if (hit.ticket != null) await reviewTicket(hit.ticket!);
      case 'order':
        if (o != null) _orderMenu(o, cx, cy);
      case 'position':
        if (d?.position != null) _positionMenu(d!.position!, cx, cy, null);
    }
  }

  void _orderMenu(TOrder o, double cx, double cy) {
    final p = trading.data?.position;
    showMenu([
      MenuItem.header('${o.roleLabel} ${o.typeLabel} ${qtyText(o.remaining)} ${o.symbol} @ ${o.linePrice == null ? 'MKT' : fmtPrice(o.linePrice!)}'),
      MenuItem('Modify…', iconName: 'edit', hint: 'or drag the line', onSelect: () => modifyOrderDialog(o)),
      if (o.role == 'stop_loss' && p != null)
        MenuItem('Move stop to breakeven (${fmtPrice(p.avgCost)})', iconName: 'shield',
            onSelect: () => _modifyPrice(o, p.avgCost)),
      MenuItem('Cancel order', iconName: 'x', danger: true,
          onSelect: () => cancelOrder(o, protectsPosition: p != null)),
    ], x: cx, y: cy);
  }

  void _positionMenu(TPosition p, double cx, double cy, double? price) {
    final orders = trading.data?.orders ?? const <TOrder>[];
    final sl = orders.where((o) => o.isOpen && o.role == 'stop_loss').firstOrNull;
    final tp = orders.where((o) => o.isOpen && o.role == 'take_profit').firstOrNull;
    final exitSide = p.isLong ? 'sell' : 'buy';
    final at = price ?? (p.marketPrice ?? p.avgCost) * (p.isLong ? 0.97 : 1.03);
    showMenu([
      MenuItem.header('${p.isLong ? 'Long' : 'Short'} ${qtyText(p.qty.abs())} ${p.symbol} @ ${fmtPrice(p.avgCost)} · ${money(p.unrealizedPnl)}'),
      MenuItem('Close position…', iconName: 'x', onSelect: () => closePositionDialog(p)),
      MenuItem('Close half…', iconName: 'minus', onSelect: () => closePositionDialog(p, fraction: 0.5)),
      if (price != null) MenuItem('Close with a limit at ${fmtPrice(price)}…', iconName: 'hline', onSelect: () => closePositionDialog(p, limitPrice: price)),
      sl == null
          ? MenuItem('Add stop-loss${price != null ? ' at ${fmtPrice(price)}' : ''}…', iconName: 'shield',
              onSelect: () => _openTicket(OrderDraft(p.symbol, exitSide, type: 'stop', price: _roundPrice(at), qty: p.qty.abs())))
          : MenuItem('Move stop-loss${price != null ? ' to ${fmtPrice(price)}' : ' to breakeven'}', iconName: 'shield',
              onSelect: () => _modifyPrice(sl, price ?? p.avgCost)),
      if (tp == null && price != null)
        MenuItem('Add take-profit at ${fmtPrice(price)}…', iconName: 'check',
            onSelect: () => _openTicket(OrderDraft(p.symbol, exitSide, price: price, qty: p.qty.abs()))),
      MenuItem('Add to position…', iconName: 'plus', onSelect: () => _openTicket(OrderDraft(p.symbol, p.isLong ? 'buy' : 'sell', price: price))),
    ], x: cx, y: cy);
  }

  Future<void> _modifyPrice(TOrder o, double price) async {
    try {
      await trade.modify(o.id, {o.priceField: _roundPrice(price)});
      toast('${o.roleLabel} moved to ${fmtPrice(_roundPrice(price))}', kind: 'success', ms: 2000);
    } catch (e) {
      toast('$e', kind: 'error', ms: 6000);
    }
    _loadTrading();
  }

  List<MenuItem> _tradeMenuItems(double p, double cx, double cy) {
    final st = trade.status.value;
    final pos = trading.data?.position;
    if (st == null || !st.connected) return const [];
    return [
      MenuItem('Trade at ${fmtPrice(p)}', iconName: 'trade', submenu: [
        MenuItem('Buy limit @ ${fmtPrice(p)}', onSelect: () => _openTicket(OrderDraft(symbol, 'buy', price: p))),
        MenuItem('Sell limit @ ${fmtPrice(p)}', onSelect: () => _openTicket(OrderDraft(symbol, 'sell', price: p))),
        MenuItem('Buy stop @ ${fmtPrice(p)}', onSelect: () => _openTicket(OrderDraft(symbol, 'buy', type: 'stop', price: p))),
        MenuItem('Sell stop @ ${fmtPrice(p)}', onSelect: () => _openTicket(OrderDraft(symbol, 'sell', type: 'stop', price: p))),
      ]),
      if (pos != null)
        MenuItem('${pos.isLong ? 'Long' : 'Short'} ${qtyText(pos.qty.abs())} position…', iconName: 'wallet',
            hint: 'stop, target, close', onSelect: () => Timer.run(() => _positionMenu(pos, cx, cy, p))),
    ];
  }

  String _fillTime(int t) {
    final d = DateTime.fromMillisecondsSinceEpoch(t * 1000);
    String two(int v) => v.toString().padLeft(2, '0');
    return '${d.year}-${two(d.month)}-${two(d.day)} ${two(d.hour)}:${two(d.minute)}';
  }

  /// An order ticket from a long/short position drawing: entry, stop and target from its points.
  void orderFromDrawing(Annotation a) {
    if (a.points.length < 3) return;
    final entry = a.points[0].price, stop = a.points[1].price, target = a.points[2].price;
    if (entry == null) return;
    _openTicket(OrderDraft(a.symbol, a.type == 'long_position' ? 'buy' : 'sell',
        price: _roundPrice(entry), stopLoss: stop == null ? null : _roundPrice(stop), takeProfit: target == null ? null : _roundPrice(target)));
  }

  /// Drawings can name layers the symbol's configuration does not have yet (e.g. the agent's);
  /// add them so every drawing shows up in the Layers panel.
  void _ensureDrawingLayers() {
    final c = cfg;
    if (c == null) return;
    final ids = <String?>{for (final a in store.drawingsFor(symbol)) a.layer};
    final missing = ids.where((id) => c.layer((id == null || id.isEmpty) ? mainLayerId : id) == null).toList();
    if (missing.isEmpty) return;
    store.editConfig(symbol, (cc) => cc.ensureDrawingLayers(missing), record: false);
  }

  void _showDefaultRange() {
    final n = bars.length;
    final saved = state.spacing.value;
    final width = engine.plotWidth > 0 ? engine.plotWidth : 900.0;
    final pad = math.max(8, _futureBars + 4);
    // a saved zoom covers the whole plot width, including the empty space right of the last bar
    final show = saved != null && saved > 0 ? width / saved - pad : (intraday ? 160 : interval == '1d' ? 180 : 150).toDouble();
    engine.setVisibleRange(n - show, n + pad.toDouble());
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

  bool _drawingLayerShown(Layer l) => l.visible && cfg!.drawingsApplyAt(l, interval);

  /// After an interval change in the focused chart, make the layer that is now on screen the active
  /// one (if the active layer does not apply at the new interval), so new items land where they show.
  void _followLayer() {
    final c = cfg;
    if (c == null || !identical(store.activeChart, state) || c.appliesAt(c.active, interval)) return;
    final target = c.chartLayerFor(interval);
    store.editConfig(symbol, (x) => x.activeLayer = target.id, label: 'Activate ${target.name}', record: false);
  }

  void _applyDrawings() {
    final all = store.drawingsFor(symbol);
    final c = cfg;
    final shown = [
      for (final a in all)
        if ((a.interval == null || a.interval == interval) && (c == null || _drawingLayerShown(c.layerOfDrawing(a.layer, a.author)))) a,
    ];
    layer.lockedLayers = c == null ? const {} : {for (final l in c.layers) if (l.locked) l.id};
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
    _alertLines = [
      for (final a in store.alerts.value)
        if (a.symbol == symbol && a.status == 'active' && a.value != null && a.kind != 'change_above' &&
            a.kind != 'change_below' && a.kind != 'expression')
          PriceLine(a.value!, '#f59e0b', '⏰ ${a.description}'),
    ];
    _syncLines();
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
    state.spacing.value = null;
    store.save();
    final n = bars.length;
    final show = intraday ? 160 : interval == '1d' ? 180 : 150;
    engine.setVisibleRange(math.max(0, n - show).toDouble(), n + math.max(8, _futureBars + 4).toDouble(), notify: true);
  }

  void _rangeChanged(double from, double to) {
    _toLatest.isHidden = !engine.scrolledBack;
    // remember the zoom level of this chart
    if (state.spacing.value == null || (state.spacing.value! - engine.spacing).abs() > 0.05) {
      state.spacing.value = engine.spacing;
      store.save();
    }
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
    final active = cfg?.activeLayer;
    final created = await createDrawing(symbol, type, pts, text: text,
        layer: active == null || active == mainLayerId ? null : active);
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
    final th = trading.hitAt(x, y);
    if (th != null && th.kind != 'fill') {
      _tdrag = _TradeDrag(th, y);
      return true;
    }
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
    final td = _tdrag;
    if (td != null) {
      final o = td.hit.order;
      if (td.hit.kind == 'order' && o != null && o.linePrice != null) {
        final price = engine.yToPrice(y);
        if (price == null || !price.isFinite || price <= 0) return;
        td.moved = true;
        trading.dragId = o.id;
        trading.dragPrice = _roundPrice(price);
        engine.cursor = 'ns-resize';
        _syncLines();
      }
      return;
    }
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
    final td = _tdrag;
    _tdrag = null;
    if (td != null) {
      final o = td.hit.order;
      if (td.moved && o != null && trading.dragPrice != null) {
        final price = trading.dragPrice!;
        if ((price - (o.linePrice ?? price)).abs() > 1e-9) {
          try {
            await trade.modify(o.id, {o.priceField: price});
            toast('${o.roleLabel} ${o.typeLabel} moved to ${fmtPrice(price)}', kind: 'success', ms: 1800);
          } catch (err) {
            toast('$err', kind: 'error', ms: 6000);
          }
        }
        trading.dragId = null;
        trading.dragPrice = null;
        await _loadTrading();
      } else if (!td.moved) {
        // pointer coordinates are fractional; package:web types them as int
        final raw = e as JSObject;
        await _tradeClick(td.hit, (raw['clientX'] as JSNumber).toDartDouble, (raw['clientY'] as JSNumber).toDartDouble);
      }
      _syncLines();
      return;
    }
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
      final th = tool == 'select' && y < engine.mainHeight ? trading.hitAt(x, y) : null;
      var marker = engine.markerAt(x, y);
      if (th != null && th.kind == 'fill') {
        final fs = th.fills!;
        marker = EventMarker(fs.first.time, '', '', [
          for (final f in fs)
            '${f.isBuy ? 'Bought' : 'Sold'} ${qtyText(f.qty)} @ ${fmtPrice(f.price)}'
                '${f.role != null ? ' (${f.role!.replaceAll('_', ' ')})' : ''}'
                '${f.realizedPnl != null ? ' · ${money(f.realizedPnl)}' : ''} · ${_fillTime(f.time)}',
        ].join('\n'));
      }
      root.setAttribute('data-marker', marker?.tooltip ?? '');
      engine.cursor = tool != 'select'
          ? 'crosshair'
          : th != null && th.kind != 'fill'
              ? (th.kind == 'order' ? 'ns-resize' : 'pointer')
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
      if (a.type == 'long_position' || a.type == 'short_position')
        btn('trade', 'Create order from this', (_) => orderFromDrawing(a)),
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
      if (a.type == 'long_position' || a.type == 'short_position')
        MenuItem('Create order from this…', iconName: 'trade', onSelect: () => orderFromDrawing(a)),
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
    final th = y < engine.mainHeight ? trading.hitAt(x, y) : null;
    if (th != null && th.kind != 'fill') {
      if (th.order != null) {
        _orderMenu(th.order!, cx, cy);
      } else if (th.ticket != null) {
        reviewTicket(th.ticket!);
      } else if (trading.data?.position != null) {
        _positionMenu(trading.data!.position!, cx, cy, price == null ? null : _roundPrice(price));
      }
      return;
    }
    final id = y < engine.mainHeight ? layer.hit(x, y) : null;
    final a = id == null ? null : store.drawingById(id);
    if (a != null) {
      store.selected.value = a.id;
      _drawingMenu(a, x: cx, y: cy);
      return;
    }
    final p = price == null ? null : double.parse(price.toStringAsFixed(price.abs() >= 1 ? 2 : 4));
    final c = cfg;
    final t = c?.priceAt(interval).chartType ?? 'candles';
    final vol = c?.volumeAt(interval);
    showMenu([
      if (p != null) ...[
        MenuItem('Add alert at ${fmtPrice(p)}', iconName: 'alarm', onSelect: () => showAlertDialog(symbol, price: p)),
        MenuItem('Horizontal line at ${fmtPrice(p)}', iconName: 'hline',
            onSelect: () => _commitDrawing('horizontal_line', [Point(bars.isEmpty ? null : bars.last.time, p)])),
        MenuItem('Copy price ${fmtPrice(p)}', iconName: 'copy',
            onSelect: () => window.navigator.clipboard.writeText(fmtPrice(p))),
        ..._tradeMenuItems(p, cx, cy),
        const MenuItem.divider(),
      ],
      MenuItem('Reset chart view', iconName: 'refresh', shortcut: 'Alt+R', onSelect: resetView),
      MenuItem('Chart type', iconName: 'candles', submenu: [
        for (final (k, label) in chartTypes)
          MenuItem(label, checked: t == k, onSelect: () => store.editConfig(symbol, (c) => c.priceAt(interval).chartType = k, label: 'Chart type')),
      ]),
      MenuItem('Price scale', iconName: 'log', submenu: [
        for (final sc in ['linear', 'log', 'percent'])
          MenuItem(sc == 'linear' ? 'Regular' : sc == 'log' ? 'Logarithmic' : 'Percent',
              checked: c?.scale == sc, onSelect: () => store.editConfig(symbol, (c) => c.scale = sc, label: 'Price scale')),
      ]),
      if (c != null)
        MenuItem(c.itemShownAt(c.priceAt(interval), interval) ? 'Hide price chart' : 'Show price chart', iconName: 'candles',
            onSelect: () => store.editConfig(symbol, (c) => c.priceAt(interval).visible = !c.priceAt(interval).visible, label: 'Price visibility')),
      if (vol != null)
        MenuItem(vol.visible ? 'Hide volume' : 'Show volume', iconName: 'chart',
            onSelect: () => store.editConfig(symbol, (c) => c.volumeAt(interval)!.visible = !c.volumeAt(interval)!.visible, label: 'Volume visibility'))
      else if (c != null)
        MenuItem('Add volume', iconName: 'chart', onSelect: () => store.editConfig(symbol, (c) => c.addItem(LayerItem.volume(id: newId('v')), layerId: c.chartLayerFor(interval).id),
            label: 'Add volume')),
      MenuItem(store.pref<bool>('chart.grid') ? 'Hide grid' : 'Show grid', iconName: 'table',
          onSelect: () => store.setPref('chart.grid', !store.pref<bool>('chart.grid'))),
      MenuItem('Trading on chart', iconName: 'wallet', submenu: [
        for (final (k, l) in [('chart.trading', 'Positions, orders & proposals'), ('chart.fills', 'Executions'), ('chart.tradeLines', 'Closed trades (entry → exit)')])
          MenuItem(l, checked: store.pref<bool>(k), onSelect: () => store.setPref(k, !store.pref<bool>(k))),
      ]),
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
    final n = math.min(b.length, engine.bars.length);
    // the hovered index can outlive the bars it pointed into (symbol/interval change)
    final hovered = _legendIndex;
    final idx = hovered != null && hovered < n ? hovered : (n == 0 ? null : n - 1);
    final bar = idx == null ? null : engine.bars[idx];
    final raw = idx == null ? null : b[idx];
    final prev = idx != null && idx > 0 ? b[idx - 1] : null;
    final chg = raw != null && prev != null ? (raw.close - prev.close) / prev.close * 100 : null;
    final cls = chg == null ? '' : chg >= 0 ? '.up' : '.down';
    final c = cfg;
    final priceShown = c == null || c.itemShownAt(c.priceAt(interval), interval);
    replace(_legend, [
      h('div.legend-row.legend-main${priceShown ? '' : '.dim'}', [
        h('button.legend-symbol', symbol, {'type': 'button', 'data-tip': 'Change symbol (or just start typing)'},
            {'click': (_) => window.dispatchEvent(CustomEvent('open-symbol-search'))}),
        h('span.legend-interval', intervalLabel(interval)),
        if (c != null && c.multiChart)
          h('span.legend-layer', c.chartLayerFor(interval).name,
              {'data-tip': 'Layer on screen at ${intervalLabel(interval)}. Each layer with its own price chart can be tied to an interval'}),
        if (bar != null)
          h('span.ohlc', [
            h('span.muted', 'O'), h('b$cls', fmt(bar.open)),
            h('span.muted', 'H'), h('b$cls', fmt(bar.high)),
            h('span.muted', 'L'), h('b$cls', fmt(bar.low)),
            h('span.muted', 'C'), h('b$cls', fmt(bar.close)),
            if (chg != null) h('b$cls', '${signed(raw!.close - prev!.close)} (${signed(chg)}%)'),
            h('span.muted', 'Vol'), h('b', compact(raw!.volume)),
          ]),
        if (c != null)
          h('span.ind-actions.main-actions', [
            h('button.legend-x', icon(priceShown ? 'eye' : 'eye-off', size: 13),
                {'type': 'button', 'aria-label': 'Toggle price chart'}, {
              'click': (_) => store.editConfig(symbol, (c) => c.priceAt(interval).visible = !c.priceAt(interval).visible, label: 'Price visibility'),
            }),
            h('button.legend-x', icon('settings', size: 13), {'type': 'button', 'aria-label': 'Price chart settings'},
                {'click': (_) => editLayerItem(symbol, c.priceAt(interval).id)}),
          ]),
        if (_loading) h('span.muted', 'loading…'),
      ]),
      for (final r in _legendRows.where((r) => r.pane == 0)) _legendRow(r, idx),
    ]);
    replace(_paneLegends, [
      for (final r in _legendRows.where((r) => r.pane > 0))
        h('div.pane-legend', _legendRow(r, idx), {'data-pane': '${r.pane}'}),
    ]);
    _afterDraw();
  }

  String _compareValue(List<double?> data, int? idx) {
    if (idx == null || idx >= data.length || data[idx] == null) return '–';
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
    final c = cfg;
    final item = c?.find(r.key)?.$2;
    final layerName = c?.find(r.key)?.$1.name;
    return h('div.legend-row.small.ind-row', [
      if (r.kind == ItemKind.compare) h('span.dot', null, {'style': 'background:${r.outputs.first.$2}'}),
      h('span.ind-name', r.label, {'data-tip': layerName == null ? '' : 'Layer: $layerName'}),
      if (r.kind == ItemKind.compare)
        h('span.ind-val', _compareValue(r.outputs.first.$3, idx))
      else
        for (final (_, color, values) in r.outputs)
          h('span.ind-val', r.kind == ItemKind.volume
              ? compact(idx != null && idx < values.length ? values[idx] : null)
              : fmt(idx != null && idx < values.length ? values[idx] : null), {'style': 'color:$color'}),
      if (item != null)
        h('span.ind-actions', [
          h('button.legend-x', icon('eye', size: 13), {'type': 'button', 'aria-label': 'Hide ${r.label}'}, {
            'click': (_) => store.editConfig(symbol, (c) => c.find(r.key)?.$2.visible = false, label: 'Hide ${r.label}'),
          }),
          h('button.legend-x', icon('settings', size: 13), {'type': 'button', 'aria-label': 'Settings for ${r.label}'},
              {'click': (_) => editLayerItem(symbol, r.key, colors: {for (final (o, col, _) in r.outputs) o: col})}),
          h('button.legend-x', icon('x', size: 13), {'type': 'button', 'aria-label': 'Remove ${r.label}'}, {
            'click': (_) => store.editConfig(symbol, (c) => c.removeItem(r.key), label: 'Remove ${r.label}'),
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
    o['compares'] = (() => _legendRows.where((r) => r.kind == ItemKind.compare).length.toJS).toJS;
    o['layers'] = (() => cfg == null ? null : jsonEncode(cfg!.toJson()).toJS).toJS;
    o['showPrice'] = (() => engine.showPrice.toJS).toJS;
    o['showVolume'] = (() => engine.showVolume.toJS).toJS;
    o['spacing'] = (() => engine.spacing.toJS).toJS;
    o['priceRange'] = (() => [engine.priceRange().$1.toJS, engine.priceRange().$2.toJS].toJS).toJS;
    o['plotWidth'] = (() => engine.plotWidth.toJS).toJS;
    o['priceLines'] = (() => engine.priceLines.length.toJS).toJS;
    o['tradeHits'] = (() => jsonEncode([
          for (final t in trading.hits) {'kind': t.kind, 'x': t.x + t.w / 2, 'y': t.y + t.hgt / 2, 'order': t.order?.id}
        ]).toJS).toJS;
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

/// Settings for one layer item: chart type (price), parameters and color (indicators), placement
/// (main pane / own pane / with another item), display (other symbols), and its layer.
const _sources = ['close', 'open', 'high', 'low', 'hl2', 'hlc3', 'ohlc4', 'volume'];

/// Settings for a layer item. Indicators get typed inputs, a style row per output (visibility, color,
/// width, line style, plot type) and editable levels; applying also makes those the defaults for new
/// instances of the indicator. [colors] are the colors currently drawn per output (for "auto").
Future<void> editLayerItem(String symbol, String itemId, {Map<String, String> colors = const {}}) async {
  final cfg = store.configNow(symbol);
  final found = cfg?.find(itemId);
  if (cfg == null || found == null) return;
  final (curLayer, item) = found;
  final spec = item.kind == ItemKind.indicator ? store.specs.value.where((s) => s.name == item.name).firstOrNull : null;
  HTMLSelectElement sel(List<(String, String)> opts, String value, [String? label]) {
    final e = h('select.input.select', [for (final (v, l) in opts) h('option', l, {'value': v})],
        {'aria-label': ?label}) as HTMLSelectElement;
    e.value = value;
    return e;
  }

  final others = [
    for (final i in cfg.all)
      if (i.id != item.id && i.kind != ItemKind.price) i
  ];
  final paneSel = sel([
    ('auto', 'Automatic'),
    ('main', 'Main price pane'),
    ('new', 'Its own pane'),
    for (final o in others) ('with:${o.id}', 'With ${o.label}'),
  ], item.pane);
  final layerSel = sel([for (final l in cfg.layers) (l.id, l.name)], curLayer.id);
  final typeSel = sel([for (final (k, l) in chartTypes) (k, l)], item.chartType);
  final displaySel = sel([('line', 'Line'), ('area', 'Area'), ('candles', 'Candles')], item.display);
  var color = item.color;
  final swatches = h('div.swatch-row');
  void renderSwatches() {
    replace(swatches, [
      h('button.swatch-btn${color == null ? '.on' : ''}', h('span.tiny', 'Auto'), {'type': 'button'}, {
        'click': (_) {
          color = null;
          renderSwatches();
        },
      }),
      for (final c in [...palette, ...compareColors])
        h('button.swatch-btn${color == c ? '.on' : ''}', h('span.swatch', null, {'style': 'background:$c'}),
            {'type': 'button', 'aria-label': c}, {
          'click': (_) {
            color = c;
            renderSwatches();
          },
        }),
    ]);
  }

  renderSwatches();

  // ---- indicator inputs
  final inputs = <String, HTMLElement>{};
  HTMLElement paramInput(String key, Object? factory, Object? value) {
    final v = '${value ?? factory ?? ''}';
    if (key == 'source') return sel([for (final x in _sources) (x, x)], v, key);
    if (factory is num) {
      return h('input.input', null, {
        'type': 'number', 'value': v, 'step': factory is int ? '1' : 'any', 'placeholder': '$factory', 'aria-label': key,
      });
    }
    return h('input.input', null, {'value': v, 'placeholder': key == 'anchor' ? 'YYYY-MM-DD' : '${factory ?? ''}', 'aria-label': key});
  }

  String inputValue(HTMLElement e) => e is HTMLSelectElement ? e.value : (e as HTMLInputElement).value;
  final paramGrid = h('div.ind-params');
  void renderParams(Map<String, Object> values) {
    inputs.clear();
    replace(paramGrid, [
      for (final e in spec?.params.entries ?? const <MapEntry<String, Object?>>[])
        h('label.field-col', [h('span', e.key.replaceAll('_', ' ')), inputs[e.key] = paramInput(e.key, e.value, values[e.key])]),
    ]);
  }

  // ---- indicator styles, one row per drawn output
  final outputs = [for (final o in spec?.outputs ?? const <String>[]) if (drawnOutput(o, spec!.overlay)) o];
  final ownPane = !(spec?.overlay ?? true);
  var styles = {for (final o in outputs) o: item.styles[o]?.copy() ?? OutputStyle()};
  if (item.color != null && outputs.isNotEmpty) styles[outputs.first]!.color ??= item.color; // legacy single color
  final styleGrid = h('div.ind-styles');
  void renderStyles() {
    replace(styleGrid, [
      h('div.ind-style-row.head', [
        h('span', null), h('span', 'Output'), h('span', 'Color'), h('span', 'Width'), h('span', 'Line'),
        if (ownPane) h('span', 'Plot'),
      ]),
      for (final (i, o) in outputs.indexed) () {
        final st = styles[o]!;
        final shown = st.color ?? colors[o] ?? palette[i % palette.length];
        final vis = h('input', null, {'type': 'checkbox', 'aria-label': 'Show $o'}) as HTMLInputElement..checked = st.visible;
        vis.addEventListener('change', ((Event _) => st.visible = vis.checked).toJS);
        final pick = h('input.color-input', null, {'type': 'color', 'value': shown, 'aria-label': '$o color'}) as HTMLInputElement;
        pick.addEventListener('input', ((Event _) => st.color = pick.value).toJS);
        final width = sel([('', 'Auto'), for (final w in ['1', '1.5', '2', '2.5', '3', '4']) (w, '${w}px')],
            st.width == null ? '' : (st.width! % 1 == 0 ? '${st.width!.toInt()}' : '${st.width}'), '$o width');
        width.addEventListener('change', ((Event _) => st.width = double.tryParse(width.value)).toJS);
        final line = sel([('', 'Auto'), ('solid', 'Solid'), ('dashed', 'Dashed'), ('dotted', 'Dotted')], st.line ?? '', '$o line style');
        line.addEventListener('change', ((Event _) => st.line = line.value.isEmpty ? null : line.value).toJS);
        final plot = sel([('', 'Auto'), ('line', 'Line'), ('histogram', 'Histogram'), ('dots', 'Dots')], st.plot ?? '', '$o plot');
        plot.addEventListener('change', ((Event _) => st.plot = plot.value.isEmpty ? null : plot.value).toJS);
        return h('div.ind-style-row', [
          vis,
          h('span.ind-style-name', o.replaceAll('_', ' ')),
          h('span.color-wrap', [
            pick,
            if (st.color != null)
              h('button.legend-x', icon('x', size: 12), {'type': 'button', 'data-tip': 'Automatic color', 'aria-label': 'Automatic $o color'}, {
                'click': (_) {
                  st.color = null;
                  renderStyles();
                },
              }),
          ]),
          width,
          line,
          if (ownPane) plot,
        ]);
      }(),
    ]);
  }

  final hasLevels = spec != null && (spec.levels.isNotEmpty || ownPane);
  String levelsText(List<double>? l) => (l ?? spec?.levels ?? const []).map((v) => v % 1 == 0 ? '${v.toInt()}' : '$v').join(', ');
  final levels = h('input.input', null, {
    'value': levelsText(item.levels), 'placeholder': spec?.levels.isEmpty ?? true ? 'e.g. 0' : levelsText(null), 'aria-label': 'Levels',
  }) as HTMLInputElement;

  void fill(Map<String, Object> params, Map<String, OutputStyle> st, List<double>? lv) {
    renderParams(params);
    styles = {for (final o in outputs) o: st[o]?.copy() ?? OutputStyle()};
    renderStyles();
    levels.value = levelsText(lv);
  }

  if (spec != null) fill(item.params, styles, item.levels);
  final title = switch (item.kind) {
    ItemKind.price => '$symbol price chart',
    ItemKind.volume => 'Volume',
    ItemKind.indicator => spec?.title ?? item.name,
    ItemKind.compare => item.symbol,
  };
  final remembered = spec != null && store.indicatorDefaults(spec.name).v != null;
  Dialog.open('$title settings', h('div.form-grid', [
    if (spec != null) h('div.muted.small', spec.description),
    if (item.kind == ItemKind.price) h('label.field-col', [h('span', 'Chart type'), typeSel]),
    if (spec != null && spec.params.isNotEmpty) ...[h('div.ind-section', 'Inputs'), paramGrid],
    if (outputs.isNotEmpty) ...[h('div.ind-section', 'Style'), styleGrid],
    if (hasLevels) h('label.field-col', [h('span', 'Levels (comma-separated, empty = none)'), levels]),
    if (item.kind == ItemKind.compare) h('label.field-col', [h('span', 'Display in its own pane as'), displaySel]),
    if (item.kind != ItemKind.price) h('label.field-col', [h('span', 'Pane'), paneSel]),
    if (item.kind == ItemKind.compare) h('div.field-col', [h('span', 'Color'), swatches]),
    h('label.field-col', [h('span', 'Layer'), layerSel]),
    if (spec != null)
      h('div.muted.tiny', 'Applying also makes these the defaults for every new ${spec.title} (any symbol).'),
  ]), width: spec != null ? '560px' : '420px', actions: [
    if (spec != null)
      DialogAction(remembered ? 'Built-in defaults' : 'Defaults', onPressed: () {
        fill(const {}, const {}, null);
        return false;
      }),
    const DialogAction('Cancel'),
    DialogAction('Apply', primary: true, onPressed: () {
      LayerItem? applied;
      store.editConfig(symbol, (c) {
        final it = c.find(itemId)?.$2;
        if (it == null) return;
        if (it.kind == ItemKind.price) it.chartType = typeSel.value;
        if (spec != null) {
          final p = <String, Object>{};
          for (final e in spec.params.entries) {
            final raw = inputValue(inputs[e.key]!).trim();
            if (raw.isEmpty || raw == '${e.value}') continue;
            final v = e.value is num ? (num.tryParse(raw) ?? e.value!) : raw;
            if (v == e.value) continue;
            p[e.key] = e.value is int && v is num ? v.round() : v;
          }
          it.params
            ..clear()
            ..addAll(p);
          it.styles
            ..clear()
            ..addAll({for (final e in styles.entries) if (!e.value.isDefault) e.key: e.value});
          it.color = null; // replaced by per-output styles
          if (hasLevels) {
            final parsed = [for (final x in levels.value.split(',')) ?double.tryParse(x.trim())];
            final def = spec.levels;
            it.levels = parsed.length == def.length && [for (var i = 0; i < def.length; i++) parsed[i] == def[i]].every((b) => b)
                ? null
                : parsed;
          }
          applied = it;
        }
        if (it.kind == ItemKind.compare) it.display = displaySel.value;
        if (it.kind != ItemKind.price) it.pane = paneSel.value;
        if (it.kind == ItemKind.compare) it.color = color;
        if (layerSel.value != c.find(itemId)!.$1.id) c.moveItem(itemId, layerSel.value);
      }, label: 'Edit $title');
      if (applied != null) store.rememberIndicator(applied!);
      return true;
    }),
  ]);
}
