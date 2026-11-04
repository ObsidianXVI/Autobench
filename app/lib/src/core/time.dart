// Calendar helpers for bar timestamps (epoch seconds). Intraday bars are shown in exchange time
// (America/New_York); daily and longer bars are dated in UTC, as the backend stamps them.

const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/// Broken-down wall-clock time.
class Civil {
  final int year, month, day, hour, minute, weekday; // month 1-12, weekday 1=Mon..7=Sun
  const Civil(this.year, this.month, this.day, this.hour, this.minute, this.weekday);
}

/// UTC offset of New York at the given instant, in seconds (-4h in daylight time, -5h otherwise).
/// US rules since 2007: DST from the second Sunday of March 02:00 local to the first Sunday of
/// November 02:00 local.
int nyOffsetSeconds(int epochSeconds) {
  final utc = DateTime.fromMillisecondsSinceEpoch(epochSeconds * 1000, isUtc: true);
  final y = utc.year;
  final start = _nthSunday(y, 3, 2).add(const Duration(hours: 7)); // 02:00 EST = 07:00 UTC
  final end = _nthSunday(y, 11, 1).add(const Duration(hours: 6)); // 02:00 EDT = 06:00 UTC
  final dst = !utc.isBefore(start) && utc.isBefore(end);
  return dst ? -4 * 3600 : -5 * 3600;
}

DateTime _nthSunday(int year, int month, int n) {
  final first = DateTime.utc(year, month, 1);
  final toSunday = (DateTime.sunday - first.weekday) % 7;
  return DateTime.utc(year, month, 1 + toSunday + 7 * (n - 1));
}

/// Wall-clock fields for a bar time: New York time when [exchangeTime], else UTC.
Civil civil(int epochSeconds, {required bool exchangeTime}) {
  final shift = exchangeTime ? nyOffsetSeconds(epochSeconds) : 0;
  final t = DateTime.fromMillisecondsSinceEpoch((epochSeconds + shift) * 1000, isUtc: true);
  return Civil(t.year, t.month, t.day, t.hour, t.minute, t.weekday);
}

String _two(int v) => v.toString().padLeft(2, '0');

/// Full timestamp for the legend and crosshair: "Oct 17, 2026" or "Oct 17, 14:30".
String formatBarTime(int t, bool intraday) {
  final c = civil(t, exchangeTime: intraday);
  return intraday
      ? '${months[c.month - 1]} ${c.day}, ${_two(c.hour)}:${_two(c.minute)}'
      : '${months[c.month - 1]} ${c.day}, ${c.year}';
}

/// Importance of the boundary between two consecutive bars, used to pick time-axis labels.
enum TickWeight { none, minute, hour, day, month, year }

TickWeight tickWeight(Civil prev, Civil cur) {
  if (cur.year != prev.year) return TickWeight.year;
  if (cur.month != prev.month) return TickWeight.month;
  if (cur.day != prev.day) return TickWeight.day;
  if (cur.hour != prev.hour) return TickWeight.hour;
  if (cur.minute != prev.minute) return TickWeight.minute;
  return TickWeight.none;
}

String tickLabel(Civil c, TickWeight w, bool intraday) {
  switch (w) {
    case TickWeight.year:
      return '${c.year}';
    case TickWeight.month:
      return months[c.month - 1];
    case TickWeight.day:
      return intraday ? '${months[c.month - 1]} ${c.day}' : '${c.day}';
    case TickWeight.hour:
    case TickWeight.minute:
    case TickWeight.none:
      return '${_two(c.hour)}:${_two(c.minute)}';
  }
}

/// "just now", "5m ago", "3h ago", "2d ago" for ISO timestamps (naive ones are treated as UTC).
String ago(String iso, {DateTime? now}) {
  final t = DateTime.tryParse(iso.endsWith('Z') || iso.contains('+') ? iso : '${iso}Z');
  if (t == null) return '';
  final s = (now ?? DateTime.now()).difference(t).inSeconds;
  if (s < 60) return 'just now';
  if (s < 3600) return '${s ~/ 60}m ago';
  if (s < 86400) return '${s ~/ 3600}h ago';
  return '${s ~/ 86400}d ago';
}

/// Local wall-clock "HH:MM:SS" for the activity feed.
String clockTime(DateTime t) => '${_two(t.hour)}:${_two(t.minute)}:${_two(t.second)}';
