// Watchlist panel (TradingView-style): multiple lists, sections (entries starting with ###),
// sortable columns, live quotes with change flashes, keyboard navigation that drives the active
// chart, drag-to-reorder, context menus. The symbol details pane sits underneath.
import 'dart:async';
import 'dart:js_interop';

import 'package:web/web.dart';

import '../../core/format.dart';
import '../../core/layers.dart';
import '../../core/models.dart';
import '../api.dart';
import '../dom.dart';
import '../kit/icons.dart';
import '../kit/overlay.dart';
import '../kit/state.dart';
import '../pickers.dart';
import '../store.dart';
import 'details.dart';

class WatchlistPanel {
  final HTMLElement root = h('div.panel.watchlist-panel');
  final HTMLElement _head = h('div.panel-head');
  final HTMLElement _cols = h('div.wl-cols');
  final HTMLElement _list = h('div.wl-list', null, {'tabindex': '0', 'role': 'listbox', 'aria-label': 'Watchlist'});
  final HTMLElement _split = h('div.wl-split', null, {'data-tip': 'Drag to resize', 'data-tip-pos': 'top'});
  late final DetailsPanel details = DetailsPanel();
  final Scope _scope = Scope();
  List<Watchlist> _lists = const [];
  final Map<String, Quote> _quotes = {};
  final Map<String, double> _prevPrice = {};
  // sort ('' | 'symbol' | '-symbol' | 'last' | …), collapsed sections and the details height persist
  Set<String> get _collapsed => store.prefList('watchlist.collapsed').toSet();
  String get _sort => store.pref<String>('watchlist.sort');
  set _sort(String v) => store.setPref('watchlist.sort', v);
  void _toggleCollapsed(String s) {
    final c = _collapsed;
    c.contains(s) ? c.remove(s) : c.add(s);
    store.setPref('watchlist.collapsed', c.toList());
  }
  Timer? _poll;
  String? _dragSym;
  double _detailsHeight = store.pref<double>('watchlist.detailsHeight');

  WatchlistPanel() {
    append(root, [_head, _cols, _list, _split, details.root]);
    _scope.listen(store.watchlist, (_) => _current == null ? refresh() : _render());
    _scope.listen(store.watchlistVersion, (_) => refresh()); // changed elsewhere (agent, screener, heatmap)
    _scope.listen(store.active, (_) => _renderRows());
    for (final c in store.charts) {
      _scope.listen(c.symbol, (_) => _renderRows());
    }
    _list.addEventListener('keydown', ((KeyboardEvent e) => _onKey(e)).toJS);
    _bindSplit();
    details.root.style.height = '${_detailsHeight}px';
    refresh();
    _poll = Timer.periodic(const Duration(seconds: 5), (_) => _loadQuotes());
  }

  Watchlist? get _current => _lists.where((l) => l.name == store.watchlist.value).firstOrNull;
  List<String> get _symbols => _current?.symbols ?? const [];

  void dispose() {
    _poll?.cancel();
    _scope.dispose();
  }

  Future<void> refresh() async {
    try {
      _lists = await api.watchlists();
      if (_current == null && _lists.isNotEmpty) store.watchlist.value = _lists.first.name;
      _render();
      _loadQuotes();
    } catch (e) {
      replace(_list, h('div.muted.pad', 'Watchlists unavailable: $e'));
    }
  }

  Future<void> _loadQuotes() async {
    if (!root.isConnected || document.hidden) return;
    final syms = [for (final s in _symbols) if (!s.startsWith('###')) s];
    if (syms.isEmpty) return;
    try {
      final q = await api.quotes(syms);
      for (final e in q.entries) {
        final old = _quotes[e.key];
        if (old != null) _prevPrice[e.key] = old.price;
        _quotes[e.key] = e.value;
      }
      _renderRows(flash: true);
    } catch (_) {}
  }

  Future<void> _update({List<String>? add, List<String>? remove, List<String>? replaceAll}) async {
    try {
      await request('/api/watchlists/${Uri.encodeComponent(store.watchlist.value)}', method: 'PUT', body: {
        'add': ?add, 'remove': ?remove, 'replace': ?replaceAll,
      });
      await refresh();
    } catch (e) {
      toast('$e', kind: 'error');
    }
  }

  // ---------------------------------------------------------------- header

