// The Financier side of the brokerage account, on Autobench's Portfolio page: which Financier Portfolio the
// account is bound to, the Physical and Virtual Accounts holding its capital (e.g. the L-Fund backed by IBKR),
// and Financier's risk plan per open position against the stop Autobench sees. Read-only
// (GET /api/financier/summary, vral-altos Firestore); "Open in Financier" switches the Mode selector.
import 'package:web/web.dart';

import '../../core/format.dart';
import '../../core/json.dart';
import '../../core/trading.dart';
import '../api.dart';
import '../dom.dart';
import '../financier_mode.dart';
import '../kit/icons.dart';
import '../store.dart';

class FinancierLink {
  final HTMLElement root = h('section.jr-section.fin-link');
  int _seq = 0;

  /// Loads and renders. [positions] are Autobench's open positions (live, with `stop`/`target`, or from the
  /// latest statement), compared with Financier's plans.
  Future<void> load(List<J> positions, {bool refresh = false}) async {
    final seq = ++_seq;
    replace(root, [_head(null), h('div.muted.small', 'Reading Financier…')]);
    J s;
    try {
      s = await request('/api/financier/summary${refresh ? '?refresh=1' : ''}');
      if (s['available'].v == false) throw s['error'].s ?? 'no access';
    } catch (e) {
      if (seq != _seq) return;
      replace(root, [
        _head(null),
        h('div.muted.small', ['Financier data unavailable: ', h('span.error', '$e')]),
        h('div.muted.tiny', 'Autobench reads Financier\'s Firestore (project vral-altos) with your Google login '
            '(gcloud auth application-default login), the same one cloud sync uses.'),
        h('button.btn.small', [icon('refresh', size: 13), 'Retry'], {'type': 'button'}, {'click': (_) => load(positions, refresh: true)}),
      ]);
      return;
    }
    if (seq != _seq) return;
    replace(root, _render(s, positions));
  }

  HTMLElement _head(J? s) {
    final linked = s?['linked_portfolio'];
    return h('div.fin-head', [
      h('h3', 'Financier'),
      if (linked != null && !linked.isNull)
        h('span.badge.up', 'bound to ${linked['name'].s}', {'data-tip': 'Financier Portfolio with broker account ${linked['broker_account_id'].s}'})
      else if (s != null)
        h('span.badge.neutral', 'no Portfolio bound to ${s['broker_account'].s ?? 'this account'}',
            {'data-tip': 'Set the broker account id on a Portfolio in Financier (Portfolio → Add/Edit) to link it'}),
      h('span.spacer'),
      if (s != null)
        h('button.icon-btn.small', icon('refresh', size: 14), {'type': 'button', 'data-tip': 'Re-read Financier', 'aria-label': 'Refresh'},
            {'click': (_) => load(_lastPositions, refresh: true)}),
      h('button.btn.small', [icon('wallet', size: 13), 'Open in Financier'], {'type': 'button', 'data-tip': 'Switch Mode to Financier · Portfolio'},
          {'click': (_) => setMode('financier', destination: 'portfolio')}),
    ]);
  }

  List<J> _lastPositions = const [];

