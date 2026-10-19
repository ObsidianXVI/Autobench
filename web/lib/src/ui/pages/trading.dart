// Trading page: broker connection and safety controls (approval mode, kill switch, live arming), the
// account (net liquidation, P&L, cash vs invested vs committed, open risk), capital allocation, the
// equity curve, and tabs for positions, orders, agent proposals, trade history, fills, the activity
// log and the risk limits.
import 'dart:async';

import 'package:web/web.dart';

import '../../chart/engine.dart';
import '../../core/format.dart';
import '../../core/json.dart';
import '../../core/models.dart';
import '../../core/trading.dart';
import '../api.dart';
import '../dom.dart';
import '../kit/icons.dart';
import '../kit/overlay.dart';
import '../kit/state.dart';
import '../store.dart';
import '../trading_state.dart';
import '../trading_ui.dart';

const _tabs = [
  ('positions', 'Positions'),
  ('orders', 'Orders'),
  ('proposals', 'Proposals'),
  ('history', 'History'),
  ('fills', 'Fills'),
  ('activity', 'Activity log'),
  ('risk', 'Risk & settings'),
];

const _palette = ['#2f7cf6', '#12b886', '#f59e0b', '#a78bfa', '#06b6d4', '#ec4899', '#84cc16', '#f97316', '#64748b'];

class TradingPage {
  final HTMLElement root = h('div.page.trading-page');
  final HTMLElement _head = h('div.tr-head');
  final HTMLElement _banner = h('div.tr-banner');
  final HTMLElement _kpis = h('div.tr-kpis');
  final HTMLElement _alloc = h('section.tr-card.tr-alloc');
  final HTMLElement _equity = h('section.tr-card.tr-equity');
  final HTMLElement _tabBar = h('div.tr-tabs');
  final HTMLElement _body = h('div.tr-body');
  final Scope _scope = Scope();
  final _reload = Debouncer(300);
  J? _ov;
  bool _includeDone = false;
  bool _loaded = false;
  ChartEngine? _eqEngine;
  final HTMLElement _eqHost = h('div.tr-eq-chart');

  String get _tab => store.pref<String>('trading.tab');

  TradingPage() {
    append(root, [
      _head,
      _banner,
      _kpis,
      h('div.tr-grid', [_alloc, _equity]),
      h('section.tr-card.tr-main', [_tabBar, _body]),
    ]);
    _scope.listen(trade.status, (_) {
      _renderHead();
      if (!root.isHidden) _reload(_load);
    });
    _scope.listen(trade.pending, (_) => _renderTabs());
    _scope.listen(trade.version, (_) {
      if (!root.isHidden) _reload(_load);
    });
  }

  void onShow() {
    trade.start();
    _renderHead();
    _renderTabs();
    _load();
    if (!_loaded) {
      _loaded = true;
      _loadEquity();
    }
  }

  void _goChart(String sym) {
    store.activeChart.symbol.value = sym;
    store.page.value = 'chart';
  }

  // ---------------------------------------------------------------- header & safety controls

  void _renderHead() {
    final st = trade.status.value;
    final conn = st == null
        ? h('span.muted', 'Loading…')
        : h('div.tr-conn', [
            h('span.dot${st.connected ? '.ok' : '.warn'}'),
            h('b', st.brokerLabel),
            if (st.connected) h('span.badge.${st.isLive ? 'down' : 'up'}', st.isLive ? 'LIVE' : 'PAPER'),
            if (st.connected && st.multiAccount)
              _accountPicker(st)
            else
              h('span.muted.small', st.connected ? (st.account ?? '') : (st.message ?? 'Not connected')),
          ]);
    replace(_head, [
      h('h2', 'Trading'),
      conn,
      h('button.btn.small', [icon('settings', size: 14), 'Connection'], {'type': 'button', 'aria-label': 'Broker connection'},
          {'click': (_) => _connectionDialog()}),
      h('span.spacer'),
      if (st != null) ...[
        h('div.tr-mode', [
          h('span.muted.small', 'Agent orders'),
          h('div.seg', [
            for (final (k, l, tip) in [
              ('confirm', 'Confirm each', 'You approve every order the agent proposes'),
              ('auto', 'Auto (paper)', 'Agent orders that pass the risk limits are sent at once — paper accounts only'),
            ])
              h('button.seg-btn${st.mode == k ? '.on' : ''}', l, {'type': 'button', 'data-tip': tip, 'aria-label': 'Mode $l'},
                  {'click': (_) => _setMode(k)}),
          ]),
        ]),
        if (st.isLive && st.liveAllowed)
          h('button.btn.small${st.armed ? '.danger' : ''}', [icon('shield', size: 14), st.armed ? 'Live armed · disarm' : 'Arm live trading'],
              {'type': 'button'}, {'click': (_) => _toggleArm(st)}),
        if (st.halted)
          h('button.btn.small.primary', [icon('play', size: 14), 'Resume trading'], {'type': 'button', 'aria-label': 'Resume trading'},
              {'click': (_) => _resume()})
        else
          h('button.btn.small.kill', [icon('octagon', size: 14), 'Kill switch'], {'type': 'button', 'aria-label': 'Kill switch'},
              {'click': (e) => _killMenu(e.currentTarget as Element)}),
      ],
    ]);
    if (st != null && st.halted) {
      final hj = st.halt;
      replace(_banner, h('div.check-msg.error', [
        h('b', 'Trading halted'),
        ' ${hj['at'].s != null ? 'at ${_time(hj['at'].s!)} ' : ''}by ${hj['by'].s ?? '?'}${hj['reason'].s != null ? ' — ${hj['reason'].s}' : ''}. '
            'New orders are blocked; you can still close positions.',
      ]));
    } else if (st != null && st.isLive && !st.canTrade) {
      replace(_banner, h('div.check-msg.warn', st.liveAllowed
          ? 'LIVE account connected. Trading is disabled until you arm it for this session.'
          : 'LIVE account connected in read-only mode. Set AUTOBENCH_ALLOW_LIVE_TRADING=1 and restart to enable live trading.'));
    } else if (st != null && !st.connected) {
      replace(_banner, h('div.check-msg.warn', [
        h('b', '${st.brokerLabel} not connected. '),
        st.kind == 'ibkr'
            ? 'Start IB Gateway (or TWS), log in to your paper account, and enable the API on port ${st.connection['port'].i ?? 4002} '
                '(Configure → Settings → API: enable socket clients, trusted IP 127.0.0.1, uncheck Read-Only API). It reconnects automatically.'
            : (st.message ?? ''),
      ]));
    } else {
      replace(_banner, null);
    }
  }

