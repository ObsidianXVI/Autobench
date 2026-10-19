// Journal page: the account's trades (outcome, excursions, what price did after the exit and what other
// exits would have made), recent chart drawings, aggregate performance, and notes per symbol and trade.
// Data comes from /api/journal/* (backend services/journal.py); notes are ordinary notes, a trade's
// notes carry the tag "trade:<trade id>".
import 'dart:async';
import 'dart:js_interop';

import 'package:web/web.dart';

import '../../core/format.dart';
import '../../core/json.dart';
import '../../core/time.dart';
import '../../core/trading.dart';
import '../api.dart';
import '../dom.dart';
import '../kit/icons.dart';
import '../kit/overlay.dart';
import '../kit/state.dart';
import '../store.dart';
import '../trading_state.dart';

const _tabs = [
  ('trades', 'Trades'),
  ('drawings', 'Drawings'),
  ('performance', 'Performance'),
  ('notes', 'Notes'),
];

class JournalPage {
  final HTMLElement root = h('div.page.journal-page');
  final HTMLElement _head = h('div.tr-head');
  final HTMLElement _tabBar = h('div.tr-tabs');
  final HTMLElement _body = h('div.jr-body');
  final Scope _scope = Scope();
  final _reload = Debouncer(400);
  String _status = 'all';
  String _symbol = '';
  final Set<String> _open = {};
  final Map<String, J> _details = {};
  int _seq = 0;

  String get _tab => store.pref<String>('journal.tab');

  JournalPage() {
    append(root, [_head, h('section.tr-card.tr-main', [_tabBar, _body])]);
    _scope.listen(trade.version, (_) {
      if (!root.isHidden && _tab != 'notes') _reload(_render);
    });
    _scope.listen(store.notesVersion, (_) {
      if (!root.isHidden && (_tab == 'notes' || _tab == 'trades')) _reload(_render);
    });
    _scope.listen(trade.status, (_) => _renderHead());
  }

  void onShow() {
    trade.start();
    _renderHead();
    _renderTabs();
    _render();
  }

  /// Opens a tab (e.g. from the agent via show_in_ui).
  void selectTab(String k) {
    if (!_tabs.any((t) => t.$1 == k)) return;
    store.setPref('journal.tab', k);
    _renderTabs();
    _render();
  }

  void _goChart(String sym) {
    store.activeChart.symbol.value = sym;
    store.page.value = 'chart';
  }

  void _renderHead() {
    final st = trade.status.value;
    replace(_head, [
      h('h2', 'Journal'),
      if (st != null)
        h('span.muted.small', st.connected ? '${st.brokerLabel} · ${st.account ?? ''}${st.isLive ? ' · LIVE' : ' · paper'}' : st.brokerLabel),
      h('span.spacer'),
      h('button.btn.small', [icon('note', size: 14), 'New note'], {'type': 'button'}, {'click': (_) => _editNote()}),
    ]);
  }

  void _renderTabs() {
    replace(_tabBar, [
      for (final (k, l) in _tabs)
        h('button.tab${_tab == k ? '.on' : ''}', l, {'type': 'button', 'aria-label': l, 'data-tab': k}, {'click': (_) => selectTab(k)}),
    ]);
  }

  Future<void> _render() async {
    final seq = ++_seq;
    final tab = _tab;
    if (_body.childNodes.length == 0) replace(_body, h('div.muted.small.pad', 'Loading…'));
    final Object? content;
    try {
      content = switch (tab) {
        'drawings' => await _drawings(),
        'performance' => await _performance(),
        'notes' => await _notes(),
        _ => await _trades(),
      };
    } catch (e) {
      if (seq == _seq) replace(_body, h('div.check-msg.error', '$e'));
      return;
    }
    if (seq != _seq) return;
    replace(_body, content);
  }

  // ---------------------------------------------------------------- trades

