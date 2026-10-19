// Stock screener page (FinViz-style): universe/signal/saved screens, dropdown filters with
// presets and custom ranges, a filter expression bar, view tabs, sortable paged results with
// chart previews on hover, a Charts tile view, CSV export and add-to-watchlist.
import 'dart:async';
import 'dart:convert';
import 'dart:js_interop';
import 'dart:math' as math;

import 'package:web/web.dart';

import '../../core/chartmath.dart';
import '../../core/format.dart';
import '../../core/json.dart';
import '../api.dart';
import '../dom.dart';
import '../kit/icons.dart';
import '../kit/overlay.dart';
import '../kit/spark.dart';
import '../store.dart';

class FieldMeta {
  final String key, label, group, fmt, desc;
  final List<J> presets;
  const FieldMeta(this.key, this.label, this.group, this.fmt, this.desc, this.presets);
  factory FieldMeta.fromJson(J j) => FieldMeta(j['key'].s ?? '', j['label'].s ?? '', j['group'].s ?? '', j['fmt'].s ?? 'num',
      j['desc'].s ?? '', j['presets'].list);
}

/// Formats a screener cell by field format.
Object cell(FieldMeta? f, Object? v) {
  if (v == null) return h('span.muted', '–');
  final key = f?.key ?? '';
  switch (f?.fmt) {
    case 'pct':
      final d = (v as num).toDouble();
      final colored = key.startsWith('perf') || key.startsWith('change') || key.endsWith('_qoq') || key.contains('growth') ||
          key.startsWith('sma') || key == 'gap_pct' || key.startsWith('from_');
      return h(colored ? (d >= 0 ? 'span.up' : 'span.down') : 'span', '${d.toStringAsFixed(2)}%');
    case 'price':
      return fmtPrice((v as num).toDouble());
    case 'compact':
      return compact(v as num);
    case 'ratio':
      return (v as num).toStringAsFixed(2);
    case 'int':
      return '${(v as num).round()}';
    case 'num':
      final d = (v as num).toDouble();
      return d.abs() >= 1000 ? compact(d) : d.toStringAsFixed(2);
    default:
      return '$v';
  }
}

class ScreenerPage {
  final HTMLElement root = h('div.page.screener-page');
  final HTMLElement _top = h('div.scr-top');
  final HTMLElement _filters = h('div.scr-filters');
  final HTMLElement _chips = h('div.scr-chips');
  final HTMLElement _bar = h('div.scr-bar');
  final HTMLElement _results = h('div.scr-results');
  final HTMLElement _pager = h('div.scr-pager');
  List<FieldMeta> _fields = [];
  Map<String, FieldMeta> _byKey = {};
  Map<String, List<String>> _views = {};
  Map<String, String> _signals = {};
  List<J> _saved = [];
  List<String> _watchlists = [];
  J _meta = const J(null);
  J? _res;
  bool _loading = false;
  String? _error;
  int _seq = 0;
  Timer? _debounce, _hoverTimer;
  Popover? _preview;
  String _filterTab = 'Descriptive';
  late final HTMLInputElement _expr;
  late final HTMLInputElement _search;

  ScreenerPage() {
    _expr = h('input.input.mono', null, {
      'placeholder': 'Filter expression, e.g.  pe < 25 and sales_qoq > 15 and roe > 15 and sma200_pct > 0',
      'aria-label': 'Filter expression', 'spellcheck': 'false',
    }) as HTMLInputElement;
    _search = h('input.input', null, {'placeholder': 'Ticker or name', 'aria-label': 'Search results'}) as HTMLInputElement;
    _expr.addEventListener('keydown', ((KeyboardEvent e) {
      if (e.key == 'Enter') _set({'expression': _expr.value.trim(), 'page': 0});
    }).toJS);
    _expr.addEventListener('blur', ((Event _) {
      if (_expr.value.trim() != (_st['expression'] ?? '')) _set({'expression': _expr.value.trim(), 'page': 0});
    }).toJS);
    _search.addEventListener('input', ((Event _) {
      _debounce?.cancel();
      _debounce = Timer(const Duration(milliseconds: 250), () => _set({'search': _search.value.trim(), 'page': 0}));
    }).toJS);
    append(root, [_top, _filters, _chips, _bar, _results, _pager]);
    _init();
  }

  Map<String, Object?> get _st => store.screenerState.value;

  List<J> get _activeFilters => [for (final f in (_st['filters'] as List?) ?? const []) J(f)];

