// Calendars: weekly earnings board (Mon–Fri columns) and the economic calendar.
import 'dart:async';
import 'dart:js_interop';

import 'package:web/web.dart';

import '../../core/format.dart';
import '../../core/json.dart';
import '../api.dart';
import '../dom.dart';
import '../kit/icons.dart';
import '../store.dart';

final _highImpact = RegExp(
    r'\b(CPI|Consumer Price|PCE|Nonfarm|Payrolls|Unemployment Rate|FOMC|Fed |Interest Rate Decision|GDP|Retail Sales|ISM|PMI|PPI|Producer Price|JOLTS|Jobless Claims|Powell)',
    caseSensitive: false);

class CalendarPage {
  final HTMLElement root = h('div.page.calendar-page');
  final HTMLElement _controls = h('div.cal-controls');
  final HTMLElement _body = h('div.cal-body');
  String get _tab => store.pref<String>('calendar.tab');
  set _tab(String v) => store.setPref('calendar.tab', v);
  String get _universe => store.pref<String>('calendar.universe');
  set _universe(String v) => store.setPref('calendar.universe', v);
  double get _minCap => store.pref<double>('calendar.minCap');
  set _minCap(double v) => store.setPref('calendar.minCap', v);
  String get _country => store.pref<String>('calendar.country');
  set _country(String v) => store.setPref('calendar.country', v);
  DateTime _week = _monday(DateTime.now());
  int _seq = 0;
  bool _shown = false;

  CalendarPage() {
    append(root, [_controls, _body]);
  }

  static DateTime _monday(DateTime d) {
    final day = DateTime(d.year, d.month, d.day);
    // on weekends show the coming week
    if (day.weekday >= 6) return day.add(Duration(days: 8 - day.weekday));
    return day.subtract(Duration(days: day.weekday - 1));
  }

  String _iso(DateTime d) =>
      '${d.year}-${d.month.toString().padLeft(2, '0')}-${d.day.toString().padLeft(2, '0')}';

  void onShow() {
    if (!_shown) {
      _shown = true;
      _render();
    }
  }

  void _render() {
    final end = _week.add(const Duration(days: 4));
    const mon = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    replace(_controls, [
      h('div.seg', [
        for (final (k, label) in [('earnings', 'Earnings'), ('economic', 'Economic')])
          h('button.seg-btn${k == _tab ? '.on' : ''}', label, {'type': 'button'}, {
            'click': (_) {
              _tab = k;
              _render();
            },
          }),
      ]),
      h('div.week-nav', [
        h('button.icon-btn', icon('chev-left', size: 16), {'type': 'button', 'aria-label': 'Previous week'}, {
          'click': (_) {
            _week = _week.subtract(const Duration(days: 7));
            _render();
          },
        }),
        h('b', '${mon[_week.month - 1]} ${_week.day} – ${mon[end.month - 1]} ${end.day}, ${end.year}'),
        h('button.icon-btn', icon('chev-right', size: 16), {'type': 'button', 'aria-label': 'Next week'}, {
          'click': (_) {
            _week = _week.add(const Duration(days: 7));
            _render();
          },
        }),
        h('button.btn.small', 'This week', {'type': 'button'}, {
          'click': (_) {
            _week = _monday(DateTime.now());
            _render();
          },
        }),
      ]),
      h('span.spacer'),
      if (_tab == 'earnings') ...[
        _sel([('sp500', 'S&P 500'), ('all', 'All US'), ('watchlist:${store.watchlist.value}', 'Watchlist: ${store.watchlist.value}')],
            _universe, (v) {
          _universe = v;
          _render();
        }),
        _sel([('0', 'Any size'), ('2000000000', '≥ \$2B'), ('10000000000', '≥ \$10B'), ('100000000000', '≥ \$100B')],
            '${_minCap.round()}', (v) {
          _minCap = double.parse(v);
          _render();
        }),
      ] else
        _sel([('United States', 'United States'), ('Euro Zone', 'Euro Zone'), ('United Kingdom', 'United Kingdom'),
              ('Japan', 'Japan'), ('China', 'China'), ('', 'All countries')], _country, (v) {
          _country = v;
          _render();
        }),
    ]);
    _tab == 'earnings' ? _loadEarnings() : _loadEconomic();
  }

  HTMLElement _sel(List<(String, String)> opts, String value, void Function(String) onChange) {
    final s = h('select.input.select', [for (final (v, l) in opts) h('option', l, {'value': v})]) as HTMLSelectElement;
    s.value = value;
    s.addEventListener('change', ((Event _) => onChange(s.value)).toJS);
    return s;
  }

