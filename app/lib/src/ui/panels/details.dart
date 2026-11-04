// Symbol details (under the watchlist): price, day and 52-week ranges, FinViz-style key stats,
// performance bars, the mechanical technical read, analyst targets and the company profile.
import 'dart:async';
import 'dart:js_interop';

import 'package:web/web.dart';

import '../../core/format.dart';
import '../../core/json.dart';
import '../../core/models.dart';
import '../api.dart';
import '../dom.dart';
import '../kit/state.dart';
import '../store.dart';

class DetailsPanel {
  final HTMLElement root = h('div.details');
  final Scope _scope = Scope();
  String _symbol = '';
  J? _snap, _fund, _tech;
  Quote? _quote;
  int _seq = 0;
  Timer? _poll;

  DetailsPanel() {
    void track() {
      for (final c in store.charts) {
        _scope.listen(c.symbol, (_) => _load());
      }
    }

    track();
    _scope.listen(store.active, (_) => _load());
    _scope.listen(store.layoutVersion, (_) => track());
    _poll = Timer.periodic(const Duration(seconds: 15), (_) => _refreshQuote());
    _load();
  }

  void dispose() {
    _poll?.cancel();
    _scope.dispose();
  }

  Future<void> _refreshQuote() async {
    if (!root.isConnected || document.hidden || _symbol.isEmpty) return;
    try {
      _quote = await api.quote(_symbol);
      _render();
    } catch (e) {
      console.warn('details quote: $e'.toJS);
    }
  }

  Future<void> _load() async {
    final sym = store.activeChart.symbol.value;
    if (sym == _symbol) return;
    _symbol = sym;
    final seq = ++_seq;
    _snap = _fund = _tech = null;
    _quote = null;
    _render();
    Future<void> get<T>(Future<T> f, void Function(T) set) async {
      try {
        final v = await f;
        if (seq != _seq) return;
        set(v);
        _render();
      } catch (e, st) {
        console.warn('details: $e\n$st'.toJS);
      }
    }

    await Future.wait([
      get(api.quote(sym), (Quote v) => _quote = v),
      get(api.snapshot(sym), (J v) => _snap = v['in_universe'].b == true ? v : null),
      get(api.fundamentals(sym), (J v) => _fund = v),
      get(api.summary(sym), (J v) => _tech = v),
    ]);
  }

  void _render() {
    final s = _snap, f = _fund, t = _tech, q = _quote;
    final name = s?['name'].s ?? f?['profile']['name'].s ?? '';
    final sector = s?['sector'].s ?? f?['profile']['sector'].s;
    final industry = s?['industry'].s ?? f?['profile']['industry'].s;
    final price = q?.price ?? s?['price'].d;
    final chg = q?.changePct ?? s?['change_pct'].d;
    final dir = chg == null ? '' : chg >= 0 ? '.up' : '.down';
    final lo52 = s?['low_52w'].d, hi52 = s?['high_52w'].d;
    replace(root, [
      h('div.det-head', [
        h('div.det-sym', [h('b', _symbol), h('span.muted.small', [name, if (s?['exchange'].s != null) ' · ${s!['exchange'].s}'])]),
        if (sector != null) h('div.muted.small', '$sector${industry != null ? ' · $industry' : ''}'),
        h('div.det-price', [
          h('span.big', price == null ? '…' : fmtPrice(price)),
          if (q?.change != null) h('span$dir', signed(q!.change!)),
          if (chg != null) h('span$dir', '${signed(chg)}%'),
        ]),
        if (q != null) h('div.muted.tiny', 'Quote: ${q.source}${q.feed != null ? ' (${q.feed})' : ''}'),
      ]),
      if (q != null || s != null) ...[
        _range('Day range', q?.dayLow ?? s?['bar_low'].d, q?.dayHigh ?? s?['bar_high'].d, price),
        if (lo52 != null && hi52 != null) _range('52-week range', lo52, hi52, price),
      ],
      if (s != null)
        h('div.stat-grid', [
          _stat('Market cap', compact(s['market_cap'].d)),
          _stat('P/E', n(s['pe'].d, 1)),
          _stat('EPS (TTM)', n(s['eps_ttm'].d)),
          _stat('Dividend', s['div_yield'].d == null ? '–' : '${n(s['div_yield'].d)}%'),
          _stat('P/S', n(s['ps'].d, 1)),
          _stat('Beta', n(s['beta'].d)),
          _stat('Sales Q/Q', pct(s['sales_qoq'].d)),
          _stat('EPS Q/Q', pct(s['eps_qoq'].d)),
          _stat('Profit margin', s['net_margin'].d == null ? '–' : '${n(s['net_margin'].d, 1)}%'),
          _stat('ROE', s['roe'].d == null ? '–' : '${n(s['roe'].d, 1)}%'),
          _stat('Avg volume', compact(s['avg_volume'].d)),
          _stat('Rel volume', n(s['rel_volume'].d)),
          _stat('ATR %', s['atr_pct'].d == null ? '–' : '${n(s['atr_pct'].d)}%'),
          _stat('RSI (14)', n(s['rsi14'].d, 1)),
          _stat('SMA50 / 200', '${pct(s['sma50_pct'].d)} / ${pct(s['sma200_pct'].d)}'),
          _stat('Short float', f?['short_interest_and_ownership']['short_pct_of_float'].d == null
              ? '–'
              : '${n(f!['short_interest_and_ownership']['short_pct_of_float'].d, 1)}%'),
        ]),
      if (s != null) _perf(s),
      if (t != null && !t.isNull) _technical(t),
      if (f != null && f['analysts']['recommendation'].truthy) _analysts(f),
      if (f != null && f['other']['next_earnings'].truthy)
        h('div.det-row', [h('span.muted', 'Next earnings'), h('b', f['other']['next_earnings'].s)]),
      if (f != null && f['profile']['summary'].truthy)
        h('details.det-about', [
          h('summary', 'About ${f['profile']['name'].s ?? _symbol}'),
          h('p.small', f['profile']['summary'].s),
          if (f['profile']['website'].truthy)
            h('a.small', f['profile']['website'].s, {'href': f['profile']['website'].s!, 'target': '_blank', 'rel': 'noreferrer'}),
        ]),
      if (s == null && f == null && t == null) h('div.muted.pad.small', 'Loading details…'),
    ]);
  }

