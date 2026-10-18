import 'dart:async';
import 'dart:js_interop';

import 'package:web/web.dart';

import '../core/format.dart';
import '../core/models.dart';
import 'api.dart';
import 'dom.dart';

class TopBar {
  final void Function(String symbol) onSymbol;
  final void Function(String interval) onInterval;
  final void Function(List<IndicatorRef> refs) onIndicators;

  late final HTMLElement root;
  late final HTMLInputElement _search;
  final HTMLElement _results = h('ul.search-results', null, {'role': 'listbox'});
  final HTMLElement _quote = h('div.quote');
  final HTMLElement _intervals = h('div.intervals', null, {'role': 'group', 'aria-label': 'Interval'});
  final HTMLElement _status = h('div.status', null, {'title': 'Live connection to the backend (agent updates arrive here)'});
  late final _IndicatorMenu _menu;

  String _symbol = '', _interval = '';
  List<SymbolMatch> _matches = const [];
  int _active = 0;
  Timer? _debounce;
  int _searchSeq = 0;

  TopBar({required this.onSymbol, required this.onInterval, required this.onIndicators}) {
    _search = h('input', null, {'aria-label': 'Search symbol', 'autocomplete': 'off', 'spellcheck': 'false'})
        as HTMLInputElement;
    _search.addEventListener('input', ((Event _) => _query()).toJS);
    _search.addEventListener('focus', ((Event _) => _renderResults()).toJS);
    _search.addEventListener('blur', ((Event _) {
      Timer(const Duration(milliseconds: 150), () => _results.isHidden = true);
    }).toJS);
    _search.addEventListener('keydown', ((KeyboardEvent e) {
      switch (e.key) {
        case 'ArrowDown':
          e.preventDefault();
          _active = (_active + 1).clamp(0, _matches.isEmpty ? 0 : _matches.length - 1);
          _renderResults();
        case 'ArrowUp':
          e.preventDefault();
          _active = (_active - 1).clamp(0, _matches.isEmpty ? 0 : _matches.length - 1);
          _renderResults();
        case 'Enter':
          final typed = _search.value.trim();
          final pick = _active < _matches.length ? _matches[_active].symbol : typed;
          if (pick.isNotEmpty) _pick(pick);
        case 'Escape':
          _results.isHidden = true;
      }
    }).toJS);
    _results.isHidden = true;
    _menu = _IndicatorMenu(onIndicators);
    root = h('header.topbar', [
      h('div.brand', [
        h('img', null, {'src': 'favicon.svg', 'alt': '', 'width': '22', 'height': '22'}),
        h('span', 'Autobench'),
      ]),
      h('div.search', [_search, _results]),
      _quote,
      _intervals,
      _menu.root,
      h('div.spacer'),
      _status,
    ]);
  }

  void update({
    required String symbol,
    required String interval,
    required Quote? quote,
    required List<IndicatorSpec> specs,
    required List<IndicatorRef> indicators,
    required String status,
    required String provider,
  }) {
    if (symbol != _symbol) {
      _symbol = symbol;
      _search.placeholder = '$symbol — search ticker or company';
    }
    if (interval != _interval) {
      _interval = interval;
      replace(_intervals, [
        for (final iv in intervals)
          h(iv == interval ? 'button.active' : 'button', iv, {'type': 'button'}, {'click': (_) => onInterval(iv)}),
      ]);
    }
    replace(_quote, quote == null
        ? null
        : [
            h('b', symbol),
            h('span.price', quote.price.toStringAsFixed(2)),
            if (quote.changePct != null)
              h(quote.changePct! >= 0 ? 'span.up' : 'span.down',
                  '${quote.change == null ? '' : signed(quote.change!)} (${pct(quote.changePct)})'),
          ]);
    _menu.update(specs, indicators);
    replace(_status, [
      h(status == 'open' ? 'span.dot.ok' : 'span.dot.warn'),
      h('span.muted.small', '${status == 'open' ? 'live' : status} · $provider'),
    ]);
  }

  void _query() {
    _debounce?.cancel();
    final q = _search.value.trim();
    if (q.isEmpty) {
      _matches = const [];
      _renderResults();
      return;
    }
    _debounce = Timer(const Duration(milliseconds: 180), () async {
      final seq = ++_searchSeq;
      try {
        final r = await api.search(q);
        if (seq != _searchSeq) return;
        _matches = r;
        _active = 0;
      } catch (_) {
        _matches = const [];
      }
      _renderResults();
    });
  }

