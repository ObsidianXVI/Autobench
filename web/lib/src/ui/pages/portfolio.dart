// Portfolio page: the account's history from imported IBKR statements (Flex Query XML / Activity Statement
// CSV, or the Flex Web Service) plus the app's own account snapshots. Keeps growth, money put in or taken
// out, and investment returns apart. Data comes from /api/portfolio/* (backend services/portfolio.py).
import 'dart:async';
import 'dart:js_interop';

import 'package:web/web.dart';

import '../../core/chart_ticks.dart';
import '../../core/format.dart';
import '../../core/json.dart';
import '../../core/trading.dart';
import '../api.dart';
import '../dom.dart';
import '../kit/icons.dart';
import '../kit/overlay.dart';
import '../kit/state.dart';
import '../store.dart';
import '../trading_state.dart';
import 'portfolio_chart.dart';

const _tabs = [
  ('overview', 'Overview'),
  ('performance', 'Performance'),
  ('cash', 'Cash flows'),
  ('imports', 'Imports'),
];

const _periods = [('1m', '1M'), ('3m', '3M'), ('6m', '6M'), ('ytd', 'YTD'), ('1y', '1Y'), ('3y', '3Y'), ('all', 'All')];

const _kindLabels = {
  'deposit': 'Deposit', 'withdrawal': 'Withdrawal', 'transfer': 'Transfer', 'dividend': 'Dividend',
  'tax': 'Withholding tax', 'interest': 'Interest', 'fee': 'Fee', 'other': 'Other',
};

class PortfolioPage {
  final HTMLElement root = h('div.page.portfolio-page');
  final HTMLElement _head = h('div.tr-head');
  final HTMLElement _tabBar = h('div.tr-tabs');
  final HTMLElement _body = h('div.pf-body');
  final Scope _scope = Scope();
  final _reload = Debouncer(400);
  String? _account;
  String _flowKind = '';
  List<J> _accounts = const [];
  int _seq = 0;

  String get _tab => store.pref<String>('portfolio.tab');
  String get _period => store.pref<String>('portfolio.period');
  String get _start => store.pref<String>('portfolio.start');
  String get _end => store.pref<String>('portfolio.end');
  final List<PerfChart> _charts = [];

  /// Shows a custom date range on the Performance tab (from a drag on a chart, a table row, or the date fields).
  void _showRange(String start, String end) {
    if (end.compareTo(start) < 0) (start, end) = (end, start);
    store.setPref('portfolio.start', start);
    store.setPref('portfolio.end', end);
    store.setPref('portfolio.period', 'custom');
    _render();
  }

  PortfolioPage() {
    append(root, [_head, h('section.tr-card.tr-main', [_tabBar, _body])]);
    _scope.listen(trade.version, (_) {
      if (!root.isHidden && _tab == 'overview') _reload(_render);
    });
  }

  /// The account and period on screen, for the quick chat with Claude.
  Map<String, Object?> agentContext() => {
        'account': ?_account,
        'tab': _tab,
        'period': _period,
        if (_start.isNotEmpty) 'start': _start,
        if (_end.isNotEmpty) 'end': _end,
      };

  void onShow() {
    trade.start();
    _renderHead();
    _renderTabs();
    _loadAccounts().then((_) => _render());
  }

  Future<void> _loadAccounts() async {
    try {
      final j = await request('/api/portfolio/accounts');
      _accounts = j['accounts'].list;
      if (_account == null || !_accounts.any((a) => a['account'].s == _account)) _account = j['default'].s;
      _renderHead();
    } catch (_) {}
  }

  /// A statement was imported (here, by the daily Flex download or in another tab).
  void onImported() {
    if (!root.isHidden) _reload(() => _loadAccounts().then((_) => _render()));
  }

  void selectTab(String k) {
    if (!_tabs.any((t) => t.$1 == k)) return;
    store.setPref('portfolio.tab', k);
    _renderTabs();
    _render();
  }

  void _renderHead() {
    final sel = h('select.input.small', [
      for (final a in _accounts)
        h('option', '${a['account'].s}${a['to'].s != null ? ' · to ${a['to'].s}' : ''}',
            {'value': a['account'].s ?? '', if (a['account'].s == _account) 'selected': ''}),
    ], {'aria-label': 'Account'}) as HTMLSelectElement;
    sel.addEventListener('change', ((Event _) {
      _account = sel.value;
      _render();
    }).toJS);
    replace(_head, [
      h('h2', 'Portfolio'),
      if (_accounts.length > 1) sel else if (_account != null) h('span.muted.small', _account),
      h('span.spacer'),
      h('button.btn.small', [icon('download', size: 14), 'Import statement'], {'type': 'button'}, {'click': (_) => _pickFile()}),
      h('button.btn.small', [icon('refresh', size: 14), 'Sync from IBKR'], {'type': 'button', 'data-tip': 'Flex Web Service'},
          {'click': (_) => _sync()}),
    ]);
  }