  void _set(Map<String, Object?> patch) {
    store.screenerState.value = {..._st, ...patch};
    store.save();
    _renderControls();
    _run();
  }

  Future<void> _init() async {
    replace(_results, h('div.loading-block', [h('div.spinner'), h('div.muted', 'Loading screener…')]));
    try {
      _meta = await request('/api/screener/meta');
      _fields = [for (final f in _meta['fields'].list) FieldMeta.fromJson(f)];
      _byKey = {for (final f in _fields) f.key: f};
      _views = {for (final e in _meta['views'].map.entries) e.key: [for (final c in e.value.list) c.s ?? '']};
      _signals = {for (final e in _meta['signals'].map.entries) e.key: e.value.s ?? ''};
      _saved = _meta['screens'].list;
      _watchlists = [for (final w in (await request('/api/watchlists')).list) w['name'].s ?? ''];
    } catch (e) {
      _error = '$e';
    }
    _renderControls();
    _run();
  }

  void onShow() {
    // the agent (or the watchlist) may have changed the screen while the page was hidden
    if (_fields.isNotEmpty) {
      _renderControls();
      _run();
    }
  }

  Future<void> reloadSaved() async {
    try {
      _saved = (await request('/api/screens')).list;
      _renderControls();
    } catch (_) {}
  }

  // ---------------------------------------------------------------- controls

  void _renderControls() {
    _expr.value = (_st['expression'] as String?) ?? '';
    if (document.activeElement != _search) _search.value = (_st['search'] as String?) ?? '';
    final universe = (_st['universe'] as String?) ?? 'all';
    final signal = (_st['signal'] as String?) ?? '';
    replace(_top, [
      h('div.scr-row', [
        h('span.scr-label', 'Universe'),
        _select('universe', universe, [('all', 'All US stocks'), ('sp500', 'S&P 500'), ..._watchlistOptions()],
            (v) => _set({'universe': v, 'page': 0})),
        h('span.scr-label', 'Signal'),
        _select('signal', signal, [('', 'None'), for (final e in _signals.entries) (e.key, e.value)],
            (v) => _set({'signal': v.isEmpty ? null : v, 'page': 0, 'sort': null})),
        h('button.btn', [icon('folder', size: 14), ' Screens ', icon('chev-down', size: 12)], {'type': 'button'},
            {'click': (e) => _screensMenu(e.currentTarget as Element)}),
        h('span.spacer'),
        h('div.search-mini', [icon('search', size: 14), _search]),
        h('button.btn', 'Reset', {'type': 'button', 'data-tip': 'Clear all filters'}, {
          'click': (_) => _set({'filters': <Object>[], 'expression': '', 'signal': null, 'search': '', 'page': 0, 'sort': null}),
        }),
      ]),
      h('div.scr-row', [
        h('span.scr-label', [icon('filter', size: 13), ' Expression']),
        _expr,
        h('button.icon-btn', icon('info', size: 16), {'type': 'button', 'data-tip': 'Fields & syntax'}, {'click': (_) => _syntaxHelp()}),
      ]),
    ]);
    _renderFilters();
    _renderChips();
    _renderBar();
  }

  List<(String, String)> _watchlistOptions() => [
        for (final w in _watchlists.isEmpty ? [store.watchlist.value] : _watchlists) ('watchlist:$w', 'Watchlist: $w'),
      ];

  HTMLElement _select(String name, String value, List<(String, String)> options, void Function(String) onChange) {
    final sel = h('select.input.select', [
      for (final (v, label) in options) h('option', label, {'value': v}),
    ], {'aria-label': name}) as HTMLSelectElement;
    sel.value = value;
    if (sel.value != value && value.isNotEmpty) {
      sel.append(h('option', value, {'value': value}));
      sel.value = value;
    }
    sel.addEventListener('change', ((Event _) => onChange(sel.value)).toJS);
    return sel;
  }

  static const _tabGroups = {
    'Descriptive': ['Descriptive'],
    'Fundamental': ['Valuation', 'Financial'],
    'Technical': ['Performance', 'Technical'],
  };

