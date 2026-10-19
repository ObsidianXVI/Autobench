// Application shell: top navigation (hash routed pages), theme, alerts bell, connection status,
// WebSocket event handling (agent ↔ active chart view sync, drawings, alerts, navigation).
import 'dart:async';
import 'dart:convert';
import 'dart:js_interop';
import 'dart:js_interop_unsafe';

import 'package:web/web.dart';

import '../chart/chart_page.dart';
import '../chart/chart_panel.dart';
import '../core/json.dart';
import '../core/layers.dart';
import '../core/models.dart';
import 'api.dart';
import 'dom.dart';
import 'events.dart';
import 'kit/icons.dart';
import 'kit/overlay.dart';
import 'kit/state.dart';
import 'pages/calendar.dart';
import 'pages/heatmap.dart';
import 'pages/markets.dart';
import 'pages/screener.dart';
import 'pages/trading.dart';
import 'panels/dock.dart';
import 'store.dart';
import 'trading_state.dart';
import 'trading_ui.dart';
import '../core/trading.dart';

const pages = [
  ('chart', 'Chart', 'candles'),
  ('screener', 'Screener', 'filter'),
  ('heatmap', 'Heatmap', 'map'),
  ('markets', 'Markets', 'globe'),
  ('calendar', 'Calendar', 'calendar'),
  ('trading', 'Trading', 'wallet'),
];

class App {
  final HTMLElement mount;
  final HTMLElement _nav = h('header.app-nav');
  final HTMLElement _pages = h('main.app-pages');
  late final Dock dock;
  late final ChartPage chartPage;
  final Map<String, HTMLElement> _pageRoots = {};
  final Map<String, Object> _pageObjects = {};
  final Scope _scope = Scope();
  int _activitySeq = 0;
  bool _applyingRemoteView = false;
  bool _firstHello = true;
  final _viewSync = Debouncer(400);

  App(this.mount);

  Future<void> start() async {
    installTooltips();
    await store.restore();
    _applyTheme();
    final fromHash = _pageFromHash();
    if (fromHash != null) store.page.value = fromHash;

    dock = Dock(onCreateAlert: (s) => showAlertDialog(s));
    chartPage = ChartPage(dock.root);
    _pageRoots['chart'] = chartPage.root;
    _pageObjects['chart'] = chartPage;
    replace(mount, h('div.app', [_nav, _pages]));
    _scope.listen(store.page, (_) => _showPage(), now: true);
    _scope.listen(store.theme, (_) => _applyTheme());
    _scope.listen(store.status, (_) => _renderNav());
    _scope.listen(store.provider, (_) => _renderNav());
    _scope.listen(store.alerts, (_) => _renderNav());
    _scope.listen(trade.status, (_) => _renderNav());
    _scope.listen(trade.pending, (_) => _renderNav());
    trade.start();
    window.addEventListener('hashchange', ((Event _) {
      final p = _pageFromHash();
      if (p != null) store.page.value = p;
    }).toJS);
    window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', ((Event _) => _applyTheme()).toJS);
    window.addEventListener('create-alert', ((CustomEvent e) {
      showAlertDialog((e.detail as JSString).toDart);
    }).toJS);

    // agent sync: what the user looks at on the active chart is the shared "view"
    void watchActive() {
      for (final c in store.charts) {
        for (final o in <Obs<Object?>>[c.symbol, c.interval]) {
          _scope.add(o.listen((_) {
            if (identical(c, store.activeChart)) _pushView();
          }));
        }
      }
    }

    _scope.listen(store.configVersion, (_) {
      if (store.lastConfigChanged == store.activeChart.symbol.value) _pushView();
    });
    // save everything still pending when the tab is hidden or closed
    document.addEventListener('visibilitychange', ((Event _) {
      if (document.hidden) store.flush();
    }).toJS);
    window.addEventListener('pagehide', ((Event _) => store.flush()).toJS);

    watchActive();
    _scope.listen(store.layoutVersion, (_) => watchActive());
    _scope.listen(store.active, (_) => _pushView());

    attempt(api.indicatorList()).then((s) {
      if (s != null) store.specs.value = s;
    });
    api.health().then((hj) {
      store.provider.value = '${hj['bar_provider'].s ?? '?'}${hj['feed'].s != null ? '/${hj['feed'].s}' : ''}';
    }, onError: (Object _) => store.provider.value = 'offline');
    store.loadAlerts();
    EventStream(onEvent: _onEvent, onStatus: (s) => store.status.value = s);
    _renderNav();
  }

