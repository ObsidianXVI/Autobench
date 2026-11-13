// Shared QBench UI pieces: the on/off preference, number formatting, score bars, verdict/evidence badges,
// equity-curve charts (reusing the Portfolio page's PerfChart) and small layout helpers.
import 'dart:js_interop';

import 'package:web/web.dart';

import '../../../core/json.dart';
import '../../api.dart';
import '../../dom.dart';
import '../../kit/icons.dart';
import '../../pickers.dart';
import '../../store.dart';
import '../portfolio_chart.dart';

/// QBench pages in the top bar: (key, label, icon). Hidden unless "Show QBench Controls" is on.
const qbenchPages = [
  ('qlab', 'Quant Lab', 'flask'),
  ('qsignals', 'Q-Signals', 'sigma'),
  ('qarb', 'Arb & Hedge', 'scale'),
  ('qlib', 'Strategies', 'book'),
];

bool get qbenchOn => store.pref<bool>('qbench.on');
bool isQbenchPage(String key) => qbenchPages.any((p) => p.$1 == key);

String fnum(double? v, {int d = 2, String suffix = ''}) => v == null ? '—' : '${v.toStringAsFixed(d)}$suffix';

String fpct(double? v, {int d = 1, bool sign = true}) =>
    v == null ? '—' : '${sign && v > 0 ? '+' : ''}${v.toStringAsFixed(d)}%';

/// \$12,345 / -\$1,234 (whole dollars).
String fmoney(double? v) {
  if (v == null) return '—';
  final s = v.abs().round().toString().replaceAllMapped(RegExp(r'\B(?=(\d{3})+(?!\d))'), (_) => ',');
  return '${v < 0 ? '-' : ''}\$$s';
}

String signCls(double? v) => v == null || v == 0 ? '' : (v > 0 ? 'up' : 'down');

String scoreCls(double? s) => s == null ? '' : s >= 60 ? 'up' : s <= 40 ? 'down' : '';

/// A labelled 0-100 bar (50 = neutral).
HTMLElement scoreBar(String label, double? score, {String? tip}) {
  final fill = h('i.qb-bar-fill.${scoreCls(score).isEmpty ? 'mid' : scoreCls(score)}');
  fill.style.width = '${(score ?? 0).clamp(0, 100)}%';
  return h('div.qb-score', [
    h('span.qb-score-label', label),
    h('span.qb-bar', [fill, h('i.qb-bar-mid')]),
    h('b.qb-score-val${score == null ? '.muted' : ''}', score == null ? '—' : score.toStringAsFixed(0)),
  ], {if (tip != null) 'data-tip': tip});
}

HTMLElement verdictBadge(String? v) {
  final kind = switch (v) {
    'robust' => 'up',
    'promising' => 'info',
    'de-risks only' => 'agent',
    'no edge' => 'down',
    _ => 'neutral',
  };
  const tips = {
    'robust': 'Out-of-sample timing alpha survives deflation for every combination tried (alpha DSR ≥ 0.9)',
    'promising': 'Positive OOS alpha with alpha DSR ≥ 0.6: suggestive, confirm on peers',
    'de-risks only': 'No significant alpha, but a similar Sharpe with a much smaller drawdown: a risk tool',
    'no edge': 'Out-of-sample Sharpe not above buy-and-hold',
    'not significant': 'Indistinguishable from data mining',
  };
  return h('span.badge.$kind', v ?? '—', {if (tips[v] != null) 'data-tip': tips[v]!});
}

HTMLElement evidenceBadge(String? e) {
  final kind = switch (e) {
    'strong' => 'up',
    'moderate' => 'info',
    'contested' => 'agent',
    'myth' => 'down',
    _ => 'neutral',
  };
  return h('span.badge.$kind', e ?? '—', {'data-tip': 'Strength of the published evidence (after decay)'});
}

HTMLElement stat(String label, Object value, {String cls = '', String? tip}) =>
    h('div.stat', [h('span.muted', label), h('b${cls.isEmpty ? '' : '.$cls'}', '$value')], {if (tip != null) 'data-tip': tip});

HTMLElement loading(String msg) => h('div.loading-block.small', [h('div.spinner'), h('div.muted', msg)]);

