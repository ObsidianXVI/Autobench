// Trading models (broker orders, fills, positions, tickets, round-trip trades) and pure helpers
// for sizing and chart overlays. Mirrors the backend's /api/trading payloads.
import 'json.dart';
import 'models.dart';

const openOrderStatuses = {'pending', 'working', 'partially_filled'};

class TOrder {
  final String id;
  final String? ticketId, role, parentId;
  final String symbol, side, type, tif, status, source;
  final double qty, filledQty;
  final double? limitPrice, stopPrice, avgFillPrice;
  final bool outsideRth;
  final String? message;
  final int createdAt, updatedAt;

  const TOrder({
    required this.id,
    this.ticketId,
    this.role,
    this.parentId,
    required this.symbol,
    required this.side,
    required this.type,
    this.tif = 'day',
    required this.status,
    this.source = 'autobench',
    required this.qty,
    this.filledQty = 0,
    this.limitPrice,
    this.stopPrice,
    this.avgFillPrice,
    this.outsideRth = false,
    this.message,
    this.createdAt = 0,
    this.updatedAt = 0,
  });

  factory TOrder.fromJson(J j) => TOrder(
        id: j['id'].s ?? '',
        ticketId: j['ticket_id'].s,
        role: j['role'].s,
        parentId: j['parent_id'].s,
        symbol: j['symbol'].s ?? '',
        side: j['side'].s ?? 'buy',
        type: j['type'].s ?? 'limit',
        tif: j['tif'].s ?? 'day',
        status: j['status'].s ?? 'pending',
        source: j['source'].s ?? 'autobench',
        qty: j['qty'].d ?? 0,
        filledQty: j['filled_qty'].d ?? 0,
        limitPrice: j['limit_price'].d,
        stopPrice: j['stop_price'].d,
        avgFillPrice: j['avg_fill_price'].d,
        outsideRth: j['outside_rth'].b ?? false,
        message: j['message'].s,
        createdAt: j['created_at'].i ?? 0,
        updatedAt: j['updated_at'].i ?? 0,
      );

  bool get isOpen => openOrderStatuses.contains(status);
  bool get isBuy => side == 'buy';
  bool get isProtective => role == 'stop_loss' || role == 'take_profit';

  /// Waiting for its parent (bracket entry) to fill.
  bool get held => status == 'pending' && parentId != null;

  /// The price the order line is drawn at: the trigger for stops, else the limit.
  double? get linePrice => type == 'stop' || type == 'stop_limit' ? stopPrice : limitPrice;

  /// The field a drag of the order line changes.
  String get priceField => type == 'stop' || type == 'stop_limit' ? 'stop_price' : 'limit_price';

  String get typeLabel => switch (type) { 'market' => 'MKT', 'limit' => 'LMT', 'stop' => 'STP', _ => 'STP LMT' };

  String get roleLabel => switch (role) {
        'stop_loss' => 'SL',
        'take_profit' => 'TP',
        'exit' => 'EXIT',
        _ => isBuy ? 'BUY' : 'SELL',
      };

  double get remaining => qty - filledQty;
}

class TFill {
  final String execId, symbol, side;
  final String? orderId, ticketId, role;
  final double qty, price;
  final int time;
  final double? commission, realizedPnl;

  const TFill({
    required this.execId,
    required this.symbol,
    required this.side,
    this.orderId,
    this.ticketId,
    this.role,
    required this.qty,
    required this.price,
    required this.time,
    this.commission,
    this.realizedPnl,
  });

  factory TFill.fromJson(J j) => TFill(
        execId: j['exec_id'].s ?? '',
        symbol: j['symbol'].s ?? '',
        side: j['side'].s ?? 'buy',
        orderId: j['order_id'].s,
        ticketId: j['ticket_id'].s,
        role: j['role'].s,
        qty: j['qty'].d ?? 0,
        price: j['price'].d ?? 0,
        time: j['time'].i ?? 0,
        commission: j['commission'].d,
        realizedPnl: j['realized_pnl'].d,
      );