  Future<Object?> _trades() async {
    final q = [if (_status != 'all') 'status=$_status', if (_symbol.isNotEmpty) 'symbol=${Uri.encodeComponent(_symbol)}'];
    final j = await request('/api/journal/trades${q.isEmpty ? '' : '?${q.join('&')}'}');
    final trades = j['trades'].list;
    final symInput = h('input.input.small', null, {'placeholder': 'Symbol', 'value': _symbol, 'aria-label': 'Filter by symbol'})
        as HTMLInputElement;
    symInput.addEventListener('change', ((Event _) {
      _symbol = symInput.value.trim().toUpperCase();
      _render();
    }).toJS);
    return [
      h('div.tr-tools', [
        h('div.seg', [
          for (final (k, l) in [('all', 'All'), ('closed', 'Closed'), ('open', 'Open')])
            h('button.seg-btn${_status == k ? '.on' : ''}', l, {'type': 'button'}, {
              'click': (_) {
                _status = k;
                _render();
              },
            }),
        ]),
        symInput,
        h('span.muted.small', '${trades.length} trade${trades.length == 1 ? '' : 's'} from the broker\'s executions · '
            'click a trade for the analysis'),
      ]),
      if (trades.isEmpty)
        h('div.empty-state', [
          icon('wallet', size: 28),
          h('div', 'No trades yet'),
          h('div.muted.small', 'Trades appear here once orders fill on the selected account (IBKR or the simulator).'),
        ])
      else
        h('div.table-wrap', h('table.data-table.compact.jr-trades', [
          h('thead', h('tr', [
            for (final (c, num) in [
              ('Opened', false), ('Symbol', false), ('Side', false), ('Qty', true), ('Entry', true), ('Exit', true),
              ('Net P&L', true), ('Return', true), ('R', true), ('Held', true), ('Best / worst in trade', true),
              ('Exit kept', true), ('5 sessions later', true), ('', false),
            ])
              h('th${num ? '.num' : ''}', c),
          ])),
          h('tbody', [for (final t in trades) ..._tradeRows(t)]),
        ])),
    ];
  }

  List<HTMLElement> _tradeRows(J t) {
    final id = t['id'].s ?? '';
    final a = t['analysis'];
    final closed = t['status'].s == 'closed';
    final pnl = closed ? t['net_pnl'].d : t['unrealized_pnl'].d;
    final after5 = a['after_exit']['marks']['5d'];
    final notes = t['notes'].list;
    final open = _open.contains(id);
    final row = h('tr.jr-trade${open ? '.open' : ''}', [
      h('td.muted', _date(t['opened_at'].i)),
      h('td.sym', h('b', t['symbol'].s ?? '')),
      h('td', h('span.badge.${t['side'].s == 'long' ? 'up' : 'down'}', t['side'].s == 'long' ? 'Long' : 'Short')),
      h('td.num', qtyText(t['max_qty'].d ?? 0)),
      h('td.num', _px(t['entry_avg'].d)),
      h('td.num', closed ? _px(t['exit_avg'].d) : h('span.badge.info', 'open')),
      h('td.num.${(pnl ?? 0) >= 0 ? 'up' : 'down'}', money(pnl)),
      h('td.num', pct(t['return_pct'].d)),
      h('td.num', t['r_multiple'].d == null ? '–' : '${n(t['r_multiple'].d, 2)}R'),
      h('td.num.muted', holdingText(t['holding_seconds'].i ?? 0)),
      h('td.num', a['in_trade'].v == null ? '–' : [h('span.up', money(a['in_trade']['mfe'].d)), ' / ', h('span.down', money(a['in_trade']['mae'].d))]),
      h('td.num', a['in_trade']['exit_efficiency_pct'].d == null ? '–' : '${n(a['in_trade']['exit_efficiency_pct'].d, 0)}%'),
      h('td.num', after5.v == null ? (closed ? h('span.muted', 'pending') : '–') : _heldText(after5['vs_exit'].d)),
      h('td', [
        if (t['proposed_by'].s == 'agent') h('span.badge-agent', '✦', {'data-tip': 'Proposed by the agent'}),
        if (notes.isNotEmpty) h('span.jr-notes-count', [icon('note', size: 13), '${notes.length}']),
      ]),
    ], {'data-id': id}, {
      'click': (_) {
        if (!_open.remove(id)) _open.add(id);
        _render();
      },
    });
    if (!open) return [row];
    final detail = h('td.jr-detail', h('div.muted.small.pad', 'Loading…'), {'colspan': '14'});
    final cached = _details[id];
    if (cached != null) {
      replace(detail, _tradeDetail(cached));
    } else {
      attempt(request('/api/journal/trades/$id')).then((d) {
        if (d == null) return;
        _details[id] = d;
        replace(detail, _tradeDetail(d));
      });
    }
    return [row, h('tr.jr-detail-row', detail)];
  }