  void _renderFilters() {
    final active = {for (final f in _activeFilters) f['field'].s: f};
    final groups = _tabGroups[_filterTab] ?? const [];
    final shown = _filterTab == 'All' ? _fields : _fields.where((f) => groups.contains(f.group));
    final textChoices = <String, List<String>>{
      'sector': [for (final s in _meta['sectors'].list) s.s ?? ''],
      'industry': [for (final s in _meta['industries'].list) s.s ?? ''],
      'country': [for (final s in _meta['countries'].list) s.s ?? ''],
      'exchange': [for (final s in _meta['exchanges'].list) s.s ?? ''],
    };
    replace(_filters, [
      h('div.scr-tabs', [
        for (final t in [..._tabGroups.keys, 'All'])
          h('button.tab${t == _filterTab ? '.on' : ''}', t, {'type': 'button'}, {
            'click': (_) {
              _filterTab = t;
              _renderFilters();
            },
          }),
        h('span.spacer'),
        h('span.muted.small', '${active.length} filter${active.length == 1 ? '' : 's'} active'),
      ]),
      h('div.filter-grid', [
        for (final f in shown)
          if (f.presets.isNotEmpty || textChoices.containsKey(f.key))
            _filterControl(f, active[f.key], textChoices[f.key]),
      ]),
    ]);
  }

  HTMLElement _filterControl(FieldMeta f, J? active, List<String>? choices) {
    final options = <(String, String)>[('', 'Any')];
    if (choices != null) {
      for (final c in choices) {
        options.add((jsonEncode({'op': '==', 'value': c}), c));
      }
    } else {
      for (final p in f.presets) {
        options.add((jsonEncode({'op': p['op'].s, 'value': p['value'].v}), p['label'].s ?? ''));
      }
      options.add(('custom', 'Custom…'));
    }
    var current = '';
    var customLabel = '';
    if (active != null) {
      current = jsonEncode({'op': active['op'].s, 'value': active['value'].v});
      if (!options.any((o) => o.$1 == current)) {
        customLabel = _describe(f, active);
        options.insert(1, (current, customLabel));
      }
    }
    final sel = h('select.input.select${active != null ? '.set' : ''}', [
      for (final (v, label) in options) h('option', label, {'value': v}),
    ], {'aria-label': f.label}) as HTMLSelectElement;
    sel.value = current;
    sel.addEventListener('change', ((Event _) {
      final v = sel.value;
      if (v == 'custom') {
        sel.value = current;
        _customFilter(f, active);
        return;
      }
      final rest = [for (final x in _activeFilters) if (x['field'].s != f.key) x.v];
      if (v.isNotEmpty) {
        final p = J(jsonDecode(v));
        rest.add({'field': f.key, 'op': p['op'].s, 'value': p['value'].v});
      }
      _set({'filters': rest, 'page': 0});
    }).toJS);
    return h('label.filter${active != null ? '.set' : ''}', [h('span', f.label, {'data-tip': f.desc.isEmpty ? f.label : f.desc}), sel]);
  }

  String _describe(FieldMeta f, J flt) {
    final op = flt['op'].s;
    final v = flt['value'];
    String fv(Object? x) => x is num ? (f.fmt == 'compact' ? compact(x) : x.toString()) : '$x';
    return switch (op) {
      'between' => '${fv(v[0].v)} – ${fv(v[1].v)}',
      'in' => v.list.map((x) => x.s).join(', '),
      'isnull' => 'None',
      _ => '$op ${fv(v.v)}',
    };
  }

  void _customFilter(FieldMeta f, J? active) {
    final min = h('input.input', null, {'type': 'number', 'step': 'any', 'placeholder': 'min'}) as HTMLInputElement;
    final max = h('input.input', null, {'type': 'number', 'step': 'any', 'placeholder': 'max'}) as HTMLInputElement;
    if (active != null) {
      final op = active['op'].s;
      if (op == 'between') {
        min.value = '${active['value'][0].v}';
        max.value = '${active['value'][1].v}';
      } else if (op == '>' || op == '>=') {
        min.value = '${active['value'].v}';
      } else if (op == '<' || op == '<=') {
        max.value = '${active['value'].v}';
      }
    }
    Dialog.open('${f.label}: custom range', h('div.form-grid', [
      if (f.desc.isNotEmpty) h('div.muted.small', f.desc),
      h('div.row.gap', [h('label.field-col', [h('span', 'Minimum'), min]), h('label.field-col', [h('span', 'Maximum'), max])]),
      h('div.muted.small', f.fmt == 'pct' ? 'Percent values, e.g. 12.5 for 12.5%.' : f.fmt == 'compact' ? 'Full numbers, e.g. 2000000000 for \$2B.' : ''),
    ]), actions: [
      const DialogAction('Cancel'),
      DialogAction('Apply', primary: true, onPressed: () {
        final lo = double.tryParse(min.value), hi = double.tryParse(max.value);
        final rest = [for (final x in _activeFilters) if (x['field'].s != f.key) x.v];
        if (lo != null && hi != null) {
          rest.add({'field': f.key, 'op': 'between', 'value': [lo, hi]});
        } else if (lo != null) {
          rest.add({'field': f.key, 'op': '>=', 'value': lo});
        } else if (hi != null) {
          rest.add({'field': f.key, 'op': '<=', 'value': hi});
        }
        _set({'filters': rest, 'page': 0});
        return true;
      }),
    ]);
  }