  bool get isBuy => side == 'buy';
}

class TPosition {
  final String symbol;
  final double qty, avgCost;
  final double? marketPrice, marketValue, unrealizedPnl, unrealizedPct, weightPct, stop, target, riskToStop;

  const TPosition({
    required this.symbol,
    required this.qty,
    required this.avgCost,
    this.marketPrice,
    this.marketValue,
    this.unrealizedPnl,
    this.unrealizedPct,
    this.weightPct,
    this.stop,
    this.target,
    this.riskToStop,
  });

  factory TPosition.fromJson(J j) => TPosition(
        symbol: j['symbol'].s ?? '',
        qty: j['qty'].d ?? 0,
        avgCost: j['avg_cost'].d ?? 0,
        marketPrice: j['market_price'].d,
        marketValue: j['market_value'].d,
        unrealizedPnl: j['unrealized_pnl'].d,
        unrealizedPct: j['unrealized_pct'].d,
        weightPct: j['weight_pct'].d,
        stop: j['stop'].d,
        target: j['target'].d,
        riskToStop: j['risk_to_stop'].d,
      );

  bool get isLong => qty > 0;

  /// P&L at [price] for the whole position.
  double pnlAt(double price) => (price - avgCost) * qty;
}

class TTicket {
  final String id, symbol, source, status;
  final J spec, check;
  final String? rationale, message, createdAt, expiresAt, account;
  final List<String> orderIds;

  const TTicket({
    required this.id,
    required this.symbol,
    required this.source,
    required this.status,
    required this.spec,
    required this.check,
    this.rationale,
    this.message,
    this.createdAt,
    this.expiresAt,
    this.account,
    this.orderIds = const [],
  });

  factory TTicket.fromJson(J j) => TTicket(
        id: j['id'].s ?? '',
        symbol: j['symbol'].s ?? '',
        source: j['source'].s ?? 'user',
        status: j['status'].s ?? 'pending',
        spec: j['spec'],
        check: j['check'],
        rationale: j['rationale'].s,
        message: j['message'].s,
        createdAt: j['created_at'].s,
        expiresAt: j['expires_at'].s,
        account: j['account'].s,
        orderIds: [for (final x in j['order_ids'].list) x.s ?? ''],
      );

  String get side => spec['side'].s ?? 'buy';
  double get qty => spec['qty'].d ?? 0;
  String get orderType => spec['order_type'].s ?? 'limit';
  double? get limitPrice => spec['limit_price'].d;
  double? get stopPrice => spec['stop_price'].d;
  double? get stopLoss => spec['stop_loss'].d;
  double? get takeProfit => spec['take_profit'].d;
  String get intent => check['intent'].s ?? 'open';

  /// A proposed change to a working order (spec: order_id, changes, role, and the order as it is).
  bool get isModify => spec['action'].s == 'modify';
  String? get orderId => spec['order_id'].s;
  double? get newStop => spec['changes']['stop_price'].d;
  double? get newLimit => spec['changes']['limit_price'].d;
  double? get newQty => spec['changes']['qty'].d;

  /// What a modify proposal changes, e.g. "stop 180.00 → 185.00".
  List<String> get changeTexts => [
        if (newStop != null) 'stop ${stopPrice?.toStringAsFixed(2) ?? '–'} → ${newStop!.toStringAsFixed(2)}',
        if (newLimit != null) 'limit ${limitPrice?.toStringAsFixed(2) ?? '–'} → ${newLimit!.toStringAsFixed(2)}',
        if (newQty != null) 'qty ${_q(qty)} → ${_q(newQty!)}',
      ];

  /// Entry price for display: limit, else stop trigger, else the market price at proposal time.
  /// For a modify proposal: the proposed new price.
  double? get entryPrice =>
      isModify ? (newStop ?? newLimit ?? stopPrice ?? limitPrice) : limitPrice ?? stopPrice ?? check['estimate']['price'].d;

