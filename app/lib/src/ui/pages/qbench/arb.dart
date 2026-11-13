// QBench Arb & Hedge: strategies that do not rely on FA/TA direction. Pairs / statistical arbitrage
// (cointegration scan + rolling pair backtest), market hedging of the account (beta and min-variance hedges)
// and risk-based allocation (inverse vol, risk parity, min variance, HRP).
import 'dart:js_interop';

import 'package:web/web.dart';

import '../../api.dart';
import '../../dom.dart';
import '../../kit/icons.dart';
import '../../store.dart';
import '../portfolio_chart.dart';
import 'common.dart';

class QArbPage {
  final HTMLElement root = h('div.page.qb-page.qarb-page');
  final HTMLElement _head = h('div.tr-head');
  final HTMLElement _tabs = h('div');
  final HTMLElement _body = h('div.qb-body');
  int _seq = 0;

  String get _tab => store.pref<String>('qbench.arbTab');
  String get _wl => store.pref<String>('qbench.watchlist').isEmpty ? store.watchlist.value : store.pref<String>('qbench.watchlist');

  QArbPage() {
    append(root, [_head, h('section.tr-card.tr-main', [_tabs, _body])]);
  }

  Map<String, Object?> agentContext() => {'tab': _tab, 'watchlist': _wl};

  bool _shown = false;

  void onShow() {
    if (!_shown) {
      _shown = true;
      _render();
    }
  }

  void selectTab(String t) {
    store.setPref('qbench.arbTab', t);
    _render();
  }

  void _render() {
    replace(_head, [
      h('h2', [icon('scale', size: 18), ' Arb & Hedge']),
      h('span.muted.small', 'Market-neutral and risk-based tools: pairs, hedges, allocation'),
    ]);
    replace(_tabs, tabBar([('pairs', 'Pairs / stat arb'), ('hedge', 'Hedge the account'), ('alloc', 'Allocation')], _tab, selectTab));
    switch (_tab) {
      case 'hedge':
        _renderHedge();
      case 'alloc':
        _renderAlloc();
      default:
        _renderPairs();
    }
  }

  List<String> _parseSymbols(String text) =>
      [for (final s in text.split(RegExp(r'[\s,;]+'))) if (s.trim().isNotEmpty) s.trim().toUpperCase()];

  HTMLInputElement _input(String key, String placeholder, {String width = '260px'}) {
    final i = h('input.input.small', null, {'placeholder': placeholder, 'value': store.pref<String>(key), 'aria-label': placeholder})
        as HTMLInputElement;
    i.style.width = width;
    i.onchange = ((Event _) => store.setPref(key, i.value)).toJS;
    return i;
  }

  /// Body for "symbols or the watchlist".
  Map<String, Object?> _universeBody(String text) {
    final syms = _parseSymbols(text);
    return syms.isNotEmpty ? {'symbols': syms} : {'watchlist': _wl};
  }

  // ------------------------------------------------------------------ pairs

  void _renderPairs() {
    final results = h('div.qb-results');
    final detail = h('div.qb-detail');
    final syms = _input('qbench.pairSymbols', 'Symbols (blank = watchlist $_wl)', width: '320px');
    final corr = select([('0.0', 'ρ ≥ 0'), ('0.5', 'ρ ≥ 0.5'), ('0.6', 'ρ ≥ 0.6'), ('0.7', 'ρ ≥ 0.7'), ('0.8', 'ρ ≥ 0.8')],
        store.pref<double>('qbench.minCorr').toStringAsFixed(1), (v) => store.setPref('qbench.minCorr', double.parse(v)),
        label: 'Minimum correlation');
    replace(_body, [
      h('div.tr-tools', [
        syms,
        corr,
        h('button.btn.primary.small', [icon('search', size: 13), 'Scan pairs'], {'type': 'button'}, {
          'click': (_) => _scanPairs(results, detail, syms.value),
        }),
        h('span.spacer'),
        h('span.muted.small', 'Tip: same-industry names, or a stock vs its sector ETF (Avellaneda-Lee residual)'),
      ]),
      results,
      detail,
    ]);
    replace(results, emptyState('Find cointegrated pairs',
        'Engle-Granger test on every correlated pair, hedge ratio, spread half-life and current z-score. Pairs need a short '
        'leg; without shorting, use the z-score to time the cheap leg.', iconName: 'scale'));
  }

