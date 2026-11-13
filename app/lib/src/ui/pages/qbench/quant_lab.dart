// QBench Quant Lab: one symbol's quant study. Confluence (TA + FA + quant pillars with quant-derived plans),
// the quant profile (regime tests, volatility, factor exposures, seasonality) and the Strategy Lab: a walk-forward
// test of every library strategy, ranked by out-of-sample timing alpha, with a drill-down per strategy.
import 'dart:async';

import 'package:web/web.dart';

import '../../../core/json.dart';
import '../../api.dart';
import '../../dom.dart';
import '../../kit/icons.dart';
import '../../kit/overlay.dart';
import '../../store.dart';
import 'common.dart';

class QuantLabPage {
  final HTMLElement root = h('div.page.qb-page.qlab-page');
  final HTMLElement _head = h('div.tr-head');
  final HTMLElement _conf = h('section.tr-card.qb-conf');
  final HTMLElement _profile = h('section.tr-card.qb-profile');
  final HTMLElement _lab = h('section.tr-card.qb-lab');
  final HTMLElement _detail = h('div.qb-detail');
  J? _labRes;
  String? _selected;
  bool _running = false;
  int _seq = 0;

  String get _sym {
    final s = store.pref<String>('qbench.symbol');
    return s.isNotEmpty ? s : store.activeChart.symbol.value;
  }

  String get _lookback => store.pref<String>('qbench.labLookback');
  double get _cost => store.pref<double>('qbench.costBps');

  QuantLabPage() {
    append(root, [
      _head,
      h('div.qb-grid', [h('div.qb-col', [_conf, _profile]), h('div.qb-col.wide', [_lab])]),
    ]);
  }

  Map<String, Object?> agentContext() => {'symbol': _sym, 'selected_strategy': _selected, 'lab_run': _labRes != null};

  String? _shown;

  void onShow() {
    if (_shown != _sym) _load();
    _renderHead();
  }

  void showSymbol(String sym) {
    store.setPref('qbench.symbol', sym.toUpperCase());
    _load();
  }

  /// The agent or another tab ran the lab.
  void onLabEvent(String symbol) {
    if (symbol == _sym && !root.isHidden && !_running) _load();
  }

  void _renderHead() {
    replace(_head, [
      h('h2', [icon('flask', size: 18), ' Quant Lab']),
      symbolButton(_sym, showSymbol),
      h('span.spacer'),
      h('span.muted.small', 'History'),
      select([('10y', '10 years'), ('15y', '15 years'), ('max', 'All')], _lookback, (v) => store.setPref('qbench.labLookback', v),
          label: 'Lab history'),
      h('span.muted.small', 'Costs'),
      select([('2.0', '2 bps'), ('5.0', '5 bps'), ('10.0', '10 bps'), ('20.0', '20 bps')], _cost.toStringAsFixed(1),
          (v) => store.setPref('qbench.costBps', double.parse(v)), label: 'Cost per unit turnover'),
      h('button.btn.primary', [_running ? h('span.spinner.tiny') : icon('play', size: 14), _running ? 'Running…' : 'Run lab'], {
        'type': 'button',
        if (_running) 'disabled': '',
        'data-tip': 'Walk-forward test of every single-asset strategy (parameters re-chosen yearly on past data only)',
      }, {'click': (_) => _runLab()}),
      h('button.btn', [icon('candles', size: 14), 'Chart'], {'type': 'button'}, {'click': (_) => goChart(_sym)}),
    ]);
  }