  void _renderTabs() {
    replace(_tabBar, [
      for (final (k, l) in _tabs)
        h('button.tab${_tab == k ? '.on' : ''}', l, {'type': 'button', 'aria-label': l, 'data-tab': k}, {'click': (_) => selectTab(k)}),
    ]);
  }

  String get _acctQ => _account == null ? '' : 'account=${Uri.encodeComponent(_account!)}';

  Future<void> _render() async {
    final seq = ++_seq;
    if (_body.childNodes.length == 0) replace(_body, h('div.muted.small.pad', 'Loading…'));
    final Object? content;
    try {
      content = switch (_tab) {
        'performance' => await _performance(),
        'cash' => await _cash(),
        'imports' => await _imports(),
        _ => await _overview(),
      };
    } catch (e) {
      if (seq == _seq) replace(_body, h('div.check-msg.error', '$e'));
      return;
    }
    if (seq != _seq) return;
    replace(_body, content);
  }

  HTMLElement _kpi(String k, Object v, {String? cls, String? sub, String? tip}) => h('div.tr-kpi', [
        h('span.muted.small', k),
        h('b${cls == null ? '' : '.$cls'}', v),
        if (sub != null) h('span.muted.tiny', sub),
      ], {if (tip != null) 'data-tip': tip});

  String _cls(num? v) => (v ?? 0) >= 0 ? 'up' : 'down';

  String _pct(num? v) => v == null ? '–' : pct(v);

  HTMLElement _empty() => h('div.empty-state.pf-empty', [
        icon('pie', size: 30),
        h('div', 'No account history yet'),
        h('div.muted.small', 'Import an IBKR statement to see your history, where your account value came from, and your '
            'returns without your deposits mixed in.'),
        h('button.btn.primary', [icon('download', size: 14), 'Import statement'], {'type': 'button'}, {'click': (_) => _pickFile()}),
        h('button.btn', 'How to export from IBKR', {'type': 'button'}, {'click': (_) => selectTab('imports')}),
      ]);

  // ---------------------------------------------------------------- overview

  Future<Object?> _overview() async {
    final j = await request('/api/portfolio/overview${_acctQ.isEmpty ? '' : '?$_acctQ'}');
    _accounts = j['accounts'].list;
    _account ??= j['account'].s;
    _renderHead();
    if (_accounts.isEmpty && j['live'].v == null) return _empty();
    final live = j['live'], life = j['lifetime'], tot = j['totals'], sp = j['statement_positions'];
    final cap = live['capital'];
    final nav = live.v != null ? cap['net_liquidation'].d : j['nav']['value'].d;
    final navWhen = live.v != null ? 'live' : 'statement ${j['nav']['date'].s ?? ''}';
    final contributed = tot['net_contributions'].d;
    final gain = life['investment_gain'].d;
    // the broker may still be loading positions: fall back to the latest statement rather than an empty table
    final showLive = live.v != null && (live['positions'].list.isNotEmpty || sp['positions'].list.isEmpty);
    return h('div.pf-overview', [
      h('div.tr-kpis.pf-kpis', [
        _kpi('Account value', money(nav, sign: false), sub: navWhen),
        _kpi('Money put in (net)', money(contributed, sign: false),
            sub: 'deposits ${money(tot['deposit'].d, sign: false)} · withdrawn ${money((tot['withdrawal'].d ?? 0).abs(), sign: false)}',
            tip: 'Deposits minus withdrawals, plus positions transferred in or out'),
        _kpi('Investment gain', money(gain), cls: _cls(gain), sub: 'all time, deposits excluded'),
        _kpi('Time-weighted return', _pct(life['twr_pct'].d), cls: _cls(life['twr_pct'].d),
            sub: life['twr_annualized_pct'].d != null ? '${pct(life['twr_annualized_pct'].d)} a year' : 'since ${life['start'].s ?? j['nav']['first_date'].s ?? '–'}',
            tip: 'How the investments did, whatever you deposited or withdrew and when'),
        _kpi('Money-weighted return', _pct(life['mwr_pct'].d), cls: _cls(life['mwr_pct'].d),
            sub: life['mwr_annualized_pct'].d != null ? '${pct(life['mwr_annualized_pct'].d)} a year (IRR)' : 'IRR of your cash flows',
            tip: 'Your own result: includes the effect of when you added or took out money'),
        if (live.v != null) ...[
          _kpi('Cash', money(cap['cash'].d, sign: false), sub: cap['cash_pct'].d == null ? null : '${n(cap['cash_pct'].d, 1)}% of value'),
          _kpi('Invested', money(cap['invested'].d, sign: false),
              sub: cap['invested_pct'].d == null ? null : '${n(cap['invested_pct'].d, 1)}% of value'),
          _kpi('Unrealized P&L', money(live['account']['unrealized_pnl'].d), cls: _cls(live['account']['unrealized_pnl'].d)),
        ],
      ]),
      if (life.v != null) _composition(life),
      h('div.pf-grid', [
        h('section.jr-section', [
          h('h3', showLive ? 'Positions (live)' : 'Positions (statement ${sp['date'].s ?? ''})'),
          showLive ? _livePositions(live['positions'].list, nav) : _statementPositions(sp['positions'].list),
        ]),
        h('section.jr-section', [
          h('h3', 'All time'),
          h('div.pf-facts', [
            for (final (k, v) in [
              ('Deposits', tot['deposit'].d), ('Withdrawals', tot['withdrawal'].d), ('Transfers in/out', tot['transfer'].d),
              ('Dividends', tot['dividend'].d), ('Withholding tax', tot['tax'].d), ('Interest', tot['interest'].d),
              ('Fees', tot['fee'].d), ('Commissions', tot['commissions'].d),
            ])
              h('div.jr-fact', [h('span.muted', k), h('span.spacer'), h('span.num.${_cls(v)}', money(v))]),
          ]),
          if (j['coverage'].v != null)
            h('div.muted.tiny', [
              'History: ',
              j['coverage']['nav'].v == null ? 'no NAV yet' : '${j['coverage']['nav'][0].s} → ${j['coverage']['nav'][1].s}',
              ' · ${j['coverage']['executions'].i ?? 0} executions',
            ]),
        ]),
      ]),
    ]);
  }