  /// "+$120 more" when holding on would have made more, "saved $80" when the exit avoided a loss.
  Object _heldText(double? v) {
    if (v == null) return '–';
    if (v.abs() < 0.005) return h('span.muted', 'same');
    return v > 0
        ? h('span.warn-text', '${money(v)} if held', {'data-tip': 'Staying in would have made this much more'})
        : h('span.up', 'saved ${money(-v, sign: false)}');
  }

  Object _tradeDetail(J t) {
    final a = t['analysis'];
    final it = a['in_trade'];
    final ae = a['after_exit'];
    final sym = t['symbol'].s ?? '';
    final closed = t['status'].s == 'closed';
    return h('div.jr-trade-detail', [
      h('div.jr-chart-box', [
        _pathChart(t),
        h('div.jr-legend.muted.tiny', [
          h('span.jr-key.entry', 'entry'), h('span.jr-key.exit', 'exit'),
          if (t['planned_stop'].d != null) h('span.jr-key.stop', 'stop'),
          if (t['planned_target'].d != null) h('span.jr-key.target', 'target'),
          'daily closes: 10 sessions before, the trade, 20 after',
        ]),
      ]),
      h('div.jr-facts', [
        h('h4', 'Outcome'),
        _fact('Net P&L', money(closed ? t['net_pnl'].d : t['unrealized_pnl'].d), cls: (t['net_pnl'].d ?? 0) >= 0 ? 'up' : 'down'),
        _fact('Commission', money(t['commission'].d, sign: false)),
        _fact('Planned stop / target', '${_px(t['planned_stop'].d)} / ${_px(t['planned_target'].d)}'),
        _fact('Held', holdingText(t['holding_seconds'].i ?? 0)),
        if (it.v != null) ...[
          h('h4', 'During the trade'),
          _fact('Best price', '${_px(it['best_price'].d)} (${money(it['mfe'].d)}, ${pct(it['mfe_pct'].d)})', cls: 'up'),
          _fact('Worst price', '${_px(it['worst_price'].d)} (${money(it['mae'].d)}, ${pct(it['mae_pct'].d)})', cls: 'down'),
          if (it['exit_efficiency_pct'].d != null)
            _fact('Exit kept', '${n(it['exit_efficiency_pct'].d, 0)}% of the best move'),
        ],
        if (ae.v != null) ...[
          h('h4', 'After the exit'),
          h('div.muted.small', ae['verdict'].s ?? ''),
          for (final e in ae['marks'].map.entries)
            _fact('${e.key.replaceAll('d', '')} session${e.key == '1d' ? '' : 's'} later', [
              '${_px(e.value['price'].d)} (${pct(e.value['pct'].d)}) · ',
              _heldText(e.value['vs_exit'].d),
            ]),
          _fact('Best within ${ae['sessions'].i} sessions', [_px(ae['best_price'].d), ' · ', _heldText(ae['best_vs_exit'].d)]),
          _fact('Worst within ${ae['sessions'].i} sessions', [_px(ae['worst_price'].d), ' · ', _heldText(ae['worst_vs_exit'].d)]),
        ] else if (closed)
          h('div.muted.small', 'What price did after the exit appears once the next session has closed.'),
      ]),
      h('div.jr-facts', [
        h('h4', 'Other exits'),
        if (a['alternatives'].list.isEmpty) h('div.muted.small', 'Available for closed trades.'),
        for (final alt in a['alternatives'].list)
          _fact(alt['label'].s ?? '', [
            '${_px(alt['price'].d)} → ${money(alt['net_pnl'].d)} ',
            h('span.${(alt['vs_actual'].d ?? 0) > 0 ? 'warn-text' : 'up'}.tiny',
                '(${money(alt['vs_actual'].d)} vs actual)'),
          ]),
        h('h4', 'Fills'),
        for (final f in t['fills'].list)
          h('div.small', [
            h('span.muted', '${_date(f['time'].i, time: true)} '),
            h('b.${f['side'].s == 'buy' ? 'up' : 'down'}', (f['side'].s ?? '').toUpperCase()),
            ' ${qtyText(f['qty'].d ?? 0)} @ ${_px(f['price'].d)}',
            h('span.muted', ' ${f['action'].s ?? ''}${f['role'].s != null && f['role'].s != f['action'].s ? ' · ${(f['role'].s ?? '').replaceAll('_', ' ')}' : ''}'),
          ]),
      ]),
      h('div.jr-trade-notes', [
        h('div.row.gap', [
          h('h4', 'Notes on this trade'),
          h('span.spacer'),
          h('button.btn.small', 'Open chart', {'type': 'button'}, {'click': (_) => _goChart(sym)}),
          h('button.btn.small.primary', 'Add note', {'type': 'button', 'aria-label': 'Add trade note'},
              {'click': (_) => _editNote(symbol: sym, tradeId: t['id'].s, title: '${t['side'].s == 'long' ? 'Long' : 'Short'} $sym ${_date(t['opened_at'].i)}')}),
        ]),
        if (t['notes'].list.isEmpty) h('div.muted.small', 'What was the plan, what happened, what to repeat or avoid.'),
        for (final nt in t['notes'].list) _noteCard(nt, symbol: sym, tradeId: t['id'].s),
      ]),
    ], null, {'click': (e) => e.stopPropagation()});
  }