  Future<void> _load() async {
    final sym = _sym;
    _shown = sym;
    final seq = ++_seq;
    _selected = null;
    replace(_conf, loading('Combining TA, FA and quant…'));
    replace(_profile, loading('Profiling $sym…'));
    replace(_lab, loading('Loading the last lab run…'));
    _renderHead();
    final enc = Uri.encodeComponent(sym);
    unawaited(() async {
      try {
        final r = await request('/api/qbench/confluence/$enc');
        if (seq == _seq) _renderConf(r);
      } catch (e) {
        if (seq == _seq) replace(_conf, errorBlock(e, retry: _load));
      }
    }());
    unawaited(() async {
      try {
        final r = await request('/api/qbench/profile/$enc');
        if (seq == _seq) _renderProfile(r);
      } catch (e) {
        if (seq == _seq) replace(_profile, errorBlock(e));
      }
    }());
    try {
      final r = await request('/api/qbench/lab/$enc');
      if (seq != _seq) return;
      _labRes = r['result'].isNull ? null : r['result'];
      _renderLab();
    } catch (e) {
      if (seq == _seq) replace(_lab, errorBlock(e));
    }
  }

  Future<void> _runLab() async {
    if (_running) return;
    _running = true;
    _renderHead();
    final sym = _sym;
    replace(_lab, loading('Walk-forward testing every strategy on $sym…'));
    try {
      final r = await request('/api/qbench/lab/${Uri.encodeComponent(sym)}',
          method: 'POST', body: {'lookback': _lookback, 'cost_bps': _cost});
      if (sym != _sym) return;
      _labRes = r;
      _selected = r['best'].s ?? (r['strategies'].list.isEmpty ? null : r['strategies'].list.first['id'].s);
      _renderLab();
      final c = await attempt(request('/api/qbench/confluence/${Uri.encodeComponent(sym)}'));
      if (c != null && sym == _sym) _renderConf(c);
    } catch (e) {
      replace(_lab, errorBlock(e, retry: _runLab));
    } finally {
      _running = false;
      _renderHead();
    }
  }

  // ------------------------------------------------------------------ confluence

  void _renderConf(J c) {
    final p = c['pillars'];
    final lv = c['levels'];
    final comp = c['composite'].d;
    final plans = lv['plans'];
    final pref = lv['preferred_plan'].s;
    replace(_conf, [
      h('div.qb-card-head', [
        h('h3', 'Confluence'),
        h('span.muted.small', [c['name'].s ?? '', if (c['sector'].s != null) ' · ${c['sector'].s}']),
      ]),
      h('div.qb-composite', [
        h('div.qb-big.${scoreCls(comp)}', comp == null ? '—' : comp.toStringAsFixed(0)),
        h('div', [
          h('b', c['stance'].s ?? 'no data'),
          h('div.muted.small', 'pillars ${c['agreement'].s ?? ''}'),
        ]),
      ]),
      scoreBar('Technical', p['technical']['score'].d, tip: '60% trend + 40% momentum score (get_technical_summary)'),
      scoreBar('Fundamental', p['fundamental']['score'].d, tip: 'Mean of value, quality and growth percentile ranks'),
      scoreBar('Quant', p['quant']['score'].d, tip: 'Momentum rank, time-series momentum, regime and the lab signal'),
      h('ul.qb-reading', [for (final l in c['reading'].list) h('li', l.s ?? '')]),
      if (!lv['plans'].isNull) ...[
        h('div.qb-sub', 'Quant plans${pref != null ? ' · preferred: ${pref.replaceAll('_', ' ')}' : ''}'),
        h('table.data-table.compact', [
          h('thead', h('tr', [h('th', 'Plan'), h('th.num', 'Entry'), h('th.num', 'Stop'), h('th.num', 'Target')])),
          h('tbody', [
            for (final e in plans.map.entries)
              h('tr${e.key == pref ? '.qb-pref' : ''}', [
                h('td', e.key.replaceAll('_', ' ')),
                h('td.num', e.value['entry_zone'].isNull
                    ? fnum(e.value['entry'].d ?? e.value['trigger'].d)
                    : '${fnum(e.value['entry_zone'].list.first.d)}–${fnum(e.value['entry_zone'].list.last.d)}'),
                h('td.num.down', fnum(e.value['stop'].d)),
                h('td.num.up', fnum(e.value['target'].d)),
              ], {'data-tip': e.value['note'].s ?? ''}),
          ]),
        ]),
        h('div.stat-grid', [
          stat('Price', fnum(lv['price'].d)),
          stat('ATR(14)', fnum(lv['atr14'].d)),
          stat('Vol (63d)', fpct(lv['sizing']['realized_vol_pct'].d, sign: false)),
          stat('Vol-target weight', fpct(lv['sizing']['vol_target_weight_pct'].d, sign: false),
              tip: lv['sizing']['note'].s),
          if (!lv['sizing']['half_kelly_capped_pct'].isNull)
            stat('½ Kelly (cap 25%)', fpct(lv['sizing']['half_kelly_capped_pct'].d, sign: false), tip: lv['sizing']['kelly_note'].s),
        ]),
      ] else if (!lv['error'].isNull)
        h('div.muted.small', lv['error'].s ?? ''),
    ]);
  }