  Future<void> _scanPairs(HTMLElement host, HTMLElement detail, String text) async {
    final seq = ++_seq;
    replace(detail, null);
    replace(host, loading('Testing pairs for cointegration…'));
    try {
      final r = await request('/api/qbench/pairs', method: 'POST',
          body: {..._universeBody(text), 'min_corr': store.pref<double>('qbench.minCorr'), 'limit': 40});
      if (seq != _seq) return;
      final pairs = r['pairs'].list;
      replace(host, [
        h('div.muted.small', '${r['pairs_tested'].i} of ${r['pairs_total'].i} pairs tested (${r['days'].i} days) · '
            '${pairs.where((p) => p['tradeable'].b == true).length} tradeable'),
        if (pairs.isEmpty)
          h('div.muted.pad', 'No pair passed the correlation filter. Lower it or add related names.')
        else
          h('div.table-wrap', h('table.data-table.compact', [
            h('thead', h('tr', [h('th', 'Long leg (A)'), h('th', 'Hedge (B)'), h('th.num', 'ρ'), h('th.num', 'Coint. p'),
              h('th.num', 'Hedge β'), h('th.num', 'Half-life'), h('th.num', 'z now'), h('th', 'Status')])),
            h('tbody', [
              for (final p in pairs)
                h('tr', [
                  h('td.sym', symLink(p['a'].s ?? '')),
                  h('td.sym', symLink(p['b'].s ?? '')),
                  h('td.num', fnum(p['correlation'].d)),
                  h('td.num${(p['p_value'].d ?? 1) < 0.05 ? '.up' : ''}', fnum(p['p_value'].d, d: 3)),
                  h('td.num', fnum(p['hedge_ratio'].d)),
                  h('td.num', '${fnum(p['half_life_days'].d, d: 0)} d'),
                  h('td.num.${(p['z_now'].d ?? 0).abs() >= 2 ? 'down' : ''}', fnum(p['z_now'].d)),
                  h('td', p['signal'].s != null
                      ? h('span.badge.info', p['signal'].s!)
                      : h('span.badge.${p['tradeable'].b == true ? 'up' : 'neutral'}', p['tradeable'].b == true ? 'tradeable' : 'weak')),
                ], {}, {'click': (_) => _analyze(detail, p['a'].s!, p['b'].s!)}),
            ]),
          ])),
        for (final n in r['notes'].list) h('div.muted.tiny', n.s ?? ''),
      ]);
    } catch (e) {
      if (seq == _seq) replace(host, errorBlock(e));
    }
  }