  void _renderChips() {
    final fs = _activeFilters;
    final expr = (_st['expression'] as String?) ?? '';
    final signal = _st['signal'] as String?;
    replace(_chips, [
      if (signal != null) h('span.chip.sig', [icon('star', size: 12), ' ${_signals[signal] ?? signal}', _x(() => _set({'signal': null, 'page': 0}))]),
      for (final f in fs)
        h('span.chip', [
          h('b', _byKey[f['field'].s]?.label ?? f['field'].s),
          ' ${_describe(_byKey[f['field'].s] ?? FieldMeta(f['field'].s ?? '', '', '', 'num', '', const []), f)}',
          _x(() => _set({'filters': [for (final x in fs) if (x['field'].s != f['field'].s) x.v], 'page': 0})),
        ]),
      if (expr.isNotEmpty) h('span.chip.mono', [expr, _x(() => _set({'expression': '', 'page': 0}))]),
    ]);
    _chips.isHidden = fs.isEmpty && expr.isEmpty && signal == null;
  }

  HTMLElement _x(void Function() onClick) =>
      h('button.chip-x', icon('x', size: 11), {'type': 'button', 'aria-label': 'Remove'}, {'click': (_) => onClick()});

  void _screensMenu(Element anchor) {
    showMenu([
      MenuItem('Save screen as…', iconName: 'save', onSelect: _saveScreen),
      if (_saved.isNotEmpty) const MenuItem.header('Saved screens'),
      for (final s in _saved)
        MenuItem(s['name'].s ?? '', iconName: 'filter', submenu: [
          MenuItem('Open', onSelect: () => _set({
                'filters': s['filters'].v ?? <Object>[], 'expression': s['expression'].s ?? '', 'signal': s['signal'].s,
                'universe': s['universe'].s ?? 'all', 'sort': s['sort'].s, 'view': s['view'].s ?? 'overview', 'page': 0,
              })),
          MenuItem('Delete', danger: true, onSelect: () async {
            if (!await confirmDialog('Delete screen', 'Delete "${s['name'].s}"?', confirm: 'Delete', danger: true)) return;
            await request('/api/screens/${Uri.encodeComponent(s['name'].s!)}', method: 'DELETE');
            reloadSaved();
          }),
        ]),
      const MenuItem.header('Presets'),
      for (final (name, spec) in _presets) MenuItem(name, iconName: 'star', onSelect: () => _set({...spec, 'page': 0})),
    ], anchor: anchor);
  }

  static final _presets = <(String, Map<String, Object?>)>[
    ('Quality growth at a fair price', {'filters': <Object>[], 'signal': null, 'universe': 'all', 'view': 'valuation',
      'expression': 'market_cap > 2e9 and sales_growth > 15 and roe > 15 and pe > 0 and pe < 35 and net_margin > 10',
      'sort': '-sales_growth'}),
    ('Momentum leaders near highs', {'filters': <Object>[], 'signal': null, 'universe': 'all', 'view': 'performance',
      'expression': 'avg_volume > 500000 and sma50_pct > 0 and sma200_pct > 0 and from_high_52w > -5 and perf_3m > 15',
      'sort': '-perf_3m'}),
    ('Oversold in an uptrend', {'filters': <Object>[], 'signal': null, 'universe': 'all', 'view': 'technical',
      'expression': 'avg_volume > 500000 and sma200_pct > 0 and rsi14 < 35', 'sort': 'rsi14'}),
    ('Dividend payers, low payout', {'filters': <Object>[], 'signal': null, 'universe': 'all', 'view': 'valuation',
      'expression': 'div_yield > 3 and payout < 60 and market_cap > 2e9', 'sort': '-div_yield'}),
    ('Unusual volume today', {'filters': <Object>[], 'signal': 'unusual_volume', 'universe': 'all', 'view': 'overview',
      'expression': '', 'sort': null}),
    ('Deep value', {'filters': <Object>[], 'signal': null, 'universe': 'all', 'view': 'valuation',
      'expression': 'pe > 0 and pe < 10 and pb < 1.5 and current_ratio > 1.5 and market_cap > 3e8', 'sort': 'pe'}),
  ];