  HTMLElement _fact(String k, Object v, {String? cls}) =>
      h('div.jr-fact', [h('span.muted', k), h('span.spacer'), h('span${cls == null ? '' : '.$cls'}', v)]);

  /// Daily closes around the trade with entry/exit markers and the planned stop/target.
  Element _pathChart(J t) {
    final pts = [for (final p in t['path'].list) (p.list[0].i ?? 0, p.list[1].d ?? 0)];
    const w = 520.0, hgt = 170.0, pad = 8.0;
    final svg = _svg('svg', {'viewBox': '0 0 $w $hgt', 'class': 'jr-path', 'preserveAspectRatio': 'none'});
    if (pts.length < 2) {
      return h('div.muted.small.pad', 'No price history for this period.');
    }
    final levels = [
      for (final k in ['entry_avg', 'exit_avg', 'planned_stop', 'planned_target']) ?t[k].d,
    ];
    final ys = [for (final p in pts) p.$2, ...levels];
    var lo = ys.reduce((a, b) => a < b ? a : b), hi = ys.reduce((a, b) => a > b ? a : b);
    if (hi - lo < 1e-9) hi = lo + 1;
    final t0 = pts.first.$1, t1 = pts.last.$1;
    double x(int ts) => pad + (ts - t0) / (t1 - t0 == 0 ? 1 : t1 - t0) * (w - 2 * pad);
    double y(double v) => pad + (hi - v) / (hi - lo) * (hgt - 2 * pad);
    final opened = t['opened_at'].i ?? t0, closedAt = t['closed_at'].i ?? t1;
    svg.append(_svg('rect', {
      'x': '${x(opened.clamp(t0, t1))}', 'y': '0', 'width': '${(x(closedAt.clamp(t0, t1)) - x(opened.clamp(t0, t1))).clamp(1, w)}',
      'height': '$hgt', 'class': 'jr-held',
    }));
    for (final (k, cls) in [('planned_stop', 'stop'), ('planned_target', 'target')]) {
      final v = t[k].d;
      if (v != null) svg.append(_svg('line', {'x1': '0', 'x2': '$w', 'y1': '${y(v)}', 'y2': '${y(v)}', 'class': 'jr-level $cls'}));
    }
    svg.append(_svg('polyline', {'points': [for (final p in pts) '${x(p.$1)},${y(p.$2)}'].join(' '), 'class': 'jr-line'}));
    void marker(int ts, double? px, String cls) {
      if (px == null) return;
      svg.append(_svg('circle', {'cx': '${x(ts.clamp(t0, t1))}', 'cy': '${y(px)}', 'r': '4.5', 'class': 'jr-dot $cls'}));
    }

    marker(opened, t['entry_avg'].d, 'entry');
    if (t['closed_at'].i != null) marker(closedAt, t['exit_avg'].d, 'exit');
    return svg;
  }