  Future<void> _analyze(HTMLElement host, String a, String b) async {
    replace(host, loading('Backtesting $a / $b with a rolling hedge ratio…'));
    try {
      final r = await request('/api/qbench/pair?a=${Uri.encodeComponent(a)}&b=${Uri.encodeComponent(b)}&notional=10000');
      final cur = r['current'], perf = r['performance'], t = r['trades'];
      final zDates = [for (final d in r['zscore_series']['dates'].list) d.s ?? ''];
      final z = [for (final v in r['zscore_series']['z'].list) v.d];
      final entry = r['settings']['entry_z'].d ?? 2;
      final zchart = zDates.length < 2
          ? h('div')
          : PerfChart(
              dates: zDates,
              lines: [
                ChartLine('+entry', 'qb-line-band', [for (final _ in z) entry]),
                ChartLine('-entry', 'qb-line-band', [for (final _ in z) -entry]),
                ChartLine('Spread z-score', 'qb-line-strat', z),
              ],
              axisLabel: (v) => v.toStringAsFixed(1),
              includeZero: true,
              height: 170,
              tip: (i) => [('z', fnum(z[i]), null)],
            ).root;
      replace(host, [
        h('div.qb-card-head', [
          h('h3', '$a / $b'),
          if (cur['signal'].s != null) h('span.badge.info', cur['signal'].s!),
          h('span.spacer'),
          h('span.muted.small', '${r['period']['start'].s} → ${r['period']['end'].s} · rolling ${r['settings']['formation_days'].i}-day formation'),
        ]),
        h('div.qb-sub', 'Spread z-score (entry ±${fnum(entry, d: 1)}, exit ${fnum(r['settings']['exit_z'].d, d: 1)}, '
            'stop ±${fnum(r['settings']['stop_z'].d, d: 1)})'),
        zchart,
        if (!r['equity_curve'].isNull) equityChart(r['equity_curve'], a: 'pair', b: '__none__', aLabel: 'Pair strategy', height: 150),
        h('div.stat-grid.qb-stats4', [
          stat('z now', fnum(cur['z'].d), cls: (cur['z'].d ?? 0).abs() >= entry ? 'down' : ''),
          stat('Hedge β (1st / 2nd half)', '${fnum(cur['hedge_ratio'].d)} (${fnum(cur['hedge_ratio_first_vs_second_half'].list.first.d)} / '
              '${fnum(cur['hedge_ratio_first_vs_second_half'].list.last.d)})'),
          stat('Half-life', '${fnum(cur['half_life_days'].d, d: 0)} d'),
          stat('Coint. p (last 1y)', fnum(cur['cointegration_p_last_year'].d, d: 3)),
          stat('Sharpe', fnum(perf['sharpe'].d)),
          stat('CAGR', fpct(perf['cagr_pct'].d), cls: signCls(perf['cagr_pct'].d)),
          stat('Trades (win %)', '${t['count'].i} (${fpct(t['win_rate_pct'].d, sign: false)})'),
          stat('Stops / time stops', '${t['stops'].i} / ${t['time_stops'].i}'),
        ]),
        if (cur['legs'].list.isNotEmpty)
          h('div.qb-legs', [
            h('span.muted.small', 'Legs for \$10,000 gross:'),
            for (final l in cur['legs'].list)
              h('span.qb-beta', h('b.${(l['action'].s ?? '').startsWith('buy') ? 'up' : 'down'}',
                  '${l['action'].s} ${l['shares'].i} ${l['symbol'].s}')),
          ]),
        for (final w in r['warnings'].list) h('div.qb-warn.small', [icon('info', size: 13), w.s ?? '']),
        if (r['recent_trades'].list.isNotEmpty)
          h('div.table-wrap', h('table.data-table.compact', [
            h('thead', h('tr', [h('th', 'Open'), h('th', 'Close'), h('th', 'Side'), h('th.num', 'z in → out'), h('th', 'Exit'),
              h('th.num', 'Days'), h('th.num', 'Return')])),
            h('tbody', [
              for (final tr in r['recent_trades'].list.reversed)
                h('tr', [
                  h('td', tr['open'].s ?? ''), h('td', tr['close'].s ?? ''), h('td', tr['side'].s ?? ''),
                  h('td.num', '${fnum(tr['z_open'].d)} → ${fnum(tr['z_close'].d)}'), h('td', tr['reason'].s ?? ''),
                  h('td.num', '${tr['days'].i}'), h('td.num.${signCls(tr['return_pct'].d)}', fpct(tr['return_pct'].d, d: 2)),
                ]),
            ]),
          ])),
      ]);
    } catch (e) {
      replace(host, errorBlock(e));
    }
  }

  // ------------------------------------------------------------------ hedge

  void _renderHedge() {
    final results = h('div.qb-results');
    final manual = _input('qbench.hedgePositions', 'Manual: AAPL:10000, MSFT:5000 (blank = broker account)', width: '360px');
    final inst = _input('qbench.hedgeInstruments', 'Hedge ETFs: SPY, QQQ, XLK', width: '200px');
    replace(_body, [
      h('div.tr-tools', [
        manual,
        inst,
        h('button.btn.primary.small', [icon('shield', size: 13), 'Analyse'], {'type': 'button'}, {
          'click': (_) => _loadHedge(results, manual.value, inst.value),
        }),
      ]),
      results,
    ]);
    replace(results, emptyState('How much market risk does the account carry?',
        'Per-position betas, the SPY short that neutralises the dollar beta, and a minimum-variance hedge on your chosen ETFs.',
        iconName: 'shield'));
  }