  Future<void> _setMode(String mode) async {
    if (mode == 'auto' &&
        !await confirmDialog('Auto mode',
            'Orders the agent proposes will be sent to the broker without asking you, as long as they pass every risk limit. '
                'This only applies to paper accounts; live accounts always need your approval.',
            confirm: 'Enable auto mode')) {
      return;
    }
    try {
      await trade.saveSettings({'mode': mode});
      await trade.refreshStatus();
    } catch (e) {
      toast('$e', kind: 'error');
    }
  }

  Future<void> _toggleArm(TStatus st) async {
    try {
      if (st.armed) {
        await trade.disarm();
      } else {
        final typed = await promptDialog('Arm LIVE trading',
            label: 'Orders will use real money on ${st.account}. Type LIVE to arm for this server session.', confirm: 'Arm');
        if (typed != 'LIVE') return;
        await trade.arm();
      }
      await trade.refreshStatus();
    } catch (e) {
      toast('$e', kind: 'error', ms: 8000);
    }
  }

  void _killMenu(Element anchor) {
    showMenu([
      MenuItem('Halt new orders', iconName: 'pause', hint: 'Cancels working entry orders and pending proposals; stops stay',
          onSelect: () async {
        final reason = await promptDialog('Halt trading', label: 'Reason (optional)', confirm: 'Halt');
        if (reason == null) return;
        await _halt(false, reason);
      }),
      MenuItem('Halt & flatten everything', iconName: 'octagon', danger: true,
          hint: 'Also cancels every order and closes all positions at market', onSelect: () async {
        if (!await confirmDialog('Flatten everything',
            'Cancel every working order and close ALL positions at market now?', confirm: 'Flatten', danger: true)) {
          return;
        }
        await _halt(true, 'Flatten');
      }),
    ], anchor: anchor, align: 'end');
  }

  Future<void> _halt(bool flatten, String? reason) async {
    try {
      final r = await trade.halt(flatten: flatten, reason: reason?.isEmpty ?? true ? null : reason);
      toast('${r['cancelled_orders'].list.length} order(s) cancelled${flatten ? ', ${r['closing_tickets'].list.length} position(s) closing' : ''}',
          title: 'Trading halted', kind: 'warn', ms: 6000);
      await trade.refreshStatus();
    } catch (e) {
      toast('$e', kind: 'error');
    }
  }

  Future<void> _resume() async {
    try {
      await trade.resume();
      await trade.refreshStatus();
      toast('Trading resumed', kind: 'success', ms: 2500);
    } catch (e) {
      toast('$e', kind: 'error');
    }
  }

  String _accountLabel((String, bool?) a) => '${a.$1} · ${a.$2 == false ? 'LIVE' : 'paper'}';

  /// The accounts of the connected login; switching re-points the page, order ticket, chart and agent.
  HTMLElement _accountPicker(TStatus st) {
    late final HTMLSelectElement sel;
    sel = h('select.input.select.small', [
      for (final a in st.accounts) h('option', _accountLabel(a), {'value': a.$1}),
    ], {'aria-label': 'Account', 'data-tip': 'Account used for orders, positions and agent proposals'},
        {'change': (_) => _switchAccount(sel.value)}) as HTMLSelectElement;
    sel.value = st.account ?? '';
    return sel;
  }

  Future<void> _switchAccount(String account) async {
    try {
      await trade.selectAccount(account);
      final st = trade.status.value;
      toast('Using account $account${st?.isLive == true ? ' (LIVE, read-only until armed)' : ''}', ms: 2500);
    } catch (e) {
      toast('$e', kind: 'error');
      _renderHead();
    }
  }