  // ---------------------------------------------------------------- drawings

  Future<Object?> _drawings() async {
    final days = store.pref<int>('journal.drawDays');
    final j = await request('/api/journal/drawings?days=$days');
    final groups = j['symbols'].list;
    return [
      h('div.tr-tools', [
        h('div.seg', [
          for (final (d, l) in [(7, '7 days'), (30, '30 days'), (90, '90 days')])
            h('button.seg-btn${days == d ? '.on' : ''}', l, {'type': 'button'}, {
              'click': (_) {
                store.setPref('journal.drawDays', d);
                _render();
              },
            }),
        ]),
        h('span.muted.small', '${j['count'].i ?? 0} drawing${j['count'].i == 1 ? '' : 's'} created or changed on ${groups.length} symbol${groups.length == 1 ? '' : 's'}'),
      ]),
      if (groups.isEmpty)
        h('div.empty-state', [icon('trendline', size: 28), h('div', 'No drawings in this period'), h('div.muted.small', 'Lines, zones and trade plans you or the agent draw on charts are listed here.')])
      else
        h('div.jr-groups', [
          for (final g in groups)
            h('section.jr-group', [
              h('header.row.gap', [
                h('b', g['symbol'].s ?? ''),
                h('span.muted.small', '${g['count'].i} · last ${ago(g['last'].s ?? '')}'),
                h('span.spacer'),
                h('button.btn.small', 'Open chart', {'type': 'button', 'aria-label': 'Open ${g['symbol'].s} chart'},
                    {'click': (_) => _goChart(g['symbol'].s ?? '')}),
              ]),
              for (final a in g['items'].list)
                h('div.jr-drawing', [
                  h('span.jr-swatch', null, {'style': 'background:${a['style']['color'].s ?? 'var(--accent)'}'}),
                  h('span', _drawingLabel(a['type'].s ?? '')),
                  if ((a['text'].s ?? '').isNotEmpty) h('span.ellipsis', '“${a['text'].s}”'),
                  h('span.muted.small', _drawingPrices(a)),
                  if (a['interval'].s != null) h('span.chip.small', a['interval'].s),
                  if (a['author'].s == 'agent') h('span.badge-agent', '✦ AI'),
                  if (a['visible'].b == false) h('span.muted.tiny', 'hidden'),
                  h('span.spacer'),
                  h('span.muted.tiny', ago(a['updated_at'].s ?? '')),
                ], null, {'click': (_) => _goChart(g['symbol'].s ?? '')}),
            ]),
        ]),
    ];
  }

  String _drawingLabel(String type) => type.replaceAll('_', ' ').replaceFirstMapped(RegExp('^.'), (m) => m[0]!.toUpperCase());

  String _drawingPrices(J a) {
    final ps = [for (final p in a['points'].list) ?p['price'].d];
    if (ps.isEmpty) return '';
    return ps.take(3).map(fmtPrice).join(' → ');
  }

  // ---------------------------------------------------------------- performance