  Future<void> _saveScreen() async {
    final name = await promptDialog('Save screen', label: 'Name');
    if (name == null || name.isEmpty) return;
    try {
      await request('/api/screens/${Uri.encodeComponent(name)}', method: 'PUT', body: {
        'filters': _st['filters'] ?? <Object>[], 'expression': _st['expression'], 'signal': _st['signal'],
        'universe': _st['universe'] ?? 'all', 'sort': _st['sort'], 'view': _st['view'] ?? 'overview',
      });
      toast('Screen "$name" saved', kind: 'success', ms: 2000);
      reloadSaved();
    } catch (e) {
      toast('$e', kind: 'error');
    }
  }

  void _syntaxHelp() {
    final groups = <String, List<FieldMeta>>{};
    for (final f in _fields) {
      groups.putIfAbsent(f.group, () => []).add(f);
    }
    Dialog.open('Screener fields & syntax', h('div.syntax', [
      h('p.small', [
        'Combine fields with ', h('code', 'and'), ', ', h('code', 'or'), ', ', h('code', 'not'), ', comparisons and arithmetic. ',
        'Percent fields are plain numbers (', h('code', 'perf_1m > 10'), ' means +10%). Text: ',
        h('code', 'sector == "Energy"'), ' or ', h('code', 'sector in ["Utilities", "Energy"]'), '.',
      ]),
      for (final e in groups.entries) ...[
        h('h4', e.key),
        h('div.field-list', [
          for (final f in e.value)
            h('button.field-chip', [h('code', f.key), h('span.muted.small', ' ${f.label}')],
                {'type': 'button', 'data-tip': f.desc.isEmpty ? f.label : f.desc}, {
              'click': (_) {
                final v = _expr.value.trim();
                _expr.value = v.isEmpty ? '${f.key} ' : '$v and ${f.key} ';
                _expr.focus();
              },
            }),
        ]),
      ],
    ]), width: '760px');
  }

  // ---------------------------------------------------------------- results

  String get _view => (_st['view'] as String?) ?? 'overview';
  int get _pageSize => (_st['pageSize'] as int?) ?? 50;
  int get _page => (_st['page'] as int?) ?? 0;

  List<String> get _columns {
    if (_view == 'custom') {
      final c = (_st['columns'] as List?)?.cast<String>();
      if (c != null && c.isNotEmpty) return c;
    }
    if (_view == 'charts') return ['symbol', 'name', 'price', 'change_pct', 'market_cap', 'sector'];
    return _views[_view] ?? _views['overview'] ?? const ['symbol'];
  }

  void _renderBar() {
    final total = _res?['total'].i;
    replace(_bar, [
      h('div.view-tabs', [
        for (final v in [..._views.keys, 'charts', 'custom'])
          h('button.tab${v == _view ? '.on' : ''}', v[0].toUpperCase() + v.substring(1), {'type': 'button'}, {
            'click': (_) => v == 'custom' && ((_st['columns'] as List?)?.isEmpty ?? true) ? _pickColumns() : _set({'view': v}),
          }),
      ]),
      h('span.spacer'),
      if (total != null) h('span.muted.small', '${_fmtInt(total)} match${total == 1 ? '' : 'es'}'),
      _statusPill(),
      h('button.btn.small', [icon('table', size: 13), ' Columns'], {'type': 'button'}, {'click': (_) => _pickColumns()}),
      h('button.btn.small', [icon('list', size: 13), ' Add to watchlist'], {'type': 'button'}, {'click': (e) => _addAll(e.currentTarget as Element)}),
      h('button.btn.small', [icon('download', size: 13), ' CSV'], {'type': 'button', 'data-tip': 'Export results (up to 500 rows)'},
          {'click': (_) => _exportCsv()}),
    ]);
  }

