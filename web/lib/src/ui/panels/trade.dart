// Order ticket (dock panel), TradingView-style: buy/sell for the active chart's symbol with limit,
// market, stop and stop-limit orders, sizing by shares / dollar value / risk to the stop, optional
// stop-loss and take-profit (sent as a bracket), a live risk check, and below it the symbol's
// position, working orders and pending agent proposals.
import 'dart:async';
import 'dart:js_interop';

import 'package:web/web.dart';

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
import '../trading_ui.dart';

class TradePanel {
  final HTMLElement root = h('div.panel.trade-panel');
  final HTMLElement _head = h('div.panel-head');
  final HTMLElement _form = h('div.trade-form');
  final HTMLElement _check = h('div.trade-check');
  final HTMLElement _submit = h('div.trade-submit');
  final HTMLElement _below = h('div.trade-below');
  final Scope _scope = Scope();
  final _previewDebounce = Debouncer(250);
  Timer? _quoteTimer;

  String _side = 'buy';
  String _type = 'limit';
  double? _price, _stopPrice, _last;
  double _qty = 10;
  double? _stopLoss, _takeProfit;
  J? _lastCheck;
  bool _busy = false;
  TChartData? _data;
  double _equity = 0;
  String? _symbolShown;
  int _previewSeq = 0;

  TradePanel() {
    append(root, [_head, h('div.panel-scroll', [_form, _check, _submit, _below])]);
    _qty = store.pref<double>('trade.qty');
    void track() {
      for (final c in store.charts) {
        _scope.listen(c.symbol, (_) => _symbolChanged());
      }
    }

    track();
    _scope.listen(store.layoutVersion, (_) => track());
    _scope.listen(store.active, (_) => _symbolChanged());
    _scope.listen(trade.status, (_) {
      _renderHead();
      _preview();
    });
    _scope.listen(trade.version, (_) => _loadBelow());
    _scope.listen(trade.draft, (d) {
      if (d != null) _applyDraft(d);
    }, now: true);
    _quoteTimer = Timer.periodic(const Duration(seconds: 10), (_) {
      if (root.isConnected) _loadQuote();
    });
    trade.start();
    _symbolChanged();
  }

  void dispose() {
    _scope.dispose();
    _quoteTimer?.cancel();
  }

  String get symbol => store.activeChart.symbol.value;

  void _symbolChanged() {
    if (_symbolShown == symbol) return;
    _symbolShown = symbol;
    _price = _stopPrice = _stopLoss = _takeProfit = _last = null;
    _data = null;
    _render();
    _loadQuote();
    _loadBelow();
    _loadEquity();
  }

  void _applyDraft(OrderDraft d) {
    if (d.symbol != symbol) store.activeChart.symbol.value = d.symbol;
    _symbolShown = d.symbol;
    _side = d.side;
    _type = d.type;
    if (d.type == 'stop' || d.type == 'stop_limit') {
      _stopPrice = d.price;
      _price = d.type == 'stop_limit' ? d.price : null;
    } else {
      _price = d.price;
    }
    if (d.stopLoss != null) store.setPref('trade.useStop', true);
    if (d.takeProfit != null) store.setPref('trade.useTarget', true);
    _stopLoss = d.stopLoss;
    _takeProfit = d.takeProfit;
    if (d.qty != null) {
      _qty = d.qty!;
      store.setPref('trade.sizing', 'shares');
    }
    _render();
    _loadQuote();
    _loadBelow();
    _loadEquity();
    Timer.run(() => (root.querySelector('.trade-submit button') as HTMLElement?)?.focus());
  }

  Future<void> _loadQuote() async {
    final sym = symbol;
    final q = await attempt(api.quote(sym));
    if (q == null || sym != symbol) return;
    final first = _last == null;
    _last = q.price;
    if (first) {
      _price ??= _round(q.price);
      _stopPrice ??= _round(q.price * (_side == 'buy' ? 1.01 : 0.99));
      _render();
    } else {
      _renderHead();
    }
  }

  Future<void> _loadEquity() async {
    final o = await attempt(trade.overview());
    _equity = o?['account']['net_liquidation'].d ?? 0;
    if (store.pref<String>('trade.sizing') != 'shares') _render();
  }

