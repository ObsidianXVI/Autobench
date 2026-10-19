// Modal pickers: symbol search (opened by typing anywhere on the chart, like TradingView),
// indicator browser, and the quick interval prompt.
import 'dart:async';
import 'dart:convert';
import 'dart:js_interop';

import 'package:web/web.dart';

import '../core/models.dart';
import 'api.dart';
import 'dom.dart';
import 'kit/icons.dart';
import 'kit/overlay.dart';
import 'store.dart';

const _recentKey = 'autobench.recentSymbols';

List<String> recentSymbols() {
  try {
    final raw = window.localStorage.getItem(_recentKey);
    if (raw == null) return [];
    return [for (final s in jsonDecode(raw) as List) '$s'];
  } catch (_) {
    return [];
  }
}

void rememberSymbol(String s) {
  try {
    final list = [s, ...recentSymbols().where((x) => x != s)].take(12).toList();
    window.localStorage.setItem(_recentKey, jsonEncode(list));
  } catch (_) {}
}

/// Opens the symbol search. [initial] pre-fills the query (the key the user started typing).
void openSymbolSearch({String initial = '', String title = 'Symbol search', required void Function(String) onPick}) {
  late Dialog d;
  final input = h('input.input.search-big', null, {
    'placeholder': 'Ticker or company name', 'aria-label': 'Symbol', 'autocomplete': 'off', 'spellcheck': 'false',
  }) as HTMLInputElement;
  input.value = initial;
  final list = h('div.search-list', null, {'role': 'listbox'});
  var items = <SymbolMatch>[];
  var active = 0;
  Timer? debounce;
  var seq = 0;

  void pick(String s) {
    final sym = s.trim().toUpperCase();
    if (sym.isEmpty) return;
    rememberSymbol(sym);
    d.close();
    onPick(sym);
  }

  void render() {
    if (input.value.trim().isEmpty) {
      final recent = recentSymbols();
      replace(list, [
        if (recent.isNotEmpty) h('div.search-head', 'Recent'),
        for (final s in recent)
          h('div.search-row', [h('b', s), h('span.muted', '')], null, {'click': (_) => pick(s)}),
        if (recent.isEmpty) h('div.muted.pad', 'Type a ticker (AAPL), a company name, an index (^VIX), a future (CL=F) or crypto (BTC-USD).'),
      ]);
      return;
    }
    replace(list, [
      for (var i = 0; i < items.length; i++)
        h('div.search-row${i == active ? '.active' : ''}', [
          h('b', items[i].symbol),
          h('span.ellipsis', items[i].name),
          h('span.muted.small', items[i].exchange),
        ], {'role': 'option'}, {
          'click': (_) => pick(items[i].symbol),
          'mouseenter': (_) {
            active = i;
            render();
          },
        }),
      if (items.isEmpty) h('div.muted.pad', ['Press Enter to open ', h('b', input.value.trim().toUpperCase())]),
    ]);
  }

  void query() {
    debounce?.cancel();
    final q = input.value.trim();
    if (q.isEmpty) {
      items = [];
      render();
      return;
    }
    debounce = Timer(const Duration(milliseconds: 120), () async {
      final s = ++seq;
      try {
        final r = await api.search(q);
        if (s != seq) return;
        items = r;
        active = 0;
      } catch (_) {
        items = [];
      }
      render();
    });
  }

  input.addEventListener('input', ((Event _) => query()).toJS);
  input.addEventListener('keydown', ((KeyboardEvent e) {
    if (e.key == 'ArrowDown' && items.isNotEmpty) {
      e.preventDefault();
      active = (active + 1) % items.length;
      render();
    } else if (e.key == 'ArrowUp' && items.isNotEmpty) {
      e.preventDefault();
      active = (active - 1 + items.length) % items.length;
      render();
    } else if (e.key == 'Enter') {
      e.preventDefault();
      final typed = input.value.trim();
      // exact ticker typed: take it even if search has other matches first
      final exact = items.where((x) => x.symbol == typed.toUpperCase()).firstOrNull;
      pick(exact?.symbol ?? (items.isNotEmpty ? items[active].symbol : typed));
    }
  }).toJS);
  d = Dialog.open(title, h('div.search-dialog', [
    h('div.search-input-wrap', [icon('search', size: 18), input]),
    list,
  ]), width: '560px', cls: 'search-modal');
  render();
  if (initial.isNotEmpty) query();
  // focus now (not on the next tick) so fast typing after the first key is not lost
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
}