  HTMLElement _statusPill() {
    final st = _res?['status'] ?? _meta['status'];
    if (st.isNull) return h('span');
    final building = ['listings', 'fundamentals', 'bars', 'quotes'].any((k) => st[k]['refreshing'].b == true);
    final ages = [for (final k in ['bars', 'quotes']) st[k]['age_seconds'].i].whereType<int>().toList();
    final age = ages.isEmpty ? null : ages.reduce(math.min);
    final tip = [
      for (final k in ['listings', 'fundamentals', 'bars', 'quotes'])
        '$k: ${st[k]['rows'].i ?? 0} rows, ${st[k]['age_seconds'].i == null ? 'never' : '${_ageText(st[k]['age_seconds'].i!)} ago'}'
            '${st[k]['refreshing'].b == true ? ' (refreshing${st[k]['progress'].s != null ? ': ${st[k]['progress'].s}' : ''})' : ''}'
            '${st[k]['error'].s != null ? ' – ${st[k]['error'].s}' : ''}',
    ].join('\n');
    return h('button.status-pill${building ? '.busy' : ''}', [
      building ? h('span.spinner.tiny') : icon('refresh', size: 12),
      building ? ' updating data' : age == null ? ' no data' : ' updated ${_ageText(age)} ago',
    ], {'type': 'button', 'data-tip': tip}, {
      'click': (_) async {
        await request('/api/universe/refresh?parts=bars,quotes', method: 'POST');
        toast('Refreshing prices and technicals…', ms: 2500);
        Timer(const Duration(seconds: 4), _run);
      },
    });
  }

  String _ageText(int s) => s < 90 ? '${s}s' : s < 5400 ? '${s ~/ 60}m' : s < 172800 ? '${s ~/ 3600}h' : '${s ~/ 86400}d';
  String _fmtInt(int n) => n.toString().replaceAllMapped(RegExp(r'\B(?=(\d{3})+(?!\d))'), (_) => ',');

  Future<void> _run() async {
    if (_fields.isEmpty) return;
    final seq = ++_seq;
    _loading = true;
    _renderResults();
    try {
      final res = await request('/api/screener', method: 'POST', body: {
        'universe': _st['universe'] ?? 'all',
        'filters': _st['filters'] ?? <Object>[],
        'expression': _st['expression'],
        'signal': _st['signal'],
        'search': _st['search'],
        'sort': _st['sort'],
        'columns': _columns,
        'offset': _page * _pageSize,
        'limit': _pageSize,
      });
      if (seq != _seq) return;
      _res = res;
      _error = null;
      if (res['building'].b == true && res['rows'].list.isEmpty) {
        Timer(const Duration(seconds: 5), () {
          if (root.isConnected && !root.isHidden) _run();
        });
      }
    } catch (e) {
      if (seq != _seq) return;
      _error = '$e';
    }
    _loading = false;
    _renderBar();
    _renderResults();
  }

  void _renderResults() {
    final res = _res;
    if (_error != null) {
      replace(_results, h('div.error-block', [icon('info', size: 18), h('div', _error)]));
      replace(_pager, null);
      return;
    }
    if (res == null) {
      replace(_results, h('div.loading-block', [h('div.spinner'), h('div.muted', 'Running screen…')]));
      return;
    }
    final rows = res['rows'].list;
    if (rows.isEmpty) {
      final building = res['building'].b == true;
      replace(_results, h('div.empty-state.tall', [
        icon(building ? 'refresh' : 'filter', size: 32),
        h('div', building ? 'Building the market universe…' : 'No stocks match these filters'),
        h('div.muted.small', building
            ? 'The first run downloads listings, SEC fundamentals and a year of daily bars for ~6,000 stocks (about a minute).'
            : 'Loosen a filter or remove one of the chips above.'),
      ]));
      replace(_pager, null);
      return;
    }
    root.classList.toggle('loading', _loading);
    if (_view == 'charts') {
      _renderTiles(rows);
    } else {
      _renderTable(rows, [for (final c in res['columns'].list) c.s ?? '']);
    }
    _renderPager(res['total'].i ?? 0);
  }