  String? _pageFromHash() {
    final hash = window.location.hash.replaceFirst('#', '').split('/').first;
    return pages.any((p) => p.$1 == hash) ? hash : null;
  }

  // ---------------------------------------------------------------- navigation

  void _renderNav() {
    final current = store.page.value;
    final triggered = store.alerts.value.where((a) => a.status == 'triggered').length;
    final active = store.alerts.value.where((a) => a.status == 'active').length;
    final st = store.status.value;
    replace(_nav, [
      h('a.brand', [h('img', null, {'src': 'favicon.svg', 'alt': '', 'width': '22', 'height': '22'}), h('span', 'Autobench')],
          {'href': '#chart'}),
      h('nav.page-tabs', [
        for (final (key, label, ic) in pages)
          h('a.page-tab${key == current ? '.on' : ''}', [icon(ic, size: 16), h('span', label)],
              {'href': '#$key', 'aria-current': key == current ? 'page' : 'false'}),
      ]),
      h('span.spacer'),
      _tradeNav(),
      h('button.nav-btn', [icon('alarm', size: 17), if (triggered > 0) h('span.nav-badge', '$triggered')], {
        'type': 'button',
        'data-tip': '$active active alert${active == 1 ? '' : 's'}${triggered > 0 ? ', $triggered triggered' : ''}',
        'aria-label': 'Alerts',
      }, {
        'click': (_) {
          store.page.value = 'chart';
          store.panel.value = 'alerts';
        },
      }),
      h('button.nav-btn', icon(_themeIcon, size: 17), {'type': 'button', 'data-tip': 'Theme: ${store.theme.value}', 'aria-label': 'Theme'}, {
        'click': (e) => showMenu([
              for (final (k, label) in [('system', 'System'), ('dark', 'Dark'), ('light', 'Light')])
                MenuItem(label, checked: store.theme.value == k, onSelect: () {
                  store.theme.value = k;
                  store.save();
                }),
            ], anchor: e.currentTarget as Element, align: 'end'),
      }),
      h('button.nav-btn', icon('keyboard', size: 17),
          {'type': 'button', 'data-tip': 'Keyboard shortcuts (?)', 'aria-label': 'Shortcuts'}, {'click': (_) => showShortcuts()}),
      h('div.conn', [
        h(st == 'open' ? 'span.dot.ok' : 'span.dot.warn'),
        h('span.muted.small', '${st == 'open' ? 'live' : st} · ${store.provider.value}'),
      ], {'data-tip': 'Live connection to the backend; agent updates arrive here', 'data-tip-pos': 'bottom'}),
    ]);
  }

  HTMLElement _tradeNav() {
    final st = trade.status.value;
    final pending = trade.pending.value.length;
    if (st == null) return h('span');
    final label = !st.connected ? '${st.brokerLabel} offline' : '${st.brokerLabel} ${st.isLive ? 'LIVE' : 'PAPER'}';
    return h('button.nav-btn.trade-nav${st.halted ? '.halted' : ''}${st.isLive ? '.live' : ''}', [
      h('span.dot${st.connected ? '.ok' : '.warn'}'),
      h('span.small', st.halted ? 'HALTED' : label),
      if (pending > 0) h('span.nav-badge.agent', '$pending'),
    ], {
      'type': 'button',
      'data-tip': pending > 0
          ? '$pending agent order proposal${pending == 1 ? '' : 's'} awaiting your approval'
          : (st.connected ? 'Account ${st.account ?? ''} · agent orders: ${st.mode == 'auto' ? 'auto (paper)' : 'you confirm each'}' : (st.message ?? '')),
      'data-tip-pos': 'bottom',
      'aria-label': 'Trading account',
    }, {
      'click': (_) {
        if (pending > 0) store.setPref('trading.tab', 'proposals');
        store.page.value = 'trading';
      },
    });
  }