  void _renderResults() {
    _results.isHidden = _matches.isEmpty || document.activeElement != _search;
    replace(_results, [
      for (var i = 0; i < _matches.length; i++)
        h(i == _active ? 'li.active' : 'li', [
          h('b', _matches[i].symbol),
          h('span.muted.ellipsis', _matches[i].name),
          h('span.muted.small', _matches[i].exchange),
        ], {
          'role': 'option'
        }, {
          'mousedown': (e) {
            e.preventDefault();
            _pick(_matches[i].symbol);
          },
        }),
    ]);
  }

  void _pick(String s) {
    onSymbol(s.toUpperCase());
    _search.value = '';
    _matches = const [];
    _results.isHidden = true;
    _search.blur();
  }
}

class _IndicatorMenu {
  final void Function(List<IndicatorRef>) onChange;
  final HTMLElement _button = h('button.btn', null, {'type': 'button'});
  final HTMLElement _panel = h('div.menu-panel');
  late final HTMLElement root = h('div.menu', [_button, _panel]);
  List<IndicatorSpec> _specs = const [];
  List<IndicatorRef> _active = const [];
  IndicatorSpec? _editing;
  final Map<String, String> _params = {};
  String _filter = '';
  late final HTMLInputElement _filterInput;
  final HTMLElement _list = h('ul.menu-list');

  _IndicatorMenu(this.onChange) {
    _panel.isHidden = true;
    _button.addEventListener('click', ((Event _) {
      _panel.isHidden = !_panel.isHidden;
      if (!_panel.isHidden) {
        _render();
        if (_editing == null) _filterInput.focus();
      }
    }).toJS);
    document.addEventListener('mousedown', ((MouseEvent e) {
      final t = e.target;
      if (t != null && !root.contains(t as Node)) _panel.isHidden = true;
    }).toJS);
    _filterInput = h('input.menu-filter', null, {'placeholder': 'Filter…', 'aria-label': 'Filter indicators'})
        as HTMLInputElement;
    _filterInput.addEventListener('input', ((Event _) {
      _filter = _filterInput.value;
      _renderList();
    }).toJS);
  }

  void update(List<IndicatorSpec> specs, List<IndicatorRef> active) {
    _specs = specs;
    _active = active;
    _button.textContent = 'ƒx Indicators${active.isEmpty ? '' : ' · ${active.length}'}';
    if (!_panel.isHidden) _render();
  }

  void _render() {
    final editing = _editing;
    replace(_panel, [
      if (_active.isNotEmpty)
        h('div.chips', [
          for (var i = 0; i < _active.length; i++)
            h('span.chip', [
              _active[i].label,
              h('button', '×', {'aria-label': 'remove ${_active[i].label}', 'type': 'button'}, {
                'click': (_) => onChange([..._active.sublist(0, i), ..._active.sublist(i + 1)]),
              }),
            ]),
        ]),
      if (editing != null)
        h('div.ind-edit', [
          h('div.ind-title', editing.title),
          h('div.muted.small', editing.description),
          for (final e in editing.params.entries)
            h('label.field', [
              h('span', e.key),
              _paramInput(e.key, e.value),
            ]),
          h('div.row.gap', [
            h('button.btn.primary', 'Add', {'type': 'button'}, {'click': (_) => _add()}),
            h('button.btn', 'Back', {'type': 'button'}, {
              'click': (_) {
                _editing = null;
                _render();
              },
            }),
          ]),
        ])
      else ...[
        _filterInput,
        _list,
      ],
    ]);
    if (editing == null) _renderList();
  }

  HTMLElement _paramInput(String key, Object? def) {
    final input = h('input', null, {'aria-label': key}) as HTMLInputElement;
    input.value = _params[key] ?? '${def ?? ''}';
    input.addEventListener('input', ((Event _) => _params[key] = input.value).toJS);
    input.addEventListener('keydown', ((KeyboardEvent e) {
      if (e.key == 'Enter') _add();
    }).toJS);
    return input;
  }

  void _renderList() {
    final f = _filter.toLowerCase();
    replace(_list, [
      for (final s in _specs)
        if (f.isEmpty || '${s.name} ${s.title}'.toLowerCase().contains(f))
          h('li', [
            h('span', s.title),
            h('span.muted.small', '${s.overlay ? 'overlay' : 'pane'} · ${s.category}'),
          ], null, {
            'click': (_) {
              _params.clear();
              _editing = s;
              _render();
            },
          }),
    ]);
  }

  void _add() {
    final editing = _editing;
    if (editing == null) return;
    final p = <String, Object>{};
    for (final e in editing.params.entries) {
      final raw = _params[e.key];
      if (raw == null || raw.isEmpty || raw == '${e.value}') continue;
      p[e.key] = e.value is num ? (num.tryParse(raw) ?? e.value!) : raw;
    }
    _editing = null;
    onChange([..._active, IndicatorRef(editing.name, p)]);
    _render();
  }
}