  Future<void> _loadHedge(HTMLElement host, String manual, String inst) async {
    final seq = ++_seq;
    replace(host, loading('Estimating betas…'));
    Map<String, double>? positions;
    if (manual.trim().isNotEmpty) {
      positions = {};
      for (final part in manual.split(RegExp(r'[,;]+'))) {
        final kv = part.split(':');
        final v = kv.length == 2 ? double.tryParse(kv[1].trim().replaceAll(r'$', '').replaceAll(',', '')) : null;
        if (v != null) positions[kv[0].trim().toUpperCase()] = v;
      }
    }
    final instruments = _parseSymbols(inst);
    try {
      final r = await request('/api/qbench/hedge', method: 'POST', body: {
        if (positions != null) 'positions': positions,
        if (instruments.isNotEmpty) 'instruments': instruments,
      });
      if (seq != _seq) return;
      final bh = r['beta_hedge'], mv = r['min_variance_hedge'];
      replace(host, [
        h('div.stat-grid.qb-stats4', [
          stat('Gross value', fmoney(r['gross_value'].d)),
          stat('Portfolio beta', fnum(r['portfolio_beta'].d)),
          stat('Dollar beta', fmoney(r['dollar_beta'].d)),
          stat('SPY hedge', '${bh['shares'].i} sh (${fmoney(bh['notional'].d)})', cls: 'down'),
          stat('Vol before → after', '${fpct(bh['vol_before_pct'].d, sign: false)} → ${fpct(bh['vol_after_pct'].d, sign: false)}'),
          if (!mv.isNull) stat('Min-var: variance removed', fpct(mv['variance_removed_pct'].d, sign: false)),
        ]),
        h('div.table-wrap', h('table.data-table.compact', [
          h('thead', h('tr', [h('th', 'Position'), h('th.num', 'Value'), h('th.num', 'Weight'), h('th.num', 'Beta 1y'),
            h('th.num', 'Beta 6m'), h('th.num', 'Dollar beta'), h('th.num', 'ρ SPY')])),
          h('tbody', [
            for (final p in r['positions'].list)
              h('tr', [
                h('td.sym', symLink(p['symbol'].s ?? '')),
                h('td.num', fmoney(p['value'].d)),
                h('td.num', fpct(p['weight_pct'].d, sign: false)),
                h('td.num', fnum(p['beta'].d)),
                h('td.num', fnum(p['beta_6m'].d)),
                h('td.num', fmoney(p['dollar_beta'].d)),
                h('td.num', fnum(p['correlation_spy'].d)),
              ]),
          ]),
        ])),
        if (!mv.isNull) ...[
          h('div.qb-sub', 'Minimum-variance hedge (R² ${fnum(mv['r2'].d)}, vol after ${fpct(mv['vol_after_pct'].d, sign: false)})'),
          h('div.qb-legs', [
            for (final i in mv['instruments'].list)
              h('span.qb-beta', [h('b.down', '${i['shares'].i} ${i['instrument'].s}'),
                h('span.muted', 'β ${fnum(i['hedge_ratio'].d)} · t ${fnum(i['t'].d, d: 1)}')]),
          ]),
        ],
        h('div.qb-sub', 'Alternatives'),
        h('ul.qb-reading', [for (final a in bh['alternatives'].list) h('li', a.s ?? '')]),
        for (final n in r['notes'].list) h('div.muted.tiny', n.s ?? ''),
      ]);
    } catch (e) {
      if (seq == _seq) replace(host, errorBlock(e));
    }
  }

  // ------------------------------------------------------------------ allocation