  /// Where the account value came from: money put in vs what it earned (trading and income).
  HTMLElement _composition(J p) {
    final opening = p['start_value'].d ?? 0;
    final net = p['contributions']['net'].d ?? 0;
    final trading = p['trading']['total'].d ?? 0, income = p['income']['total'].d ?? 0;
    final parts = [
      ('Starting value', opening, 'pf-c-open'),
      ('Net deposits', net, 'pf-c-contrib'),
      ('Trading & market', trading, trading >= 0 ? 'pf-c-up' : 'pf-c-down'),
      ('Income & costs', income, income >= 0 ? 'pf-c-inc' : 'pf-c-down'),
    ].where((x) => x.$2.abs() > 0.005).toList();
    final total = parts.fold<double>(0, (a, x) => a + x.$2.abs());
    return h('section.jr-section', [
      h('h3', 'Where the value came from'),
      h('div.pf-stack', [
        for (final (l, v, c) in parts)
          h('span.pf-seg.$c', null, {'style': 'flex-grow:${total == 0 ? 1 : (v.abs() / total * 1000).round()}', 'data-tip': '$l ${money(v)}'}),
      ]),
      h('div.pf-legend', [
        for (final (l, v, c) in parts) h('span', [h('i.pf-dot.$c'), '$l ', h('b.num', money(v, sign: l != 'Starting value'))]),
        h('span', ['= ', h('b.num', money(p['end_value'].d, sign: false)), h('span.muted', ' on ${p['end'].s}')]),
      ]),
    ]);
  }

  HTMLElement _table(List<(String, bool)> cols, List<HTMLElement> rows, {String empty = 'Nothing here yet'}) =>
      rows.isEmpty
          ? h('div.empty-state', h('div.muted.small', empty))
          : h('div.table-wrap', h('table.data-table.compact', [
              h('thead', h('tr', [for (final (c, num) in cols) h('th${num ? '.num' : ''}', c)])),
              h('tbody', rows),
            ]));

  HTMLElement _sym(String s) => h('td.sym', h('a', s, {'href': '#chart'}, {
        'click': (e) {
          e.preventDefault();
          store.activeChart.symbol.value = s;
          store.page.value = 'chart';
        },
      }));

  HTMLElement _livePositions(List<J> ps, double? nav) => _table([
        ('Symbol', false), ('Qty', true), ('Avg cost', true), ('Last', true), ('Value', true), ('Weight', true), ('Unrealized', true),
      ], [
        for (final p in ps)
          h('tr', [
            _sym(p['symbol'].s ?? ''),
            h('td.num', qtyText(p['qty'].d ?? 0)),
            h('td.num', p['avg_cost'].d == null ? '–' : fmtPrice(p['avg_cost'].d!)),
            h('td.num', p['market_price'].d == null ? '–' : fmtPrice(p['market_price'].d!)),
            h('td.num', money(p['market_value'].d, sign: false)),
            h('td.num', nav == null || nav == 0 ? '–' : '${n((p['market_value'].d ?? 0) / nav * 100, 1)}%'),
            h('td.num.${_cls(p['unrealized_pnl'].d)}', money(p['unrealized_pnl'].d)),
          ]),
      ], empty: 'No open positions');

