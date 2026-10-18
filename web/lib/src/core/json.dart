/// Null-safe navigation over decoded JSON: `J(data)['trend']['score'].d` is null instead of throwing
/// when any step is missing or has the wrong type.
class J {
  final Object? v;
  const J(this.v);

  J operator [](Object key) {
    final x = v;
    if (x is Map && x.containsKey(key)) return J(x[key]);
    if (x is List && key is int && key >= 0 && key < x.length) return J(x[key]);
    return const J(null);
  }

  bool get isNull => v == null;
  double? get d => v is num ? (v as num).toDouble() : null;
  int? get i => v is num ? (v as num).toInt() : null;
  bool? get b => v is bool ? v as bool : null;

  /// The value as a string when it is a string or number, else null.
  String? get s {
    final x = v;
    if (x is String) return x;
    if (x is num || x is bool) return '$x';
    return null;
  }

  List<J> get list => v is List ? [for (final e in v as List) J(e)] : const [];
  Map<String, J> get map => v is Map ? {for (final e in (v as Map).entries) '${e.key}': J(e.value)} : const {};

  /// Truthiness in the JavaScript sense (used where the UI shows an item only when it is set).
  bool get truthy {
    final x = v;
    if (x == null || x == false || x == 0 || x == '') return false;
    if (x is List) return x.isNotEmpty;
    return true;
  }
}