  void _connectionDialog() {
    final st = trade.status.value;
    final c = st?.connection ?? const J(null);
    var broker = c['broker'].s ?? 'ibkr';
    final host = h('input.input', null, {'value': c['host'].s ?? '127.0.0.1', 'aria-label': 'Host'}) as HTMLInputElement;
    final port = h('input.input', null, {'type': 'number', 'value': '${c['port'].i ?? 4002}', 'aria-label': 'Port'}) as HTMLInputElement;
    final client = h('input.input', null, {'type': 'number', 'value': '${c['client_id'].i ?? 7}', 'aria-label': 'Client id'}) as HTMLInputElement;
    // Accounts are only known once a login is connected; a saved account missing from the next login falls
    // back to that login's first account.
    final accts = st?.accounts ?? const <(String, bool?)>[];
    final account = h('select.input.select', [
      h('option', accts.isEmpty ? 'First account in the login' : 'Default (first account)', {'value': ''}),
      for (final a in accts) h('option', _accountLabel(a), {'value': a.$1}),
    ], {'aria-label': 'Account'}) as HTMLSelectElement;
    account.value = st?.account ?? c['account'].s ?? '';
    final accountRow = h('label.field-col', [
      h('span', 'Account'),
      account,
      h('span.muted.tiny', accts.length > 1
          ? 'This login holds ${accts.length} accounts. You can also switch from the Trading page header.'
          : 'Logins with several accounts (e.g. individual + IRA, or advisor sub-accounts) can pick one here once connected.'),
    ]);
    final ibkr = h('div.form-grid', [
      h('div.row.gap', [
        h('label.field-col', [h('span', 'Host'), host]),
        h('label.field-col', [h('span', 'Port'), port]),
        h('label.field-col', [h('span', 'Client id'), client]),
      ]),
      h('div.row.gap.wrap', [
        for (final (p, l) in [(4002, 'Gateway paper'), (4001, 'Gateway live'), (7497, 'TWS paper'), (7496, 'TWS live')])
          h('button.btn.small', '$l · $p', {'type': 'button'}, {'click': (_) => port.value = '$p'}),
      ]),
      h('div.muted.small', 'In IB Gateway/TWS: Configure → Settings → API → Settings: enable ActiveX and Socket Clients, '
          'add 127.0.0.1 to trusted IPs, uncheck Read-Only API. Paper accounts (DU…) trade within your risk limits; '
          'live accounts are read-only unless AUTOBENCH_ALLOW_LIVE_TRADING=1 and armed.'),
    ]);
    final sim = h('div.form-grid', [
      h('div.muted.small', 'A local paper account that fills orders at the latest quote. Good for trying the workflow.'),
      h('button.btn.small', 'Reset this simulator account to \$100,000…', {'type': 'button'}, {
        'click': (_) async {
          if (!await confirmDialog('Reset simulator', 'Delete this simulated account\'s positions, orders, fills and history?', confirm: 'Reset', danger: true)) return;
          await attempt(trade.simReset(100000));
          toast('Simulator reset', ms: 2000);
        },
      }),
    ]);
    final seg = h('div.seg');
    void renderSeg() {
      replace(seg, [
        for (final (k, l) in [('ibkr', 'Interactive Brokers'), ('sim', 'Simulator'), ('none', 'None')])
          h('button.seg-btn${broker == k ? '.on' : ''}', l, {'type': 'button'}, {
            'click': (_) {
              broker = k;
              renderSeg();
            },
          }),
      ]);
      ibkr.isHidden = broker != 'ibkr';
      sim.isHidden = broker != 'sim';
      accountRow.isHidden = broker == 'none' || (broker != (st?.kind ?? '') && accts.isNotEmpty);
    }

    renderSeg();
    Dialog.open('Broker connection', h('div.form-grid', [seg, ibkr, sim, accountRow]), width: '560px', actions: [
      const DialogAction('Cancel'),
      DialogAction('Connect', primary: true, onPressed: () async {
        try {
          await trade.connect(broker, host: host.value, port: int.tryParse(port.value), clientId: int.tryParse(client.value),
              account: account.value.isEmpty ? null : account.value);
          await Future<void>.delayed(const Duration(milliseconds: 800));
          await trade.refresh();
          return true;
        } catch (e) {
          toast('$e', kind: 'error');
          return false;
        }
      }),
    ]);
  }

  // ---------------------------------------------------------------- account, allocation, equity

  Future<void> _load() async {
    final ov = await attempt(trade.overview());
    _ov = ov;
    _renderKpis();
    _renderAlloc();
    _renderTab();
  }

