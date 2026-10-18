import 'dart:async';
import 'dart:js_interop';

import 'package:web/web.dart';

import '../chart/chart_view.dart';
import '../core/models.dart';
import 'api.dart';
import 'dom.dart';
import 'events.dart';
import 'sidebar.dart';
import 'toolbar.dart';
import 'topbar.dart';

class App {
  final HTMLElement mount;
  ChartViewState view = const ChartViewState();
  Quote? quote;
  List<IndicatorSpec> specs = const [];
  List<Annotation> annotations = const [];
  String? selectedId;
  String tool = 'select';
  List<Activity> activity = [];
  int _activitySeq = 0;
  String status = 'connecting';
  String provider = '…';
  String? _toast;
  Timer? _toastTimer, _quoteTimer;
  int _quoteSeq = 0;
  String _symbolLoaded = '';

  late final TopBar topbar;
  late final Toolbar toolbar;
  late final ChartView chart;
  late final Sidebar sidebar;
  final HTMLElement _toastEl = h('div.toast', null, {'role': 'status'});

  App(this.mount);

  String get symbol => view.symbol;

  Future<void> start() async {
    replace(mount, h('div.boot', 'Connecting to Autobench…'));
    try {
      view = await api.view();
    } catch (_) {
      view = const ChartViewState();
    }
    topbar = TopBar(
      onSymbol: (s) => changeView(view.copyWith(symbol: s)),
      onInterval: (i) => changeView(view.copyWith(interval: i)),
      onIndicators: (r) => changeView(view.copyWith(indicators: r)),
    );
    toolbar = Toolbar(
      onTool: setTool,
      onClearMine: () => _clear('user', 'your'),
      onClearAgent: () => _clear('agent', 'agent'),
    );
    chart = ChartView(
      onSelect: select,
      onDraw: _draw,
      onError: notify,
      onLoaded: (source) {
        if (source.isNotEmpty && !provider.startsWith(source)) {
          provider = source;
          _renderTop();
        }
      },
    );
    sidebar = Sidebar(
      onTab: _showTab,
      onShowTag: (tag) {
        sidebar.drawings.tagFilter = tag;
        _showTab('drawings');
      },
      onSelect: select,
      onToggle: (a) => api.updateAnnotation(a.id, {'visible': !a.visible}).catchError((Object e) {
        notify('$e');
        return a;
      }),
      onDelete: _delete,
      onTagFilter: (tag) {
        sidebar.drawings.tagFilter = tag;
        sidebar.drawings.refresh();
      },
      onPick: (s) => changeView(view.copyWith(symbol: s)),
    );
    _toastEl.isHidden = true;
    replace(mount, h('div.app', [
      topbar.root,
      h('main.main', [toolbar.root, chart.root, sidebar.root]),
      _toastEl,
    ]));
    toolbar.active = tool;
    _applyView();
    _showTab('overview');

    api.indicatorList().then((s) {
      specs = s;
      _renderTop();
    }, onError: (Object _) {});
    api.health().then((hj) {
      provider = '${hj['bar_provider'].s ?? '?'}${hj['feed'].s != null ? '/${hj['feed'].s}' : ''}';
      _renderTop();
    }, onError: (Object _) {
      provider = 'offline';
      _renderTop();
    });
    EventStream(onEvent: _onEvent, onStatus: (s) {
      status = s;
      _renderTop();
    });
    window.addEventListener('keydown', ((KeyboardEvent e) => _onKey(e)).toJS);
  }

  // ---------------------------------------------------------------- view

  /// Applies [view] to every component; reloads per-symbol data when the symbol changed.
  void _applyView() {
    chart.show(view.symbol, view.interval);
    chart.setIndicators(view.indicators);
    chart.setRange(view.fromTime, view.toTime);
    if (view.symbol != _symbolLoaded) {
      _symbolLoaded = view.symbol;
      selectedId = null;
      sidebar.drawings.tagFilter = null;
      _setSymbolOnPanels();
      _loadAnnotations();
      _startQuotes();
    }
    _renderTop();
  }