  HTMLElement _statementPositions(List<J> ps) {
    final cash = ps.where((p) => p['asset_class'].s == 'CASH').toList();
    final held = ps.where((p) => p['asset_class'].s != 'CASH').toList()
      ..sort((a, b) => (b['value_base'].d ?? b['value'].d ?? 0).compareTo(a['value_base'].d ?? a['value'].d ?? 0));
    return h('div.pf-col', [
      _table([('Symbol', false), ('Class', false), ('Qty', true), ('Mark', true), ('Value', true), ('Cost', true), ('Unrealized', true)], [
        for (final p in held)
          h('tr', [
            p['asset_class'].s == 'STK' ? _sym(p['symbol'].s ?? '') : h('td.sym', p['symbol'].s),
            h('td.muted', p['asset_class'].s),
            h('td.num', qtyText(p['qty'].d ?? 0)),
            h('td.num', p['mark'].d == null ? '–' : fmtPrice(p['mark'].d!)),
            h('td.num', '${money(p['value'].d, sign: false)}${p['currency'].s != null && p['currency'].s != 'USD' ? ' ${p['currency'].s}' : ''}'),
            h('td.num', money(p['cost_basis'].d, sign: false)),
            h('td.num.${_cls(p['unrealized'].d)}', money(p['unrealized'].d)),
          ]),
      ], empty: 'No positions on the statement date'),
      if (cash.isNotEmpty)
        h('div.pf-cash', [
          h('span.muted.small', 'Cash'),
          for (final c in cash) h('span.chip', '${c['symbol'].s} ${n(c['qty'].d, 2)}'),
        ]),
    ]);
  }

  // ---------------------------------------------------------------- performance

