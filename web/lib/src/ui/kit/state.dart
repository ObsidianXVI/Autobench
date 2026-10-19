import 'dart:async';

// Minimal observable state: Obs<T> holds a value and notifies listeners on change; Scope collects
// subscriptions so a component can drop them all when it goes away.

typedef Unsubscribe = void Function();

class Obs<T> {
  T _value;
  final List<void Function(T)> _listeners = [];
  Obs(this._value);

  T get value => _value;
  set value(T v) {
    if (identical(v, _value) || v == _value) return;
    _value = v;
    notify();
  }

  /// Applies an in-place change and notifies (for collections).
  void update(void Function(T v) change) {
    change(_value);
    notify();
  }

  void notify() {
    for (final l in List.of(_listeners)) {
      l(_value);
    }
  }

  Unsubscribe listen(void Function(T) fn, {bool now = false}) {
    _listeners.add(fn);
    if (now) fn(_value);
    return () => _listeners.remove(fn);
  }
}

class Scope {
  final List<Unsubscribe> _subs = [];

  void add(Unsubscribe u) => _subs.add(u);

  Unsubscribe listen<T>(Obs<T> obs, void Function(T) fn, {bool now = false}) {
    final u = obs.listen(fn, now: now);
    _subs.add(u);
    return u;
  }

  void dispose() {
    for (final u in _subs) {
      u();
    }
    _subs.clear();
  }
}

/// Runs the latest [call] once no new call has arrived for [ms] milliseconds.
class Debouncer {
  final int ms;
  Timer? _timer;
  Debouncer(this.ms);

  void call(void Function() fn) {
    _timer?.cancel();
    _timer = Timer(Duration(milliseconds: ms), fn);
  }

  void cancel() => _timer?.cancel();
}
