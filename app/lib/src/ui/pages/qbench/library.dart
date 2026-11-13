// QBench Strategies: the strategy library as cards (rules, papers, evidence and its decay, when it works and
// fails, parameters, the Autobench expression) with a family filter and "Test on <symbol>" for lab strategies.
import 'package:web/web.dart';

import '../../../core/json.dart';
import '../../api.dart';
import '../../dom.dart';
import '../../kit/icons.dart';
import '../../kit/overlay.dart';
import '../../store.dart';
import 'common.dart';

class QLibPage {
  final HTMLElement root = h('div.page.qb-page.qlib-page');
  final HTMLElement _head = h('div.tr-head');
  final HTMLElement _body = h('div.qb-lib');
  List<J> _all = [];

  String get _family => store.pref<String>('qbench.libFamily');

  QLibPage() {
    append(root, [_head, _body]);
  }

  Map<String, Object?> agentContext() => {'family': _family, 'focus': store.pref<String>('qbench.libFocus')};

  Future<void> onShow() async {
    if (_all.isEmpty) {
      replace(_body, loading('Loading the strategy library…'));
      try {
        _all = (await request('/api/qbench/strategies'))['strategies'].list;
      } catch (e) {
        replace(_body, errorBlock(e, retry: onShow));
        return;
      }
    }
    _render();
  }

  void _render() {
    final families = {for (final s in _all) s['family'].s ?? ''}.toList()..sort();
    final focus = store.pref<String>('qbench.libFocus');
    replace(_head, [
      h('h2', [icon('book', size: 18), ' Strategy library']),
      h('span.muted.small', '${_all.length} strategies · evidence after post-publication decay'),
      h('span.spacer'),
      select([('', 'All families'), for (final f in families) (f, f.replaceAll('_', ' '))], _family, (v) {
        store.setPref('qbench.libFamily', v);
        _render();
      }, label: 'Family'),
    ]);
    const order = {'single': 0, 'cross_sectional': 1, 'pairs': 2, 'portfolio': 3, 'reference': 4};
    final shown = [for (final s in _all) if (_family.isEmpty || s['family'].s == _family) s]
      ..sort((a, b) => (order[a['kind'].s] ?? 9).compareTo(order[b['kind'].s] ?? 9));
    replace(_body, [for (final s in shown) _card(s, s['id'].s == focus)]);
    if (focus.isNotEmpty) {
      final el = _body.querySelector('.qb-strat.focus');
      el?.scrollIntoView();
      store.setPref('qbench.libFocus', '');
    }
  }

  HTMLElement _card(J s, bool focus) {
    final kind = s['kind'].s ?? '';
    const kindLabel = {
      'single': 'Lab-testable on one stock',
      'cross_sectional': 'Ranks a list',
      'pairs': 'Pairs / two legs',
      'portfolio': 'Portfolio overlay',
      'reference': 'Knowledge only',
    };
    final expr = s['expression'];
    return h('article.qb-strat${focus ? '.focus' : ''}', [
      h('div.qb-card-head', [
        h('h3', s['name'].s ?? ''),
        evidenceBadge(s['evidence'].s),
        h('span.badge.neutral', (s['family'].s ?? '').replaceAll('_', ' ')),
        h('span.muted.small', kindLabel[kind] ?? kind),
        h('span.spacer'),
        if (kind == 'single')
          h('button.btn.small', [icon('play', size: 13), 'Test on ${store.activeChart.symbol.value}'], {'type': 'button',
              'data-tip': 'Backtest with default parameters on the active chart symbol'}, {
            'click': (e) => _quickTest(s, e.currentTarget as HTMLElement),
          }),
      ]),
      h('p.qb-summary', s['summary'].s ?? ''),
      h('div.qb-rules', [h('b', 'Rules '), s['rules'].s ?? '']),
      h('div.qb-ev', [h('b', 'Evidence '), s['evidence_note'].s ?? '']),
      if ((s['works_when'].s ?? '').isNotEmpty || (s['fails_when'].s ?? '').isNotEmpty)
        h('div.qb-whens', [
          h('div', [h('b.up', 'Works when '), s['works_when'].s ?? '']),
          h('div', [h('b.down', 'Fails when '), s['fails_when'].s ?? '']),
        ]),
      h('div.qb-meta', [
        if ((s['horizon'].s ?? '').isNotEmpty) h('span', [h('span.muted', 'Horizon '), s['horizon'].s!]),
        if (s['params'].map.isNotEmpty) h('span', [h('span.muted', 'Params '), paramsText(s['params'])]),
        if (s['combinations'].i != null) h('span', [h('span.muted', 'Lab grid '), '${s['combinations'].i} combinations']),
        if ((s['run_with'].s ?? '').isNotEmpty) h('span', [h('span.muted', 'Run with '), h('code', s['run_with'].s!)]),
      ]),
      if (!expr.isNull)
        h('div.qb-meta', [h('span.muted', 'Expression '), h('code', 'entry: ${expr['entry'].s}'), h('code', 'exit: ${expr['exit'].s}')]),
      h('details.qb-refs', [
        h('summary', 'References (${s['references'].list.length})'),
        h('ul', [for (final r in s['references'].list) h('li', r.s ?? '')]),
      ]),
      h('div.qb-quick'),
    ]);
  }

  Future<void> _quickTest(J s, HTMLElement btn) async {
    final card = btn.closest('.qb-strat');
    final host = card?.querySelector('.qb-quick') as HTMLElement?;
    if (host == null) return;
    final sym = store.activeChart.symbol.value;
    replace(host, loading('Backtesting ${s['name'].s} on $sym…'));
    try {
      final r = await request('/api/qbench/strategy-run', method: 'POST', body: {'symbol': sym, 'strategy': s['id'].s});
      final p = r['performance'], b = r['buy_and_hold'];
      replace(host, [
        equityChart(r['equity_curve'], aLabel: '${s['name'].s} on $sym', height: 160),
        h('div.stat-grid.qb-stats4', [
          stat('Sharpe (B&H)', '${fnum(p['sharpe'].d)} (${fnum(b['sharpe'].d)})'),
          stat('CAGR (B&H)', '${fpct(p['cagr_pct'].d)} (${fpct(b['cagr_pct'].d)})'),
          stat('Max DD (B&H)', '${fpct(p['max_drawdown_pct'].d, sign: false)} (${fpct(b['max_drawdown_pct'].d, sign: false)})'),
          stat('Now', (r['position_now'].d ?? 0) > 0 ? 'LONG' : 'flat'),
        ]),
        for (final w in r['warnings'].list) h('div.qb-warn.small', [icon('info', size: 13), w.s ?? '']),
        h('button.btn.small', [icon('flask', size: 13), 'Walk-forward in Quant Lab'], {'type': 'button'},
            {'click': (_) => openQuantLab(sym)}),
      ]);
    } catch (e) {
      replace(host, errorBlock(e));
      toast('$e', kind: 'error');
    }
  }
}