  Future<Object?> _performance() async {
    final custom = _period == 'custom' && _start.isNotEmpty;
    final q = [
      'period=${custom ? 'custom' : _period}',
      if (custom) 'start=$_start',
      if (custom && _end.isNotEmpty) 'end=$_end',
      if (_acctQ.isNotEmpty) _acctQ,
    ];
    final j = await request('/api/portfolio/performance?${q.join('&')}');
    _account ??= j['account'].s;
    final p = j['summary'];
    final acct = _accounts.where((a) => a['account'].s == j['account'].s).firstOrNull;
    HTMLInputElement dateInput(String label, String value) => h('input.input.small.pf-date', null, {
          'type': 'date',
          'aria-label': label,
          'value': value,
          if (acct?['from'].s != null) 'min': acct!['from'].s!,
          'max': acct?['to'].s ?? '',
        }) as HTMLInputElement;
    final from = dateInput('From', j['from'].s ?? ''), to = dateInput('To', j['to'].s ?? '');
    void apply() {
      if (from.value.isNotEmpty && to.value.isNotEmpty) _showRange(from.value, to.value);
    }

    from.addEventListener('change', ((Event _) => apply()).toJS);
    to.addEventListener('change', ((Event _) => apply()).toJS);
    final tools = h('div.tr-tools', [
      h('div.seg', [
        for (final (k, l) in _periods)
          h('button.seg-btn${!custom && _period == k ? '.on' : ''}', l, {'type': 'button'}, {
            'click': (_) {
              store.setPref('portfolio.period', k);
              _render();
            },
          }),
      ]),
      h('div.pf-range${custom ? '.on' : ''}', [from, h('span.muted', '→'), to], {'data-tip': 'Custom date range'}),
      if (p.v != null) h('span.muted.small', '${p['days'].i} days'),
    ]);
    if (p.v == null) return [tools, j['account'].s == null ? _empty() : h('div.muted.small.pad', 'No account values in this period.')];
    final c = p['contributions'], tr = p['trading'], inc = p['income'];
    final st = j['statement'];
    final charts = _charts2(j['series'].list);
    return h('div.jr-perf', [
      tools,
      h('div.pf-equation', [
        h('span', [h('span.muted', 'Start '), h('b.num', money(p['start_value'].d, sign: false))]),
        h('span.op', '+'),
        h('span', [h('span.muted', 'Net deposits '), h('b.num', money(c['net'].d))]),
        h('span.op', '+'),
        h('span', [h('span.muted', 'Investment gain '), h('b.num.${_cls(p['investment_gain'].d)}', money(p['investment_gain'].d))]),
        h('span.op', '='),
        h('span', [h('span.muted', 'End '), h('b.num', money(p['end_value'].d, sign: false))]),
      ]),
      h('div.tr-kpis', [
        _kpi('Growth', money(p['growth'].d), cls: _cls(p['growth'].d),
            sub: p['growth_pct'].d == null ? 'change in account value' : '${pct(p['growth_pct'].d)} of the start value',
            tip: 'Change in account value, including deposits and withdrawals'),
        _kpi('Net deposits', money(c['net'].d),
            sub: 'in ${money(c['deposits'].d, sign: false)} · out ${money((c['withdrawals'].d ?? 0).abs(), sign: false)}'
                '${(c['transfers'].d ?? 0) != 0 ? ' · transfers ${money(c['transfers'].d)}' : ''}'),
        _kpi('Investment gain', money(p['investment_gain'].d), cls: _cls(p['investment_gain'].d), sub: 'growth minus net deposits'),
        _kpi('Time-weighted return', _pct(p['twr_pct'].d), cls: _cls(p['twr_pct'].d),
            sub: p['twr_annualized_pct'].d != null ? '${pct(p['twr_annualized_pct'].d)} a year' : 'not affected by deposits',
            tip: 'Chain-linked Modified Dietz between daily account values'),
        _kpi('Money-weighted return', _pct(p['mwr_pct'].d), cls: _cls(p['mwr_pct'].d),
            sub: p['mwr_annualized_pct'].d != null ? '${pct(p['mwr_annualized_pct'].d)} a year (IRR)' : 'IRR of your deposits/withdrawals'),
        _kpi('Trading & market', money(tr['total'].d), cls: _cls(tr['total'].d),
            sub: 'realised ${money(tr['realized'].d)} · other ${money(tr['unrealized_fx_other'].d)}',
            tip: 'Realised P&L from closed positions; "other" is the change in unrealised P&L, FX and anything else'),
        _kpi('Income & costs', money(inc['total'].d), cls: _cls(inc['total'].d),
            sub: 'dividends ${money(inc['dividends'].d)} · tax ${money(inc['withholding_tax'].d)} · interest ${money(inc['interest'].d)} · fees ${money(inc['fees'].d)}'),
        _kpi('Commissions', money(tr['commissions'].d), sub: '${tr['executions'].i ?? 0} executions (part of trading)'),
      ]),
      h('section.jr-section', [
        h('div.row.gap', [
          h('h3', 'Account value vs money put in'),
          h('span.spacer'),
          h('span.pf-legend', [
            h('span', [h('i.pf-dot.pf-c-nav'), 'Account value']),
            h('span', [h('i.pf-dot.pf-c-contrib'), 'Money put in (net)']),
            h('span', [h('i.pf-dot.pf-c-up'), 'Gain']),
          ]),
        ]),
        charts.$1,
      ]),
      h('section.jr-section', [h('h3', 'Time-weighted return (cumulative)'), charts.$2]),
      if (j['yearly'].list.length > 1 || j['monthly'].list.isNotEmpty)
        h('div.pf-grid', [
          if (j['yearly'].list.isNotEmpty) h('section.jr-section', [h('h3', 'By year'), _periodTable(j['yearly'].list)]),
          h('section.jr-section', [h('h3', 'By month'), _periodTable(j['monthly'].list.reversed.toList())]),
        ]),
      if (st.v != null)
        h('div.muted.tiny', 'IBKR\'s own figures for ${st['from'].s} → ${st['to'].s}: TWR ${st['twr'].d == null ? '–' : '${n(st['twr'].d)}%'}, '
            'deposits/withdrawals ${money(st['deposits_withdrawals'].d)}, mark-to-market ${money(st['mtm'].d)}.'),
    ]);
  }

  HTMLElement _periodTable(List<J> rows) => _table([
        ('Period', false), ('Start', true), ('Net deposits', true), ('Gain', true), ('Trading', true), ('Income', true),
        ('TWR', true), ('MWR', true), ('End', true),
      ], [
        for (final r in rows)
          h('tr.pf-row-link', [
            h('td', r['key'].s),
            h('td.num', money(r['start_value'].d, sign: false)),
            h('td.num', money(r['contributions'].d)),
            h('td.num.${_cls(r['investment_gain'].d)}', money(r['investment_gain'].d)),
            h('td.num', money(r['trading'].d)),
            h('td.num', money(r['income'].d)),
            h('td.num.${_cls(r['twr_pct'].d)}', _pct(r['twr_pct'].d)),
            h('td.num', _pct(r['mwr_pct'].d)),
            h('td.num', money(r['end_value'].d, sign: false)),
          ], {'data-tip': 'Show ${r['key'].s}', 'tabindex': '0'}, {
            'click': (_) => _zoomTo(r['key'].s),
            'keydown': (e) {
              if ((e as KeyboardEvent).key == 'Enter') _zoomTo(r['key'].s);
            },
          }),
      ]);