  void _setSymbolOnPanels() {
    sidebar.overview.symbol = symbol;
    sidebar.overview.quote = null;
    sidebar.agent.symbol = symbol;
    sidebar.notes.symbol = symbol;
    sidebar.watchlist.symbol = symbol;
    sidebar.refreshIfShown(sidebar.tab);
  }

  void changeView(ChartViewState next) {
    final reset = next.symbol != view.symbol || next.interval != view.interval;
    view = next;
    _applyView();
    api.setView(next, resetRange: reset).catchError((Object e) {
      notify('$e');
      return next;
    });
  }

  void _startQuotes() {
    _quoteTimer?.cancel();
    quote = null;
    Future<void> load() async {
      final seq = ++_quoteSeq;
      final s = symbol;
      try {
        final q = await api.quote(s);
        if (seq != _quoteSeq || s != symbol) return;
        quote = q;
      } catch (_) {
        if (seq == _quoteSeq) quote = null;
      }
      sidebar.overview.quote = quote;
      _renderTop();
    }

    load();
    _quoteTimer = Timer.periodic(const Duration(seconds: 15), (_) => load());
  }

  void _renderTop() => topbar.update(
        symbol: view.symbol,
        interval: view.interval,
        quote: quote,
        specs: specs,
        indicators: view.indicators,
        status: status,
        provider: provider,
      );

  // ---------------------------------------------------------------- drawings

  Future<void> _loadAnnotations() async {
    final s = symbol;
    try {
      final list = await api.annotations(s);
      if (s != symbol) return;
      annotations = list;
      _renderAnnotations();
    } catch (e) {
      notify('$e');
    }
  }

  void _renderAnnotations() {
    if (selectedId != null && !annotations.any((a) => a.id == selectedId)) selectedId = null;
    chart.setAnnotations(annotations, selectedId);
    sidebar.drawings
      ..annotations = annotations
      ..selectedId = selectedId;
    sidebar.refreshIfShown('drawings');
    sidebar.renderTabs();
  }

  void _upsert(Annotation a) {
    final i = annotations.indexWhere((x) => x.id == a.id);
    annotations = i < 0 ? [...annotations, a] : [...annotations.sublist(0, i), a, ...annotations.sublist(i + 1)];
  }

  void select(String? id) {
    selectedId = id;
    _renderAnnotations();
  }

  void setTool(String t) {
    tool = t;
    toolbar.active = t;
    chart.currentTool = t;
  }

  Future<void> _draw(String type, List<Point> points, {String? text}) async {
    var pts = points;
    if ((type == 'long_position' || type == 'short_position') && points.length == 2) {
      // two clicks: entry then target; the stop defaults to half the target distance (2R)
      final e = points[0], t = points[1];
      final ep = e.price!, tp = t.price!;
      final ok = type == 'long_position' ? tp > ep : tp < ep;
      if (!ok) {
        notify(type == 'long_position' ? 'Long: click the target above the entry' : 'Short: click the target below the entry');
        return;
      }
      final stop = ep - (tp - ep) / 2;
      pts = [e, Point(t.time, double.parse(stop.toStringAsFixed(4))), Point(t.time, tp)];
    }
    try {
      final created = await api.createAnnotation(symbol, type, pts, text: text);
      _upsert(created);
      selectedId = created.id;
      setTool('select');
      _renderAnnotations();
    } catch (e) {
      notify('$e');
    }
  }

  Future<void> _delete(Annotation a) async {
    try {
      await api.deleteAnnotation(a.id);
      annotations = annotations.where((x) => x.id != a.id).toList();
      if (selectedId == a.id) selectedId = null;
      _renderAnnotations();
    } catch (e) {
      notify('$e');
    }
  }

  Future<void> _clear(String author, String whose) async {
    try {
      final n = await api.deleteAnnotations(symbol, author: author);
      notify('Removed $n $whose drawings');
    } catch (e) {
      notify('$e');
    }
  }

