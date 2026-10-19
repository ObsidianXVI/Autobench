// Application state shared by pages and panels, and its persistence.
//
// What is stored where:
// - UI state (pages, chart grid, each chart's symbol/interval/zoom, panels, every preference) →
//   /api/ui-state, mirrored to localStorage so a reload restores instantly and nothing is lost
//   if the server is briefly unreachable; flushed with sendBeacon when the page is hidden/closed.
// - Per-symbol chart configuration (layers: price, volume, indicators, other symbols) →
//   /api/chart-config/<SYMBOL>, plus a "_default" template for symbols never opened.
// - Drawings, alerts, notes, watchlists, layouts → their own API resources.
import 'dart:async';
import 'dart:convert';
import 'dart:js_interop';

import 'package:web/web.dart';

import '../core/json.dart';
import '../core/layers.dart';
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

/// One chart cell of the workbench grid. What it *shows* for a symbol (layers) belongs to the
/// symbol; the cell only knows which symbol, which interval and how far it is zoomed.
class ChartState {
  final Obs<String> symbol;
  final Obs<String> interval;
  final Obs<double?> spacing; // bar spacing in px (zoom); null = default
  ChartState({String symbol = 'SPY', String interval = '1d', double? spacing})
      : symbol = Obs(symbol),
        interval = Obs(interval),
        spacing = Obs(spacing);

  Map<String, Object?> toJson() => {'symbol': symbol.value, 'interval': interval.value, 'spacing': spacing.value};

  factory ChartState.fromJson(J j) =>
      ChartState(symbol: j['symbol'].s ?? 'SPY', interval: j['interval'].s ?? '1d', spacing: j['spacing'].d);

  List<Obs<Object?>> get all => [symbol, interval];
}

const gridModes = ['1', '2v', '2h', '3', '4'];
int chartsFor(String grid) => switch (grid) { '1' => 1, '2v' || '2h' => 2, '3' => 3, _ => 4 };

const uiStateVersion = 3;
const _localKey = 'autobench.uiState';
const defaultFavoriteIntervals = ['1m', '5m', '15m', '1h', '1d', '1wk'];

/// Every user preference, with its default. Anything set through [Store.setPref] persists.
const prefDefaults = <String, Object?>{
  'chart.grid': true,
  'chart.countdown': true,
  'chart.markers': true,
  'chart.lastPriceLine': true,
  'chart.upColor': null, // null = theme color
  'chart.downColor': null,
  'chart.favoriteIntervals': defaultFavoriteIntervals,
  'draw.magnet': true,
  'draw.stay': false,
  'draw.hidden': false,
  'draw.locked': false,
  'draw.groups': <String, Object?>{}, // last tool chosen per toolbar group
  'watchlist.sort': '',
  'watchlist.collapsed': <Object?>[],
  'watchlist.detailsHeight': 300.0,
  'alerts.filter': 'all',
  'news.market': false,
  'calendar.tab': 'earnings',
  'calendar.universe': 'sp500',
  'calendar.minCap': 0.0,
  'calendar.country': 'United States',
  'markets.sectorPeriod': '1d',
  'markets.boardGroup': 'Futures',
  'recentSymbols': <Object?>[],
};

class Store {
  // shell
  final page = Obs<String>('chart');
  final theme = Obs<String>('system'); // system | dark | light
  final status = Obs<String>('connecting');
  final provider = Obs<String>('…');
  final specs = Obs<List<IndicatorSpec>>(const []);

  // preferences (persisted)
  final prefs = Obs<Map<String, Object?>>({});

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

  // per-symbol chart configuration (layers)
  final Map<String, SymbolConfig> _configs = {};
  final Map<String, Future<SymbolConfig>> _configLoads = {};
  final Set<String> _dirtyConfigs = {};
  final Map<String, Timer> _configTimers = {};
  final configVersion = Obs<int>(0);
  String? lastConfigChanged;
  SymbolConfig? _template;

  // alerts
  final alerts = Obs<List<AlertInfo>>(const []);

  // workbench layout
  final grid = Obs<String>('1');
  final charts = <ChartState>[ChartState()];
  final active = Obs<int>(0);
  final syncSymbol = Obs<bool>(false);
  final syncInterval = Obs<bool>(false);
  final syncCrosshair = Obs<bool>(true);
  final syncRange = Obs<bool>(false);
  final layoutName = Obs<String?>(null);
  final layoutVersion = Obs<int>(0); // bumped when the set of charts is replaced wholesale

  // right widget panel
  final panel = Obs<String?>('watchlist'); // null = collapsed
  final panelWidth = Obs<double>(330);
  final watchlist = Obs<String>('default');

  // page states (persisted so pages reopen where the user left them)
  final screenerState = Obs<Map<String, Object?>>({});
  final heatmapState = Obs<Map<String, Object?>>({'universe': 'sp500', 'period': '1d'});

  final navigate = Obs<J?>(null);
  final activity = Obs<List<Activity>>(const []);
  final unseenAgent = Obs<int>(0);
  final analysesVersion = Obs<int>(0);
  final notesVersion = Obs<int>(0);
  final watchlistVersion = Obs<int>(0);