  void _zoomTo(String? key) {
    if (key == null) return;
    final (a, b) = periodBounds(key);
    _showRange(a, b);
  }

  /// The account-value chart and the TWR chart; their cursors move together.
  (Element, Element) _charts2(List<J> pts) {
    _charts.clear();
    if (pts.length < 2) {
      Element none() => h('div.muted.small.pad', 'Needs at least two account values in the period.');
      return (none(), none());
    }
    final dates = [for (final p in pts) p['date'].s ?? ''];
    final nav = [for (final p in pts) p['nav'].d], put = [for (final p in pts) p['contributed'].d];
    final twr = [for (final p in pts) p['twr_pct'].d];
    double? flowAt(int i) => i == 0 || put[i] == null || put[i - 1] == null ? null : put[i]! - put[i - 1]!;
    double? dayGain(int i) => i == 0 || nav[i] == null || nav[i - 1] == null ? null : nav[i]! - nav[i - 1]! - (flowAt(i) ?? 0);
    final gain0 = (nav[0] ?? 0) - (put[0] ?? 0);
    void sync(int? i) {
      for (final c in _charts) {
        c.showAt(i);
      }
    }

    String axisMoney(double v) => v == 0 ? '\$0' : '${v < 0 ? '−' : ''}\$${compact(v.abs())}';
    final value = PerfChart(
      dates: dates,
      lines: [ChartLine('Money put in', 'pf-line-contrib', put), ChartLine('Account value', 'pf-line-nav', nav)],
      band: const ChartBand(1, 0),
      axisLabel: axisMoney,
      onRange: _showRange,
      onHover: sync,
      tip: (i) {
        final g = nav[i] == null || put[i] == null ? null : nav[i]! - put[i]!;
        final dg = dayGain(i), f = flowAt(i);
        final prev = i > 0 ? nav[i - 1] : null;
        return [
          ('Account value', money(nav[i], sign: false), null),
          ('Money put in', money(put[i], sign: false), null),
          ('Gain (all time)', money(g), _cls(g)),
          if (i > 0) ('Gain in period', money(g == null ? null : g - gain0), _cls(g == null ? null : g - gain0)),
          if (dg != null)
            ('Day', '${money(dg)}${prev != null && prev != 0 ? ' (${pct(dg / prev * 100)})' : ''}', _cls(dg)),
          if (f != null && f.abs() >= 0.005) (f > 0 ? 'Deposited' : 'Withdrawn', money(f.abs(), sign: false), null),
        ];
      },
    );
    final ret = PerfChart(
      dates: dates,
      lines: [ChartLine('Time-weighted return', 'jr-line ${(twr.last ?? 0) >= 0 ? 'up' : 'down'}', twr)],
      axisLabel: (v) => '${v.abs() < 1e-9 ? '0' : v.toStringAsFixed(v.abs() >= 10 ? 0 : 1)}%',
      includeZero: true,
      height: 150,
      onRange: _showRange,
      onHover: sync,
      tip: (i) {
        final prev = i > 0 ? twr[i - 1] : null;
        final day = prev == null || twr[i] == null ? null : ((1 + twr[i]! / 100) / (1 + prev / 100) - 1) * 100;
        return [
          ('Return in period', _pct(twr[i]), _cls(twr[i])),
          if (day != null) ('Day', pct(day), _cls(day)),
        ];
      },
    );
    _charts.addAll([value, ret]);
    return (value.root, ret.root);
  }

  // ---------------------------------------------------------------- cash flows