  Future<void> _loadBelow() async {
    final sym = symbol;
    final j = await attempt(trade.chart(sym));
    if (j == null || sym != symbol) return;
    final wasClosing = _closing;
    _data = TChartData.fromJson(j);
    if (_closing != wasClosing) _renderForm(); // the bracket fields depend on the position
    _renderBelow();
    _preview();
  }

  double _round(double v) => double.parse(v.toStringAsFixed(v >= 1 ? 2 : 4));

  /// Entry reference price for sizing and default stop/target.
  double? get _entry => switch (_type) { 'market' => _last, 'stop' => _stopPrice, _ => _price };

  double get _effQty {
    final sizing = store.pref<String>('trade.sizing');
    final e = _entry;
    if (sizing == 'value' && e != null) return sharesForValue(store.pref<double>('trade.value'), e).toDouble();
    if (sizing == 'risk' && e != null && _effStop != null && _equity > 0) {
      return sharesForRisk(_equity, store.pref<double>('trade.riskPct'), e, _effStop!).toDouble();
    }
    return _qty;
  }

  double? get _effStop {
    if (!store.pref<bool>('trade.useStop')) return null;
    if (_stopLoss != null) return _stopLoss;
    final e = _entry;
    if (e == null) return null;
    final pct = store.pref<double>('trade.stopPct') / 100;
    return _round(_side == 'buy' ? e * (1 - pct) : e * (1 + pct));
  }

  double? get _effTarget {
    if (!store.pref<bool>('trade.useTarget')) return null;
    if (_takeProfit != null) return _takeProfit;
    final e = _entry, s = _effStop;
    if (e == null) return null;
    final dist = s != null ? (e - s).abs() : e * store.pref<double>('trade.stopPct') / 100;
    final r = store.pref<double>('trade.targetR');
    return _round(_side == 'buy' ? e + dist * r : e - dist * r);
  }

  bool get _closing {
    final p = _data?.position;
    return p != null && (p.isLong ? _side == 'sell' : _side == 'buy');
  }

  Map<String, Object?> _spec() {
    final closing = _closing;
    return {
      'symbol': symbol,
      'side': _side,
      'qty': _effQty,
      'order_type': _type,
      if (_type == 'limit' || _type == 'stop_limit') 'limit_price': _price,
      if (_type == 'stop' || _type == 'stop_limit') 'stop_price': _stopPrice,
      'tif': store.pref<String>('trade.tif'),
      if (!closing && _effStop != null) 'stop_loss': _effStop,
      if (!closing && _effTarget != null) 'take_profit': _effTarget,
    };
  }

  // ---------------------------------------------------------------- render

  void _render() {
    _renderHead();
    _renderForm();
    _renderBelow();
    _preview();
  }

  void _renderHead() {
    final st = trade.status.value;
    replace(_head, [
      h('span.panel-title.static', ['Trade', h('span.muted.small', ' · $symbol')]),
      h('span.spacer'),
      if (st != null)
        h('span.acct-pill${!st.connected ? '.off' : st.isLive ? '.live' : ''}', [
          h('span.dot${st.connected ? '.ok' : '.warn'}'),
          st.connected
              ? '${st.brokerLabel} ${st.isLive ? 'LIVE' : 'PAPER'}${st.multiAccount ? ' · ${st.account ?? ''}' : ''}'
              : '${st.brokerLabel} offline',
        ], {'data-tip': st.connected ? 'Account ${st.account ?? ''}' : (st.message ?? ''), 'data-tip-pos': 'left'}),
      h('button.icon-btn', icon('external', size: 15), {'type': 'button', 'data-tip': 'Open the Trading page', 'aria-label': 'Open trading page'},
          {'click': (_) => store.page.value = 'trading'}),
    ]);
  }

  HTMLInputElement _num(String label, double? value, void Function(double?) onChange, {String step = '0.01'}) {
    final el = h('input.input.num', null, {'type': 'number', 'step': step, 'min': '0', 'aria-label': label,
        if (value != null) 'value': value == value.roundToDouble() && step == '1' ? value.toStringAsFixed(0) : value.toStringAsFixed(2)})
        as HTMLInputElement;
    el.addEventListener('input', ((Event _) {
      onChange(double.tryParse(el.value));
      _preview();
    }).toJS);
    return el;
  }