  ChartState get activeChart => charts[active.value.clamp(0, charts.length - 1)];

  Store() {
    // tool toggles mirror their preferences
    for (final (obs, key) in [(magnet, 'draw.magnet'), (stayInDrawMode, 'draw.stay'), (drawingsHidden, 'draw.hidden'),
      (drawingsLocked, 'draw.locked')]) {
      obs.listen((v) => setPref(key, v));
    }
  }

  // ---------------------------------------------------------------- preferences

  T pref<T>(String key) {
    final v = prefs.value.containsKey(key) ? prefs.value[key] : prefDefaults[key];
    if (v is T) return v;
    final d = prefDefaults[key];
    if (T == double && d is num) return d.toDouble() as T;
    return d as T;
  }

  List<String> prefList(String key) => [for (final v in (pref<List<Object?>?>(key) ?? const [])) '$v'];

  void setPref(String key, Object? value) {
    if (prefs.value[key] == value) return;
    prefs.value = {...prefs.value, key: value};
    save();
  }

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

  // ---------------------------------------------------------------- chart configs (layers)

  SymbolConfig? configNow(String symbol) => _configs[symbol];

  /// The symbol's layers: the saved configuration, or a copy of the default template.
  Future<SymbolConfig> config(String symbol) {
    final have = _configs[symbol];
    if (have != null) return Future.value(have);
    return _configLoads.putIfAbsent(symbol, () async {
      SymbolConfig cfg;
      try {
        final j = await request('/api/chart-config/${Uri.encodeComponent(symbol)}');
        cfg = j['layers'].v is List ? SymbolConfig.fromJson(j) : (await defaultTemplate()).clone();
      } catch (_) {
        cfg = (await defaultTemplate()).clone();
      }
      _configs.putIfAbsent(symbol, () => cfg);
      _configLoads.remove(symbol);
      lastConfigChanged = symbol;
      configVersion.value++;
      return _configs[symbol]!;
    });
  }

  Future<SymbolConfig> defaultTemplate() async {
    if (_template != null) return _template!;
    try {
      final j = await request('/api/chart-config/_default');
      _template = j['layers'].v is List ? SymbolConfig.fromJson(j) : SymbolConfig.builtin();
    } catch (_) {
      _template = SymbolConfig.builtin();
    }
    return _template!;
  }

  /// Applies [change] to the symbol's layers, records it for undo and saves it.
  void editConfig(String symbol, void Function(SymbolConfig c) change, {String label = 'Edit layers', bool record = true}) {
    final cfg = _configs[symbol];
    if (cfg == null) return;
    final before = jsonEncode(cfg.toJson());
    change(cfg);
    cfg.normalize();
    final after = jsonEncode(cfg.toJson());
    if (before == after) return;
    _changed(symbol);
    if (record) {
      undo.push(Command(label, undo: () async => _replaceConfig(symbol, before), redo: () async => _replaceConfig(symbol, after)));
      undoVersion.value++;
    }
  }

  void _replaceConfig(String symbol, String json) {
    _configs[symbol] = SymbolConfig.fromJson(J(jsonDecode(json)));
    _changed(symbol);
  }

  void _changed(String symbol) {
    lastConfigChanged = symbol;
    configVersion.value++;
    _dirtyConfigs.add(symbol);
    _configTimers[symbol]?.cancel();
    _configTimers[symbol] = Timer(const Duration(milliseconds: 500), () => _saveConfig(symbol));
  }

  Future<void> _saveConfig(String symbol) async {
    final cfg = _configs[symbol];
    if (cfg == null) return;
    try {
      await request('/api/chart-config/${Uri.encodeComponent(symbol)}', method: 'PUT', body: cfg.toJson());
      _dirtyConfigs.remove(symbol);
    } catch (_) {
      // kept dirty: retried on the next change or flushed on page hide
    }
  }

  Future<void> saveAsDefault(String symbol) async {
    final cfg = _configs[symbol];
    if (cfg == null) return;
    final tpl = cfg.clone();
    // drawings are per symbol; the template carries only layers that hold series
    tpl.layers.removeWhere((l) => l.items.isEmpty && l.id != mainLayerId);
    tpl.normalize();
    _template = tpl;
    await request('/api/chart-config/_default', method: 'PUT', body: tpl.toJson());
  }