  // ------------------------------------------------------------------ profile

  void _renderProfile(J p) {
    final rt = p['regime_tests'];
    final reg = p['regime'];
    final vol = p['volatility'];
    final ret = p['returns'];
    final fe = p['factor_exposures'];
    final label = reg['label'].s ?? '';
    final season = p['seasonality'];
    replace(_profile, [
      h('div.qb-card-head', [h('h3', 'Quant profile'), h('span.muted.small', 'as of ${p['as_of'].s ?? ''}')]),
      h('div.qb-regime', [
        h('span.badge.${label == 'trending' ? 'up' : label == 'mean-reverting' ? 'info' : 'neutral'}', label),
        h('span.small', 'favours ${reg['favoured_families'].list.map((f) => f.s!.replaceAll('_', ' ')).join(', ')}'),
      ]),
      h('div.muted.small', reg['evidence'].list.map((e) => e.s).join(' · ')),
      h('div.stat-grid', [
        stat('Hurst', fnum(rt['hurst'].d, d: 2), tip: '≈0.5 random walk; > 0.58 trending; < 0.42 mean-reverting (sd ≈ 0.04 on 2y)'),
        for (final v in rt['variance_ratios'].list)
          stat('VR(${v['q'].i})', '${fnum(v['vr'].d)} (z ${fnum(v['z'].d, d: 1)})',
              cls: (v['z'].d ?? 0).abs() > 1.96 ? ((v['z'].d ?? 0) > 0 ? 'up' : 'down') : '',
              tip: 'Lo-MacKinlay variance ratio; |z| > 1.96 is significant'),
        stat('ADF p (log price)', fnum(rt['adf_log_price']['p_value'].d, d: 3)),
        stat('Half-life vs SMA50', '${fnum(rt['half_life_vs_sma50_bars'].d, d: 0)} d'),
        stat('Autocorr 1d / 1w', '${fnum(rt['autocorr_1d'].d)} / ${fnum(rt['autocorr_1w'].d)}'),
        stat('Vol 21d (pctile 3y)', '${fpct(vol['realized_21d_pct'].d, sign: false)} (${fnum(vol['percentile_3y'].d, d: 0)})'),
        stat('1σ move 1d / 1w / 1m',
            [for (final k in ['1d', '1w', '1m']) fnum(vol['expected_move_1sd_pct'][k].d, d: 1)].join(' / ') + '%'),
        stat('Sharpe / Sortino', '${fnum(ret['sharpe'].d)} / ${fnum(ret['sortino'].d)}'),
        stat('Max DD', fpct(ret['max_drawdown_pct'].d), cls: 'down'),
        stat('Skew / kurtosis', '${fnum(ret['skew'].d)} / ${fnum(ret['kurtosis'].d, d: 1)}'),
        stat('CVaR 95% (1d)', fpct(ret['cvar_95_daily_pct'].d, d: 2), cls: 'down'),
        stat('Overnight / intraday (ann.)',
            '${fpct(p['overnight_vs_intraday']['overnight_annual_pct'].d)} / ${fpct(p['overnight_vs_intraday']['intraday_annual_pct'].d)}'),
      ]),
      if (fe.map.isNotEmpty && fe['error'].isNull) ...[
        h('div.qb-sub', 'Factor exposures (ETF proxies, R² ${fnum(fe['r2'].d)})'),
        h('div.qb-betas', [
          for (final e in fe['betas'].map.entries)
            h('span.qb-beta${(e.value['t'].d ?? 0).abs() >= 2 ? '.sig' : ''}', [
              h('span.muted', e.key.replaceAll('_', ' ')),
              h('b.${signCls(e.value['beta'].d)}', fnum(e.value['beta'].d)),
            ], {'data-tip': 't = ${fnum(e.value['t'].d, d: 1)} · proxy ${fe['proxies'][e.key].s ?? ''}'}),
        ]),
        h('div.muted.small', 'Alpha ${fpct(fe['alpha_annual_pct'].d)}/yr (t ${fnum(fe['alpha_t'].d, d: 1)}) · '
            'idiosyncratic vol ${fpct(fe['idiosyncratic_vol_pct'].d, sign: false)}'),
      ],
      h('div.qb-sub', 'Seasonality'),
      h('div.qb-betas', [
        for (final e in season['day_of_week_mean_pct'].map.entries)
          h('span.qb-beta', [h('span.muted', e.key), h('b.${signCls(e.value.d)}', fnum(e.value.d, d: 2))]),
        h('span.qb-beta', [h('span.muted', 'Turn of month'), h('b', 't ${fnum(season['turn_of_month']['t_stat'].d, d: 1)}')],
            {'data-tip': 'Mean daily return in the TOM window vs other days (t-stat); |t| < 2 is noise'}),
      ]),
    ]);
  }

