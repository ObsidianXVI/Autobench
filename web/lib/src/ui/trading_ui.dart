// Shared trading UI: the risk-check summary, the agent-proposal review dialog, and the close-position,
// modify-order and cancel-order flows used by the Trading page, the order panel and the chart.
import 'dart:async';

import 'package:web/web.dart';

import '../core/format.dart';
import '../core/json.dart';
import '../core/trading.dart';
import 'dom.dart';
import 'kit/overlay.dart';
import 'trading_state.dart';

String statusBadge(String status) => switch (status) {
      'filled' => 'badge.up',
      'working' || 'partially_filled' || 'submitted' => 'badge.info',
      'pending' => 'badge.agent',
      'rejected' || 'error' || 'inactive' => 'badge.down',
      _ => 'badge.neutral',
    };

HTMLElement statusTag(String status) => h('span.${statusBadge(status)}', status.replaceAll('_', ' '));

/// The risk check as rows: estimate, then errors (blocking) and warnings.
HTMLElement checkView(J check, {bool compact = false}) {
  final e = check['estimate'];
  final cur = e['currency'].s ?? 'USD';
  String m(double? v) => v == null
      ? '–'
      : cur == 'USD'
          ? money(v, sign: false)
          : '${money(v, sign: false).replaceFirst('\$', '')} $cur';
  final rows = <(String, String, String?)>[
    ('Value', m(e['value'].d), e['pct_of_equity'].d != null ? '${n(e['pct_of_equity'].d, 1)}% of equity' : null),
    if (e['risk_value'].d != null)
      ('Risk to stop', m(e['risk_value'].d), e['risk_pct_equity'].d != null ? '${n(e['risk_pct_equity'].d, 2)}% of equity' : null),
    if (e['reward_value'].d != null)
      ('Reward', m(e['reward_value'].d), e['reward_risk'].d != null ? '${n(e['reward_risk'].d, 2)} R' : null),
    ('Position', '${qtyText(e['position_before'].d ?? 0)} → ${qtyText(e['position_after'].d ?? 0)}',
        e['position_pct_after'].d != null ? '${n(e['position_pct_after'].d, 1)}% of equity' : null),
  ];
  return h('div.check', [
    h('div.check-rows', [
      for (final (k, v, sub) in rows)
        h('div.check-row', [h('span.muted', k), h('span.spacer'), h('b', v), if (sub != null) h('span.muted.tiny', ' · $sub')]),
    ]),
    for (final err in check['errors'].list) h('div.check-msg.error', [h('b', '✕ '), err.s ?? '']),
    for (final w in check['warnings'].list) h('div.check-msg.warn', [h('b', '! '), w.s ?? '']),
  ]);
}

/// Review an agent proposal: rationale, a fresh risk check, approve or reject (with a reason).
Future<void> reviewTicket(TTicket t) async {
  final checkHost = h('div', h('div.muted.small', 'Re-checking against the account…'));
  late Dialog d;
  final body = h('div.ticket-review', [
    h('div.ticket-title', [
      if (t.source == 'agent') h('span.badge-agent', '✦ AI'),
      h('b.${t.side == 'buy' ? 'up' : 'down'}', t.summary),
      if (t.intent == 'close') h('span.badge.neutral', 'closes position'),
    ]),
    if (trade.otherAccount(t))
      h('div.check-msg.warn', 'Made for account ${t.account}. Switch to that account on the Trading page to approve it.'),
    h('div.ticket-levels', [
      if (t.stopLoss != null) h('span', [h('span.muted', 'Stop '), h('b.down', fmtPrice(t.stopLoss!))]),
      if (t.takeProfit != null) h('span', [h('span.muted', 'Target '), h('b.up', fmtPrice(t.takeProfit!))]),
      h('span.muted', 'TIF ${(t.spec['tif'].s ?? 'day').toUpperCase()}'),
      if (t.expiresAt != null) h('span.muted', 'expires ${_clock(t.expiresAt!)}'),
    ]),
    if ((t.rationale ?? '').isNotEmpty) h('div.ticket-rationale', markdown(t.rationale!)),
    checkHost,
  ]);
  Future<void> recheck() async {
    final spec = Map<String, Object?>.from(t.spec.v as Map)..removeWhere((k, v) => v == null);
    try {
      final c = await trade.preview(spec);
      replace(checkHost, checkView(c));
    } catch (e) {
      replace(checkHost, h('div.check-msg.error', '$e'));
    }
  }

  d = Dialog.open('Review order proposal', body, width: '520px', actions: [
    DialogAction('Reject…', onPressed: () async {
      final reason = await promptDialog('Reject proposal', label: 'Reason (the agent sees this)', initial: '', confirm: 'Reject');
      if (reason == null) return false;
      try {
        await trade.reject(t.id, reason.isEmpty ? null : reason);
        toast('Proposal rejected', ms: 2500);
        return true;
      } catch (e) {
        toast('$e', kind: 'error');
        return false;
      }
    }),
    DialogAction('Approve & send', primary: true, onPressed: () async {
      try {
        final res = await trade.approve(t.id);
        toast('${t.summary} · ${res.status}', title: 'Order sent', kind: 'success', ms: 4000);
        return true;
      } catch (e) {
        toast('$e', kind: 'error', ms: 8000);
        await recheck();
        return false;
      }
    }),
  ]);
  unawaited(recheck());
  d.panel.classList.add('trade-dialog');
}

String _clock(String iso) {
  final t = DateTime.tryParse(iso)?.toLocal();
  if (t == null) return iso;
  return '${t.hour.toString().padLeft(2, '0')}:${t.minute.toString().padLeft(2, '0')}';
}