  String get _themeIcon => store.theme.value == 'light' ? 'sun' : store.theme.value == 'dark' ? 'moon' : 'settings';

  void _showPage() {
    final key = store.page.value;
    if (_pageFromHash() != key) window.history.replaceState(null, '', '#$key');
    _pageRoots.putIfAbsent(key, () => _createPage(key));
    for (final e in _pageRoots.entries) {
      if (!_pages.contains(e.value)) _pages.append(e.value);
      e.value.isHidden = e.key != key;
    }
    switch (_pageObjects[key]) {
      case ScreenerPage p:
        p.onShow();
      case HeatmapPage p:
        p.onShow();
      case MarketsPage p:
        p.onShow();
      case CalendarPage p:
        p.onShow();
      case TradingPage p:
        p.onShow();
      case ChartPage p:
        for (final panel in p.panels) {
          panel.engine.invalidate();
        }
    }
    _renderNav();
    store.save();
  }

  HTMLElement _createPage(String key) {
    switch (key) {
      case 'screener':
        final p = ScreenerPage();
        _pageObjects[key] = p;
        return p.root;
      case 'heatmap':
        final p = HeatmapPage();
        _pageObjects[key] = p;
        return p.root;
      case 'markets':
        final p = MarketsPage();
        _pageObjects[key] = p;
        return p.root;
      case 'calendar':
        final p = CalendarPage();
        _pageObjects[key] = p;
        return p.root;
      case 'trading':
        final p = TradingPage();
        _pageObjects[key] = p;
        return p.root;
      default:
        return chartPage.root;
    }
  }

  void _applyTheme() {
    final t = store.theme.value;
    final prefersLight = window.matchMedia('(prefers-color-scheme: light)').matches;
    final dark = t == 'dark' || (t == 'system' && !prefersLight);
    document.documentElement!.setAttribute('data-theme', dark ? 'dark' : 'light');
    // canvases read their colors from CSS variables
    Timer.run(() {
      for (final p in chartPage.panels) {
        p.engine.refreshTheme();
      }
      final hm = _pageObjects['heatmap'];
      if (hm is HeatmapPage) hm.redraw();
    });
    if (_nav.isConnected) _renderNav();
  }

  // ---------------------------------------------------------------- agent view sync

  void _pushView() {
    if (_applyingRemoteView) return;
    _viewSync(() {
      final c = store.activeChart;
      final cfg = store.configNow(c.symbol.value);
      request('/api/view', method: 'PUT', body: {
        'symbol': c.symbol.value,
        'interval': c.interval.value,
        // what is on screen: the indicators of every visible layer
        'indicators': [
          if (cfg != null)
            for (final i in cfg.shown)
              if (i.kind == ItemKind.indicator) i.ref.toJson(),
        ],
      }).catchError((Object _) => const J(null));
    });
  }

  void _applyRemoteView(J v) {
    final c = store.activeChart;
    _applyingRemoteView = true;
    try {
      if (v['symbol'].s != null) c.symbol.value = v['symbol'].s!;
      if (v['interval'].s != null) c.interval.value = v['interval'].s!;
    } finally {
      _applyingRemoteView = false;
    }
    if (v['indicators'].v is List) {
      // the agent's indicators live in the symbol's Agent layer, next to (not instead of) the user's
      final refs = [for (final r in v['indicators'].list) IndicatorRef.fromJson(r)];
      final sym = c.symbol.value;
      store.config(sym).then((cfg) {
        final mine = {for (final l in cfg.layers) if (l.id != agentLayerId) for (final i in l.items) if (i.kind == ItemKind.indicator) jsonEncode(i.ref.toJson())};
        store.editConfig(sym, (cc) {
          final agent = cc.agentLayer()..visible = true;
          agent.items.removeWhere((i) => i.kind == ItemKind.indicator);
          for (final r in refs) {
            if (mine.contains(jsonEncode(r.toJson()))) continue; // already on the user's layers
            agent.items.add(LayerItem.indicator(newId('a'), r.name, Map.of(r.params)));
          }
        }, label: 'Agent indicators');
      });
    }
    final from = v['from_time'].i;
    if (from != null) {
      // once bars for the (possibly new) symbol have loaded
      Timer(const Duration(milliseconds: 1200), () => chartPage.activePanel?.setVisibleTimes(from, v['to_time'].i));
    }
  }

