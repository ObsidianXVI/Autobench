// Application state shared by pages and panels. Persisted pieces (layout, charts, panel sizes,
// theme, screener state) are saved to the backend's /api/ui-state, so they survive reloads and
// follow the user across browsers.
import 'dart:async';
import 'dart:convert';

import '../core/json.dart';
import '../core/models.dart';
import '../core/undo.dart';
import 'api.dart';
import 'kit/state.dart';

class AlertInfo {
  final String id, symbol, kind, status, description;
  final double? value;
  final String? message, annotationId, triggeredAt, expression;
  final double? triggerPrice;
  final bool repeat;
  final String author;
  const AlertInfo(this.id, this.symbol, this.kind, this.status, this.description, this.value, this.message,
      this.annotationId, this.triggeredAt, this.triggerPrice, this.repeat, this.author, this.expression);

  factory AlertInfo.fromJson(J j) => AlertInfo(
      j['id'].s ?? '', j['symbol'].s ?? '', j['kind'].s ?? '', j['status'].s ?? '', j['description'].s ?? '',
      j['value'].d, j['message'].s, j['annotation_id'].s, j['triggered_at'].s, j['trigger_price'].d,
      j['repeat'].b ?? false, j['author'].s ?? 'user', j['expression'].s);
}

class Activity {
  final int id;
  final DateTime at;
  final String text;
  final String? symbol;
  final String kind; // agent | user | system
  const Activity(this.id, this.at, this.text, this.symbol, this.kind);
}

/// One chart in the workbench grid.
class ChartState {
  final Obs<String> symbol;
  final Obs<String> interval;
  final Obs<List<IndicatorRef>> indicators;
  final Obs<String> chartType;
  final Obs<String> scale; // linear | log | percent
  final Obs<List<String>> compares;
  final Obs<List<String>> hidden; // indicator keys hidden from view
  ChartState({String symbol = 'SPY', String interval = '1d', List<IndicatorRef> indicators = const [],
      String chartType = 'candles', String scale = 'linear', List<String> compares = const [],
      List<String> hidden = const []})
      : symbol = Obs(symbol),
        interval = Obs(interval),
        indicators = Obs(indicators),
        chartType = Obs(chartType),
        scale = Obs(scale),
        compares = Obs(compares),
        hidden = Obs(hidden);

  Map<String, Object?> toJson() => {
        'symbol': symbol.value, 'interval': interval.value,
        'indicators': [for (final r in indicators.value) r.toJson()],
        'chartType': chartType.value, 'scale': scale.value, 'compares': compares.value, 'hidden': hidden.value,
      };

  factory ChartState.fromJson(J j) => ChartState(
        symbol: j['symbol'].s ?? 'SPY',
        interval: j['interval'].s ?? '1d',
        indicators: [for (final r in j['indicators'].list) IndicatorRef.fromJson(r)],
        chartType: j['chartType'].s ?? 'candles',
        scale: j['scale'].s ?? 'linear',
        compares: [for (final c in j['compares'].list) c.s ?? ''],
        hidden: [for (final c in j['hidden'].list) c.s ?? ''],
      );

  List<Obs<Object?>> get all => [symbol, interval, indicators, chartType, scale, compares, hidden];
}

const gridModes = ['1', '2v', '2h', '3', '4'];

/// Indicators on a fresh chart (same as the backend's default view).
const defaultIndicators = [
  IndicatorRef('sma', {'length': 50}),
  IndicatorRef('sma', {'length': 200}),
  IndicatorRef('rsi', {}),
];
int chartsFor(String grid) => switch (grid) { '1' => 1, '2v' || '2h' => 2, '3' => 3, _ => 4 };

class Store {
  // shell
  final page = Obs<String>('chart');
  final theme = Obs<String>('system'); // system | dark | light
  final status = Obs<String>('connecting');
  final provider = Obs<String>('…');
  final specs = Obs<List<IndicatorSpec>>(const []);

  // drawing tools (shared by all charts, like TradingView)
  final tool = Obs<String>('select');
  final stayInDrawMode = Obs<bool>(false);
  final magnet = Obs<bool>(true);
  final drawingsHidden = Obs<bool>(false);
  final drawingsLocked = Obs<bool>(false);
  final selected = Obs<String?>(null); // selected drawing id
  final undo = UndoStack();
  final undoVersion = Obs<int>(0);

  // drawings per symbol (kept in sync by WebSocket events)
  final Map<String, List<Annotation>> _drawings = {};
  final drawingsVersion = Obs<int>(0);

  // alerts
  final alerts = Obs<List<AlertInfo>>(const []);

  // workbench layout
  final grid = Obs<String>('1');
  final charts = <ChartState>[ChartState(indicators: defaultIndicators)];
  final active = Obs<int>(0);
  final syncSymbol = Obs<bool>(false);
  final syncInterval = Obs<bool>(false);
  final syncCrosshair = Obs<bool>(true);
  final syncRange = Obs<bool>(false);
  final layoutName = Obs<String?>(null);

  // right widget panel
  final panel = Obs<String?>('watchlist'); // null = collapsed
  final panelWidth = Obs<double>(330);
  final watchlist = Obs<String>('default');

  // screener (persisted so the page reopens where the user left it)
  final screenerState = Obs<Map<String, Object?>>({});
  final heatmapState = Obs<Map<String, Object?>>({'universe': 'sp500', 'period': '1d'});

  // pending navigation payload (from the agent's show_in_ui)
  final navigate = Obs<J?>(null);