  Future<Object?> _performance() async {
    final days = store.pref<int>('journal.days');
    final j = await request('/api/journal/performance?days=$days');
    final s = j['stats'], p = j['period'], ex = j['exits'];
    HTMLElement kpi(String k, Object v, {String? cls, String? sub}) =>
        h('div.tr-kpi', [h('span.muted.small', k), h('b${cls == null ? '' : '.$cls'}', v), if (sub != null) h('span.muted.tiny', sub)]);
    final net = p['net_pnl'].d ?? 0;
    return h('div.jr-perf', [
      h('div.tr-tools', [
        h('div.seg', [
          for (final (d, l) in [(30, '1M'), (90, '3M'), (365, '1Y'), (3650, 'All')])
            h('button.seg-btn${days == d ? '.on' : ''}', l, {'type': 'button'}, {
              'click': (_) {
                store.setPref('journal.days', d);
                _render();
              },
            }),
        ]),
        h('span.muted.small', 'Closed trades of ${j['account'].s ?? 'the selected account'}'),
      ]),
      h('div.tr-kpis', [
        kpi('Net P&L', money(net), cls: net >= 0 ? 'up' : 'down', sub: '${p['closed_trades'].i} closed trades'),
        kpi('Win rate', s['win_rate'].d == null ? '–' : '${n(s['win_rate'].d, 1)}%', sub: '${s['wins'].i} W / ${s['losses'].i} L (all time)'),
        kpi('Profit factor', n(s['profit_factor'].d)),
        kpi('Expectancy', money(s['expectancy'].d), sub: 'per trade'),
        kpi('Avg R', s['avg_r'].d == null ? '–' : '${n(s['avg_r'].d)}R'),
        kpi('Avg win / loss', '${money(s['avg_win'].d)} / ${money(s['avg_loss'].d)}'),
        kpi('Best / worst', '${money(s['best'].d)} / ${money(s['worst'].d)}'),
        kpi('Max drawdown', money(p['max_drawdown'].d), cls: 'down', sub: 'of realised P&L'),
        kpi('Streaks', '${p['best_streak'].i ?? 0}W / ${p['worst_streak'].i ?? 0}L', sub: 'longest'),
        kpi('Hold: winners / losers', '${n(p['avg_hold_hours_winners'].d, 1)}h / ${n(p['avg_hold_hours_losers'].d, 1)}h'),
        kpi('Commission', money(s['commission'].d, sign: false)),
        kpi('Account drawdown', j['equity']['max_drawdown_pct'].d == null ? '–' : '${n(j['equity']['max_drawdown_pct'].d)}%',
            sub: 'equity curve'),
      ]),
      h('section.jr-section', [
        h('h3', 'Exits'),
        h('div.tr-kpis', [
          kpi('Exit kept', ex['avg_exit_efficiency_pct'].d == null ? '–' : '${n(ex['avg_exit_efficiency_pct'].d, 0)}%',
              sub: 'of the best move in the trade (avg)'),
          kpi('Left on the table', money(ex['left_on_table_5d'].d, sign: false), cls: 'warn-text', sub: 'gains if held 5 more sessions'),
          kpi('Saved by exiting', money(ex['saved_by_exit_5d'].d, sign: false), cls: 'up', sub: 'losses avoided over 5 sessions'),
          kpi('Early / good exits', '${ex['exited_too_early'].i ?? 0} / ${ex['good_exits'].i ?? 0}', sub: 'price kept going vs reversed'),
          kpi('Avg best / worst', '${money(ex['avg_mfe'].d)} / ${money(ex['avg_mae'].d)}', sub: 'in the trade (MFE / MAE)'),
        ]),
      ]),
      h('section.jr-section', [h('h3', 'Cumulative realised P&L'), _curve(j['curve'].list)]),
      h('div.jr-breakdowns', [
        _breakdown('By symbol', j['by_symbol'].list),
        _breakdown('Long vs short', j['by_side'].list),
        _breakdown('By month', j['by_month'].list),
        _breakdown('By weekday (entry)', j['by_weekday'].list),
        _breakdown('Agent proposals vs your own', j['by_source'].list),
      ]),
    ]);
  }