  void _renderForm() {
    final sizing = store.pref<String>('trade.sizing');
    final last = _last;
    HTMLElement sideBtn(String side) => h('button.side-btn.$side${_side == side ? '.on' : ''}', [
          h('span.side-label', side == 'buy' ? 'Buy' : 'Sell'),
          h('span.side-price', last == null ? '…' : fmtPrice(last)),
        ], {'type': 'button', 'aria-pressed': '${_side == side}', 'aria-label': side == 'buy' ? 'Buy side' : 'Sell side'}, {
          'click': (_) {
            if (_side == side) return;
            _side = side;
            _stopLoss = _takeProfit = null;
            _render();
          },
        });
    HTMLElement seg(List<(String, String)> opts, String current, void Function(String) pick, {String label = ''}) =>
        h('div.seg', [
          for (final (k, l) in opts)
            h('button.seg-btn${current == k ? '.on' : ''}', l, {'type': 'button', 'aria-label': '$label $l'.trim()}, {'click': (_) => pick(k)}),
        ]);
    final entry = _entry;
    final stop = _effStop, target = _effTarget;
    String dist(double? p) => p == null || entry == null || entry == 0 ? '' : pct((p / entry - 1) * 100);

    replace(_form, [
      h('div.side-row', [sideBtn('buy'), sideBtn('sell')]),
      seg([('limit', 'Limit'), ('market', 'Market'), ('stop', 'Stop'), ('stop_limit', 'Stop limit')], _type, (k) {
        _type = k;
        _render();
      }, label: 'Order type'),
      if (_type == 'stop' || _type == 'stop_limit')
        h('label.trade-field', [h('span', 'Stop (trigger)'), _num('Stop trigger price', _stopPrice, (v) => _stopPrice = v),
          _nudges((m) {
            _stopPrice = _round((_stopPrice ?? last ?? 0) * m);
            _render();
          })]),
      if (_type == 'limit' || _type == 'stop_limit')
        h('label.trade-field', [h('span', 'Limit price'), _num('Limit price', _price, (v) => _price = v),
          _nudges((m) {
            _price = _round(m == 0 ? (last ?? 0) : (_price ?? last ?? 0) * m);
            _render();
          })]),
      if (_type == 'market') h('div.muted.small', 'Fills at the market (about ${last == null ? '…' : fmtPrice(last)}). '
          'Opening positions at market is blocked unless allowed in the risk limits.'),
      h('div.trade-field', [
        h('span', 'Size'),
        seg([('shares', 'Shares'), ('value', '\$ Value'), ('risk', 'Risk %')], sizing, (k) {
          store.setPref('trade.sizing', k);
          _render();
        }, label: 'Size by'),
      ]),
      switch (sizing) {
        'value' => h('label.trade-field', [h('span', 'Amount (\$)'),
            _num('Dollar amount', store.pref<double>('trade.value'), (v) => store.setPref('trade.value', v ?? 0), step: '100'),
            h('span.muted.small.qty-out', '= ${qtyText(_effQty)} sh')]),
        'risk' => h('label.trade-field', [h('span', 'Risk % equity'),
            _num('Risk percent of equity', store.pref<double>('trade.riskPct'), (v) => store.setPref('trade.riskPct', v ?? 0), step: '0.1'),
            h('span.muted.small.qty-out', stop == null ? 'needs a stop' : '= ${qtyText(_effQty)} sh')]),
        _ => h('label.trade-field', [h('span', 'Shares'), _num('Shares', _qty, (v) {
            _qty = (v ?? 0).floorToDouble();
            store.setPref('trade.qty', _qty);
          }, step: '1'),
            h('span.muted.small.qty-out', entry == null ? '' : '≈ ${money(_effQty * entry, sign: false)}')]),
      },
      if (_closing)
        h('div.muted.small', 'Reduces your ${_data!.position!.isLong ? 'long' : 'short'} position — no bracket is attached.')
      else ...[
        h('div.trade-field.bracket', [
          h('label.check-row', [
            h('input', null, {'type': 'checkbox', if (store.pref<bool>('trade.useStop')) 'checked': '', 'aria-label': 'Use stop-loss'}, {
              'change': (e) {
                store.setPref('trade.useStop', (e.target as HTMLInputElement).checked);
                _render();
              },
            }),
            h('span.down', 'Stop loss'),
          ]),
          if (stop != null) ...[
            _num('Stop-loss price', stop, (v) => _stopLoss = v),
            h('span.muted.small', dist(stop)),
          ],
        ]),
        h('div.trade-field.bracket', [
          h('label.check-row', [
            h('input', null, {'type': 'checkbox', if (store.pref<bool>('trade.useTarget')) 'checked': '', 'aria-label': 'Use take-profit'}, {
              'change': (e) {
                store.setPref('trade.useTarget', (e.target as HTMLInputElement).checked);
                _render();
              },
            }),
            h('span.up', 'Take profit'),
          ]),
          if (target != null) ...[
            _num('Take-profit price', target, (v) => _takeProfit = v),
            h('span.muted.small', dist(target)),
          ],
        ]),
      ],
      h('div.trade-field', [
        h('span', 'Time in force'),
        seg([('day', 'Day'), ('gtc', 'GTC')], store.pref<String>('trade.tif'), (k) {
          store.setPref('trade.tif', k);
          _render();
        }, label: 'Time in force'),
      ]),
    ]);
  }