  void _renderKpis() {
    final a = _ov?['account'];
    final c = _ov?['capital'] ?? const J(null);
    if (a == null || a.isNull) {
      replace(_kpis, null);
      return;
    }
    final cur = a['currency'].s ?? 'USD';
    String m(double? v, {bool sign = false}) =>
        cur == 'USD' ? money(v, sign: sign) : '${money(v, sign: sign).replaceFirst('\$', '')} $cur';
    HTMLElement kpi(String label, String value, {String? cls, String? sub, String? tip}) => h('div.tr-kpi', [
          h('span.muted.small', label),
          h('b${cls != null ? '.$cls' : ''}', value),
          if (sub != null) h('span.muted.tiny', sub),
        ], {if (tip != null) 'data-tip': tip});
    final day = a['daily_pnl'].d, unreal = a['unrealized_pnl'].d, real = a['realized_pnl'].d;
    final net = a['net_liquidation'].d;
    replace(_kpis, [
      kpi('Net liquidation', m(net), tip: 'Account value: cash plus positions at market'),
      kpi('Day P&L', m(day, sign: true), cls: (day ?? 0) >= 0 ? 'up' : 'down',
          sub: net != null && day != null && net != 0 ? pct(day / (net - day) * 100) : null),
      kpi('Unrealized', m(unreal, sign: true), cls: (unreal ?? 0) >= 0 ? 'up' : 'down'),
      kpi('Realized', m(real, sign: true), cls: (real ?? 0) >= 0 ? 'up' : 'down'),
      kpi('Cash', m(c['cash'].d), sub: c['cash_pct'].d != null ? '${n(c['cash_pct'].d, 1)}%' : null),
      kpi('Invested', m(c['invested'].d), sub: c['invested_pct'].d != null ? '${n(c['invested_pct'].d, 1)}%' : null,
          tip: 'Gross market value of open positions (long + short)'),
      kpi('In open orders', m(c['committed_to_orders'].d), tip: 'Cash reserved by working buy entry orders'),
      kpi('Free cash', m(c['free_cash'].d), tip: 'Cash not committed to working orders'),
      kpi('Open risk', m(c['open_risk'].d), cls: 'down', tip: 'Loss if every position hits its stop'),
      kpi('Buying power', m(a['buying_power'].d)),
    ]);
  }

  void _renderAlloc() {
    final c = _ov?['capital'];
    final positions = [for (final p in _ov?['positions'].list ?? const <J>[]) TPosition.fromJson(p)];
    if (c == null || c.isNull) {
      replace(_alloc, [h('h3', 'Capital'), h('div.muted.small', 'Connect a broker to see your account.')]);
      return;
    }
    final net = c['net_liquidation'].d ?? 0;
    final committed = c['committed_to_orders'].d ?? 0;
    final free = (c['free_cash'].d ?? 0).clamp(0, double.infinity).toDouble();
    final parts = <(String, double, String, String)>[
      for (final (i, p) in (positions..sort((a, b) => (b.marketValue ?? 0).abs().compareTo((a.marketValue ?? 0).abs()))).indexed)
        (p.symbol, (p.marketValue ?? 0).abs(), _palette[i % (_palette.length - 1)], '${p.isLong ? 'Long' : 'Short'} ${qtyText(p.qty.abs())}'),
      if (committed > 0) ('Open orders', committed, 'hatch', 'Reserved by working buy orders'),
      if (free > 0) ('Free cash', free, 'cash', 'Not invested or committed'),
    ];
    final total = parts.fold<double>(0, (s, p) => s + p.$2);
    final unprotected = [for (final x in c['unprotected_positions'].list) x.s ?? ''];
    replace(_alloc, [
      h('div.title-row', [h('h3', 'Capital allocation'), h('span.muted.small', net > 0 ? 'of ${money(net, sign: false)}' : '')]),
      h('div.alloc-bar', [
        for (final (label, v, color, tip) in parts)
          if (total > 0 && v > 0)
            h('div.alloc-seg${color == 'hatch' ? '.hatch' : color == 'cash' ? '.cash' : ''}', null, {
              'style': 'flex-grow:${v / total}${color.startsWith('#') ? ';background:$color' : ''}',
              'data-tip': '$label · ${money(v, sign: false)} · ${n(net > 0 ? v / net * 100 : 0, 1)}% — $tip',
            }),
      ]),
      h('div.alloc-legend', [
        for (final (label, v, color, _) in parts)
          h('span.alloc-item', [
            h('span.swatch${color == 'hatch' ? '.hatch' : color == 'cash' ? '.cash' : ''}', null,
                {if (color.startsWith('#')) 'style': 'background:$color'}),
            h(label == 'Open orders' || label == 'Free cash' ? 'span' : 'a.link', label, null,
                label == 'Open orders' || label == 'Free cash' ? null : {'click': (_) => _goChart(label)}),
            h('span.muted', ' ${n(net > 0 ? v / net * 100 : 0, 1)}%'),
          ]),
      ]),
      if (unprotected.isNotEmpty)
        h('div.check-msg.warn', ['No stop-loss on: ', for (final s in unprotected) h('a.link', '$s ', null, {'click': (_) => _goChart(s)})]),
    ]);
  }

  Future<void> _loadEquity() async {
    final days = store.pref<int>('trading.equityDays');
    final pts = await attempt(trade.equity(days));
    final list = pts?.list ?? const <J>[];
    final head = h('div.title-row', [
      h('h3', 'Equity'),
      h('div.seg', [
        for (final (d, l) in [(7, '1W'), (30, '1M'), (90, '3M'), (365, '1Y'), (3650, 'All')])
          h('button.seg-btn${days == d ? '.on' : ''}', l, {'type': 'button'}, {
            'click': (_) {
              store.setPref('trading.equityDays', d);
              _loadEquity();
            },
          }),
      ]),
    ]);
    if (list.length < 2) {
      replace(_equity, [head, h('div.muted.small.pad', 'The equity curve fills in as the account is snapshotted (every 5 minutes while connected, and after fills).')]);
      return;
    }
    final first = list.first['net_liquidation'].d ?? 0, last = list.last['net_liquidation'].d ?? 0;
    replace(_equity, [
      head,
      h('div.row.gap.small', [
        h('b', money(last, sign: false)),
        h('span.${last >= first ? 'up' : 'down'}', '${money(last - first)} (${pct(first == 0 ? 0 : (last / first - 1) * 100)})'),
        h('span.muted', 'over ${days >= 3650 ? 'all time' : '$days days'}'),
      ]),
      _eqHost,
    ]);
    final bars = [
      for (final p in list) Bar(p['t'].i!, p['net_liquidation'].d!, p['net_liquidation'].d!, p['net_liquidation'].d!, p['net_liquidation'].d!, 0),
    ];
    Timer.run(() {
      final e = _eqEngine ??= (ChartEngine(_eqHost)
        ..showVolume = false
        ..chartType = ChartType.area);
      e.setBars(bars, intraday: true);
      e.setVisibleRange(0, bars.length + 1.0);
    });
  }