  Future<Object?> _cash() async {
    final q = [if (_acctQ.isNotEmpty) _acctQ, if (_flowKind.isNotEmpty) 'kind=$_flowKind'];
    final j = await request('/api/portfolio/cash-flows${q.isEmpty ? '' : '?${q.join('&')}'}');
    final items = j['items'].list;
    final sum = items.fold<double>(0, (a, f) => a + (f['amount'].d ?? 0));
    return [
      h('div.tr-tools', [
        h('div.seg', [
          for (final (k, l) in [('', 'All'), ('contributions', 'Deposits & withdrawals'), ('income', 'Dividends, interest & fees')])
            h('button.seg-btn${_flowKind == k ? '.on' : ''}', l, {'type': 'button'}, {
              'click': (_) {
                _flowKind = k;
                _render();
              },
            }),
        ]),
        h('span.muted.small', '${items.length} entries · total ${money(sum)}'),
      ]),
      _table([('Date', false), ('Type', false), ('Symbol', false), ('Description', false), ('Amount', true)], [
        for (final f in items)
          h('tr', [
            h('td', f['date'].s),
            h('td', h('span.badge.pf-k-${f['kind'].s}', _kindLabels[f['kind'].s] ?? f['kind'].s)),
            h('td', f['symbol'].s ?? ''),
            h('td.muted.pf-desc', f['description'].s ?? ''),
            h('td.num.${_cls(f['amount'].d)}', [
              money(f['amount'].d),
              if (f['currency'].s != null && f['currency'].s != 'USD' && f['amount_local'].d != f['amount'].d)
                h('span.muted.tiny', ' (${n(f['amount_local'].d)} ${f['currency'].s})'),
            ]),
          ]),
      ], empty: _account == null ? 'Import a statement first' : 'No cash flows'),
    ];
  }

  // ---------------------------------------------------------------- imports

  Future<Object?> _imports() async {
    final res = await Future.wait([request('/api/portfolio/imports'), request('/api/portfolio/flex')]);
    final list = res[0]['imports'].list, flex = res[1];
    final drop = h('div.pf-drop', [
      icon('download', size: 22),
      h('div', 'Drop an IBKR statement here, or'),
      h('button.btn.small', 'Choose file…', {'type': 'button'}, {'click': (_) => _pickFile()}),
      h('div.muted.tiny', 'Flex Query (.xml) — recommended — or Activity Statement (.csv). Importing again or overlapping '
          'periods is safe: nothing is counted twice.'),
    ]);
    drop.addEventListener('dragover', ((DragEvent e) {
      e.preventDefault();
      drop.classList.add('over');
    }).toJS);
    drop.addEventListener('dragleave', ((Event _) => drop.classList.remove('over')).toJS);
    drop.addEventListener('drop', ((DragEvent e) {
      e.preventDefault();
      drop.classList.remove('over');
      final files = e.dataTransfer?.files;
      if (files != null && files.length > 0) _importFiles([for (var i = 0; i < files.length; i++) files.item(i)!]);
    }).toJS);

    final token = h('input.input', null, {
      'type': 'password', 'autocomplete': 'off', 'aria-label': 'Flex token',
      'placeholder': flex['has_token'].b == true ? 'saved (enter a new one to replace)' : 'Flex Web Service token',
    }) as HTMLInputElement;
    final query = h('input.input', null, {'aria-label': 'Flex query id', 'placeholder': 'Query ID', 'value': flex['query_id'].s ?? ''})
        as HTMLInputElement;
    final auto = h('input', null, {'type': 'checkbox', 'aria-label': 'Download daily'}) as HTMLInputElement
      ..checked = flex['auto_sync'].b == true;
    Future<void> save() async {
      try {
        await request('/api/portfolio/flex', method: 'POST', body: {
          if (token.value.trim().isNotEmpty) 'token': token.value.trim(),
          'query_id': query.value.trim(),
          'auto_sync': auto.checked,
        });
        token.value = '';
        toast('Flex settings saved', kind: 'success');
        _render();
      } catch (e) {
        toast('$e', kind: 'error');
      }
    }

    return h('div.pf-imports', [
      drop,
      h('div.pf-grid', [
        h('section.jr-section', [
          h('h3', 'Sync from IBKR (Flex Web Service)'),
          h('div.muted.small', 'Downloads your Flex Query directly. The token stays on this computer and is never shown again '
              'or given to the agent.'),
          h('label.field-col', [h('span', 'Token'), token]),
          h('label.field-col', [h('span', 'Query ID'), query]),
          h('label.row.gap.small', [auto, 'Download every day (after 7:00 New York time)']),
          h('div.row.gap', [
            h('button.btn.small.primary', 'Save', {'type': 'button'}, {'click': (_) => save()}),
            h('button.btn.small', [icon('refresh', size: 13), 'Sync now'], {'type': 'button'}, {'click': (_) => _sync()}),
            if (flex['has_token'].b == true && flex['token_from_env'].b != true)
              h('button.btn.small', 'Forget token', {'type': 'button'}, {
                'click': (_) async {
                  await request('/api/portfolio/flex', method: 'POST', body: {'clear_token': true});
                  _render();
                },
              }),
          ]),
          if (flex['last_sync'].s != null)
            h('div.small${flex['last_error'].s != null ? '.down' : '.muted'}',
                'Last sync ${_ago(flex['last_sync'].s!)}${flex['last_error'].s != null ? ': ${flex['last_error'].s}' : ' · ok'}'),
        ]),
        h('section.jr-section.pf-howto', [
          h('h3', 'Setting up the Flex Query in IBKR'),
          h('ol.small', [
            h('li', 'Client Portal → Performance & Reports → Flex Queries → Activity Flex Query → +.'),
            h('li', 'Sections: Account Information, Change in NAV, Net Asset Value (NAV) in Base (daily), Trades (Executions), '
                'Cash Transactions, Transfers, Open Positions (Summary), Cash Report.'),
            h('li', 'Format XML, period e.g. “Last 365 Calendar Days” (run it once with a custom range from your account '
                'opening for the full history), date format yyyyMMdd.'),
            h('li', 'Run it and import the file here, or use its Query ID with a Flex Web Service token (Flex Queries → '
                'Flex Web Service Configuration) to sync automatically.'),
          ]),
        ]),
      ]),
      h('section.jr-section', [
        h('h3', 'Imported statements'),
        _table([('Imported', false), ('File', false), ('Account', false), ('Period', false), ('Added', false), ('', false)], [
          for (final r in list)
            h('tr', [
              h('td', _ago(r['created_at'].s ?? '')),
              h('td', [r['filename'].s ?? '–', h('span.muted.tiny', r['source'].s == 'flex_web' ? ' · Flex Web Service' : ' · upload')]),
              h('td', r['accounts'].list.map((a) => a.s).join(', ')),
              h('td', '${r['from_date'].s ?? '?'} → ${r['to_date'].s ?? '?'}'),
              h('td.muted.small', _countsText(r['counts'])),
              h('td', h('button.icon-btn', icon('trash', size: 14), {'type': 'button', 'aria-label': 'Remove import'}, {
                'click': (_) async {
                  if (!await confirmDialog('Remove this import?',
                      'Removes the trades, cash flows, account values and positions this import added.', confirm: 'Remove', danger: true)) {
                    return;
                  }
                  await request('/api/portfolio/imports/${r['id'].s}', method: 'DELETE');
                  _render();
                },
              })),
            ]),
        ], empty: 'No statements imported yet'),
      ]),
    ]);
  }