  void _render() {
    final name = store.watchlist.value;
    replace(_head, [
      h('button.panel-title', [h('span', name), icon('chev-down', size: 13)], {'type': 'button', 'data-tip': 'Switch list'}, {
        'click': (e) => _listMenu(e.currentTarget as Element),
      }),
      h('span.spacer'),
      h('button.icon-btn', icon('plus', size: 16), {'type': 'button', 'data-tip': 'Add symbol', 'aria-label': 'Add symbol'}, {
        'click': (_) => openSymbolSearch(title: 'Add to $name', onPick: (s) => _update(add: [s])),
      }),
      h('button.icon-btn', icon('more', size: 16), {'type': 'button', 'data-tip': 'List options', 'aria-label': 'List options'}, {
        'click': (e) => _optionsMenu(e.currentTarget as Element),
      }),
    ]);
    replace(_cols, [
      _colHead('Symbol', 'symbol'),
      _colHead('Last', 'last'),
      _colHead('Chg', 'abs'),
      _colHead('Chg%', 'chg'),
    ]);
    _renderRows();
  }

  HTMLElement _colHead(String label, String key) {
    final on = _sort == key || _sort == '-$key';
    return h('button.wl-col${on ? '.on' : ''}', [label, if (on) icon(_sort.startsWith('-') ? 'chev-down' : 'chev-up', size: 11)],
        {'type': 'button'}, {
      'click': (_) {
        _sort = _sort == key ? '-$key' : _sort == '-$key' ? '' : key;
        _render();
      },
    });
  }

  void _listMenu(Element anchor) {
    final name = store.watchlist.value;
    showMenu([
      for (final l in _lists)
        MenuItem(l.name, checked: l.name == name, hint: '${l.symbols.where((s) => !s.startsWith('###')).length}',
            onSelect: () {
          store.watchlist.value = l.name;
          store.save();
          _loadQuotes();
        }),
      const MenuItem.divider(),
      MenuItem('New list…', iconName: 'plus', onSelect: () async {
        final n = await promptDialog('New watchlist', label: 'Name');
        if (n == null || n.isEmpty) return;
        await request('/api/watchlists/${Uri.encodeComponent(n)}', method: 'PUT', body: {'add': <String>[]});
        store.watchlist.value = n;
        store.save();
        refresh();
      }),
      MenuItem('Rename…', iconName: 'edit', onSelect: () async {
        final n = await promptDialog('Rename watchlist', label: 'Name', initial: name);
        if (n == null || n.isEmpty || n == name) return;
        await request('/api/watchlists/${Uri.encodeComponent(n)}', method: 'PUT', body: {'replace': _symbols});
        await request('/api/watchlists/${Uri.encodeComponent(name)}', method: 'DELETE');
        store.watchlist.value = n;
        store.save();
        refresh();
      }),
      MenuItem('Delete list', iconName: 'trash', danger: true, disabled: _lists.length <= 1, onSelect: () async {
        if (!await confirmDialog('Delete watchlist', 'Delete "$name"?', confirm: 'Delete', danger: true)) return;
        await request('/api/watchlists/${Uri.encodeComponent(name)}', method: 'DELETE');
        store.watchlist.value = _lists.firstWhere((l) => l.name != name).name;
        refresh();
      }),
    ], anchor: anchor);
  }

  void _optionsMenu(Element anchor) {
    showMenu([
      MenuItem('Add section…', iconName: 'list', onSelect: () async {
        final n = await promptDialog('New section', label: 'Section title');
        if (n != null && n.isNotEmpty) _update(add: ['###${n.toUpperCase()}']);
      }),
      MenuItem('Add symbols…', iconName: 'plus', onSelect: () async {
        final raw = await promptDialog('Add symbols', label: 'Tickers, separated by spaces or commas', placeholder: 'AAPL MSFT NVDA');
        if (raw == null) return;
        final syms = raw.split(RegExp(r'[\s,]+')).where((s) => s.isNotEmpty).map((s) => s.toUpperCase()).toList();
        if (syms.isNotEmpty) _update(add: syms);
      }),
      MenuItem('Open all in screener', iconName: 'filter', onSelect: () {
        store.screenerState.value = {'universe': 'watchlist:${store.watchlist.value}', 'filters': <Object>[]};
        store.page.value = 'screener';
      }),
      MenuItem('Sort A→Z (permanently)', iconName: 'list', onSelect: () {
        final syms = [for (final s in _symbols) if (!s.startsWith('###')) s]..sort();
        _update(replaceAll: syms);
      }),
      const MenuItem.divider(),
      MenuItem('Clear list', iconName: 'trash', danger: true, onSelect: () async {
        if (await confirmDialog('Clear watchlist', 'Remove every symbol from "${store.watchlist.value}"?',
            confirm: 'Clear', danger: true)) {
          _update(replaceAll: []);
        }
      }),
    ], anchor: anchor, align: 'end');
  }