  void _renderTable(List<J> rows, List<String> cols) {
    final sort = (_st['sort'] as String?) ?? (_st['signal'] != null ? null : '-market_cap');
    final desc = sort?.startsWith('-') ?? false;
    final sortKey = sort?.replaceFirst('-', '');
    final table = h('table.data-table', [
      h('thead', h('tr', [
        h('th.num', '#'),
        for (final c in cols)
          h('th${_isNum(c) ? '.num' : ''}${c == sortKey ? '.sorted' : ''}', [
            _byKey[c]?.label ?? c,
            if (c == sortKey) icon(desc ? 'chev-down' : 'chev-up', size: 11),
          ], {'data-tip': _byKey[c]?.desc ?? '', 'role': 'columnheader'}, {
            'click': (_) {
              final next = c == sortKey ? (desc ? c : '-$c') : (_isNum(c) ? '-$c' : c);
              _set({'sort': next, 'page': 0});
            },
          }),
      ])),
      h('tbody', [
        for (final (i, r) in rows.indexed)
          h('tr', [
            h('td.num.muted', '${_page * _pageSize + i + 1}'),
            for (final c in cols)
              c == 'symbol'
                  ? h('td.sym', h('a', r['symbol'].s, {'href': '#chart'}, {'click': (e) {
                      e.preventDefault();
                      _open(r['symbol'].s!);
                    }}))
                  : h('td${_isNum(c) ? '.num' : '.ellipsis'}', cell(_byKey[c], r[c].v)),
          ], {'data-symbol': r['symbol'].s ?? ''}, {
            'click': (e) {
              if ((e.target as Element).tagName == 'A') return;
              _open(r['symbol'].s!);
            },
            'mouseenter': (e) => _hover(r['symbol'].s!, e.currentTarget as Element),
            'mouseleave': (_) => _unhover(),
            'contextmenu': (e) {
              e.preventDefault();
              final me = e as MouseEvent;
              _rowMenu(r['symbol'].s!, clientXY(me).$1, clientXY(me).$2);
            },
          }),
      ]),
    ]);
    replace(_results, h('div.table-wrap', table));
  }

  bool _isNum(String c) {
    final f = _byKey[c];
    return f != null && f.fmt != 'text';
  }

  void _renderTiles(List<J> rows) {
    final grid = h('div.tile-grid');
    for (final r in rows) {
      final sym = r['symbol'].s!;
      final canvas = h('canvas.tile-chart') as HTMLCanvasElement;
      final chg = r['change_pct'].d;
      grid.append(h('div.tile', [
        h('div.tile-head', [
          h('b', sym),
          h('span.muted.ellipsis.small', r['name'].s ?? ''),
          h('span.spacer'),
          h('span', r['price'].d == null ? '' : fmtPrice(r['price'].d!)),
          h(chg == null ? 'span' : chg >= 0 ? 'span.up' : 'span.down', chg == null ? '' : '${signed(chg)}%'),
        ]),
        canvas,
        h('div.muted.tiny', '${r['sector'].s ?? ''} · ${compact(r['market_cap'].d)}'),
      ], null, {'click': (_) => _open(sym)}));
      previewBars(sym).then((bars) {
        if (canvas.isConnected) drawSpark(canvas, [for (final b in bars) b.close]);
      });
    }
    replace(_results, grid);
  }

  void _renderPager(int total) {
    final pages = (total / _pageSize).ceil();
    final sizeSel = h('select.input.select.small', [for (final n in [20, 50, 100, 200]) h('option', '$n / page', {'value': '$n'})])
        as HTMLSelectElement;
    sizeSel.value = '$_pageSize';
    sizeSel.addEventListener('change', ((Event _) => _set({'pageSize': int.parse(sizeSel.value), 'page': 0})).toJS);
    replace(_pager, [
      h('button.btn.small', icon('chev-left', size: 14), {'type': 'button', 'aria-label': 'Previous page', if (_page == 0) 'disabled': ''},
          {'click': (_) => _set({'page': math.max(0, _page - 1)})}),
      h('span.small', 'Page ${_page + 1} of ${math.max(1, pages)}'),
      h('button.btn.small', icon('chev-right', size: 14),
          {'type': 'button', 'aria-label': 'Next page', if (_page + 1 >= pages) 'disabled': ''},
          {'click': (_) => _set({'page': math.min(pages - 1, _page + 1)})}),
      sizeSel,
    ]);
  }

  void _open(String sym) {
    store.activeChart.symbol.value = sym;
    store.page.value = 'chart';
  }

  void _rowMenu(String sym, double x, double y) {
    showMenu([
      MenuItem('Open chart', iconName: 'chart', onSelect: () => _open(sym)),
      MenuItem('Add to ${store.watchlist.value}', iconName: 'list', onSelect: () async {
        await request('/api/watchlists/${Uri.encodeComponent(store.watchlist.value)}', method: 'PUT', body: {'add': [sym]});
        toast('$sym added to ${store.watchlist.value}', kind: 'success', ms: 2000);
        store.watchlistVersion.value++;
      }),
      MenuItem('Copy ticker', iconName: 'copy', onSelect: () => window.navigator.clipboard.writeText(sym)),
    ], x: x, y: y);
  }

  // ---------------------------------------------------------------- hover preview