  // ------------------------------------------------------------------ lab

  void _renderLab() {
    final r = _labRes;
    if (r == null) {
      replace(_lab, [
        h('div.qb-card-head', [h('h3', 'Strategy Lab')]),
        emptyState('No lab run for $_sym yet',
            'Run the lab to walk-forward test the strategy library on this stock. It takes a few seconds.',
            iconName: 'flask'),
      ]);
      return;
    }
    final rows = r['strategies'].list;
    _selected ??= r['best'].s ?? (rows.isEmpty ? null : rows.first['id'].s);
    final bh = r['buy_and_hold'];
    replace(_lab, [
      h('div.qb-card-head', [
        h('h3', 'Strategy Lab'),
        h('span.muted.small',
            '${r['trials'].i} combinations · ${r['history']['start'].s} → ${r['history']['end'].s} · costs ${fnum(r['settings']['cost_bps'].d, d: 0)} bps · '
            'buy & hold Sharpe ${fnum(bh['sharpe'].d)} · run ${_ago(r['saved_at'].i)}'),
      ]),
      h('div.table-wrap', h('table.data-table.compact.qb-lab-table', [
        h('thead', h('tr', [
          h('th', 'Strategy'),
          h('th', 'Verdict'),
          h('th.num', h('span', 'OOS Sharpe', {'data-tip': 'Out-of-sample (walk-forward) Sharpe; buy & hold over the same window in grey'})),
          h('th.num', h('span', 'Alpha/yr', {'data-tip': 'Timing alpha vs holding the stock (regression on buy & hold)'})),
          h('th.num', h('span', 'α t', {'data-tip': 't-stat of the alpha'})),
          h('th.num', h('span', 'α DSR', {'data-tip': 'Deflated: probability the alpha beats the best of all combinations by luck'})),
          h('th.num', 'Max DD'),
          h('th.num', h('span', 'Entries', {'data-tip': 'Number of entries out-of-sample'})),
          h('th.num', h('span', 'Stability', {'data-tip': 'Share of walk-forward years that picked the same parameters'})),
          h('th', 'Now'),
        ])),
        h('tbody', [
          for (final s in rows)
            () {
              final o = s['oos'];
              final on = (s['current']['position'].d ?? 0) > 0;
              return h('tr${s['id'].s == _selected ? '.on' : ''}', [
                h('td', [h('b', s['name'].s ?? ''), h('div.muted.tiny', '${s['family'].s?.replaceAll('_', ' ')} · ${s['evidence'].s}')]),
                h('td', verdictBadge(s['verdict'].s)),
                h('td.num', [fnum(o['sharpe'].d), h('span.muted.tiny', ' / ${fnum(o['buy_hold_sharpe'].d)}')]),
                h('td.num.${signCls(o['alpha_annual_pct'].d)}', fpct(o['alpha_annual_pct'].d)),
                h('td.num', fnum(o['alpha_t'].d, d: 1)),
                h('td.num', fnum(o['alpha_dsr'].d)),
                h('td.num.down', fpct(o['max_drawdown_pct'].d, sign: false)),
                h('td.num', '${o['entries'].i ?? '—'}'),
                h('td.num', fnum(o['param_stability'].d)),
                h('td', h('span.badge.${on ? 'up' : 'neutral'}', on ? 'LONG ${fnum((s['current']['position'].d ?? 0) * 100, d: 0)}%' : 'flat',
                    {'data-tip': 'Since ${s['current']['since'].s} with ${paramsText(s['current']['params'])}'})),
              ], {}, {
                'click': (_) {
                  _selected = s['id'].s;
                  _renderLab();
                },
              });
            }(),
        ]),
      ])),
      h('div.muted.tiny', r['notes'].list.map((n) => n.s).join(' ')),
      _detail,
    ]);
    _renderDetail();
  }