  HTMLElement _nudges(void Function(double mult) apply) => h('span.nudges', [
        h('button.btn.small', 'Last', {'type': 'button', 'aria-label': 'Use last price'}, {'click': (e) {
          e.preventDefault();
          apply(0);
        }}),
        h('button.btn.small', '−', {'type': 'button', 'aria-label': 'Lower 0.5%'}, {'click': (e) {
          e.preventDefault();
          apply(0.995);
        }}),
        h('button.btn.small', '+', {'type': 'button', 'aria-label': 'Raise 0.5%'}, {'click': (e) {
          e.preventDefault();
          apply(1.005);
        }}),
      ]);

  void _preview() {
    _renderSubmit();
    _previewDebounce(() async {
      final seq = ++_previewSeq;
      final spec = _spec();
      final missing = (spec.containsKey('limit_price') && spec['limit_price'] == null) ||
          (spec.containsKey('stop_price') && spec['stop_price'] == null);
      if ((spec['qty'] as double) <= 0 || missing) {
        replace(_check, h('div.check-msg.warn', missing ? 'Enter a price' : 'Enter a size'));
        _lastCheck = null;
        _renderSubmit();
        return;
      }
      try {
        final c = await trade.preview(spec);
        if (seq != _previewSeq) return;
        _lastCheck = c;
        replace(_check, checkView(c, compact: true));
      } catch (e) {
        if (seq != _previewSeq) return;
        _lastCheck = null;
        replace(_check, h('div.check-msg.error', '$e'));
      }
      _renderSubmit();
    });
  }

  void _renderSubmit() {
    final spec = _spec();
    final q = spec['qty'] as double;
    final px = switch (_type) {
      'market' => 'MKT',
      'limit' => '@ ${_price == null ? '…' : fmtPrice(_price!)} LMT',
      'stop' => 'STP ${_stopPrice == null ? '…' : fmtPrice(_stopPrice!)}',
      _ => 'STP ${_stopPrice == null ? '…' : fmtPrice(_stopPrice!)} LMT ${_price == null ? '…' : fmtPrice(_price!)}',
    };
    final ok = _lastCheck?['ok'].b ?? false;
    final st = trade.status.value;
    replace(_submit, [
      h('button.btn.submit-btn.$_side', [
        h('b', '${_side == 'buy' ? 'Buy' : 'Sell'} ${qtyText(q)} $symbol'),
        h('span', ' $px'),
        if (spec['stop_loss'] != null || spec['take_profit'] != null) h('span.tiny', ' · bracket'),
      ], {
        'type': 'button',
        'aria-label': 'Submit order',
        if (!ok || _busy || !(st?.canTrade ?? false)) 'disabled': '',
      }, {'click': (_) => _send()}),
      if (st != null && !st.canTrade)
        h('div.muted.small', st.connected
            ? (st.isLive ? 'Live account: trading is disabled or not armed.' : 'Trading unavailable.')
            : 'Broker not connected: ${st.message ?? ''}'),
      if (st?.halted ?? false) h('div.check-msg.error', 'Trading is halted (kill switch). Only closing orders are accepted.'),
    ]);
  }