  // ---------------------------------------------------------------- events

  void _pushActivity(String text, String kind, String? sym) {
    final now = DateTime.now();
    final list = store.activity.value;
    final last = list.isEmpty ? null : list.first;
    if (last != null && last.kind == kind && last.symbol == sym && now.difference(last.at).inSeconds < 3 &&
        last.text.startsWith('Drew') && text.startsWith('Drew')) {
      final m = RegExp(r'Drew (\d+) objects?').firstMatch(last.text);
      final count = (m == null ? 1 : int.parse(m.group(1)!)) + 1;
      store.activity.value = [Activity(last.id, now, 'Drew $count objects on $sym', sym, kind), ...list.skip(1)];
    } else {
      store.activity.value = [Activity(++_activitySeq, now, text, sym, kind), ...list.take(199)];
    }
    if (kind == 'agent' && store.panel.value != 'agent') store.unseenAgent.value++;
  }

  void _onTradingEvent(ServerEvent e) {
    final d = e.data;
    switch (e.type) {
      case 'trading.fill':
        final f = TFill.fromJson(d);
        final verb = f.isBuy ? 'Bought' : 'Sold';
        toast('${qtyText(f.qty)} ${f.symbol} @ ${f.price.toStringAsFixed(2)}${f.role != null ? ' · ${f.role!.replaceAll('_', ' ')}' : ''}',
            title: '$verb ${f.symbol}', kind: 'success', ms: 6000, actionLabel: 'Chart', onAction: () {
          store.activeChart.symbol.value = f.symbol;
          store.page.value = 'chart';
        });
        _pushActivity('$verb ${qtyText(f.qty)} ${f.symbol} @ ${f.price.toStringAsFixed(2)}', 'user', f.symbol);
      case 'trading.ticket':
        final t = TTicket.fromJson(d);
        if (t.status == 'pending' && t.source == 'agent' && _seenTickets.add(t.id)) {
          _chime();
          toast(t.rationale ?? '', title: '✦ Agent proposes ${t.summary}', kind: 'alert', ms: 0, actionLabel: 'Review',
              onAction: () => reviewTicket(t));
          _pushActivity('Proposed ${t.summary}', 'agent', t.symbol);
        } else if (t.status == 'error') {
          toast(t.message ?? 'Order failed', title: '${t.summary} failed', kind: 'error', ms: 10000);
        } else if (t.source == 'agent' && t.status == 'working' && _seenTickets.add('${t.id}:sent')) {
          _pushActivity('Order sent: ${t.summary}', 'agent', t.symbol);
        }
      case 'trading.error':
        final code = d['code'].i;
        if (code != null && code >= 2100 && code < 2200) return;
        toast(d['message'].s ?? '', title: 'Broker${code != null ? ' ($code)' : ''}', kind: 'warn', ms: 8000);
    }
  }

  final Set<String> _seenTickets = {};