  String _ago(int? ts) {
    if (ts == null) return '';
    final d = DateTime.now().difference(DateTime.fromMillisecondsSinceEpoch(ts * 1000));
    if (d.inMinutes < 1) return 'just now';
    if (d.inHours < 1) return '${d.inMinutes} min ago';
    if (d.inDays < 1) return '${d.inHours} h ago';
    return '${d.inDays} d ago';
  }

  void _renderDetail() {
    final s = _labRes?['strategies'].list.where((x) => x['id'].s == _selected).firstOrNull;
    if (s == null) {
      replace(_detail, null);
      return;
    }
    final o = s['oos'];
    final wf = s['walk_forward'];
    replace(_detail, [
      h('div.qb-card-head', [
        h('h3', s['name'].s ?? ''),
        verdictBadge(s['verdict'].s),
        h('span.spacer'),
        h('button.btn.small', [icon('info', size: 13), 'Strategy card'], {'type': 'button'}, {
          'click': (_) {
            store.setPref('qbench.libFocus', s['id'].s ?? '');
            store.page.value = 'qlib';
          },
        }),
        h('button.btn.small', [icon('chart', size: 13), 'Full backtest'], {'type': 'button',
            'data-tip': 'Fixed current parameters over the whole history: yearly returns and trades'},
            {'click': (_) => _backtest(s)}),
      ]),
      if (!s['equity_curve'].isNull) equityChart(s['equity_curve'], aLabel: '${s['name'].s} (OOS)'),
      h('div.stat-grid.qb-stats4', [
        stat('OOS CAGR', fpct(o['cagr_pct'].d), cls: signCls(o['cagr_pct'].d)),
        stat('B&H CAGR', fpct(o['buy_hold_cagr_pct'].d)),
        stat('Max DD (B&H)', '${fpct(o['max_drawdown_pct'].d, sign: false)} (${fpct(o['buy_hold_max_drawdown_pct'].d, sign: false)})'),
        stat('Exposure', fpct(o['exposure_pct'].d, sign: false)),
        stat('Sharpe DSR', fnum(o['dsr'].d), tip: 'Raw Sharpe deflated for luck (inflated by the stock’s own drift for long-only rules)'),
        stat('PSR', fnum(o['psr'].d)),
        stat('Beta to stock', fnum(o['beta_to_stock'].d)),
        stat('Current params', paramsText(s['current']['params'])),
      ]),
      if (wf.list.isNotEmpty) ...[
        h('div.qb-sub', 'Walk-forward windows (parameters chosen on prior data only)'),
        h('div.table-wrap', h('table.data-table.compact', [
          h('thead', h('tr', [h('th', 'Test window'), h('th', 'Parameters'), h('th.num', 'Train Sharpe'), h('th.num', 'Test Sharpe'),
            h('th.num', 'Test return')])),
          h('tbody', [
            for (final f in wf.list)
              h('tr', [
                h('td', '${f['test_start'].s} → ${f['test_end'].s}'),
                h('td.muted', paramsText(f['params'])),
                h('td.num', fnum(f['train_sharpe'].d)),
                h('td.num.${signCls(f['test_sharpe'].d)}', fnum(f['test_sharpe'].d)),
                h('td.num.${signCls(f['test_return_pct'].d)}', fpct(f['test_return_pct'].d)),
              ]),
          ]),
        ])),
      ],
      h('div.qb-bt'),
    ]);
  }