  // ---------------------------------------------------------------- tabs

  void _renderTabs() {
    final pending = trade.pending.value.length;
    replace(_tabBar, [
      for (final (k, l) in _tabs)
        h('button.tab${_tab == k ? '.on' : ''}', [
          l,
          if (k == 'proposals' && pending > 0) h('span.nav-badge.agent', '$pending'),
        ], {'type': 'button', 'aria-label': l}, {
          'click': (_) {
            store.setPref('trading.tab', k);
            _renderTabs();
            _renderTab();
          },
        }),
    ]);
  }

  Future<void> _renderTab() async {
    final tab = _tab;
    final st = trade.status.value;
    if (tab != 'risk' && tab != 'proposals' && tab != 'activity' && st != null && !st.connected && tab != 'history' && tab != 'fills') {
      replace(_body, h('div.empty-state', [h('div', 'Not connected'), h('div.small', st.message ?? '')]));
      return;
    }
    switch (tab) {
      case 'positions':
        _renderPositions();
      case 'orders':
        await _renderOrders();
      case 'proposals':
        await _renderProposals();
      case 'history':
        await _renderHistory();
      case 'fills':
        await _renderFills();
      case 'activity':
        await _renderActivity();
      case 'risk':
        await _renderRisk();
    }
  }

  HTMLElement _table(List<(String, bool)> cols, List<HTMLElement> rows, {String empty = 'Nothing here yet'}) =>
      rows.isEmpty
          ? h('div.empty-state', h('div', empty))
          : h('div.table-wrap', h('table.data-table.compact', [
              h('thead', h('tr', [for (final (c, num) in cols) h('th${num ? '.num' : ''}', c)])),
              h('tbody', rows),
            ]));

  HTMLElement _symCell(String sym) => h('td.sym', h('a', sym, {'href': '#chart'}, {
        'click': (e) {
          e.preventDefault();
          _goChart(sym);
        },
      }));

  void _renderPositions() {
    final positions = [for (final p in _ov?['positions'].list ?? const <J>[]) TPosition.fromJson(p)];
    replace(_body, _table([
      ('Symbol', false), ('Side', false), ('Qty', true), ('Avg cost', true), ('Last', true), ('Market value', true),
      ('Weight', true), ('Unrealized', true), ('%', true), ('Stop', true), ('Target', true), ('Risk to stop', true), ('', false),
    ], [
      for (final p in positions)
        h('tr', [
          _symCell(p.symbol),
          h('td', h('span.badge.${p.isLong ? 'up' : 'down'}', p.isLong ? 'Long' : 'Short')),
          h('td.num', qtyText(p.qty.abs())),
          h('td.num', fmtPrice(p.avgCost)),
          h('td.num', p.marketPrice == null ? '–' : fmtPrice(p.marketPrice!)),
          h('td.num', money(p.marketValue, sign: false)),
          h('td.num', p.weightPct == null ? '–' : '${n(p.weightPct, 1)}%'),
          h('td.num.${(p.unrealizedPnl ?? 0) >= 0 ? 'up' : 'down'}', money(p.unrealizedPnl)),
          h('td.num.${(p.unrealizedPct ?? 0) >= 0 ? 'up' : 'down'}', pct(p.unrealizedPct)),
          h('td.num${p.stop == null ? '.warn-text' : ''}', p.stop == null ? 'none' : fmtPrice(p.stop!)),
          h('td.num', p.target == null ? '–' : fmtPrice(p.target!)),
          h('td.num', money(p.riskToStop, sign: false)),
          h('td.actions', [
            h('button.btn.small', 'Trade', {'type': 'button', 'aria-label': 'Trade ${p.symbol}'}, {
              'click': (_) {
                _goChart(p.symbol);
                store.panel.value = 'trade';
              },
            }),
            h('button.btn.small', 'Close', {'type': 'button', 'aria-label': 'Close ${p.symbol}'}, {'click': (_) => closePositionDialog(p)}),
          ]),
        ], {'data-symbol': p.symbol}),
    ], empty: 'No open positions'));
  }