  List<Object> _render(J s, List<J> positions) {
    _lastPositions = positions;
    final linked = s['linked_portfolio'];
    final ccy = linked.isNull ? 'SGD' : (linked['base_currency'].s ?? 'SGD');
    final plans = s['plans'].map;
    final linkedId = linked.isNull ? null : linked['id'].s;

    // capital: the bound Portfolio's Physical Account and the Virtual Accounts it backs
    final accounts = <HTMLElement>[];
    if (!linked.isNull) {
      final pa = linked['physical_account'];
      if (!pa.isNull) {
        accounts.add(_acct('${pa['name'].s} (physical)', pa['balance'].d, ccy,
            pa['recorded_at'].s == null ? 'no balance recorded' : 'recorded ${pa['recorded_at'].s!.substring(0, 10)}'));
      }
      for (final v in linked['virtual_accounts'].list) {
        accounts.add(_acct(v['name'].s ?? '', v['balance'].d, ccy, 'virtual · backed by ${v['backed_by'].list.map((b) => b.s).join(', ')}'));
      }
      if (linked['deposits'].d != null) {
        accounts.add(_acct('Deposits recorded', linked['deposits'].d, ccy, 'into ${linked['name'].s}'));
      }
    }

    // plans: every symbol either side holds
    final bySym = {for (final p in positions) (p['symbol'].s ?? '').toUpperCase(): p};
    final syms = {
      ...bySym.keys.where((k) => k.isNotEmpty),
      for (final t in s['open_trades'].list)
        if (linkedId == null || t['portfolio_id'].s == linkedId) t['symbol'].s ?? '',
    }.where((k) => k.isNotEmpty).toList()
      ..sort();

    return [
      _head(s),
      if (accounts.isNotEmpty) h('div.fin-accts', accounts)
      else if (linked.isNull)
        h('div.muted.small', 'Bind a Financier Portfolio to this broker account to see the capital behind it.'),
      if (syms.isNotEmpty)
        h('div.table-wrap', h('table.data-table.compact', [
          h('thead', h('tr', [
            h('th', 'Symbol'), h('th.num', 'Qty (broker)'), h('th.num', 'Autobench stop'), h('th.num', 'Financier stop'),
            h('th.num', 'Financier target'), h('th', 'Check'), h('th', 'Financier note'),
          ])),
          h('tbody', [
            for (final sym in syms) _planRow(sym, bySym[sym], plans[sym]),
          ]),
        ])),
      h('div.muted.tiny', 'From Financier (vral-altos) · read ${_time(s['fetched_at'].s)}'),
    ];
  }

  HTMLElement _planRow(String sym, J? pos, J? plan) {
    final aStop = pos?['stop'].d;
    final fStop = plan?['stop'].d;
    final (check, cls) = switch ((pos, plan)) {
      (null, _) => ('closed at broker', 'fin-plan-diff'),
      (_, null) => ('not in Financier', 'muted'),
      _ when aStop == null && fStop == null => ('no stop on either', 'fin-plan-diff'),
      _ when aStop == null => ('no working stop', 'fin-plan-diff'),
      _ when fStop == null => ('no Financier stop', 'muted'),
      _ when (aStop - fStop).abs() <= 0.005 * fStop.abs().clamp(1, double.infinity) => ('stops agree', 'up'),
      _ => ('stops differ', 'fin-plan-diff'),
    };
    return h('tr', [
      h('td.sym', h('a', sym, {'href': '#chart'}, {
        'click': (e) {
          e.preventDefault();
          store.activeChart.symbol.value = sym;
          store.page.value = 'chart';
        },
      })),
      h('td.num', pos == null ? '–' : qtyText(pos['qty'].d ?? 0)),
      h('td.num', aStop == null ? '–' : fmtPrice(aStop)),
      h('td.num', fStop == null ? '–' : fmtPrice(fStop)),
      h('td.num', plan?['target'].d == null ? '–' : fmtPrice(plan!['target'].d!)),
      h('td.$cls', check),
      h('td.muted.small.ellipsis', plan?['note'].s ?? ''),
    ]);
  }

  HTMLElement _acct(String label, double? v, String ccy, String sub) => h('div.fin-acct', [
        h('span.muted.small', label),
        h('b.num', v == null ? '–' : '${money(v, sign: false)} $ccy'),
        h('span.muted.tiny', sub),
      ]);

  String _time(String? iso) {
    final t = iso == null ? null : DateTime.tryParse(iso)?.toLocal();
    if (t == null) return '–';
    return '${t.hour.toString().padLeft(2, '0')}:${t.minute.toString().padLeft(2, '0')}';
  }
}