  void _onEvent(ServerEvent e) {
    final d = e.data;
    if (trade.onEvent(e)) {
      _onTradingEvent(e);
      return;
    }
    switch (e.type) {
      case 'hello':
        // only on the first connection, and only if the agent changed the view while we were away
        if (_firstHello && d['view']['updated_by'].s == 'agent') _applyRemoteView(d['view']);
        _firstHello = false;
      case 'view.changed':
        if (d['updated_by'].s == 'agent') {
          _applyRemoteView(d);
          _pushActivity('Switched chart to ${d['symbol'].s} · ${d['interval'].s}', 'agent', d['symbol'].s);
          if (store.page.value != 'chart') store.page.value = 'chart';
        }
      case 'annotation.created':
        final a = Annotation.fromJson(d);
        store.upsertDrawing(a);
        if (a.author == 'agent') _pushActivity('Drew 1 object on ${a.symbol}', 'agent', a.symbol);
      case 'annotation.updated':
        store.upsertDrawing(Annotation.fromJson(d));
      case 'annotation.deleted':
        store.removeDrawing(d['id'].s ?? '');
      case 'analysis.created':
        store.analysesVersion.value++;
        final action = (d['action'].s ?? '').replaceAll('_', ' ').toUpperCase();
        final conf = ((d['confidence'].d ?? 0) * 100).round();
        _pushActivity('Saved analysis: $action ${d['symbol'].s} ($conf%)', d['author'].s == 'agent' ? 'agent' : 'user',
            d['symbol'].s);
        if (d['author'].s == 'agent') {
          toast(d['summary'].s ?? '', title: '✦ $action ${d['symbol'].s} · $conf% confidence', ms: 9000, actionLabel: 'Open',
              onAction: () {
            store.panel.value = 'agent';
            store.page.value = 'chart';
          });
        }
      case 'note.saved' || 'note.deleted':
        store.notesVersion.value++;
        if (e.type == 'note.saved' && d['author'].s == 'agent') {
          _pushActivity('Saved note: ${d['title'].s}', 'agent', d['symbol'].s);
        }
      case 'watchlist.updated' || 'watchlist.deleted':
        store.watchlistVersion.value++;
      case 'alert.created' || 'alert.updated' || 'alert.deleted':
        store.loadAlerts();
        if (e.type == 'alert.created' && d['author'].s == 'agent') {
          _pushActivity('Created alert: ${d['description'].s}', 'agent', d['symbol'].s);
        }
      case 'alert.triggered':
        store.loadAlerts();
        _chime();
        final sym = d['symbol'].s ?? '';
        toast(d['message'].s ?? 'Triggered at ${d['trigger_price'].d ?? ''}', title: '⏰ ${d['description'].s}', kind: 'alert',
            ms: 0, actionLabel: 'Open chart', onAction: () {
          store.activeChart.symbol.value = sym;
          store.page.value = 'chart';
        });
      case 'ui.navigate':
        final page = d['page'].s;
        if (page == 'chart' && d['symbol'].s != null) store.activeChart.symbol.value = d['symbol'].s!;
        if (page == 'screener' && d['screen'].v is Map) {
          store.screenerState.value = Map<String, Object?>.from(d['screen'].v as Map);
        }
        if (page == 'heatmap' && d['period'].s != null) {
          store.heatmapState.value = {...store.heatmapState.value, 'period': d['period'].s};
        }
        if (page != null && pages.any((p) => p.$1 == page)) store.page.value = page;
        _pushActivity('Opened ${page ?? 'a page'} for you', 'agent', d['symbol'].s);
      case 'screens.updated':
        final s = _pageObjects['screener'];
        if (s is ScreenerPage) s.reloadSaved();
    }
  }

  /// A short two-tone chime for triggered alerts (Web Audio; silent if unavailable).
  void _chime() {
    try {
      final ctor = globalContext['AudioContext'] as JSFunction?;
      if (ctor == null) return;
      final ac = ctor.callAsConstructor<JSObject>();
      final now = (ac['currentTime'] as JSNumber).toDartDouble;
      for (final (i, f) in [(0, 880.0), (1, 1320.0)]) {
        final start = now + i * 0.16;
        final osc = ac.callMethod<JSObject>('createOscillator'.toJS);
        final gain = ac.callMethod<JSObject>('createGain'.toJS);
        (osc['frequency'] as JSObject)['value'] = f.toJS;
        final g = gain['gain'] as JSObject;
        g.callMethod('setValueAtTime'.toJS, 0.0001.toJS, start.toJS);
        g.callMethod('exponentialRampToValueAtTime'.toJS, 0.12.toJS, (start + 0.02).toJS);
        g.callMethod('exponentialRampToValueAtTime'.toJS, 0.0001.toJS, (start + 0.25).toJS);
        osc.callMethod('connect'.toJS, gain);
        gain.callMethod('connect'.toJS, ac['destination']);
        osc.callMethod('start'.toJS, start.toJS);
        osc.callMethod('stop'.toJS, (start + 0.3).toJS);
      }
    } catch (_) {}
  }
}