  Element _curve(List<J> pts) {
    if (pts.isEmpty) return h('div.muted.small.pad', 'Appears after the first closed trade.');
    const w = 900.0, hgt = 160.0, pad = 6.0;
    final vals = [0.0, for (final p in pts) p['pnl'].d ?? 0];
    var lo = vals.reduce((a, b) => a < b ? a : b), hi = vals.reduce((a, b) => a > b ? a : b);
    if (hi - lo < 1e-9) hi = lo + 1;
    double x(int i) => pad + i / (vals.length - 1 == 0 ? 1 : vals.length - 1) * (w - 2 * pad);
    double y(double v) => pad + (hi - v) / (hi - lo) * (hgt - 2 * pad);
    final svg = _svg('svg', {'viewBox': '0 0 $w $hgt', 'class': 'jr-curve', 'preserveAspectRatio': 'none'});
    svg.append(_svg('line', {'x1': '0', 'x2': '$w', 'y1': '${y(0)}', 'y2': '${y(0)}', 'class': 'jr-zero'}));
    svg.append(_svg('polyline', {
      'points': [for (var i = 0; i < vals.length; i++) '${x(i)},${y(vals[i])}'].join(' '),
      'class': 'jr-line ${vals.last >= 0 ? 'up' : 'down'}',
    }));
    return svg;
  }

  HTMLElement _breakdown(String title, List<J> rows) {
    final maxAbs = rows.fold<double>(0, (m, r) => (r['net_pnl'].d ?? 0).abs() > m ? (r['net_pnl'].d ?? 0).abs() : m);
    return h('section.jr-section.jr-breakdown', [
      h('h3', title),
      if (rows.isEmpty) h('div.muted.small', 'No closed trades'),
      for (final r in rows)
        h('div.jr-bar-row', [
          h('span.jr-bar-key', r['key'].s ?? ''),
          h('span.jr-bar', h('span.jr-bar-fill.${(r['net_pnl'].d ?? 0) >= 0 ? 'up' : 'down'}', null, {
            'style': 'width:${maxAbs == 0 ? 0 : ((r['net_pnl'].d ?? 0).abs() / maxAbs * 100).toStringAsFixed(1)}%',
          })),
          h('span.num.${(r['net_pnl'].d ?? 0) >= 0 ? 'up' : 'down'}', money(r['net_pnl'].d)),
          h('span.muted.tiny', '${r['trades'].i} · ${n(r['win_rate'].d, 0)}% W'),
        ]),
    ]);
  }

  // ---------------------------------------------------------------- notes

  Future<Object?> _notes() async {
    final j = await request('/api/journal/notes');
    final groups = j['symbols'].list;
    return [
      h('div.tr-tools', [
        h('span.muted.small', 'Notes per symbol; a trade\'s notes are linked to it (add them from the trade).'),
      ]),
      if (groups.isEmpty)
        h('div.empty-state', [icon('note', size: 28), h('div', 'No notes yet'), h('div.muted.small', 'Use “New note”, or add one to a trade in the Trades tab.')])
      else
        h('div.jr-groups', [
          for (final g in groups)
            h('section.jr-group', [
              h('header.row.gap', [
                h('b', (g['symbol'].s ?? '').isEmpty ? 'General' : g['symbol'].s),
                h('span.muted.small', '${g['count'].i}'),
                h('span.spacer'),
                if ((g['symbol'].s ?? '').isNotEmpty) ...[
                  h('button.btn.small', 'Add note', {'type': 'button', 'aria-label': 'Add note on ${g['symbol'].s}'},
                      {'click': (_) => _editNote(symbol: g['symbol'].s)}),
                  h('button.btn.small', 'Open chart', {'type': 'button'}, {'click': (_) => _goChart(g['symbol'].s ?? '')}),
                ],
              ]),
              for (final nt in g['items'].list) _noteCard(nt, symbol: g['symbol'].s, tradeId: nt['trade_id'].s),
            ]),
        ]),
    ];
  }