  void _renderAlloc() {
    final results = h('div.qb-results');
    final syms = _input('qbench.allocSymbols', 'Symbols (blank = watchlist $_wl)', width: '300px');
    final capital = _input('qbench.allocCapital', 'Capital \$ (optional)', width: '150px');
    replace(_body, [
      h('div.tr-tools', [
        syms,
        select([('risk_parity', 'Risk parity (ERC)'), ('hrp', 'Hierarchical risk parity'), ('min_variance', 'Minimum variance'),
          ('inverse_vol', 'Inverse volatility'), ('equal', 'Equal weight')], store.pref<String>('qbench.allocMethod'),
            (v) => store.setPref('qbench.allocMethod', v), label: 'Method'),
        select([('0', 'No cap'), ('0.1', 'Max 10%'), ('0.2', 'Max 20%'), ('0.25', 'Max 25%'), ('0.35', 'Max 35%')],
            store.pref<String>('qbench.allocCap'), (v) => store.setPref('qbench.allocCap', v), label: 'Max weight'),
        capital,
        h('button.btn.primary.small', [icon('pie', size: 13), 'Optimise'], {'type': 'button'}, {
          'click': (_) => _loadAlloc(results, syms.value, capital.value),
        }),
      ]),
      results,
    ]);
    replace(results, emptyState('Risk-based weights',
        'Equal risk contribution, HRP or minimum variance on a Ledoit-Wolf shrunk covariance. Expected returns are '
        'deliberately ignored: they are the noisiest input.', iconName: 'pie'));
  }

  Future<void> _loadAlloc(HTMLElement host, String text, String capital) async {
    final seq = ++_seq;
    replace(host, loading('Estimating the covariance…'));
    final cap = double.tryParse(store.pref<String>('qbench.allocCap')) ?? 0;
    final money = double.tryParse(capital.replaceAll(RegExp(r'[\$,\s]'), ''));
    try {
      final r = await request('/api/qbench/allocate', method: 'POST', body: {
        ..._universeBody(text),
        'method': store.pref<String>('qbench.allocMethod'),
        if (cap > 0) 'max_weight': cap,
        if (money != null && money > 0) 'capital': money,
      });
      if (seq != _seq) return;
      final pf = r['portfolio'];
      replace(host, [
        h('div.stat-grid.qb-stats4', [
          stat('Portfolio vol', fpct(pf['vol_pct'].d, sign: false)),
          stat('Diversification ratio', fnum(pf['diversification_ratio'].d)),
          stat('Effective N', fnum(pf['effective_n'].d, d: 1)),
          stat('Avg correlation', fnum(pf['average_correlation'].d)),
          stat('Shrinkage', fnum(r['shrinkage'].d), tip: 'Ledoit-Wolf intensity toward a scaled identity'),
          stat('Days', '${r['days'].i}'),
        ]),
        h('div.table-wrap', h('table.data-table.compact', [
          h('thead', h('tr', [h('th', 'Symbol'), h('th.num', 'Weight'), h('th', ''), h('th.num', 'Vol'), h('th.num', 'Risk contrib.'),
            if (money != null) ...[h('th.num', '\$'), h('th.num', 'Shares')]])),
          h('tbody', [
            for (final w in r['weights'].list)
              h('tr', [
                h('td.sym', symLink(w['symbol'].s ?? '')),
                h('td.num', fpct(w['weight_pct'].d, sign: false)),
                h('td', () {
                  final bar = h('i.qb-wbar');
                  bar.style.width = '${((w['weight_pct'].d ?? 0) * 2).clamp(0, 100)}%';
                  return h('span.qb-wtrack', bar);
                }()),
                h('td.num', fpct(w['vol_pct'].d, sign: false)),
                h('td.num', fpct(w['risk_contribution_pct'].d, sign: false)),
                if (money != null) ...[h('td.num', fmoney(w['dollars'].d)), h('td.num', '${w['shares'].i ?? '—'}')],
              ]),
          ]),
        ])),
        h('div.qb-sub', 'Methods compared (same covariance)'),
        h('div.qb-betas', [
          for (final e in r['compare_methods'].map.entries)
            h('span.qb-beta${e.key == r['method'].s ? '.sig' : ''}', [
              h('span.muted', e.key.replaceAll('_', ' ')),
              h('b', fpct(e.value['vol_pct'].d, sign: false)),
              h('span.muted.tiny', 'DR ${fnum(e.value['diversification_ratio'].d)}'),
            ]),
        ]),
        if (r['dropped'].list.isNotEmpty) h('div.muted.small', 'Dropped (not enough history): ${r['dropped'].list.map((d) => d.s).join(', ')}'),
        for (final n in r['notes'].list) h('div.muted.tiny', n.s ?? ''),
      ]);
    } catch (e) {
      if (seq == _seq) replace(host, errorBlock(e));
    }
  }
}
