// QBench Q-Signals: which names to focus on. Watchlist ranked by the TA + FA + quant confluence; a weighted
// multi-factor screen over the US universe (or the watchlist); and the factor-ranking backtest of a watchlist
// against its own equal-weighted average.
import 'dart:js_interop';

import 'package:web/web.dart';

import '../../../core/format.dart';
import '../../api.dart';
import '../../dom.dart';
import '../../kit/icons.dart';
import '../../store.dart';
import 'common.dart';

const _factorKeys = ['value', 'quality', 'growth', 'momentum', 'low_vol'];
const _defaultWeights = {'value': 0.2, 'quality': 0.25, 'growth': 0.1, 'momentum': 0.3, 'low_vol': 0.15};

class QSignalsPage {
  final HTMLElement root = h('div.page.qb-page.qsignals-page');
  final HTMLElement _head = h('div.tr-head');
  final HTMLElement _tabs = h('div');
  final HTMLElement _body = h('div.qb-body');
  int _seq = 0;

  String get _tab => store.pref<String>('qbench.signalsTab');
  String get _wl => store.pref<String>('qbench.watchlist').isEmpty ? store.watchlist.value : store.pref<String>('qbench.watchlist');

  Map<String, double> get _weights {
    final w = store.pref<Map<String, Object?>>('qbench.weights');
    return {for (final k in _factorKeys) k: (w[k] as num?)?.toDouble() ?? _defaultWeights[k]!};
  }

  QSignalsPage() {
    append(root, [_head, h('section.tr-card.tr-main', [_tabs, _body])]);
  }

  Map<String, Object?> agentContext() => {'tab': _tab, 'watchlist': _wl, if (_tab == 'factors') 'weights': _weights};

  bool _shown = false;

  void onShow() {
    _renderHead();
    if (!_shown) {
      _shown = true;
      _render();
    }
  }

  void selectTab(String t) {
    store.setPref('qbench.signalsTab', t);
    _render();
  }

  Future<void> _renderHead() async {
    final sel = await watchlistSelect(_wl, (v) {
      store.setPref('qbench.watchlist', v);
      _render();
    });
    replace(_head, [
      h('h2', [icon('sigma', size: 18), ' Q-Signals']),
      h('span.muted.small', 'Watchlist'),
      sel,
      h('span.spacer'),
      h('span.muted.small', 'Ranks are percentiles (0-100, 50 = neutral)'),
    ]);
  }

  void _render() {
    replace(_tabs, tabBar([('rank', 'Watchlist confluence'), ('factors', 'Factor screen'), ('xs', 'Factor backtest')], _tab,
        selectTab));
    switch (_tab) {
      case 'factors':
        _renderFactors();
      case 'xs':
        _renderXs();
      default:
        _loadRank();
    }
  }

  // ------------------------------------------------------------------ watchlist confluence

  Future<void> _loadRank() async {
    final seq = ++_seq;
    replace(_body, loading('Scoring ${_wl}: technicals, factor ranks and quant…'));
    try {
      final r = await request('/api/qbench/rank', method: 'POST', body: {'watchlist': _wl});
      if (seq != _seq) return;
      final rows = r['rows'].list;
      if (rows.isEmpty) {
        replace(_body, emptyState('Watchlist "$_wl" is empty',
            'Add symbols in the chart\'s Watchlist panel (or pick another list above) to rank them by TA + FA + quant.',
            iconName: 'list'));
        return;
      }
      replace(_body, [
        h('div.tr-tools', [
          h('span.muted.small', '${r['count'].i} names · weights TA ${r['weights']['technical'].d} / FA ${r['weights']['fundamental'].d} / '
              'quant ${r['weights']['quant'].d} · ${r['note'].s}'),
          h('span.spacer'),
          h('button.btn.small', [icon('refresh', size: 13), 'Refresh'], {'type': 'button'}, {'click': (_) => _loadRank()}),
        ]),
        h('div.table-wrap', h('table.data-table', [
          h('thead', h('tr', [
            h('th', 'Symbol'), h('th.num', 'Composite'), h('th', 'Stance'), h('th.num', 'TA'), h('th.num', 'FA'),
            h('th.num', 'Quant'), h('th.num', h('span', 'Q-Score', {'data-tip': 'Universe factor composite'})),
            h('th.num', 'Mom rank'), h('th', 'Regime'), h('th', 'Plan'), h('th.num', 'Vol'), h('th', 'Lab signal'),
          ])),
          h('tbody', [
            for (final row in rows)
              if (!row['error'].isNull)
                h('tr', [h('td.sym', symLink(row['symbol'].s ?? '')), h('td.muted', row['error'].s ?? '', {'colspan': '11'})])
              else
                h('tr', [
                  h('td.sym', [symLink(row['symbol'].s ?? ''), h('div.muted.tiny', row['sector'].s ?? '')]),
                  h('td.num', h('b.${scoreCls(row['composite'].d)}', fnum(row['composite'].d, d: 0))),
                  h('td', row['stance'].s ?? '—'),
                  h('td.num.${scoreCls(row['technical'].d)}', fnum(row['technical'].d, d: 0)),
                  h('td.num.${scoreCls(row['fundamental'].d)}', fnum(row['fundamental'].d, d: 0)),
                  h('td.num.${scoreCls(row['quant'].d)}', fnum(row['quant'].d, d: 0)),
                  h('td.num', fnum(row['q_score'].d, d: 0)),
                  h('td.num', fnum(row['momentum_rank'].d, d: 0)),
                  h('td', row['regime'].s ?? '—'),
                  h('td', (row['preferred_plan'].s ?? '—').replaceAll('_', ' ')),
                  h('td.num', fpct(row['vol_pct'].d, d: 0, sign: false)),
                  h('td.small', row['lab_signal'].s ?? h('span.muted', 'run lab')),
                ], {}, {'click': (_) => openQuantLab(row['symbol'].s ?? '')}),
          ]),
        ])),
      ]);
    } catch (e) {
      if (seq == _seq) replace(_body, errorBlock(e, retry: _loadRank));
    }
  }