/// Close (part of) a position at market or a limit.
Future<void> closePositionDialog(TPosition p, {double? fraction, double? limitPrice}) async {
  final qty = h('input.input', null, {
    'type': 'number', 'min': '1', 'step': '1', 'max': qtyText(p.qty.abs()),
    'value': qtyText(((fraction ?? 1) * p.qty.abs()).roundToDouble().clamp(1, p.qty.abs())),
    'aria-label': 'Shares to close',
  }) as HTMLInputElement;
  var type = limitPrice != null ? 'limit' : 'market';
  final price = h('input.input', null, {
    'type': 'number', 'step': '0.01', 'value': (limitPrice ?? p.marketPrice ?? p.avgCost).toStringAsFixed(2),
    'aria-label': 'Limit price',
  }) as HTMLInputElement;
  final priceRow = h('label.field-col', [h('span', 'Limit price'), price]);
  priceRow.isHidden = type == 'market';
  final seg = h('div.seg');
  void renderSeg() => replace(seg, [
        for (final (k, l) in [('market', 'Market'), ('limit', 'Limit')])
          h('button.seg-btn${type == k ? '.on' : ''}', l, {'type': 'button'}, {
            'click': (_) {
              type = k;
              priceRow.isHidden = type == 'market';
              renderSeg();
            },
          }),
      ]);
  renderSeg();
  Dialog.open('Close ${p.symbol} ${p.isLong ? 'long' : 'short'}', h('div.form-grid', [
    h('div.muted.small', '${qtyText(p.qty.abs())} shares @ ${fmtPrice(p.avgCost)} · P&L ${money(p.unrealizedPnl)}'),
    h('div.row.gap', [
      h('label.field-col', [h('span', 'Shares'), qty]),
      h('div.field-col', [h('span', 'Order type'), seg]),
    ]),
    priceRow,
    h('div.row.gap', [
      for (final f in [0.25, 0.5, 1.0])
        h('button.btn.small', f == 1 ? 'All' : '${(f * 100).round()}%', {'type': 'button'},
            {'click': (_) => qty.value = qtyText((f * p.qty.abs()).roundToDouble().clamp(1, p.qty.abs()))}),
    ]),
  ]), actions: [
    const DialogAction('Cancel'),
    DialogAction(p.isLong ? 'Sell to close' : 'Buy to close', danger: true, onPressed: () async {
      final q = double.tryParse(qty.value);
      if (q == null || q <= 0) return false;
      try {
        final t = await trade.close(p.symbol, qty: q, type: type, limitPrice: type == 'limit' ? double.tryParse(price.value) : null);
        toast('${t.summary} · ${t.status}', title: 'Closing order', kind: 'success', ms: 3500);
        return true;
      } catch (e) {
        toast('$e', kind: 'error', ms: 8000);
        return false;
      }
    }),
  ]);
}

/// Change quantity and price(s) of a working order.
Future<void> modifyOrderDialog(TOrder o) async {
  final qty = h('input.input', null, {'type': 'number', 'min': '1', 'step': '1', 'value': qtyText(o.qty), 'aria-label': 'Quantity'})
      as HTMLInputElement;
  final lim = h('input.input', null, {'type': 'number', 'step': '0.01', 'value': o.limitPrice?.toStringAsFixed(2) ?? '', 'aria-label': 'Limit price'})
      as HTMLInputElement;
  final stp = h('input.input', null, {'type': 'number', 'step': '0.01', 'value': o.stopPrice?.toStringAsFixed(2) ?? '', 'aria-label': 'Stop price'})
      as HTMLInputElement;
  Dialog.open('Modify ${o.roleLabel} ${o.typeLabel} · ${o.symbol}', h('div.form-grid', [
    h('div.row.gap', [
      h('label.field-col', [h('span', 'Quantity'), qty]),
      if (o.type == 'limit' || o.type == 'stop_limit') h('label.field-col', [h('span', 'Limit price'), lim]),
      if (o.type == 'stop' || o.type == 'stop_limit') h('label.field-col', [h('span', 'Stop price'), stp]),
    ]),
  ]), actions: [
    const DialogAction('Cancel'),
    DialogAction('Modify', primary: true, onPressed: () async {
      final patch = <String, Object?>{
        if (double.tryParse(qty.value) != o.qty) 'qty': double.tryParse(qty.value),
        if ((o.type == 'limit' || o.type == 'stop_limit') && double.tryParse(lim.value) != o.limitPrice)
          'limit_price': double.tryParse(lim.value),
        if ((o.type == 'stop' || o.type == 'stop_limit') && double.tryParse(stp.value) != o.stopPrice)
          'stop_price': double.tryParse(stp.value),
      }..removeWhere((k, v) => v == null);
      if (patch.isEmpty) return true;
      try {
        await trade.modify(o.id, patch);
        toast('Order modified', kind: 'success', ms: 2000);
        return true;
      } catch (e) {
        toast('$e', kind: 'error', ms: 8000);
        return false;
      }
    }),
  ]);
}

/// Cancel an order (asks first when it is a stop protecting a position).
Future<void> cancelOrder(TOrder o, {bool protectsPosition = false}) async {
  if (o.role == 'stop_loss' && protectsPosition &&
      !await confirmDialog('Cancel stop-loss', 'This stop protects your open ${o.symbol} position. Cancel it anyway?',
          confirm: 'Cancel stop', danger: true)) {
    return;
  }
  try {
    await trade.cancel(o.id);
    toast('${o.roleLabel} ${o.typeLabel} ${qtyText(o.qty)} ${o.symbol} cancelled', ms: 2500);
  } catch (e) {
    toast('$e', kind: 'error', ms: 6000);
  }
}
