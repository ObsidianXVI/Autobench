// Client-side trading state: broker status, pending agent proposals, a change counter that views
// re-fetch on (orders, fills, account), the order-ticket draft that chart actions fill in, and thin
// wrappers over /api/trading.
import 'dart:async';

import '../core/json.dart';
import '../core/trading.dart';
import 'api.dart';
import 'events.dart';
import 'kit/state.dart';

/// A request to open the order ticket prefilled (from the chart, a drawing, a position, …).
class OrderDraft {
  final String symbol;
  final String side; // buy | sell
  final String type; // market | limit | stop | stop_limit
  final double? price, stopLoss, takeProfit, qty;
  const OrderDraft(this.symbol, this.side, {this.type = 'limit', this.price, this.stopLoss, this.takeProfit, this.qty});
}

class TradingState {
  final status = Obs<TStatus?>(null);
  final pending = Obs<List<TTicket>>(const []);
  final version = Obs<int>(0); // bumped on every order / fill / ticket / account change
  final draft = Obs<OrderDraft?>(null);
  Timer? _bump;
  bool _started = false;

  Future<void> start() async {
    if (_started) return;
    _started = true;
    await refresh();
  }

  Future<void> refresh() async {
    await Future.wait([refreshStatus(), refreshPending()]);
  }

  Future<void> refreshStatus() async {
    final s = await attempt(request('/api/trading/status'));
    if (s != null) status.value = TStatus.fromJson(s);
  }

  Future<void> refreshPending() async {
    final t = await attempt(request('/api/trading/tickets?status=pending'));
    if (t != null) pending.value = [for (final x in t.list) TTicket.fromJson(x)];
  }

  void _changed() {
    _bump?.cancel();
    _bump = Timer(const Duration(milliseconds: 150), () => version.value++);
  }

  /// Returns true when the event was a trading event.
  bool onEvent(ServerEvent e) {
    switch (e.type) {
      case 'trading.status':
        status.value = TStatus.fromJson(e.data);
        _changed();
      case 'trading.ticket':
        refreshPending();
        refreshStatus();
        _changed();
      case 'trading.order' || 'trading.fill' || 'trading.account':
        _changed();
      case 'trading.error':
        _changed();
      default:
        return false;
    }
    return true;
  }

  bool get canTrade => status.value?.canTrade ?? false;

  /// Opens the order ticket with these values.
  void openTicket(OrderDraft d) {
    draft.value = null;
    draft.value = d;
  }

  // ------------------------------------------------------------------- API

  Future<J> overview() => request('/api/trading/overview');
  Future<J> chart(String symbol) => request('/api/trading/chart/${Uri.encodeComponent(symbol)}');
  Future<J> preview(Map<String, Object?> spec) => request('/api/trading/preview', method: 'POST', body: spec);
  Future<TTicket> submit(Map<String, Object?> spec) async =>
      TTicket.fromJson(await request('/api/trading/orders', method: 'POST', body: spec));
  Future<TOrder> modify(String id, Map<String, Object?> patch) async =>
      TOrder.fromJson(await request('/api/trading/orders/${Uri.encodeComponent(id)}', method: 'PATCH', body: patch));
  Future<J> cancel(String id) => request('/api/trading/orders/${Uri.encodeComponent(id)}', method: 'DELETE');
  Future<TTicket> close(String symbol, {double? qty, String type = 'market', double? limitPrice}) async =>
      TTicket.fromJson(await request('/api/trading/positions/${Uri.encodeComponent(symbol)}/close', method: 'POST', body: {
        'qty': ?qty,
        'order_type': type,
        'limit_price': ?limitPrice,
      }));
  Future<TTicket> approve(String id) async =>
      TTicket.fromJson(await request('/api/trading/tickets/$id/approve', method: 'POST'));
  Future<TTicket> reject(String id, String? reason) async =>
      TTicket.fromJson(await request('/api/trading/tickets/$id/reject', method: 'POST', body: {'reason': reason}));
  Future<J> tickets({String? status, String? symbol, int limit = 200}) => request(
      '/api/trading/tickets?limit=$limit${status != null ? '&status=$status' : ''}${symbol != null ? '&symbol=$symbol' : ''}');
  Future<J> trades({String? symbol}) => request('/api/trading/trades${symbol != null ? '?symbol=$symbol' : ''}');
  Future<J> fills({int limit = 500}) => request('/api/trading/fills?limit=$limit');
  Future<J> equity(int days) => request('/api/trading/equity?days=$days');
  Future<J> audit() => request('/api/trading/audit?limit=300');
  Future<J> settings() => request('/api/trading/settings');
  Future<J> saveSettings(Map<String, Object?> patch) => request('/api/trading/settings', method: 'PUT', body: patch);
  Future<J> halt({bool flatten = false, String? reason}) =>
      request('/api/trading/halt', method: 'POST', body: {'flatten': flatten, 'reason': reason});
  Future<J> resume() => request('/api/trading/resume', method: 'POST');
  Future<J> arm() => request('/api/trading/arm', method: 'POST', body: {'confirm': 'LIVE'});
  Future<J> disarm() => request('/api/trading/disarm', method: 'POST');
  Future<J> connect(String broker, {String? host, int? port, int? clientId}) => request('/api/trading/connect',
      method: 'POST', body: {'broker': broker, 'host': ?host, 'port': ?port, 'client_id': ?clientId});
  Future<J> simReset(double cash) => request('/api/trading/sim/reset?cash=$cash', method: 'POST');
}

final trade = TradingState();