  Future<void> _loadEarnings() async {
    final seq = ++_seq;
    replace(_body, h('div.loading-block', [h('div.spinner'), h('div.muted', 'Loading earnings calendar…')]));
    try {
      final r = await request('/api/calendar/earnings?days=5&start=${_iso(_week)}&universe=${Uri.encodeComponent(_universe)}'
          '&min_market_cap=${_minCap.round()}');
      if (seq != _seq) return;
      final byDay = <String, List<J>>{};
      for (final e in r['events'].list) {
        byDay.putIfAbsent(e['date'].s ?? '', () => []).add(e);
      }
      const days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'];
      replace(_body, [
        h('div.muted.small.pad', '${r['count'].i ?? 0} reports · BMO = before market open, AMC = after market close'),
        h('div.earn-week', [
          for (var i = 0; i < 5; i++)
            () {
              final d = _week.add(Duration(days: i));
              final list = byDay[_iso(d)] ?? const [];
              final today = _iso(d) == _iso(DateTime.now());
              return h('div.earn-day${today ? '.today' : ''}', [
                h('div.earn-day-head', [h('b', days[i]), h('span.muted.small', ' ${d.day}'), h('span.spacer'), h('span.muted.tiny', '${list.length}')]),
                for (final timing in ['BMO', 'AMC', null])
                  if (list.any((e) => e['time'].s == timing)) ...[
                    h('div.earn-sub', timing == 'BMO' ? 'Before open' : timing == 'AMC' ? 'After close' : 'Time not set'),
                    for (final e in list.where((e) => e['time'].s == timing))
                      h('div.earn-item', [
                        h('div.row', [h('b', e['symbol'].s), h('span.spacer'), h('span.muted.tiny', compact(e['market_cap'].d))]),
                        h('div.muted.tiny.ellipsis', e['name'].s),
                        h('div.tiny', [
                          'EPS est ${e['eps_forecast'].d == null ? '–' : n(e['eps_forecast'].d)}',
                          h('span.muted', ' · last yr ${e['eps_last_year'].d == null ? '–' : n(e['eps_last_year'].d)}'),
                        ]),
                      ], {'data-tip': '${e['sector'].s ?? ''} · ${e['fiscal_quarter'].s ?? ''} · ${e['estimates'].i ?? 0} estimates'}, {
                        'click': (_) {
                          store.activeChart.symbol.value = e['symbol'].s!;
                          store.page.value = 'chart';
                        },
                      }),
                  ],
                if (list.isEmpty) h('div.muted.tiny.pad', 'No reports'),
              ]);
            }(),
        ]),
      ]);
    } catch (e) {
      if (seq == _seq) replace(_body, h('div.error-block', '$e'));
    }
  }

  Future<void> _loadEconomic() async {
    final seq = ++_seq;
    replace(_body, h('div.loading-block', [h('div.spinner'), h('div.muted', 'Loading economic calendar…')]));
    try {
      final r = await request('/api/calendar/economic?days=5&start=${_iso(_week)}&country=${Uri.encodeComponent(_country)}');
      if (seq != _seq) return;
      final byDay = <String, List<J>>{};
      for (final e in r['events'].list) {
        byDay.putIfAbsent(e['date'].s ?? '', () => []).add(e);
      }
      replace(_body, [
        if (r['events'].list.isEmpty) h('div.empty-state.tall', [icon('calendar', size: 30), h('div', 'No scheduled releases')]),
        for (final e in byDay.entries) ...[
          h('h4.econ-day', e.key),
          h('table.data-table.compact.econ-table', [
            h('thead', h('tr', [h('th', 'Time (GMT)'), h('th', 'Country'), h('th', 'Event'), h('th.num', 'Actual'), h('th.num', 'Consensus'), h('th.num', 'Previous')])),
            h('tbody', [
              for (final x in e.value)
                h('tr${_highImpact.hasMatch(x['event'].s ?? '') ? '.high' : ''}', [
                  h('td.muted', x['time_gmt'].s ?? ''),
                  h('td.muted', x['country'].s ?? ''),
                  h('td', [if (_highImpact.hasMatch(x['event'].s ?? '')) h('span.impact', '●'), ' ', x['event'].s]),
                  h('td.num', h('b', x['actual'].s ?? '')),
                  h('td.num.muted', x['consensus'].s ?? ''),
                  h('td.num.muted', x['previous'].s ?? ''),
                ]),
            ]),
          ]),
        ],
      ]);
    } catch (e) {
      if (seq == _seq) replace(_body, h('div.error-block', '$e'));
    }
  }
}