const _categoryOrder = ['trend', 'momentum', 'volatility', 'volume', 'statistics'];

/// Indicator browser: search, categories, click to add (stays open so several can be added).
void openIndicatorPicker({required void Function(IndicatorRef) onAdd, List<IndicatorRef> current = const []}) {
  final specs = store.specs.value;
  final input = h('input.input', null, {'placeholder': 'Search indicators (e.g. rsi, bollinger, vwap)', 'autocomplete': 'off'})
      as HTMLInputElement;
  final list = h('div.ind-list');
  var category = 'all';
  final cats = h('div.ind-cats');
  final added = <String>[for (final r in current) r.name];

  void render() {
    final q = input.value.trim().toLowerCase();
    final shown = specs.where((s) =>
        (category == 'all' || s.category == category) &&
        (q.isEmpty || '${s.name} ${s.title} ${s.description}'.toLowerCase().contains(q)));
    replace(cats, [
      for (final c in ['all', ..._categoryOrder])
        h(c == category ? 'button.chip-btn.on' : 'button.chip-btn', c[0].toUpperCase() + c.substring(1), {'type': 'button'}, {
          'click': (_) {
            category = c;
            render();
          },
        }),
    ]);
    replace(list, [
      for (final s in shown)
        h('div.ind-item', [
          h('div', [
            h('b', s.title),
            h('span.muted.small', '  ${s.overlay ? 'overlay' : 'pane'} · ${s.category}'),
            if (added.contains(s.name)) h('span.tag', 'on chart'),
          ]),
          h('div.muted.small.ellipsis', s.description),
        ], null, {
          'click': (_) {
            onAdd(IndicatorRef(s.name, const {}));
            added.add(s.name);
            toast('${s.title} added', kind: 'success', ms: 1500);
            render();
          },
        }),
      if (shown.isEmpty) h('div.muted.pad', 'No indicator matches.'),
    ]);
  }

  input.addEventListener('input', ((Event _) => render()).toJS);
  Dialog.open('Indicators', h('div.ind-dialog', [input, cats, list]), width: '620px', cls: 'ind-modal');
  render();
  Timer.run(() => input.focus());
}

/// Maps typed interval shorthands (TradingView style) to interval names.
String? parseInterval(String raw) {
  final s = raw.trim().toUpperCase();
  return switch (s) {
    '1' => '1m',
    '5' => '5m',
    '15' => '15m',
    '30' => '30m',
    '60' || '1H' || 'H' => '1h',
    'D' || '1D' => '1d',
    'W' || '1W' => '1wk',
    'M' || '1M' => '1mo',
    _ => intervals.contains(raw.trim().toLowerCase()) ? raw.trim().toLowerCase() : null,
  };
}

/// Small prompt that appears when the user types a digit on the chart (TradingView's quick interval).
void openIntervalPrompt(String first, void Function(String) onPick) {
  final input = h('input.input', null, {'aria-label': 'Interval', 'placeholder': '5, 15, 60, D, W, M'}) as HTMLInputElement;
  input.value = first;
  late Popover pop;
  input.addEventListener('keydown', ((KeyboardEvent e) {
    e.stopPropagation();
    if (e.key == 'Enter') {
      final iv = parseInterval(input.value);
      pop.close();
      if (iv != null) {
        onPick(iv);
      } else {
        toast('Unknown interval "${input.value}"', kind: 'warn');
      }
    } else if (e.key == 'Escape') {
      pop.close();
    }
  }).toJS);
  pop = Popover.open(h('div.interval-prompt', [h('span.muted.small', 'Change interval'), input]),
      x: window.innerWidth / 2 - 100, y: window.innerHeight / 3, cls: 'center-pop');
  Timer.run(() {
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  });
}
