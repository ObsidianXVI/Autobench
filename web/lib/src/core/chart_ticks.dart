// Axis ticks and date helpers for the Portfolio charts (pure Dart, unit-tested in test/core_test.dart).
const _monthNames = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const _dayNames = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

/// A 'YYYY-MM-DD' date as UTC midnight.
DateTime dayUtc(String iso) => DateTime.parse('${iso.substring(0, 10)}T00:00:00Z');

String _iso(DateTime d) => d.toIso8601String().substring(0, 10);

/// "Mon 19 Oct 2026".
String longDate(String iso) {
  final d = dayUtc(iso);
  return '${_dayNames[d.weekday - 1]} ${d.day} ${_monthNames[d.month - 1]} ${d.year}';
}

/// Date ticks for a span: days, weeks, months, quarters or years, whichever gives a readable count.
List<(DateTime, String)> dateTicks(DateTime a, DateTime b) {
  final days = b.difference(a).inDays;
  final out = <(DateTime, String)>[];
  if (days <= 75) {
    final step = days <= 10 ? 1 : days <= 21 ? 3 : 7;
    var d = a;
    if (step == 7) d = d.add(Duration(days: (8 - d.weekday) % 7)); // Mondays
    for (; !d.isAfter(b); d = d.add(Duration(days: step))) {
      out.add((d, '${d.day} ${_monthNames[d.month - 1]}'));
    }
    return out;
  }
  final months = days / 30.4;
  final step = months <= 14 ? 1 : months <= 30 ? 3 : months <= 72 ? 6 : 12;
  var d = DateTime.utc(a.year, a.month + (a.day == 1 ? 0 : 1));
  while ((d.month - 1) % step != 0) {
    d = DateTime.utc(d.year, d.month + 1);
  }
  for (; !d.isAfter(b); d = DateTime.utc(d.year, d.month + step)) {
    out.add((d, step == 12 || d.month == 1 ? (step == 12 ? '${d.year}' : 'Jan ${d.year}') : _monthNames[d.month - 1]));
  }
  return out;
}

/// First and last day of a "2026" or "2026-03" period key.
(String, String) periodBounds(String key) {
  if (key.length == 4) return ('$key-01-01', '$key-12-31');
  final y = int.parse(key.substring(0, 4)), m = int.parse(key.substring(5, 7));
  return ('$key-01', _iso(DateTime.utc(y, m + 1, 0)));
}