HTMLElement errorBlock(Object e, {void Function()? retry}) => h('div.error-block', [
      h('div', '$e'),
      if (retry != null) h('button.btn.small', 'Retry', {'type': 'button'}, {'click': (_) => retry()}),
    ]);

HTMLElement emptyState(String title, String body, {String iconName = 'sigma'}) =>
    h('div.empty-state', [icon(iconName, size: 28), h('div', title), h('div.small', body)]);

/// Strategy vs buy-and-hold (both rebased to 100).
HTMLElement equityChart(J curve, {String a = 'strategy', String b = 'buy_hold', String aLabel = 'Strategy',
    String bLabel = 'Buy & hold', double height = 200}) {
  final dates = [for (final d in curve['dates'].list) d.s ?? ''];
  if (dates.length < 2) return h('div.muted.small', 'Not enough data for a chart');
  final sa = [for (final v in curve[a].list) v.d];
  final sb = curve[b].isNull ? null : [for (final v in curve[b].list) v.d];
  final chart = PerfChart(
    dates: dates,
    lines: [if (sb != null) ChartLine(bLabel, 'qb-line-bh', sb), ChartLine(aLabel, 'qb-line-strat', sa)],
    axisLabel: (v) => v.toStringAsFixed(0),
    height: height,
    tip: (i) => [
      (aLabel, fnum(sa[i], d: 1), null),
      if (sb != null) (bLabel, fnum(sb[i], d: 1), null),
    ],
  );
  return h('div.qb-chart', [
    chart.root,
    h('div.qb-legend', [
      h('span', [h('i.qb-key.strat'), aLabel]),
      if (sb != null) h('span', [h('i.qb-key.bh'), bLabel]),
      h('span.muted', 'rebased to 100'),
    ]),
  ]);
}

/// Button showing a symbol; click opens the symbol search.
HTMLElement symbolButton(String symbol, void Function(String) onPick) =>
    h('button.btn.qb-sym', [icon('search', size: 14), h('b', symbol.isEmpty ? 'Pick a symbol' : symbol)],
        {'type': 'button', 'data-tip': 'Change symbol'}, {
      'click': (_) => openSymbolSearch(initial: symbol, title: 'QBench symbol', onPick: onPick),
    });

/// Select of the user's watchlists.
Future<HTMLElement> watchlistSelect(String value, void Function(String) onChange) async {
  final lists = await attempt(api.watchlists()) ?? const [];
  final sel = h('select.input.select', [
    for (final w in lists) h('option', '${w.name} (${w.symbols.where((s) => !s.startsWith('###')).length})', {'value': w.name}),
  ], {'aria-label': 'Watchlist'}) as HTMLSelectElement;
  sel.value = value;
  sel.addEventListener('change', ((Event _) => onChange(sel.value)).toJS);
  return sel;
}

HTMLElement select(List<(String, String)> opts, String value, void Function(String) onChange, {String label = ''}) {
  final s = h('select.input.select', [for (final (v, l) in opts) h('option', l, {'value': v})], {'aria-label': label})
      as HTMLSelectElement;
  s.value = value;
  s.addEventListener('change', ((Event _) => onChange(s.value)).toJS);
  return s;
}

/// Tab bar bound to a preference key.
HTMLElement tabBar(List<(String, String)> tabs, String current, void Function(String) onPick) => h('div.tr-tabs', [
      for (final (k, l) in tabs)
        h('button.tab${current == k ? '.on' : ''}', l, {'type': 'button', 'data-tab': k}, {'click': (_) => onPick(k)}),
    ]);

void goChart(String sym) {
  store.activeChart.symbol.value = sym;
  store.page.value = 'chart';
}

void openQuantLab(String sym) {
  store.setPref('qbench.symbol', sym);
  store.page.value = 'qlab';
}

HTMLElement symLink(String sym) => h('a.qb-symlink', sym, {'href': '#qlab', 'data-tip': 'Open in Quant Lab'}, {
      'click': (e) {
        e.preventDefault();
        e.stopPropagation();
        openQuantLab(sym);
      },
    });

String paramsText(J p) => [for (final e in p.map.entries) '${e.key}=${e.value.v}'].join(', ');