  Future<void> _renderOrders() async {
    final res = await attempt(request('/api/trading/orders?include_done=$_includeDone'));
    final orders = [for (final o in res?.list ?? const <J>[]) TOrder.fromJson(o)];
    final positions = {for (final p in _ov?['positions'].list ?? const <J>[]) p['symbol'].s};
    replace(_body, [
      h('div.tr-tools', [
        h('label.check-row', [
          h('input', null, {'type': 'checkbox', if (_includeDone) 'checked': '', 'aria-label': 'Include filled and cancelled'}, {
            'change': (e) {
              _includeDone = (e.target as HTMLInputElement).checked;
              _renderOrders();
            },
          }),
          'Include filled & cancelled (this session)',
        ]),
      ]),
      _table([
        ('Updated', false), ('Symbol', false), ('Role', false), ('Side', false), ('Type', false), ('Qty', true), ('Filled', true),
        ('Limit', true), ('Stop', true), ('TIF', false), ('Status', false), ('Source', false), ('', false),
      ], [
        for (final o in orders)
          h('tr${o.held ? '.held' : ''}', [
            h('td.muted', _ts(o.updatedAt)),
            _symCell(o.symbol),
            h('td', o.role == null ? '–' : o.roleLabel),
            h('td.${o.isBuy ? 'up' : 'down'}', o.side.toUpperCase()),
            h('td', o.typeLabel),
            h('td.num', qtyText(o.qty)),
            h('td.num', o.filledQty > 0 ? '${qtyText(o.filledQty)} @ ${fmtPrice(o.avgFillPrice ?? 0)}' : '–'),
            h('td.num', o.limitPrice == null ? '–' : fmtPrice(o.limitPrice!)),
            h('td.num', o.stopPrice == null ? '–' : fmtPrice(o.stopPrice!)),
            h('td', o.tif.toUpperCase()),
            h('td', [statusTag(o.held ? 'held' : o.status), if (o.message != null && !o.isOpen) h('span.muted.tiny', ' ${o.message}')]),
            h('td.muted', o.source == 'external' ? 'TWS/other' : 'Autobench'),
            h('td.actions', o.isOpen
                ? [
                    h('button.btn.small', 'Modify', {'type': 'button'}, {'click': (_) => modifyOrderDialog(o)}),
                    h('button.btn.small', 'Cancel', {'type': 'button', 'aria-label': 'Cancel order ${o.id}'},
                        {'click': (_) => cancelOrder(o, protectsPosition: positions.contains(o.symbol))}),
                  ]
                : null),
          ]),
      ], empty: _includeDone ? 'No orders this session' : 'No working orders'),
    ]);
  }

  Future<void> _renderProposals() async {
    final all = await attempt(trade.tickets(limit: 100));
    final tickets = [for (final t in all?.list ?? const <J>[]) TTicket.fromJson(t)];
    final pending = tickets.where((t) => t.status == 'pending').toList();
    final decided = tickets.where((t) => t.status != 'pending').toList();
    replace(_body, [
      h('div.tr-sub', 'Awaiting your approval'),
      if (pending.isEmpty)
        h('div.muted.small.pad', trade.status.value?.mode == 'auto'
            ? 'Auto mode: agent orders that pass the limits are sent directly (paper only).'
            : 'No proposals. Agent orders appear here for you to approve or reject.'),
      h('div.proposal-grid', [
        for (final t in pending)
          h('div.proposal-card', [
            h('div.row.gap', [
              h('span.badge-agent', t.source == 'agent' ? '✦ AI' : 'You'),
              h('b.${t.side == 'buy' ? 'up' : 'down'}', t.summary),
              h('span.spacer'),
              h('span.muted.tiny', t.expiresAt == null ? '' : 'expires ${_time(t.expiresAt!)}'),
            ]),
            if (trade.status.value?.multiAccount == true || trade.otherAccount(t))
              h('div.small${trade.otherAccount(t) ? '.warn-text' : '.muted'}',
                  trade.otherAccount(t) ? 'For account ${t.account}; switch to it to approve' : 'Account ${t.account ?? ''}'),
            h('div.row.gap.small', [
              if (t.stopLoss != null) h('span', ['Stop ', h('b.down', fmtPrice(t.stopLoss!))]),
              if (t.takeProfit != null) h('span', ['Target ', h('b.up', fmtPrice(t.takeProfit!))]),
              if (t.check['estimate']['risk_value'].d != null) h('span.muted', 'risk ${money(t.check['estimate']['risk_value'].d, sign: false)}'),
              if (t.check['estimate']['reward_risk'].d != null) h('span.muted', '${n(t.check['estimate']['reward_risk'].d, 1)}R'),
            ]),
            if ((t.rationale ?? '').isNotEmpty) h('div.proposal-why', t.rationale!.length > 280 ? '${t.rationale!.substring(0, 280)}…' : t.rationale!),
            h('div.row.gap', [
              h('button.btn.small', 'Chart', {'type': 'button'}, {'click': (_) => _goChart(t.symbol)}),
              h('span.spacer'),
              h('button.btn.small.primary', 'Review & approve', {'type': 'button', 'aria-label': 'Review ${t.summary}'},
                  {'click': (_) => reviewTicket(t)}),
            ]),
          ]),
      ]),
      h('div.tr-sub', 'Recent tickets'),
      _table([('Created', false), ('Source', false), ('Order', false), ('Status', false), ('Note', false)], [
        for (final t in decided)
          h('tr', [
            h('td.muted', t.createdAt == null ? '' : _time(t.createdAt!)),
            h('td', t.source == 'agent' ? h('span.badge-agent', '✦ AI') : 'You'),
            h('td', t.summary),
            h('td', statusTag(t.status)),
            h('td.muted.ellipsis', t.message ?? (t.rationale ?? '')),
          ], null, {'click': (_) => _goChart(t.symbol)}),
      ], empty: 'No tickets yet'),
    ]);
  }

