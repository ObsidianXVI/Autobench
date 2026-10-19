import 'dart:math' as math;

// Number formatting shared by the chart, legend and panels.

/// Legend/indicator values: 2 decimals, 4 below 1, exponent for very large values.
String fmt(num? v) {
  if (v == null || v.isNaN) return '–';
  final a = v.abs();
  if (a >= 1e6) return v.toStringAsExponential(2);
  return v.toStringAsFixed(a >= 1 ? 2 : 4);
}

/// Prices on drawings: precision scaled to magnitude.
String fmtPrice(num p) {
  final a = p.abs();
  return p.toStringAsFixed(a >= 1000 ? 1 : a >= 10 ? 2 : a >= 1 ? 3 : 4);
}

/// Fixed decimals, or "–" when missing.
String n(num? v, [int digits = 2]) => v == null || !v.isFinite ? '–' : v.toStringAsFixed(digits);

/// Signed percentage: "+1.23%".
String pct(num? v) => v == null ? '–' : '${signed(v)}%';

/// Signed number: "+1.23". Values that round to zero print as "0.00" (no "-0.00").
String signed(num v, [int digits = 2]) {
  final s = v.toStringAsFixed(digits);
  if (double.parse(s) == 0) return (0).toStringAsFixed(digits);
  return '${v > 0 ? '+' : ''}$s';
}

/// Compact notation: 1.25K, 3.4M, 2.1B, 1.05T.
String compact(num? v, {int maxDigits = 2}) {
  if (v == null || !v.isFinite) return '–';
  const units = [(1e12, 'T'), (1e9, 'B'), (1e6, 'M'), (1e3, 'K')];
  final a = v.abs();
  for (final (size, suffix) in units) {
    if (a >= size) return '${_trim((v / size).toStringAsFixed(maxDigits))}$suffix';
  }
  return _trim(v.toStringAsFixed(a >= 100 ? 0 : maxDigits));
}

String _trim(String s) => s.contains('.') ? s.replaceFirst(RegExp(r'\.?0+$'), '') : s;

/// Price-axis label: two decimals (like the price format), more when the tick step is finer.
/// Steps are 1/2/5 x 10^k, so the digits a step needs follow from its exponent.
String axisPrice(double v, double step) {
  final stepDigits = step > 0 ? -(math.log(step) / math.ln10 + 1e-9).floor() : 0;
  return v.toStringAsFixed(math.max(2, math.min(8, stepDigits)));
}