  Future<void> _send() async {
    if (_busy) return;
    _busy = true;
    _renderSubmit();
    try {
      final t = await trade.submit(_spec());
      toast('${t.summary} · ${t.status}', title: 'Order sent', kind: 'success', ms: 3500);
      _stopLoss = _takeProfit = null;
    } catch (e) {
      toast('$e', kind: 'error', ms: 8000);
    } finally {
      _busy = false;
      _render();
    }
  }

  // ---------------------------------------------------------------- position, orders, proposals

  void _renderBelow() {
    final d = _data;
    if (d == null) {
      replace(_below, null);
      return;
    }
    final p = d.position;
    final working = d.orders.where((o) => o.isOpen).toList();
    replace(_below, [
      h('div.trade-sec-title', 'Position'),
      if (p == null)
        h('div.muted.small.pad-x', 'No ${d.symbol} position')
      else
        h('div.pos-card${(p.unrealizedPnl ?? 0) >= 0 ? '.up-b' : '.down-b'}', [
          h('div.row.gap', [
            h('b.${p.isLong ? 'up' : 'down'}', '${p.isLong ? 'LONG' : 'SHORT'} ${qtyText(p.qty.abs())}'),
            h('span.muted', '@ ${fmtPrice(p.avgCost)}'),
            h('span.spacer'),
            h('b.${(p.unrealizedPnl ?? 0) >= 0 ? 'up' : 'down'}', money(p.unrealizedPnl)),
            h('span.muted.small', pct(p.unrealizedPct)),
          ]),
          h('div.row.gap.small', [
            h('span.muted', 'Stop ${p.stop == null ? 'none' : fmtPrice(p.stop!)}'),
            h('span.muted', 'Target ${p.target == null ? 'none' : fmtPrice(p.target!)}'),
            h('span.spacer'),
            h('button.btn.small', 'Close', {'type': 'button', 'aria-label': 'Close position'}, {'click': (_) => closePositionDialog(p)}),
            h('button.btn.small', '½', {'type': 'button', 'aria-label': 'Close half', 'data-tip': 'Close half'},
                {'click': (_) => closePositionDialog(p, fraction: 0.5)}),
            if (p.stop == null)
              h('button.btn.small', 'Add stop', {'type': 'button', 'data-tip': 'Protective stop order for the position'}, {
                'click': (_) => trade.openTicket(OrderDraft(p.symbol, p.isLong ? 'sell' : 'buy', type: 'stop',
                    price: _round((p.marketPrice ?? p.avgCost) * (p.isLong ? 0.97 : 1.03)), qty: p.qty.abs())),
              }),
          ]),
        ]),
      h('div.trade-sec-title', ['Working orders', h('span.muted', ' ${working.length}')]),
      if (working.isEmpty) h('div.muted.small.pad-x', 'None'),
      for (final o in working) _orderRow(o, p),
      if (d.tickets.isNotEmpty) ...[
        h('div.trade-sec-title', ['Agent proposals', h('span.badge-agent', ' ${d.tickets.length}')]),
        for (final t in d.tickets)
          h('div.order-row.proposal', [
            h('span.badge-agent', '✦'),
            h('span.ellipsis', t.summary),
            h('span.spacer'),
            h('button.btn.small.primary', 'Review', {'type': 'button'}, {'click': (_) => reviewTicket(t)}),
          ]),
      ],
    ]);
  }

  HTMLElement _orderRow(TOrder o, TPosition? p) => h('div.order-row${o.held ? '.held' : ''}', [
        h('span.ord-role.${o.isBuy ? 'up' : 'down'}', o.roleLabel),
        h('span', '${o.typeLabel} ${qtyText(o.remaining)}'),
        h('b', o.linePrice == null ? 'MKT' : fmtPrice(o.linePrice!)),
        if (o.held) h('span.muted.tiny', 'after entry'),
        if (o.message != null && o.status != 'working') h('span.down.tiny.ellipsis', o.message!),
        h('span.spacer'),
        h('button.icon-btn.small', icon('edit', size: 13), {'type': 'button', 'aria-label': 'Modify order', 'data-tip': 'Modify'},
            {'click': (_) => modifyOrderDialog(o)}),
        h('button.icon-btn.small', icon('x', size: 13), {'type': 'button', 'aria-label': 'Cancel order', 'data-tip': 'Cancel'},
            {'click': (_) => cancelOrder(o, protectsPosition: p != null)}),
      ]);
}
