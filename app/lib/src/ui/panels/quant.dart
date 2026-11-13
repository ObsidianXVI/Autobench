// QBench dock panel (chart page, only when QBench is on): the active symbol's TA / FA / quant confluence,
// regime, the Strategy Lab's live signal and the quant-derived plan levels, next to the chart.
import 'dart:async';

import 'package:web/web.dart';

import '../../core/json.dart';
import '../api.dart';
import '../dom.dart';
import '../kit/icons.dart';
import '../kit/state.dart';
import '../pages/qbench/common.dart';
import '../store.dart';

class QuantPanel {
  final HTMLElement root = h('div.panel.qb-panel');
  final HTMLElement _body = h('div.panel-scroll');
  final HTMLElement _title = h('span.panel-title.static', 'Quant');
  final Scope _scope = Scope();
  final _debounce = Debouncer(350);
  int _seq = 0;
  String? _loaded;
  J? _data;

  QuantPanel() {
    append(root, [
      h('div.panel-head', [
        _title,
        h('span.spacer'),
        h('button.icon-btn', icon('refresh', size: 15), {'type': 'button', 'data-tip': 'Reload', 'aria-label': 'Reload'},
            {'click': (_) => _load(force: true)}),
        h('button.icon-btn', icon('flask', size: 15), {'type': 'button', 'data-tip': 'Open in Quant Lab', 'aria-label': 'Quant Lab'},
            {'click': (_) => openQuantLab(store.activeChart.symbol.value)}),
      ]),
      _body,
    ]);
    void track() {
      for (final c in store.charts) {
        _scope.listen(c.symbol, (_) => _debounce(_load));
      }
    }

    track();
    _scope.listen(store.layoutVersion, (_) => track());
    _scope.listen(store.active, (_) => _debounce(_load));
    _load();
  }

  void dispose() => _scope.dispose();

  /// The lab was run (by the agent or on the Quant Lab page).
  void onLabEvent(String symbol) {
    if (symbol == store.activeChart.symbol.value) _load(force: true);
  }

  Future<void> _load({bool force = false}) async {
    final sym = store.activeChart.symbol.value;
    if (!force && sym == _loaded && _data != null) return;
    _loaded = sym;
    final seq = ++_seq;
    _title.textContent = 'Quant · $sym';
    replace(_body, loading('Scoring $sym…'));
    try {
      final c = await request('/api/qbench/confluence/${Uri.encodeComponent(sym)}');
      if (seq != _seq) return;
      _data = c;
      _render(c);
    } catch (e) {
      if (seq == _seq) replace(_body, errorBlock(e, retry: () => _load(force: true)));
    }
  }

  Future<void> _runLab(HTMLButtonElement btn) async {
    btn.disabled = true;
    replace(btn, [h('span.spinner.tiny'), 'Running…']);
    final sym = store.activeChart.symbol.value;
    await attempt(request('/api/qbench/lab/${Uri.encodeComponent(sym)}', method: 'POST', body: <String, Object?>{}));
    await _load(force: true);
  }

  void _render(J c) {
    final p = c['pillars'];
    final q = p['quant'];
    final lb = q['lab_best'];
    final lv = c['levels'];
    final pref = lv['preferred_plan'].s;
    final plan = pref == null ? null : lv['plans'][pref];
    final comp = c['composite'].d;
    replace(_body, h('div.qb-panel-body', [
      h('div.qb-composite', [
        h('div.qb-big.${scoreCls(comp)}', comp == null ? '—' : comp.toStringAsFixed(0)),
        h('div', [h('b', c['stance'].s ?? 'no data'), h('div.muted.small', 'pillars ${c['agreement'].s ?? ''}')]),
      ]),
      scoreBar('Technical', p['technical']['score'].d),
      scoreBar('Fundamental', p['fundamental']['score'].d),
      scoreBar('Quant', q['score'].d),
      if (c['factor_scores'].map.isNotEmpty) ...[
        h('div.qb-sub', 'Factor ranks (Q-Score ${fnum(c['q_score'].d, d: 0)})'),
        for (final e in c['factor_scores'].map.entries) scoreBar(e.key.replaceAll('_', ' '), e.value.d),
      ],
      h('div.qb-sub', 'Regime'),
      h('div.qb-regime', [
        h('span.badge.${q['regime'].s == 'trending' ? 'up' : q['regime'].s == 'mean-reverting' ? 'info' : 'neutral'}', q['regime'].s ?? '—'),
        if (!c['volatility'].isNull)
          h('span.small.muted', 'vol ${fpct(c['volatility']['realized_21d_pct'].d, sign: false)} '
              '(pctile ${fnum(c['volatility']['percentile_3y'].d, d: 0)})'),
      ]),
      h('div.qb-sub', 'Strategy Lab'),
      if (lb.isNull)
        h('div.small.muted', q['lab_as_of'].isNull ? 'Not run for this symbol.' : 'No strategy beat data-mining odds.')
      else
        h('div.qb-labsig', [
          h('b', lb['name'].s ?? ''),
          verdictBadge(lb['verdict'].s),
          h('span.badge.${(lb['position'].d ?? 0) > 0 ? 'up' : 'neutral'}', (lb['position'].d ?? 0) > 0 ? 'LONG' : 'flat'),
          h('div.muted.tiny', 'since ${lb['since'].s} · OOS Sharpe ${fnum(lb['oos_sharpe'].d)} · α DSR ${fnum(lb['dsr'].d)}'),
        ]),
      if (plan != null && !plan.isNull) ...[
        h('div.qb-sub', 'Plan: ${pref!.replaceAll('_', ' ')}'),
        h('div.stat-grid', [
          stat('Entry', plan['entry_zone'].isNull
              ? fnum(plan['entry'].d ?? plan['trigger'].d)
              : '${fnum(plan['entry_zone'].list.first.d)}–${fnum(plan['entry_zone'].list.last.d)}'),
          stat('Stop', fnum(plan['stop'].d), cls: 'down'),
          stat('Target', fnum(plan['target'].d), cls: 'up'),
          stat('Vol-target wt', fpct(lv['sizing']['vol_target_weight_pct'].d, sign: false)),
        ]),
        h('div.muted.tiny', plan['note'].s ?? ''),
      ],
      h('div.qb-panel-actions', [
        h('button.btn.small', [icon('play', size: 13), q['lab_as_of'].isNull ? 'Run lab' : 'Re-run lab'], {'type': 'button'}, {
          'click': (e) => unawaited(_runLab(e.currentTarget as HTMLButtonElement)),
        }),
        h('button.btn.small', [icon('flask', size: 13), 'Quant Lab'], {'type': 'button'},
            {'click': (_) => openQuantLab(store.activeChart.symbol.value)}),
      ]),
    ]));
  }
}