  HTMLElement _noteCard(J nt, {String? symbol, String? tradeId}) => h('article.card.jr-note', [
        h('header', [
          if (nt['author'].s == 'agent') h('span.author.agent', '✦ AI'),
          h('b.ellipsis', nt['title'].s ?? ''),
          if (tradeId != null)
            h('span.chip.small', 'trade', {'data-tip': 'Linked to a trade'}),
          h('span.spacer'),
          h('span.muted.tiny', ago(nt['updated_at'].s ?? '')),
          h('button.icon-btn.small', icon('edit', size: 13), {'type': 'button', 'aria-label': 'Edit note', 'data-tip': 'Edit'}, {
            'click': (e) {
              e.stopPropagation();
              _editNote(id: nt['id'].s, symbol: symbol, tradeId: tradeId, title: nt['title'].s, content: nt['content'].s,
                  tags: [for (final t in nt['tags'].list) t.s ?? '']);
            },
          }),
          h('button.icon-btn.small', icon('trash', size: 13), {'type': 'button', 'aria-label': 'Delete note', 'data-tip': 'Delete'}, {
            'click': (e) async {
              e.stopPropagation();
              if (!await confirmDialog('Delete note', 'Delete "${nt['title'].s}"?', confirm: 'Delete', danger: true)) return;
              await request('/api/notes/${nt['id'].s}', method: 'DELETE');
              _details.clear();
              store.notesVersion.value++;
            },
          }),
        ]),
        markdown(nt['content'].s ?? ''),
      ]);

  /// Creates or edits a note; [tradeId] links it to a trade (tag trade:<id>).
  Future<void> _editNote({String? id, String? symbol, String? tradeId, String? title, String? content, List<String>? tags}) async {
    final titleIn = h('input.input', null, {'value': title ?? '', 'aria-label': 'Note title', 'placeholder': 'Title'}) as HTMLInputElement;
    final symIn = h('input.input', null, {'value': symbol ?? '', 'aria-label': 'Note symbol', 'placeholder': 'Symbol (optional)'})
        as HTMLInputElement;
    final body = h('textarea.input', null, {'rows': '9', 'aria-label': 'Note content', 'placeholder': 'Markdown: plan, execution, emotions, lessons…'})
        as HTMLTextAreaElement;
    body.value = content ?? '';
    Dialog.open(id == null ? (tradeId != null ? 'Note on this trade' : 'New note') : 'Edit note', h('div.form-grid', [
      h('label.field-col', [h('span', 'Title'), titleIn]),
      if (tradeId == null) h('label.field-col', [h('span', 'Symbol'), symIn]),
      h('label.field-col', [h('span', 'Note'), body]),
    ]), width: '520px', actions: [
      DialogAction('Cancel'),
      DialogAction('Save', primary: true, onPressed: () async {
        final t = titleIn.value.trim();
        if (t.isEmpty) {
          toast('Give the note a title', kind: 'warn');
          return false;
        }
        final sym = (tradeId != null ? symbol : symIn.value.trim().toUpperCase()) ?? '';
        final payload = {
          'title': t,
          'content': body.value,
          'symbol': sym.isEmpty ? null : sym,
          'tags': [...?tags?.where((x) => !x.startsWith('trade:')), if (tradeId != null) 'trade:$tradeId'],
        };
        try {
          await request(id == null ? '/api/notes' : '/api/notes/$id', method: id == null ? 'POST' : 'PUT', body: payload);
        } catch (e) {
          toast('$e', kind: 'error');
          return false;
        }
        _details.clear();
        store.notesVersion.value++;
        return true;
      }),
    ]);
    Timer.run(() => titleIn.value.isEmpty ? titleIn.focus() : body.focus());
  }

  // ---------------------------------------------------------------- helpers

  String _px(double? v) => v == null ? '–' : fmtPrice(v);

  String _date(int? ts, {bool time = false}) {
    if (ts == null) return '–';
    final d = DateTime.fromMillisecondsSinceEpoch(ts * 1000);
    String two(int v) => v.toString().padLeft(2, '0');
    return '${d.year}-${two(d.month)}-${two(d.day)}${time ? ' ${two(d.hour)}:${two(d.minute)}' : ''}';
  }
}

const _svgNs = 'http://www.w3.org/2000/svg';

Element _svg(String tag, Map<String, String> attrs) {
  final el = document.createElementNS(_svgNs, tag);
  attrs.forEach((k, v) => el.setAttribute(k, v));
  return el;
}