  String get summary {
    if (isModify) {
      final role = (spec['role'].s ?? 'order').replaceAll('_', ' ');
      return 'Change $symbol $role: ${changeTexts.join(', ')}';
    }
    final t = switch (orderType) { 'market' => 'MKT', 'limit' => 'LMT', 'stop' => 'STP', _ => 'STP LMT' };
    final px = entryPrice;
    return '${side.toUpperCase()} ${_q(qty)} $symbol $t${px != null && orderType != 'market' ? ' @ ${px.toStringAsFixed(2)}' : ''}';
  }
}

class TTrade {
  final String id, symbol, side, status;
  final int openedAt;
  final int? closedAt;
  final double qty, maxQty, realizedPnl, commission, netPnl;
  final double? entryAvg, exitAvg, returnPct, rMultiple, plannedStop, unrealizedPnl, marketPrice;
  final int holdingSeconds;
  final List<TFill> fills;

  const TTrade({
    required this.id,
    required this.symbol,
    required this.side,
    required this.status,
    required this.openedAt,
    this.closedAt,
    required this.qty,
    required this.maxQty,
    required this.realizedPnl,
    required this.commission,
    required this.netPnl,
    this.entryAvg,
    this.exitAvg,
    this.returnPct,
    this.rMultiple,
    this.plannedStop,
    this.unrealizedPnl,
    this.marketPrice,
    this.holdingSeconds = 0,
    this.fills = const [],
  });

  factory TTrade.fromJson(J j) => TTrade(
        id: j['id'].s ?? '',
        symbol: j['symbol'].s ?? '',
        side: j['side'].s ?? 'long',
        status: j['status'].s ?? 'open',
        openedAt: j['opened_at'].i ?? 0,
        closedAt: j['closed_at'].i,
        qty: j['qty'].d ?? 0,
        maxQty: j['max_qty'].d ?? 0,
        realizedPnl: j['realized_pnl'].d ?? 0,
        commission: j['commission'].d ?? 0,
        netPnl: j['net_pnl'].d ?? 0,
        entryAvg: j['entry_avg'].d,
        exitAvg: j['exit_avg'].d,
        returnPct: j['return_pct'].d,
        rMultiple: j['r_multiple'].d,
        plannedStop: j['planned_stop'].d,
        unrealizedPnl: j['unrealized_pnl'].d,
        marketPrice: j['market_price'].d,
        holdingSeconds: j['holding_seconds'].i ?? 0,
        fills: [for (final f in j['fills'].list) TFill.fromJson(f)],
      );

  bool get closed => status == 'closed';
  bool get isLong => side == 'long';
}

class TStatus {
  final String kind;
  final bool connected, halted, armed, liveAllowed, canTrade;
  final bool? paper;
  final String? account, message;
  final String mode;
  final int pendingTickets;
  final J connection, halt;

  /// Accounts in the broker login: (id, paper). One login can hold several.
  final List<(String, bool?)> accounts;

  const TStatus({
    required this.kind,
    required this.connected,
    this.paper,
    this.account,
    this.message,
    this.mode = 'confirm',
    this.halted = false,
    this.armed = false,
    this.liveAllowed = false,
    this.canTrade = false,
    this.pendingTickets = 0,
    this.connection = const J(null),
    this.halt = const J(null),
    this.accounts = const [],
  });

  factory TStatus.fromJson(J j) => TStatus(
        kind: j['kind'].s ?? 'none',
        connected: j['connected'].b ?? false,
        paper: j['paper'].b,
        account: j['account'].s,
        message: j['message'].s,
        mode: j['mode'].s ?? 'confirm',
        halted: j['halted'].b ?? false,
        armed: j['armed'].b ?? false,
        liveAllowed: j['live_allowed'].b ?? false,
        canTrade: j['can_trade'].b ?? false,
        pendingTickets: j['pending_tickets'].i ?? 0,
        connection: j['connection'],
        halt: j['halt'],
        accounts: [for (final a in j['accounts'].list) (a['id'].s ?? '', a['paper'].b)],
      );