  // ------------------------------------------------------------------ factor screen

  String get _universe => store.pref<String>('qbench.universe');

  void _renderFactors() {
    final w = _weights;
    final results = h('div.qb-results');
    HTMLElement slider(String k) {
      final input = h('input.qb-range', null, {'type': 'range', 'min': '0', 'max': '1', 'step': '0.05', 'value': '${w[k]}',
        'aria-label': '$k weight'}) as HTMLInputElement;
      final val = h('b', w[k]!.toStringAsFixed(2));
      input.addEventListener('input', ((Event _) => val.textContent = double.parse(input.value).toStringAsFixed(2)).toJS);
      input.addEventListener('change', ((Event _) {
        store.setPref('qbench.weights', {..._weights, k: double.parse(input.value)});
        _loadScreen(results);
      }).toJS);
      return h('label.qb-slider', [h('span', k.replaceAll('_', ' ')), input, val]);
    }

    replace(_body, [
      h('div.tr-tools.qb-weights', [
        for (final k in _factorKeys) slider(k),
        select([('all', 'All US stocks'), ('sp500', 'S&P 500'), ('watchlist:$_wl', 'Watchlist: $_wl')], _universe, (v) {
          store.setPref('qbench.universe', v);
          _loadScreen(results);
        }, label: 'Universe'),
        h('label.qb-check', [
          h('input', null, {'type': 'checkbox', if (store.pref<bool>('qbench.sectorNeutral')) 'checked': ''}, {
            'change': (e) {
              store.setPref('qbench.sectorNeutral', (e.target as HTMLInputElement).checked);
              _loadScreen(results);
            },
          }),
          h('span', 'Sector-neutral', {'data-tip': 'Rank each stock within its sector (no accidental sector bets)'}),
        ]),
        h('button.btn.small', 'Reset weights', {'type': 'button'}, {
          'click': (_) {
            store.setPref('qbench.weights', <String, Object?>{});
            _renderFactors();
          },
        }),
      ]),
      results,
    ]);
    _loadScreen(results);
  }

  Future<void> _loadScreen(HTMLElement host) async {
    final seq = ++_seq;
    replace(host, loading('Ranking the universe…'));
    try {
      final r = await request('/api/qbench/factor-screen', method: 'POST', body: {
        'weights': _weights,
        'universe': _universe,
        'sector_neutral': store.pref<bool>('qbench.sectorNeutral'),
        'limit': 60,
      });
      if (seq != _seq) return;
      final cols = [('score', 'Q-Score'), ('value', 'Value'), ('quality', 'Quality'), ('growth', 'Growth'),
        ('momentum', 'Momentum'), ('low_vol', 'Low vol')];
      replace(host, [
        h('div.muted.small', '${r['total'].i} eligible names · ${r['note'].s}'),
        h('div.table-wrap', h('table.data-table', [
          h('thead', h('tr', [
            h('th', 'Symbol'), h('th', 'Sector'), h('th.num', 'Mkt cap'),
            for (final (_, l) in cols) h('th.num', l),
            h('th.num', 'P/E'), h('th.num', 'ROE'), h('th.num', 'Mom 12-1'), h('th.num', 'Vol 1y'),
          ])),
          h('tbody', [
            for (final row in r['rows'].list)
              h('tr', [
                h('td.sym', [symLink(row['symbol'].s ?? ''), h('div.muted.tiny', row['name'].s ?? '')]),
                h('td.small', row['sector'].s ?? ''),
                h('td.num', row['market_cap'].d == null ? '—' : compact(row['market_cap'].d!)),
                for (final (k, _) in cols) h('td.num.${scoreCls(row[k].d)}', fnum(row[k].d, d: 0)),
                h('td.num', fnum(row['pe'].d, d: 1)),
                h('td.num', fpct(row['roe'].d, d: 0, sign: false)),
                h('td.num.${signCls(row['mom_12_1'].d)}', fpct(row['mom_12_1'].d, d: 0)),
                h('td.num', fpct(row['volatility_1y'].d, d: 0, sign: false)),
              ], {}, {'click': (_) => openQuantLab(row['symbol'].s ?? '')}),
          ]),
        ])),
      ]);
    } catch (e) {
      if (seq == _seq) replace(host, errorBlock(e));
    }
  }