  // live agent activity (WebSocket) and the unseen counter on the Agent rail icon
  final activity = Obs<List<Activity>>(const []);
  final unseenAgent = Obs<int>(0);
  final analysesVersion = Obs<int>(0);
  final notesVersion = Obs<int>(0);
  final watchlistVersion = Obs<int>(0);

  ChartState get activeChart => charts[active.value.clamp(0, charts.length - 1)];

  // ---------------------------------------------------------------- drawings

  List<Annotation> drawingsFor(String symbol) => _drawings[symbol] ?? const [];

  Future<void> loadDrawings(String symbol, {bool force = false}) async {
    if (_drawings.containsKey(symbol) && !force) return;
    _drawings[symbol] = await api.annotations(symbol);
    drawingsVersion.value++;
  }

  void upsertDrawing(Annotation a) {
    final list = [...?_drawings[a.symbol]];
    final i = list.indexWhere((x) => x.id == a.id);
    if (i < 0) {
      list.add(a);
    } else {
      list[i] = a;
    }
    _drawings[a.symbol] = list;
    drawingsVersion.value++;
  }

  void removeDrawing(String id) {
    for (final e in _drawings.entries) {
      if (e.value.any((a) => a.id == id)) {
        _drawings[e.key] = e.value.where((a) => a.id != id).toList();
        if (selected.value == id) selected.value = null;
        drawingsVersion.value++;
        return;
      }
    }
  }

  Annotation? drawingById(String id) {
    for (final list in _drawings.values) {
      for (final a in list) {
        if (a.id == id) return a;
      }
    }
    return null;
  }

  // ---------------------------------------------------------------- alerts

  Future<void> loadAlerts() async {
    try {
      alerts.value = [for (final a in (await request('/api/alerts')).list) AlertInfo.fromJson(a)];
    } catch (_) {}
  }

  // ---------------------------------------------------------------- layout

  void setGrid(String g) {
    final n = chartsFor(g);
    while (charts.length < n) {
      final src = activeChart;
      charts.add(ChartState(symbol: src.symbol.value, interval: src.interval.value,
          indicators: List.of(src.indicators.value)));
    }
    if (active.value >= n) active.value = 0;
    grid.value = g;
    save();
  }

  Map<String, Object?> layoutJson() => {
        'grid': grid.value,
        'charts': [for (final c in charts.take(chartsFor(grid.value))) c.toJson()],
        'active': active.value,
        'sync': {'symbol': syncSymbol.value, 'interval': syncInterval.value, 'crosshair': syncCrosshair.value,
                 'range': syncRange.value},
      };

  /// Replaces the workbench with a saved layout (grid, charts, sync settings).
  void applyLayout(J data) {
    final cs = [for (final c in data['charts'].list) ChartState.fromJson(c)];
    if (cs.isEmpty) return;
    charts
      ..clear()
      ..addAll(cs);
    final g = data['grid'].s ?? '1';
    grid.value = gridModes.contains(g) ? g : '1';
    while (charts.length < chartsFor(grid.value)) {
      charts.add(ChartState(symbol: charts.first.symbol.value));
    }
    active.value = (data['active'].i ?? 0).clamp(0, charts.length - 1);
    syncSymbol.value = data['sync']['symbol'].b ?? false;
    syncInterval.value = data['sync']['interval'].b ?? false;
    syncCrosshair.value = data['sync']['crosshair'].b ?? true;
    syncRange.value = data['sync']['range'].b ?? false;
    layoutVersion.value++;
    save();
  }

  /// Bumped when the set of charts is replaced wholesale (the chart grid rebuilds).
  final layoutVersion = Obs<int>(0);

  // ---------------------------------------------------------------- persistence

  Timer? _saveTimer;
  bool _restoring = false;

  void save() {
    if (_restoring) return;
    _saveTimer?.cancel();
    _saveTimer = Timer(const Duration(milliseconds: 600), () {
      final state = {
        'v': 2,
        'page': page.value,
        'theme': theme.value,
        'layout': layoutJson(),
        'layoutName': layoutName.value,
        'panel': panel.value,
        'panelWidth': panelWidth.value,
        'watchlist': watchlist.value,
        'magnet': magnet.value,
        'stay': stayInDrawMode.value,
        'screener': screenerState.value,
        'heatmap': heatmapState.value,
      };
      request('/api/ui-state', method: 'PUT', body: state).catchError((Object _) => const J(null));
    });
  }

  Future<void> restore() async {
    _restoring = true;
    try {
      final s = await request('/api/ui-state');
      if ((s['v'].i ?? 0) == 2) {
        theme.value = s['theme'].s ?? 'system';
        if (s['layout'].truthy) applyLayout(s['layout']);
        layoutName.value = s['layoutName'].s;
        panel.value = s['panel'].isNull ? null : s['panel'].s;
        panelWidth.value = (s['panelWidth'].d ?? 330).clamp(240, 640);
        watchlist.value = s['watchlist'].s ?? 'default';
        magnet.value = s['magnet'].b ?? true;
        stayInDrawMode.value = s['stay'].b ?? false;
        if (s['screener'].v is Map) screenerState.value = Map<String, Object?>.from(s['screener'].v as Map);
        if (s['heatmap'].v is Map) heatmapState.value = Map<String, Object?>.from(s['heatmap'].v as Map);
        page.value = s['page'].s ?? 'chart';
      }
    } catch (_) {
      // first run or server without state: defaults
    } finally {
      _restoring = false;
    }
  }

  String encode() => jsonEncode(layoutJson());
}

final store = Store();