  bool get isLive => paper == false;
  bool get multiAccount => accounts.length > 1;
  String get brokerLabel => kind == 'ibkr' ? 'IBKR' : kind == 'sim' ? 'Simulator' : 'No broker';
}

/// Everything the chart overlays for one symbol.
class TChartData {
  final String symbol;
  final bool connected;
  final TPosition? position;
  final List<TOrder> orders;
  final List<TTicket> tickets;
  final List<TFill> fills;
  final List<TTrade> trades;

  const TChartData(this.symbol,
      {this.connected = false,
      this.position,
      this.orders = const [],
      this.tickets = const [],
      this.fills = const [],
      this.trades = const []});

  factory TChartData.fromJson(J j) => TChartData(
        j['symbol'].s ?? '',
        connected: j['connected'].b ?? false,
        position: j['position'].isNull ? null : TPosition.fromJson(j['position']),
        orders: [for (final o in j['orders'].list) TOrder.fromJson(o)],
        tickets: [for (final t in j['tickets'].list) TTicket.fromJson(t)],
        fills: [for (final f in j['fills'].list) TFill.fromJson(f)],
        trades: [for (final t in j['trades'].list) TTrade.fromJson(t)],
      );

  bool get isEmpty => position == null && orders.isEmpty && tickets.isEmpty && fills.isEmpty;
}

// ------------------------------------------------------------------------------------- helpers

String _q(double v) => v == v.roundToDouble() ? v.toStringAsFixed(0) : v.toString();

String qtyText(double v) => _q(v);

/// Shares for a fixed-fractional risk: risk [riskPct]% of [equity] between [entry] and [stop].
int sharesForRisk(double equity, double riskPct, double entry, double stop) {
  final per = (entry - stop).abs();
  if (equity <= 0 || per <= 0) return 0;
  return (equity * riskPct / 100 / per).floor();
}

/// Shares a dollar amount buys at [price].
int sharesForValue(double value, double price) => price <= 0 ? 0 : (value / price).floor();

/// Index of the bar containing [time]: the last bar starting at or before it (-1 when before the first).
int barIndexForTime(List<Bar> bars, int time) {
  var lo = 0, hi = bars.length - 1, ans = -1;
  while (lo <= hi) {
    final mid = (lo + hi) >> 1;
    if (bars[mid].time <= time) {
      ans = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return ans;
}

/// Fills grouped by bar index (fills before the first bar are dropped).
Map<int, List<TFill>> fillsByBar(List<Bar> bars, List<TFill> fills) {
  final out = <int, List<TFill>>{};
  for (final f in fills) {
    final i = barIndexForTime(bars, f.time);
    if (i >= 0) (out[i] ??= []).add(f);
  }
  return out;
}

/// Signed money: "+$1,234.50" / "−$12.00".
String money(num? v, {bool sign = true, int digits = 2}) {
  if (v == null || !v.isFinite) return '–';
  final a = v.abs().toStringAsFixed(digits);
  final parts = a.split('.');
  final whole = parts[0].replaceAllMapped(RegExp(r'\B(?=(\d{3})+(?!\d))'), (_) => ',');
  final body = '\$$whole${parts.length > 1 ? '.${parts[1]}' : ''}';
  if (!sign) return v < 0 ? '−$body' : body;
  return v > 0 ? '+$body' : v < 0 ? '−$body' : body;
}

/// "3d 4h", "2h 05m", "12m".
String holdingText(int seconds) {
  final d = seconds ~/ 86400, h = seconds % 86400 ~/ 3600, m = seconds % 3600 ~/ 60;
  if (d > 0) return '${d}d ${h}h';
  if (h > 0) return '${h}h ${m.toString().padLeft(2, '0')}m';
  return '${m}m';
}
