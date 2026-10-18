/// Maps epoch-second times to logical bar indexes, interpolating between bars and extrapolating
/// beyond the loaded data in trading time (e.g. a trade plan that ends in the future).
class TimeMapper {
  List<int> times = const [];
  int barSeconds = 86400;
  bool intraday = false;

  void set(List<int> times, int barSeconds, bool intraday) {
    this.times = times;
    this.barSeconds = barSeconds;
    this.intraday = intraday;
  }

  /// Approximate number of bars spanning [seconds] of calendar time (trading time only).
  double _barsFor(int seconds) {
    if (intraday) return seconds * (6.5 / 24) * (5 / 7) / barSeconds;
    if (barSeconds >= 7 * 86400) return seconds / barSeconds;
    return seconds / 86400 * (5 / 7); // business days
  }

  double get _calendarSecondsPerBar {
    if (intraday) return barSeconds / ((6.5 / 24) * (5 / 7));
    if (barSeconds >= 7 * 86400) return barSeconds.toDouble();
    return 86400 * (7 / 5);
  }

  double? logical(int t) {
    final ts = times;
    final n = ts.length;
    if (n == 0) return null;
    if (t <= ts[0]) return -_barsFor(ts[0] - t);
    if (t >= ts[n - 1]) return n - 1 + _barsFor(t - ts[n - 1]);
    var lo = 0, hi = n - 1;
    while (hi - lo > 1) {
      final mid = (lo + hi) >> 1;
      if (ts[mid] <= t) {
        lo = mid;
      } else {
        hi = mid;
      }
    }
    return lo + (t - ts[lo]) / (ts[hi] - ts[lo]);
  }

  /// Inverse of [logical]: logical index -> epoch seconds.
  int? time(double logical) {
    final ts = times;
    final n = ts.length;
    if (n == 0) return null;
    if (logical <= 0) return (ts[0] + logical * _calendarSecondsPerBar).round();
    if (logical >= n - 1) return (ts[n - 1] + (logical - (n - 1)) * _calendarSecondsPerBar).round();
    final i = logical.floor();
    return (ts[i] + (logical - i) * (ts[i + 1] - ts[i])).round();
  }
}