  HTMLElement _stat(String label, String value) => h('div.stat', [h('span.muted', label), h('b', value)]);

  HTMLElement _range(String label, double? lo, double? hi, double? cur) {
    final ok = lo != null && hi != null && cur != null && hi > lo;
    final p = ok ? ((cur - lo) / (hi - lo)).clamp(0.0, 1.0) * 100 : 50.0;
    return h('div.range', [
      h('div.range-labels', [h('span', lo == null ? '–' : fmtPrice(lo)), h('span.muted', label), h('span', hi == null ? '–' : fmtPrice(hi))]),
      h('div.range-bar', h('div.range-marker', null, {'style': 'left:$p%'})),
    ]);
  }

  HTMLElement _perf(J s) {
    final items = [('1W', s['perf_1w'].d), ('1M', s['perf_1m'].d), ('3M', s['perf_3m'].d), ('6M', s['perf_6m'].d),
                   ('YTD', s['perf_ytd'].d), ('1Y', s['perf_1y'].d)];
    final maxAbs = items.map((e) => (e.$2 ?? 0).abs()).fold<double>(1, (a, b) => b > a ? b : a);
    return h('div.perf-bars', [
      for (final (label, v) in items)
        h('div.perf', [
          h('div.perf-track', h('div.perf-fill${(v ?? 0) >= 0 ? '.up' : '.down'}', null,
              {'style': 'height:${((v ?? 0).abs() / maxAbs * 100).clamp(2, 100)}%'})),
          h('span${(v ?? 0) >= 0 ? '.up' : '.down'}.tiny', v == null ? '–' : '${v.toStringAsFixed(1)}%'),
          h('span.muted.tiny', label),
        ]),
    ]);
  }

  HTMLElement _technical(J t) {
    final trend = t['trend']['score'].d, mom = t['momentum']['score'].d;
    return h('div.det-tech', [
      h('div.muted.tiny.upper', 'Technical read (daily)'),
      h('div.bias', t['mechanical_bias'].s ?? ''),
      h('div.gauges', [_gauge('Trend', trend), _gauge('Momentum', mom)]),
    ]);
  }

  HTMLElement _gauge(String label, double? v) {
    final x = v ?? 0;
    return h('div.gauge', [
      h('span.muted.tiny', label),
      h('div.gauge-bar', [
        h('div.gauge-mid'),
        h('div.gauge-fill${x >= 0 ? '.up' : '.down'}', null,
            {'style': x >= 0 ? 'left:50%;width:${x / 2}%' : 'left:${50 + x / 2}%;width:${-x / 2}%'}),
      ]),
      h('b.tiny', v == null ? '–' : v.toStringAsFixed(0)),
    ]);
  }

  HTMLElement _analysts(J f) {
    final a = f['analysts'];
    final lo = a['target_low'].d, hi = a['target_high'].d, mean = a['target_mean'].d;
    final price = _quote?.price;
    return h('div.det-analysts', [
      h('div.det-row', [
        h('span.muted', 'Analysts (${a['analyst_count'].i ?? '–'})'),
        h('b.rating.${(a['recommendation'].s ?? '').replaceAll('_', '-')}', (a['recommendation'].s ?? '').replaceAll('_', ' ').toUpperCase()),
      ]),
      if (lo != null && hi != null && mean != null) ...[
        _range('Price targets (mean ${fmtPrice(mean)})', lo, hi, price ?? mean),
        h('div.muted.tiny', 'Upside to mean target: ${pct(a['upside_to_mean_target_pct'].d)}'),
      ],
    ]);
  }
}