  Future<void> _renderHistory() async {
    final res = await attempt(trade.trades());
    if (res == null) {
      replace(_body, h('div.empty-state', h('div', 'History unavailable')));
      return;
    }
    final trades = [for (final t in res['trades'].list) TTrade.fromJson(t)];
    final s = res['stats'];
    HTMLElement stat(String k, String v, {String? cls}) => h('div.tr-kpi.small', [h('span.muted.small', k), h('b${cls != null ? '.$cls' : ''}', v)]);
    final net = s['net_pnl'].d ?? 0;
    replace(_body, [
      h('div.tr-kpis.stats', [
        stat('Net P&L (closed)', money(net), cls: net >= 0 ? 'up' : 'down'),
        stat('Trades', '${s['closed_trades'].i ?? 0} closed · ${s['open_trades'].i ?? 0} open'),
        stat('Win rate', s['win_rate'].d == null ? '–' : '${n(s['win_rate'].d, 1)}%'),
        stat('Profit factor', n(s['profit_factor'].d)),
        stat('Expectancy', money(s['expectancy'].d)),
        stat('Avg R', n(s['avg_r'].d)),
        stat('Avg win / loss', '${money(s['avg_win'].d, sign: false)} / ${money(s['avg_loss'].d, sign: false)}'),
        stat('Avg hold', s['avg_hold_hours'].d == null ? '–' : '${n(s['avg_hold_hours'].d, 1)} h'),
        stat('Commissions', money(s['commission'].d, sign: false)),
      ]),
      _table([
        ('Opened', false), ('Closed', false), ('Symbol', false), ('Side', false), ('Qty', true), ('Entry', true), ('Exit', true),
        ('Net P&L', true), ('Return', true), ('R', true), ('Held', true),
      ], [
        for (final t in trades)
          h('tr', [
            h('td.muted', _ts(t.openedAt)),
            h('td.muted', t.closedAt == null ? h('span.badge.info', 'open') : _ts(t.closedAt!)),
            _symCell(t.symbol),
            h('td', h('span.badge.${t.isLong ? 'up' : 'down'}', t.side)),
            h('td.num', qtyText(t.maxQty)),
            h('td.num', t.entryAvg == null ? '–' : fmtPrice(t.entryAvg!)),
            h('td.num', t.exitAvg == null ? '–' : fmtPrice(t.exitAvg!)),
            h('td.num.${(t.closed ? t.netPnl : (t.unrealizedPnl ?? 0)) >= 0 ? 'up' : 'down'}',
                t.closed ? money(t.netPnl) : '${money(t.unrealizedPnl)} unreal.'),
            h('td.num', pct(t.returnPct)),
            h('td.num', t.rMultiple == null ? '–' : '${n(t.rMultiple, 2)}R'),
            h('td.num.muted', holdingText(t.holdingSeconds)),
          ], {'data-trade': t.id}, {'click': (_) => _goChart(t.symbol)}),
      ], empty: 'No trades yet. Fills are recorded here as they happen.'),
    ]);
  }

  Future<void> _renderFills() async {
    final res = await attempt(trade.fills());
    final fills = [for (final f in res?.list ?? const <J>[]) TFill.fromJson(f)];
    replace(_body, _table([
      ('Time', false), ('Symbol', false), ('Side', false), ('Qty', true), ('Price', true), ('Value', true), ('Commission', true),
      ('Realized', true), ('Role', false), ('Order', false),
    ], [
      for (final f in fills)
        h('tr', [
          h('td.muted', _ts(f.time)),
          _symCell(f.symbol),
          h('td.${f.isBuy ? 'up' : 'down'}', f.isBuy ? 'BOT' : 'SLD'),
          h('td.num', qtyText(f.qty)),
          h('td.num', fmtPrice(f.price)),
          h('td.num', money(f.qty * f.price, sign: false)),
          h('td.num', money(f.commission, sign: false)),
          h('td.num.${(f.realizedPnl ?? 0) >= 0 ? 'up' : 'down'}', f.realizedPnl == null ? '–' : money(f.realizedPnl)),
          h('td', f.role ?? 'external'),
          h('td.muted', f.orderId ?? ''),
        ]),
    ], empty: 'No fills recorded'));
  }

  Future<void> _renderActivity() async {
    final res = await attempt(trade.audit());
    String detail(J d) {
      final m = d.map;
      if (m.containsKey('spec')) {
        final s = d['spec'];
        return '${s['side'].s?.toUpperCase()} ${qtyText(s['qty'].d ?? 0)} ${s['order_type'].s}'
            '${s['limit_price'].d != null ? ' @ ${s['limit_price'].d}' : ''}${s['stop_loss'].d != null ? ' SL ${s['stop_loss'].d}' : ''}';
      }
      return [
        for (final e in m.entries)
          if (!e.value.isNull && e.value.v is! Map && e.value.v is! List) '${e.key}=${e.value.s}'
      ].join(' ');
    }

    replace(_body, _table([('Time', false), ('Who', false), ('Action', false), ('Symbol', false), ('Details', false)], [
      for (final a in res?.list ?? const <J>[])
        h('tr', [
          h('td.muted', _time(a['time'].s ?? '')),
          h('td', a['actor'].s == 'agent' ? h('span.badge-agent', '✦ agent') : a['actor'].s ?? ''),
          h('td', (a['action'].s ?? '').replaceAll('_', ' ')),
          h('td', a['symbol'].s ?? ''),
          h('td.muted.ellipsis', detail(a['details'])),
        ]),
    ], empty: 'No activity yet'));
  }