  // ---------------------------------------------------------------- rows

  List<String> get _ordered {
    if (_sort.isEmpty) return _symbols;
    // sorting ignores sections (TradingView flattens while sorted)
    final syms = [for (final s in _symbols) if (!s.startsWith('###')) s];
    final desc = _sort.startsWith('-');
    final key = _sort.replaceFirst('-', '');
    double? val(String s) => switch (key) {
          'last' => _quotes[s]?.price,
          'chg' => _quotes[s]?.changePct,
          'abs' => _quotes[s]?.change,
          _ => null,
        };
    syms.sort((a, b) {
      final c = key == 'symbol' ? a.compareTo(b) : (val(a) ?? -1e18).compareTo(val(b) ?? -1e18);
      return desc ? -c : c;
    });
    return syms;
  }

  void _renderRows({bool flash = false}) {
    final active = store.activeChart.symbol.value;
    final rows = <Object>[];
    String? section;
    for (final s in _ordered) {
      if (s.startsWith('###')) {
        section = s;
        final title = s.substring(3);
        final collapsed = _collapsed.contains(s);
        rows.add(h('div.wl-section', [icon(collapsed ? 'chev-right' : 'chev-down', size: 12), h('span', title)], null, {
          'click': (_) {
            _toggleCollapsed(s);
            _renderRows();
          },
          'contextmenu': (e) {
            e.preventDefault();
            final me = e as MouseEvent;
            showMenu([
              MenuItem('Remove section', iconName: 'trash', onSelect: () => _update(remove: [s])),
            ], x: clientXY(me).$1, y: clientXY(me).$2);
          },
        }));
        continue;
      }
      if (section != null && _collapsed.contains(section) && _sort.isEmpty) continue;
      rows.add(_row(s, s == active, flash));
    }
    if (_symbols.where((s) => !s.startsWith('###')).isEmpty) {
      rows.add(h('div.empty-state', [
        icon('list', size: 28),
        h('div', 'This list is empty'),
        h('div.muted.small', 'Add symbols with + above, or from the screener, a chart or the agent.'),
      ]));
    }
    replace(_list, rows);
  }

  HTMLElement _row(String s, bool active, bool flash) {
    final q = _quotes[s];
    final prev = _prevPrice[s];
    final dir = q?.changePct == null ? '' : q!.changePct! >= 0 ? '.up' : '.down';
    final flashCls = flash && q != null && prev != null && prev != q.price ? (q.price > prev ? '.flash-up' : '.flash-down') : '';
    final row = h('div.wl-row${active ? '.active' : ''}$flashCls', [
      h('span.wl-sym', [h('span.sym-dot', null, {'style': 'background:${_symColor(s)}'}), h('b', s)]),
      h('span.wl-num', q == null ? '…' : fmtPrice(q.price)),
      h('span.wl-num$dir', q?.change == null ? '' : signed(q!.change!)),
      h('span.wl-num$dir', q?.changePct == null ? '' : '${signed(q!.changePct!)}%'),
      h('button.wl-x', icon('x', size: 12), {'type': 'button', 'aria-label': 'Remove $s'}, {
        'click': (e) {
          e.stopPropagation();
          _update(remove: [s]);
        },
      }),
    ], {
      'draggable': '${_sort.isEmpty}',
      'role': 'option',
      'aria-selected': '$active',
      'data-symbol': s,
    }, {
      'click': (_) {
        store.activeChart.symbol.value = s;
        _list.focus();
      },
      'dblclick': (_) => store.page.value = 'chart',
      'contextmenu': (e) {
        e.preventDefault();
        final me = e as MouseEvent;
        _rowMenu(s, clientXY(me).$1, clientXY(me).$2);
      },
      'dragstart': (e) {
        _dragSym = s;
        (e as DragEvent).dataTransfer?.effectAllowed = 'move';
      },
      'dragover': (e) {
        e.preventDefault();
        (e.currentTarget as Element).classList.add('drop-target');
      },
      'dragleave': (e) => (e.currentTarget as Element).classList.remove('drop-target'),
      'drop': (e) {
        e.preventDefault();
        (e.currentTarget as Element).classList.remove('drop-target');
        final from = _dragSym;
        _dragSym = null;
        if (from == null || from == s) return;
        final list = [..._symbols]..remove(from);
        list.insert(list.indexOf(s), from);
        _update(replaceAll: list);
      },
    });
    return row;
  }