  String _countsText(J c) {
    final parts = <String>[
      if ((c['trades'].i ?? 0) > 0) '${c['trades'].i} trades',
      if ((c['cash'].i ?? 0) > 0) '${c['cash'].i} cash flows',
      if ((c['nav'].i ?? 0) > 0) '${c['nav'].i} days of NAV',
      if ((c['positions'].i ?? 0) > 0) '${c['positions'].i} positions',
    ];
    final known = (c['trades_known'].i ?? 0) + (c['cash_known'].i ?? 0);
    return [if (parts.isEmpty) 'nothing new' else parts.join(' · '), if (known > 0) '$known already known'].join(' · ');
  }

  String _ago(String iso) {
    final t = DateTime.tryParse(iso);
    if (t == null) return iso;
    final d = DateTime.now().difference(t);
    if (d.inMinutes < 1) return 'just now';
    if (d.inHours < 1) return '${d.inMinutes}m ago';
    if (d.inDays < 1) return '${d.inHours}h ago';
    return t.toLocal().toString().substring(0, 10);
  }

  // ---------------------------------------------------------------- import actions

  void _pickFile() {
    final input = h('input', null, {'type': 'file', 'accept': '.xml,.csv,text/xml,text/csv', 'multiple': ''}) as HTMLInputElement;
    input.addEventListener('change', ((Event _) {
      final files = input.files;
      if (files != null && files.length > 0) _importFiles([for (var i = 0; i < files.length; i++) files.item(i)!]);
    }).toJS);
    input.click();
  }

  Future<void> _importFiles(List<File> files) async {
    for (final f in files) {
      try {
        final text = (await f.text().toDart).toDart;
        final r = await request('/api/portfolio/import', method: 'POST', body: {'text': text, 'filename': f.name});
        toast('Imported ${f.name}: ${_countsText(r['counts'])}', kind: 'success');
        final accts = r['accounts'].list;
        if (accts.isNotEmpty) _account = accts.first.s;
      } catch (e) {
        toast('${f.name}: $e', kind: 'error', ms: 8000);
      }
    }
    await _loadAccounts();
    _render();
  }

  Future<void> _sync() async {
    toast('Downloading the Flex statement from IBKR…');
    try {
      final r = await request('/api/portfolio/flex/sync', method: 'POST');
      toast('IBKR sync: ${_countsText(r['counts'])}', kind: 'success');
      final accts = r['accounts'].list;
      if (accts.isNotEmpty) _account = accts.first.s;
      await _loadAccounts();
      _render();
    } catch (e) {
      toast('$e', kind: 'error', ms: 8000);
      if (_tab != 'imports') selectTab('imports');
    }
  }
}