  Future<void> _renderRisk() async {
    final s = await attempt(trade.settings());
    if (s == null) {
      replace(_body, h('div.empty-state', h('div', 'Settings unavailable')));
      return;
    }
    final l = s['limits'];
    final inputs = <String, HTMLInputElement>{};
    HTMLElement numField(String key, String label, String help, {String step = '1'}) {
      final el = h('input.input', null, {'type': 'number', 'step': step, 'value': l[key].s ?? '', 'aria-label': label}) as HTMLInputElement;
      inputs[key] = el;
      return h('label.risk-field', [h('span', label), el, h('span.muted.tiny', help)]);
    }

    HTMLElement boolField(String key, String label, String help) {
      final el = h('input', null, {'type': 'checkbox', if (l[key].b ?? false) 'checked': '', 'aria-label': label}) as HTMLInputElement;
      inputs[key] = el;
      return h('label.risk-field.check', [h('span.check-row', [el, h('span', label)]), h('span.muted.tiny', help)]);
    }

    final allow = h('input.input', null, {'value': [for (final x in l['symbol_allowlist'].list) x.s].join(', '), 'aria-label': 'Allowed symbols',
        'placeholder': 'any'}) as HTMLInputElement;
    final block = h('input.input', null, {'value': [for (final x in l['symbol_blocklist'].list) x.s].join(', '), 'aria-label': 'Blocked symbols'})
        as HTMLInputElement;
    final ttl = h('input.input', null, {'type': 'number', 'value': '${s['ticket_ttl_minutes'].i ?? 240}', 'aria-label': 'Proposal expiry minutes'})
        as HTMLInputElement;
    replace(_body, h('div.risk-form', [
      h('div.muted.small', 'Every order — yours and the agent\'s — is checked against these limits on the server. '
          'Orders that reduce a position skip the sizing limits so you can always get out. The agent cannot change them.'),
      h('div.risk-grid', [
        numField('max_order_value', 'Max order value (\$)', 'Largest order that opens or adds to a position', step: '100'),
        numField('max_position_pct', 'Max position (% of equity)', 'Including working entry orders', step: '0.5'),
        numField('max_risk_pct', 'Max risk per order (% of equity)', 'Entry-to-stop loss on one order', step: '0.1'),
        numField('max_daily_loss_pct', 'Daily loss limit (%)', 'No new positions after losing this much today', step: '0.5'),
        numField('max_orders_per_day', 'Max orders per day', 'Orders sent to the broker'),
        h('label.risk-field', [h('span', 'Proposal expiry (minutes)'), ttl, h('span.muted.tiny', 'Unapproved agent proposals expire')]),
        boolField('require_stop_for_agent', 'Agent entries need a stop-loss', 'Recommended'),
        boolField('allow_short', 'Allow short selling', 'Opening short positions'),
        boolField('allow_market_entries', 'Allow market orders to open', 'Market orders can always close'),
        boolField('allow_outside_rth', 'Allow outside regular hours', 'Pre-market / after-hours orders'),
        h('label.risk-field', [h('span', 'Only these symbols'), allow, h('span.muted.tiny', 'Comma-separated; empty = any')]),
        h('label.risk-field', [h('span', 'Never these symbols'), block, h('span.muted.tiny', 'Comma-separated')]),
      ]),
      h('div.row.gap', [
        h('span.spacer'),
        h('button.btn.primary', 'Save limits', {'type': 'button'}, {
          'click': (_) async {
            List<String> syms(String v) => [for (final x in v.split(RegExp(r'[,\s]+'))) if (x.trim().isNotEmpty) x.trim().toUpperCase()];
            final limits = <String, Object?>{
              for (final e in inputs.entries)
                e.key: e.value.type == 'checkbox' ? e.value.checked : num.tryParse(e.value.value),
              'symbol_allowlist': syms(allow.value),
              'symbol_blocklist': syms(block.value),
            };
            try {
              await trade.saveSettings({'limits': limits, 'ticket_ttl_minutes': int.tryParse(ttl.value) ?? 240});
              toast('Risk limits saved', kind: 'success', ms: 2000);
            } catch (e) {
              toast('$e', kind: 'error', ms: 6000);
            }
          },
        }),
      ]),
    ]));
  }

  String _ts(int t) {
    if (t <= 0) return '';
    final d = DateTime.fromMillisecondsSinceEpoch(t * 1000);
    final now = DateTime.now();
    final hm = '${d.hour.toString().padLeft(2, '0')}:${d.minute.toString().padLeft(2, '0')}';
    if (d.year == now.year && d.month == now.month && d.day == now.day) return hm;
    return '${d.year}-${d.month.toString().padLeft(2, '0')}-${d.day.toString().padLeft(2, '0')} $hm';
  }

  String _time(String iso) {
    final d = DateTime.tryParse(iso);
    return d == null ? iso : _ts(d.millisecondsSinceEpoch ~/ 1000);
  }
}