  void _rowMenu(String s, double x, double y) {
    showMenu([
      MenuItem('Open $s', iconName: 'chart', onSelect: () {
        store.activeChart.symbol.value = s;
        store.page.value = 'chart';
      }),
      MenuItem('Compare with ${store.activeChart.symbol.value}', iconName: 'compare', onSelect: () {
        final base = store.activeChart.symbol.value;
        final cfg = store.configNow(base);
        if (cfg == null || base == s || cfg.all.any((i) => i.kind == ItemKind.compare && i.symbol == s)) return;
        store.editConfig(base, (c) {
          c.addItem(LayerItem.compare(newId('c'), s));
          if (c.scale == 'linear') c.scale = 'percent';
        }, label: 'Compare $s');
      }),
      MenuItem('Add alert…', iconName: 'alarm', onSelect: () {
        // imported lazily to avoid a cycle with the chart package
        window.dispatchEvent(CustomEvent('create-alert', CustomEventInit(detail: s.toJS)));
      }),
      MenuItem('Copy symbol', iconName: 'copy', onSelect: () => window.navigator.clipboard.writeText(s)),
      MenuItem('Add section above…', iconName: 'list', onSelect: () async {
        final n = await promptDialog('New section', label: 'Section title');
        if (n == null || n.isEmpty) return;
        final list = [..._symbols];
        list.insert(list.indexOf(s), '###${n.toUpperCase()}');
        _update(replaceAll: list);
      }),
      const MenuItem.divider(),
      MenuItem('Remove from list', iconName: 'trash', danger: true, onSelect: () => _update(remove: [s])),
    ], x: x, y: y);
  }

  void _onKey(KeyboardEvent e) {
    if (e.key != 'ArrowDown' && e.key != 'ArrowUp' && e.key != 'Delete' && e.key != 'Backspace') return;
    e.preventDefault();
    e.stopPropagation();
    final syms = [
      for (final s in _ordered)
        if (!s.startsWith('###')) s
    ];
    if (syms.isEmpty) return;
    final cur = store.activeChart.symbol.value;
    if (e.key == 'Delete' || e.key == 'Backspace') {
      if (syms.contains(cur)) _update(remove: [cur]);
      return;
    }
    var i = syms.indexOf(cur);
    i = i < 0 ? 0 : (i + (e.key == 'ArrowDown' ? 1 : -1)).clamp(0, syms.length - 1);
    store.activeChart.symbol.value = syms[i];
    Timer.run(() => _list.querySelector('.wl-row.active')?.scrollIntoView(ScrollIntoViewOptions(block: 'nearest')));
  }

  void _bindSplit() {
    double? startY, startH;
    _split.addEventListener('pointerdown', ((PointerEvent e) {
      startY = clientXY(e).$2;
      startH = _detailsHeight;
      _split.setPointerCapture(e.pointerId);
    }).toJS);
    _split.addEventListener('pointermove', ((PointerEvent e) {
      if (startY == null) return;
      final total = root.getBoundingClientRect().height;
      _detailsHeight = (startH! - (clientXY(e).$2 - startY!)).clamp(0.0, total - 120);
      details.root.style.height = '${_detailsHeight}px';
    }).toJS);
    _split.addEventListener('pointerup', ((PointerEvent e) {
      startY = null;
      store.setPref('watchlist.detailsHeight', _detailsHeight);
    }).toJS);
  }
}

/// Stable per-symbol color for the little dot in lists.
String _symColor(String s) {
  var h = 0;
  for (final c in s.codeUnits) {
    h = (h * 31 + c) & 0xffffff;
  }
  return 'hsl(${h % 360}, 55%, 55%)';
}