  Future<void> _backtest(J s) async {
    final host = _detail.querySelector('.qb-bt') as HTMLElement?;
    if (host == null) return;
    replace(host, loading('Backtesting…'));
    try {
      final params = <String, Object?>{for (final e in s['current']['params'].map.entries) e.key: e.value.v};
      final r = await request('/api/qbench/strategy-run', method: 'POST',
          body: {'symbol': _sym, 'strategy': s['id'].s, 'params': params, 'lookback': _lookback, 'cost_bps': _cost});
      final p = r['performance'], t = r['trades'];
      replace(host, [
        h('div.qb-sub', 'Full-history backtest · ${r['period']['start'].s} → ${r['period']['end'].s}'),
        h('div.stat-grid.qb-stats4', [
          stat('Sharpe (B&H)', '${fnum(p['sharpe'].d)} (${fnum(r['buy_and_hold']['sharpe'].d)})'),
          stat('CAGR', fpct(p['cagr_pct'].d), cls: signCls(p['cagr_pct'].d)),
          stat('Round trips', '${t['round_trips'].i ?? 0}'),
          stat('Win rate', fpct(t['win_rate_pct'].d, sign: false)),
          stat('Avg trade', fpct(t['avg_trade_pct'].d, d: 2)),
          stat('Trade t-stat', fnum(t['t_stat'].d, d: 1)),
          stat('IS / OOS Sharpe', '${fnum(r['in_sample']['sharpe'].d)} / ${fnum(r['out_of_sample']['sharpe'].d)}'),
          stat('Max DD', fpct(p['max_drawdown_pct'].d, sign: false), cls: 'down'),
        ]),
        for (final w in r['warnings'].list) h('div.qb-warn.small', [icon('info', size: 13), w.s ?? '']),
        h('div.qb-years', [
          for (final y in r['yearly'].list)
            h('div.qb-year', [
              h('span.muted.tiny', '${y['year'].i}'),
              h('b.${signCls(y['strategy_pct'].d)}', fpct(y['strategy_pct'].d, d: 0)),
              h('span.muted.tiny', fpct(y['buy_hold_pct'].d, d: 0)),
            ], {'data-tip': 'Strategy vs buy & hold in ${y['year'].i}'}),
        ]),
        if (!r['expression'].isNull)
          h('div.muted.small', [
            'Autobench expression: entry ', h('code', r['expression']['entry'].s ?? ''), ' · exit ', h('code', r['expression']['exit'].s ?? ''),
            ' (run_backtest fills at the next open)',
          ]),
      ]);
    } catch (e) {
      replace(host, errorBlock(e));
      toast('$e', kind: 'error');
    }
  }
}