  Future<void> resetToDefault(String symbol) async {
    final tpl = await defaultTemplate();
    final keepDrawingLayers = _configs[symbol]?.layers.where((l) => l.items.isEmpty).toList() ?? const [];
    editConfig(symbol, (c) {
      final fresh = tpl.clone();
      c.layers
        ..clear()
        ..addAll(fresh.layers);
      for (final l in keepDrawingLayers) {
        if (c.layer(l.id) == null) c.layers.insert(0, l);
      }
      c.activeLayer = fresh.activeLayer;
      c.scale = fresh.scale;
    }, label: 'Reset layers');
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
      charts.add(ChartState(symbol: src.symbol.value, interval: src.interval.value));
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

  /// Replaces the workbench with a saved layout (grid, charts, sync settings). Older layouts that
  /// stored indicators per chart seed the symbols' layers when those have none yet.
  void applyLayout(J data) {
    final cs = [for (final c in data['charts'].list) ChartState.fromJson(c)];
    if (cs.isEmpty) return;
    for (final c in data['charts'].list) {
      if (c['indicators'].v is List) _migrateLegacyChart(c);
    }
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

  /// v2 kept indicators/chart type/compares on the chart; move them into the symbol's layers.
  Future<void> _migrateLegacyChart(J c) async {
    final sym = c['symbol'].s;
    if (sym == null) return;
    try {
      final existing = await request('/api/chart-config/${Uri.encodeComponent(sym)}');
      if (existing['layers'].v is List) return;
      final cfg = SymbolConfig.fromLegacy(
        indicators: [for (final r in c['indicators'].list) IndicatorRef.fromJson(r)],
        chartType: c['chartType'].s ?? 'candles',
        scale: c['scale'].s ?? 'linear',
        compares: [for (final s in c['compares'].list) s.s ?? ''],
      );
      _configs[sym] = cfg;
      _changed(sym);
    } catch (_) {}
  }

  // ---------------------------------------------------------------- persistence

  Timer? _saveTimer;
  bool _restoring = false;

  Map<String, Object?> _uiState() => {
        'v': uiStateVersion,
        'savedAt': DateTime.now().millisecondsSinceEpoch,
        'page': page.value,
        'theme': theme.value,
        'layout': layoutJson(),
        'layoutName': layoutName.value,
        'panel': panel.value,
        'panelWidth': panelWidth.value,
        'watchlist': watchlist.value,
        'prefs': prefs.value,
        'screener': screenerState.value,
        'heatmap': heatmapState.value,
      };

  void save() {
    if (_restoring) return;
    final state = _uiState();
    _writeLocal(state);
    _saveTimer?.cancel();
    _saveTimer = Timer(const Duration(milliseconds: 600), () {
      request('/api/ui-state', method: 'PUT', body: state).catchError((Object _) => const J(null));
    });
  }

  void _writeLocal(Map<String, Object?> state) {
    try {
      window.localStorage.setItem(_localKey, jsonEncode(state));
    } catch (_) {}
  }

  /// Sends everything still pending (UI state and unsaved layer edits) – used when the page is
  /// hidden or closed, where normal requests may be cancelled.
  void flush() {
    final state = _uiState();
    _writeLocal(state);
    _saveTimer?.cancel();
    try {
      window.navigator.sendBeacon('/api/ui-state', jsonEncode(state).toJS);
      for (final sym in _dirtyConfigs) {
        final cfg = _configs[sym];
        if (cfg == null) continue;
        window.navigator.sendBeacon('/api/chart-config/${Uri.encodeComponent(sym)}', jsonEncode(cfg.toJson()).toJS);
      }
      _dirtyConfigs.clear();
    } catch (_) {}
  }

  Future<void> restore() async {
    _restoring = true;
    try {
      J? server;
      try {
        server = await request('/api/ui-state');
      } catch (_) {}
      J? local;
      try {
        final raw = window.localStorage.getItem(_localKey);
        if (raw != null) local = J(jsonDecode(raw));
      } catch (_) {}
      // the newer of the two wins (the local copy covers saves the server never received);
      // an explicitly emptied server state (v missing) resets the local mirror too
      var s = server;
      final serverEmpty = server != null && server['v'].isNull;
      if (!serverEmpty && local != null && (s == null || (local['savedAt'].i ?? 0) > (s['savedAt'].i ?? 0))) s = local;
      if (s == null) return;
      final v = s['v'].i ?? 0;
      if (v < 2) return;
      theme.value = s['theme'].s ?? 'system';
      if (s['prefs'].v is Map) prefs.value = Map<String, Object?>.from(s['prefs'].v as Map);
      if (v == 2) {
        // v2 kept tool toggles at the top level
        prefs.value = {...prefs.value, 'draw.magnet': s['magnet'].b ?? true, 'draw.stay': s['stay'].b ?? false};
      }
      magnet.value = pref<bool>('draw.magnet');
      stayInDrawMode.value = pref<bool>('draw.stay');
      drawingsHidden.value = pref<bool>('draw.hidden');
      drawingsLocked.value = pref<bool>('draw.locked');
      if (s['layout'].truthy) applyLayout(s['layout']);
      layoutName.value = s['layoutName'].s;
      panel.value = s['panel'].isNull ? null : s['panel'].s;
      panelWidth.value = (s['panelWidth'].d ?? 330).clamp(240, 640);
      watchlist.value = s['watchlist'].s ?? 'default';
      if (s['screener'].v is Map) screenerState.value = Map<String, Object?>.from(s['screener'].v as Map);
      if (s['heatmap'].v is Map) heatmapState.value = Map<String, Object?>.from(s['heatmap'].v as Map);
      page.value = s['page'].s ?? 'chart';
    } finally {
      _restoring = false;
    }
  }
}

final store = Store();