  void _hover(String sym, Element row) {
    _hoverTimer?.cancel();
    _hoverTimer = Timer(const Duration(milliseconds: 350), () async {
      final bars = await previewBars(sym);
      if (!row.isConnected || !(row as HTMLElement).matches(':hover')) return;
      _preview?.close();
      final canvas = h('canvas.preview-chart') as HTMLCanvasElement;
      final last = bars.isEmpty ? null : bars.last;
      final first = bars.isEmpty ? null : bars.first;
      final chg = last != null && first != null ? (last.close / first.close - 1) * 100 : null;
      _preview = Popover.open(h('div.preview', [
        h('div.preview-head', [
          h('b', sym),
          h('span.muted.small', '6 months, daily'),
          h('span.spacer'),
          if (chg != null) h(chg >= 0 ? 'span.up' : 'span.down', '${signed(chg)}%'),
        ]),
        canvas,
      ]), anchor: row, align: 'end', cls: 'preview-pop');
      drawSpark(canvas, [for (final b in bars) b.close], lineWidth: 1.6);
    });
  }

  void _unhover() {
    _hoverTimer?.cancel();
    _preview?.close();
    _preview = null;
  }

  // ---------------------------------------------------------------- columns, export, watchlist

  void _pickColumns() {
    final chosen = [..._columns];
    final list = h('div.col-picker');
    void render() {
      final groups = <String, List<FieldMeta>>{};
      for (final f in _fields) {
        groups.putIfAbsent(f.group, () => []).add(f);
      }
      replace(list, [
        for (final e in groups.entries) ...[
          h('h4', e.key),
          h('div.field-list', [
            for (final f in e.value)
              h('label.col-check', [
                (h('input', null, {'type': 'checkbox', if (chosen.contains(f.key)) 'checked': '', if (f.key == 'symbol') 'disabled': ''},
                    {'change': (ev) {
                      final on = ((ev.target as JSObject) as HTMLInputElement).checked;
                      on ? chosen.add(f.key) : chosen.remove(f.key);
                    }})),
                ' ${f.label}',
              ]),
          ]),
        ],
      ]);
    }

    render();
    Dialog.open('Choose columns', list, width: '720px', actions: [
      const DialogAction('Cancel'),
      DialogAction('Apply', primary: true, onPressed: () {
        final cols = ['symbol', ...chosen.where((c) => c != 'symbol')];
        _set({'view': 'custom', 'columns': cols});
        return true;
      }),
    ]);
  }

  Future<void> _exportCsv() async {
    try {
      final res = await request('/api/screener', method: 'POST', body: {
        'universe': _st['universe'] ?? 'all', 'filters': _st['filters'] ?? <Object>[], 'expression': _st['expression'],
        'signal': _st['signal'], 'search': _st['search'], 'sort': _st['sort'], 'columns': _columns, 'offset': 0, 'limit': 500,
      });
      final cols = [for (final c in res['columns'].list) c.s ?? ''];
      final csv = toCsv([for (final c in cols) _byKey[c]?.label ?? c], [
        for (final r in res['rows'].list) [for (final c in cols) r[c].v],
      ]);
      final blob = Blob([csv.toJS].toJS, BlobPropertyBag(type: 'text/csv'));
      final a = document.createElement('a') as HTMLAnchorElement;
      a.href = URL.createObjectURL(blob);
      a.download = 'screen_${DateTime.now().toIso8601String().substring(0, 10)}.csv';
      a.click();
      toast('Exported ${res['rows'].list.length} rows', kind: 'success', ms: 2000);
    } catch (e) {
      toast('$e', kind: 'error');
    }
  }

  Future<void> _addAll(Element anchor) async {
    final rows = _res?['rows'].list ?? const [];
    if (rows.isEmpty) return;
    List<String> lists = [store.watchlist.value];
    try {
      lists = [for (final w in (await request('/api/watchlists')).list) w['name'].s ?? ''];
    } catch (_) {}
    showMenu([
      const MenuItem.header('Add this page to'),
      for (final l in lists)
        MenuItem(l, iconName: 'list', onSelect: () async {
          await request('/api/watchlists/${Uri.encodeComponent(l)}', method: 'PUT',
              body: {'add': [for (final r in rows) r['symbol'].s]});
          store.watchlistVersion.value++;
          toast('Added ${rows.length} symbols to $l', kind: 'success', ms: 2500);
        }),
    ], anchor: anchor, align: 'end');
  }
}
