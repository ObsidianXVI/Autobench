// Pure chart math: Heikin-Ashi, price-scale transforms (linear / log / percent), date ranges.
import 'dart:math' as math;

import 'models.dart';
import 'time.dart';

/// Heikin-Ashi candles from regular bars (same times and volumes).
List<Bar> heikinAshi(List<Bar> bars) {
  final out = <Bar>[];
  double? prevO, prevC;
  for (final b in bars) {
    final c = (b.open + b.high + b.low + b.close) / 4;
    final o = prevO == null ? (b.open + b.close) / 2 : (prevO + prevC!) / 2;
    out.add(Bar(b.time, o, math.max(b.high, math.max(o, c)), math.min(b.low, math.min(o, c)), c, b.volume));
    prevO = o;
    prevC = c;
  }
  return out;
}

enum ScaleMode { linear, log, percent }

/// Maps prices to a transformed axis value and back. Percent mode is relative to [base] (the first
/// visible close); log mode uses natural logs. The y mapping is linear in the transformed value.
class PriceTransform {
  final ScaleMode mode;
  final double base;
  const PriceTransform(this.mode, [this.base = 1]);

  double to(double price) {
    switch (mode) {
      case ScaleMode.linear:
        return price;
      case ScaleMode.log:
        return price > 0 ? math.log(price) : double.nan;
      case ScaleMode.percent:
        return base == 0 ? double.nan : (price / base - 1) * 100;
    }
  }

  double from(double v) {
    switch (mode) {
      case ScaleMode.linear:
        return v;
      case ScaleMode.log:
        return math.exp(v);
      case ScaleMode.percent:
        return base * (1 + v / 100);
    }
  }
}

/// Visible-range presets of the bottom bar. Returns the start time (epoch seconds) for a range
/// ending at [lastTime], or null for "all".
int? rangeStart(String range, int lastTime) {
  final end = DateTime.fromMillisecondsSinceEpoch(lastTime * 1000, isUtc: true);
  DateTime? start;
  switch (range) {
    case '1D':
      start = end.subtract(const Duration(days: 1));
    case '5D':
      start = end.subtract(const Duration(days: 7));
    case '1M':
      start = DateTime.utc(end.year, end.month - 1, end.day);
    case '3M':
      start = DateTime.utc(end.year, end.month - 3, end.day);
    case '6M':
      start = DateTime.utc(end.year, end.month - 6, end.day);
    case 'YTD':
      start = DateTime.utc(end.year, 1, 1);
    case '1Y':
      start = DateTime.utc(end.year - 1, end.month, end.day);
    case '5Y':
      start = DateTime.utc(end.year - 5, end.month, end.day);
    case 'All':
      return null;
  }
  return start == null ? null : start.millisecondsSinceEpoch ~/ 1000;
}

/// The interval that suits a range preset (TradingView switches resolution with the range).
const rangeInterval = {
  '1D': '1m', '5D': '5m', '1M': '30m', '3M': '1h', '6M': '1d', 'YTD': '1d', '1Y': '1d', '5Y': '1wk', 'All': '1mo',
};

/// Index of the first bar at or after [t] (bars sorted by time); bars.length if none.
int firstIndexAtOrAfter(List<Bar> bars, int t) {
  var lo = 0, hi = bars.length;
  while (lo < hi) {
    final mid = (lo + hi) >> 1;
    if (bars[mid].time < t) {
      lo = mid + 1;
    } else {
      hi = mid;
    }
  }
  return lo;
}

/// US equity market session at [now] (UTC instant): pre, open, post or closed (weekends and NYSE
/// holidays included). Times in New York.
({String state, String label, int? nextChange}) marketSession(DateTime nowUtc) {
  final epoch = nowUtc.millisecondsSinceEpoch ~/ 1000;
  final c = civil(epoch, exchangeTime: true);
  final minutes = c.hour * 60 + c.minute;
  final ymd = '${c.year}-${c.month.toString().padLeft(2, '0')}-${c.day.toString().padLeft(2, '0')}';
  final holiday = nyseHolidays.contains(ymd);
  final early = nyseEarlyCloses.contains(ymd);
  final close = early ? 13 * 60 : 16 * 60;
  if (c.weekday >= 6 || holiday) return (state: 'closed', label: holiday ? 'Holiday' : 'Weekend', nextChange: null);
  int at(int m) => epoch + (m - minutes) * 60;
  if (minutes < 4 * 60) return (state: 'closed', label: 'Closed', nextChange: at(4 * 60));
  if (minutes < 9 * 60 + 30) return (state: 'pre', label: 'Pre-market', nextChange: at(9 * 60 + 30));
  if (minutes < close) return (state: 'open', label: early ? 'Open (early close)' : 'Market open', nextChange: at(close));
  if (minutes < 20 * 60) return (state: 'post', label: 'After hours', nextChange: at(20 * 60));
  return (state: 'closed', label: 'Closed', nextChange: null);
}

/// NYSE full-day holidays and 1 pm early closes, 2025–2027.
const nyseHolidays = {
  '2025-01-01', '2025-01-09', '2025-01-20', '2025-02-17', '2025-04-18', '2025-05-26', '2025-06-19', '2025-07-04',
  '2025-09-01', '2025-11-27', '2025-12-25',
  '2026-01-01', '2026-01-19', '2026-02-16', '2026-04-03', '2026-05-25', '2026-06-19', '2026-07-03', '2026-09-07',
  '2026-11-26', '2026-12-25',
  '2027-01-01', '2027-01-18', '2027-02-15', '2027-03-26', '2027-05-31', '2027-06-18', '2027-07-05', '2027-09-06',
  '2027-11-25', '2027-12-24',
};
const nyseEarlyCloses = {'2025-07-03', '2025-11-28', '2025-12-24', '2026-11-27', '2026-12-24', '2027-11-26'};

/// Diverging red/grey/green color for a change of [v] where [span] maps to full saturation
/// (FinViz-style map colors).
String changeColor(double? v, double span) {
  if (v == null || v.isNaN) return '#3b4252';
  final t = (v / span).clamp(-1.0, 1.0);
  // anchors: deep red, neutral grey, deep green
  const neg = [246, 53, 56], mid = [65, 69, 84], pos = [48, 204, 90];
  final to = t < 0 ? neg : pos;
  final k = t.abs();
  int mix(int i) => (mid[i] + (to[i] - mid[i]) * k).round();
  return 'rgb(${mix(0)},${mix(1)},${mix(2)})';
}

/// CSV text for a table (RFC 4180 quoting).
String toCsv(List<String> header, List<List<Object?>> rows) {
  String cell(Object? v) {
    final s = v == null ? '' : '$v';
    return s.contains(RegExp(r'[",\n]')) ? '"${s.replaceAll('"', '""')}"' : s;
  }

  return [header.map(cell).join(','), for (final r in rows) r.map(cell).join(',')].join('\n');
}