  // ------------------------------------------------------------------ factor backtest

  void _renderXs() {
    final results = h('div.qb-results');
    final f = store.pref<String>('qbench.xsFactor');
    final k = store.pref<double>('qbench.xsTopK').round();
    replace(_body, [
      h('div.tr-tools', [
        h('span.muted.small', 'Each month hold the top'),
        select([for (final n in [2, 3, 5, 8, 10]) ('$n', '$n')], '$k', (v) => store.setPref('qbench.xsTopK', double.parse(v)),
            label: 'Top k'),
        h('span.muted.small', 'of $_wl by'),
        select([
          ('momentum_12_1', '12-1 momentum'), ('residual_momentum', 'Residual momentum'), ('reversal_1m', '1-month reversal'),
          ('low_vol', 'Low volatility'), ('high52', '52-week-high proximity'), ('trend_strength', 'Price / 200-day'),
        ], f, (v) => store.setPref('qbench.xsFactor', v), label: 'Factor'),
        h('button.btn.primary.small', [icon('play', size: 13), 'Backtest'], {'type': 'button'}, {'click': (_) => _loadXs(results)}),
      ]),
      results,
    ]);
    replace(results, emptyState('Rank your watchlist by a factor',
        'Compares the monthly top-k with the equal-weighted watchlist (the honest benchmark: your list is all survivors). '
        'Needs at least top-k + 2 names; 15+ for meaningful results.'));
  }

  Future<void> _loadXs(HTMLElement host) async {
    final seq = ++_seq;
    replace(host, loading('Backtesting monthly rankings…'));
    try {
      final r = await request('/api/qbench/factor-backtest', method: 'POST', body: {
        'watchlist': _wl,
        'factor': store.pref<String>('qbench.xsFactor'),
        'top_k': store.pref<double>('qbench.xsTopK').round(),
      });
      if (seq != _seq) return;
      final top = r['top_k_portfolio'], ew = r['equal_weight_list'], ex = r['excess_vs_equal_weight'];
      replace(host, [
        h('div.muted.small', '${r['description'].s} · ${r['period']['start'].s} → ${r['period']['end'].s} · '
            '${r['rebalances'].i} rebalances · costs ${fnum(r['cost_bps'].d, d: 0)} bps'),
        equityChart(r['equity_curve'], a: 'top_k', b: 'equal_weight', aLabel: 'Top ${r['top_k'].i}', bLabel: 'Equal-weighted list'),
        h('div.stat-grid.qb-stats4', [
          stat('Top-k Sharpe (list)', '${fnum(top['sharpe'].d)} (${fnum(ew['sharpe'].d)})'),
          stat('Top-k CAGR (list)', '${fpct(top['cagr_pct'].d)} (${fpct(ew['cagr_pct'].d)})'),
          stat('Excess / yr', fpct(ex['annual_pct'].d), cls: signCls(ex['annual_pct'].d)),
          stat('Information ratio', fnum(ex['information_ratio'].d)),
          stat('PSR of excess', fnum(ex['psr'].d), tip: 'Probability the excess Sharpe is above zero'),
          stat('Monthly hit rate', fpct(ex['monthly_hit_rate_pct'].d, sign: false)),
          stat('Bottom-k Sharpe', fnum(r['bottom_k_portfolio']['sharpe'].d)),
          stat('Max DD top-k', fpct(top['max_drawdown_pct'].d, sign: false), cls: 'down'),
        ]),
        h('div.qb-sub', 'Current ranking'),
        h('div.qb-betas', [
          for (final c in r['current_ranking'].list)
            h('span.qb-beta${c['selected'].b == true ? '.sig' : ''}', [symLink(c['symbol'].s ?? ''), h('b', fnum(c['signal'].d, d: 3))]),
        ]),
        for (final n in r['notes'].list) h('div.muted.tiny', n.s ?? ''),
      ]);
    } catch (e) {
      if (seq == _seq) replace(host, errorBlock(e));
    }
  }
}