  void _onKey(KeyboardEvent e) {
    if (typingIn(e.target)) return;
    final id = selectedId;
    if ((e.key == 'Delete' || e.key == 'Backspace') && id != null) {
      for (final a in annotations) {
        if (a.id == id) {
          e.preventDefault();
          _delete(a);
          break;
        }
      }
    }
    if (e.key == 'Escape') {
      setTool('select');
      select(null);
    }
  }

  // ---------------------------------------------------------------- sidebar & activity

  void _showTab(String t) {
    if (t == 'agent') sidebar.unseen = 0;
    sidebar.show(t);
  }

  void _pushActivity(String text, String kind, String? sym) {
    final now = DateTime.now();
    final last = activity.isEmpty ? null : activity.first;
    // collapse bursts ("Drew 3 objects") from one batch
    if (last != null &&
        last.kind == kind &&
        last.symbol == sym &&
        now.difference(last.at).inSeconds < 3 &&
        last.text.startsWith('Drew') &&
        text.startsWith('Drew')) {
      final m = RegExp(r'Drew (\d+) objects?').firstMatch(last.text);
      final count = (m == null ? 1 : int.parse(m.group(1)!)) + 1;
      activity = [Activity(last.id, now, 'Drew $count objects on $sym', sym, kind), ...activity.skip(1)];
    } else {
      activity = [Activity(++_activitySeq, now, text, sym, kind), ...activity.take(99)];
    }
    if (kind == 'agent' && sidebar.tab != 'agent') sidebar.unseen++;
    sidebar.agent
      ..activity = activity
      ..render();
    sidebar.renderTabs();
  }

  void _onEvent(ServerEvent e) {
    final d = e.data;
    switch (e.type) {
      case 'hello':
        if (d['view'].truthy) _applyRemoteView(ChartViewState.fromJson(d['view']));
      case 'view.changed':
        _applyRemoteView(ChartViewState.fromJson(d));
        if (d['updated_by'].s == 'agent') {
          _pushActivity('Switched chart to ${d['symbol'].s} · ${d['interval'].s}', 'agent', d['symbol'].s);
        }
      case 'annotation.created':
        final a = Annotation.fromJson(d);
        if (a.symbol == symbol) {
          _upsert(a);
          _renderAnnotations();
        }
        if (a.author == 'agent') _pushActivity('Drew 1 object on ${a.symbol}', 'agent', a.symbol);
      case 'annotation.updated':
        final a = Annotation.fromJson(d);
        if (annotations.any((x) => x.id == a.id)) {
          _upsert(a);
          _renderAnnotations();
        }
      case 'annotation.deleted':
        final id = d['id'].s;
        if (annotations.any((x) => x.id == id)) {
          annotations = annotations.where((x) => x.id != id).toList();
          _renderAnnotations();
        }
      case 'analysis.created':
        sidebar.refreshIfShown('agent');
        final action = (d['action'].s ?? '').replaceAll('_', ' ').toUpperCase();
        final conf = ((d['confidence'].d ?? 0) * 100).round();
        _pushActivity('Saved analysis: $action ${d['symbol'].s} ($conf%)', d['author'].s == 'agent' ? 'agent' : 'user',
            d['symbol'].s);
      case 'note.saved' || 'note.deleted':
        sidebar.refreshIfShown('notes');
        if (e.type == 'note.saved' && d['author'].s == 'agent') {
          _pushActivity('Saved note: ${d['title'].s}', 'agent', d['symbol'].s);
        }
      case 'watchlist.updated' || 'watchlist.deleted':
        sidebar.refreshIfShown('watchlist');
    }
  }

  void _applyRemoteView(ChartViewState v) {
    view = v;
    _applyView();
  }

  void notify(String msg) {
    _toast = msg;
    _toastEl
      ..textContent = msg
      ..isHidden = false;
    _toastTimer?.cancel();
    _toastTimer = Timer(const Duration(seconds: 5), () {
      if (_toast == msg) _toastEl.isHidden = true;
    });
  }
}
