// Markets overview (FinViz home + macro desk): headline instruments, breadth, signal tables,
// sector performance, cross-asset board, macro indicators with the yield curve, calendars, news.
import 'dart:async';
import 'dart:js_interop';
import 'dart:math' as math;

import 'package:web/web.dart';

import '../../chart/engine.dart';
import '../../core/format.dart';
import '../../core/json.dart';
import '../../core/models.dart';
import '../../core/time.dart';
import '../api.dart';
import '../dom.dart';
import '../kit/overlay.dart';
import '../kit/spark.dart';
import '../store.dart';

const _headline = ['^GSPC', '^NDX', '^DJI', '^RUT', '^VIX', '^TNX', 'DX-Y.NYB', 'CL=F', 'GC=F', 'BTC-USD'];

class MarketsPage {
  final HTMLElement root = h('div.page.markets-page');
  final HTMLElement _strip = h('div.mk-strip');
  final HTMLElement _breadth = h('section.mk-card.mk-breadth');
  final HTMLElement _signals = h('section.mk-signals');
  final HTMLElement _sectors = h('section.mk-card.mk-sectors');
  final HTMLElement _board = h('section.mk-card.mk-board');
  final HTMLElement _macro = h('section.mk-card.mk-macro');
  final HTMLElement _side = h('div.mk-side');
  J? _markets, _breadthData, _macroData;
  String _sectorPeriod = '1d';
  String _boardGroup = 'Futures';
  bool _loaded = false;
  Timer? _poll;

  void dispose() => _poll?.cancel();

  MarketsPage() {
    append(root, [
      _strip,
      h('div.mk-grid', [
        h('div.mk-main', [_breadth, _signals, h('div.mk-two', [_sectors, _board]), _macro]),
        _side,
      ]),
    ]);
    for (final el in [_breadth, _sectors, _board, _macro]) {
      el.append(h('div.loading-block.small', [h('div.spinner'), h('div.muted', 'Loading…')]));
    }
  }

  void onShow() {
    if (!_loaded) {
      _loaded = true;
      _load();
      _poll = Timer.periodic(const Duration(seconds: 90), (_) {
        if (!root.isHidden) _load();
      });
    }
  }

  Future<void> _load() async {
    Future<void> get(String path, void Function(J) set) async {
      try {
        set(await request(path));
      } catch (e) {
        set(J({'error': '$e'}));
      }
    }

    await Future.wait([
      get('/api/markets', (v) {
        _markets = v;
        _renderStrip();
        _renderBoard();
      }),
      get('/api/breadth', (v) {
        _breadthData = v;
        _renderBreadth();
        _renderSignals();
        _renderSectors();
        if (v['building'].b == true) Timer(const Duration(seconds: 6), _load);
      }),
      get('/api/macro', (v) {
        _macroData = v;
        _renderMacro();
      }),
      _loadSide(),
    ]);
  }

  Map<String, J> get _instruments => {
        for (final g in _markets?['groups'].list ?? const <J>[])
          for (final i in g['items'].list) i['symbol'].s ?? '': i,
      };

  // ---------------------------------------------------------------- headline strip

  void _renderStrip() {
    final inst = _instruments;
    replace(_strip, [
      for (final sym in _headline)
        if (inst[sym] != null) _instrumentCard(inst[sym]!),
    ]);
    for (final c in qsa(_strip, 'canvas')) {
      final sym = c.getAttribute('data-symbol') ?? '';
      final data = inst[sym];
      if (data != null) drawSpark(c as HTMLCanvasElement, [for (final v in data['spark'].list) v.d]);
    }
  }

  HTMLElement _instrumentCard(J i) {
    final chg = i['change_pct'].d;
    final sym = i['symbol'].s ?? '';
    return h('div.mk-inst', [
      h('div.mk-inst-head', [h('b', i['name'].s ?? sym), h('span.muted.tiny', sym)]),
      h('div.mk-inst-price', [
        h('span', i['last'].d == null ? '–' : fmtPrice(i['last'].d!)),
        h(chg == null ? 'span' : chg >= 0 ? 'span.up' : 'span.down', chg == null ? '' : '${signed(chg)}%'),
      ]),
      h('canvas.mk-spark', null, {'data-symbol': sym}),
    ], {'data-tip': '1W ${pct(i['1w'].d)} · 1M ${pct(i['1m'].d)} · YTD ${pct(i['ytd'].d)} · 1Y ${pct(i['1y'].d)}'}, {
      'click': (_) => _open(sym),
    });
  }

  // ---------------------------------------------------------------- breadth

  String _int(num v) => v.round().toString().replaceAllMapped(RegExp(r'\B(?=(\d{3})+(?!\d))'), (_) => ',');

  void _renderBreadth() {
    final b = _breadthData;
    if (b == null) return;
    if (b['error'].truthy || b['building'].b == true) {
      replace(_breadth, [h('h3', 'Market breadth'), h('div.muted.small', b['error'].s ?? 'Building the market universe…')]);
      return;
    }
    final above50 = b['above_sma50_pct'].d ?? 0, above200 = b['above_sma200_pct'].d ?? 0;
    replace(_breadth, [
      h('div.title-row', [h('h3', 'Market breadth'), h('span.muted.small', '${_int(b['count'].i ?? 0)} US stocks, price ≥ \$1, liquid')]),
      h('div.split-grid', [
        _splitRow('Advancing', b['advancing'].i ?? 0, 'Declining', b['declining'].i ?? 0),
        _splitRow('New high', b['new_highs'].i ?? 0, 'New low', b['new_lows'].i ?? 0),
        _splitRow('Above SMA50', above50, 'Below SMA50', 100 - above50, percent: true),
        _splitRow('Above SMA200', above200, 'Below SMA200', 100 - above200, percent: true),
        _splitRow('Up volume', b['up_volume'].d ?? 0, 'Down volume', b['down_volume'].d ?? 0, compactNums: true),
        _splitRow('Oversold', b['oversold'].i ?? 0, 'Overbought', b['overbought'].i ?? 0),
      ]),
    ]);
  }

  HTMLElement _splitRow(String l, num lv, String r, num rv, {bool percent = false, bool compactNums = false}) {
    final total = lv + rv;
    final lp = total <= 0 ? 50.0 : lv / total * 100;
    String fmtv(num v) => percent ? '${v.toStringAsFixed(1)}%' : compactNums ? compact(v) : _int(v);
    return h('div.split', [
      h('div.split-labels', [
        h('span', [h('b.up', fmtv(lv)), ' $l']),
        h('span', ['$r ', h('b.down', fmtv(rv))]),
      ]),
      h('div.split-bar', [
        h('div.split-left', null, {'style': 'width:${lp.toStringAsFixed(2)}%'}),
        h('div.split-right', null, {'style': 'width:${(100 - lp).toStringAsFixed(2)}%'}),
      ]),
    ]);
  }

  // ---------------------------------------------------------------- signal tables

  void _renderSignals() {
    final b = _breadthData;
    if (b == null || b['signals'].isNull) {
      replace(_signals, null);
      return;
    }
    replace(_signals, [
      for (final e in b['signals'].map.entries)
        h('div.mk-card.sig-card', [
          h('div.title-row', [
            h('h4', e.value['label'].s),
            h('button.link.small', 'All ${e.value['total'].i ?? ''} ›', {'type': 'button'}, {
              'click': (_) {
                store.screenerState.value = {'signal': e.key, 'universe': 'all', 'filters': <Object>[], 'expression': '', 'page': 0};
                store.page.value = 'screener';
              },
            }),
          ]),
          h('table.mini-table', [
            for (final r in e.value['rows'].list)
              h('tr', [
                h('td.sym', r['symbol'].s),
                h('td.num', r['price'].d == null ? '' : fmtPrice(r['price'].d!)),
                h('td.num${(r['change_pct'].d ?? 0) >= 0 ? '.up' : '.down'}', r['change_pct'].d == null ? '' : '${signed(r['change_pct'].d!)}%'),
                h('td.num.muted', e.key == 'unusual_volume' ? '${n(r['rel_volume'].d, 1)}×' : compact(r['volume'].d)),
              ], {'data-tip': r['name'].s ?? ''}, {'click': (_) => _open(r['symbol'].s!)}),
            if (e.value['rows'].list.isEmpty) h('tr', h('td.muted', 'None today')),
          ]),
        ]),
    ]);
  }

  // ---------------------------------------------------------------- sectors

  void _renderSectors() {
    final b = _breadthData;
    if (b == null || b['sectors'].isNull) return;
    final rows = [...b['sectors'].list]..sort((a, c) => (c[_sectorPeriod].d ?? -999).compareTo(a[_sectorPeriod].d ?? -999));
    final maxAbs = rows.map((r) => (r[_sectorPeriod].d ?? 0).abs()).fold<double>(0.5, math.max);
    replace(_sectors, [
      h('div.title-row', [
        h('h3', 'Sector performance'),
        h('div.seg.inline', [
          for (final p in ['1d', '1w', '1m', '3m', 'ytd', '1y'])
            h('button.seg-btn${p == _sectorPeriod ? '.on' : ''}', p.toUpperCase(), {'type': 'button'}, {
              'click': (_) {
                _sectorPeriod = p;
                _renderSectors();
              },
            }),
        ]),
      ]),
      h('div.bars', [
        for (final r in rows)
          h('div.bar-row', [
            h('span.bar-label.ellipsis', r['name'].s),
            h('div.bar-track', [
              h('div.bar-mid'),
              h((r[_sectorPeriod].d ?? 0) >= 0 ? 'div.bar-fill.up' : 'div.bar-fill.down', null, {
                'style': () {
                  final v = r[_sectorPeriod].d ?? 0;
                  final w = v.abs() / maxAbs * 50;
                  return v >= 0 ? 'left:50%;width:$w%' : 'left:${50 - w}%;width:$w%';
                }(),
              }),
            ]),
            h('span.bar-val${(r[_sectorPeriod].d ?? 0) >= 0 ? '.up' : '.down'}', pct(r[_sectorPeriod].d)),
          ], {'data-tip': '${r['count'].i} stocks · ${r['advancing_pct'].d}% advancing today'}, {
            'click': (_) {
              store.heatmapState.value = {...store.heatmapState.value, 'period': _sectorPeriod};
              store.page.value = 'heatmap';
            },
          }),
      ]),
    ]);
  }

  // ---------------------------------------------------------------- cross-asset board

  void _renderBoard() {
    final groups = _markets?['groups'].list ?? const <J>[];
    if (groups.isEmpty) return;
    final g = groups.firstWhere((x) => x['name'].s == _boardGroup, orElse: () => groups.first);
    replace(_board, [
      h('div.title-row', [h('h3', 'Cross-asset')]),
      h('div.tabs-scroll', [
        for (final x in groups)
          if (x['name'].s != 'US indices')
            h('button.tab${x['name'].s == g['name'].s ? '.on' : ''}', x['name'].s, {'type': 'button'}, {
              'click': (_) {
                _boardGroup = x['name'].s!;
                _renderBoard();
              },
            }),
      ]),
      h('div.board-scroll', h('table.data-table.compact', [
        h('thead', h('tr', [h('th', 'Name'), h('th.num', 'Last'), h('th.num', 'Chg'), h('th.num', '1W'), h('th.num', '1M'), h('th.num', 'YTD'), h('th.num', '1Y'), h('th', '')])),
        h('tbody', [
          for (final i in g['items'].list)
            h('tr', [
              h('td', [h('b', i['name'].s), h('span.muted.tiny', '  ${i['symbol'].s}')]),
              h('td.num', i['last'].d == null ? (i['error'].s != null ? '–' : '') : fmtPrice(i['last'].d!)),
              for (final k in ['change_pct', '1w', '1m', 'ytd', '1y'])
                h('td.num${(i[k].d ?? 0) >= 0 ? '.up' : '.down'}', i[k].d == null ? '–' : '${signed(i[k].d!)}%'),
              h('td', h('canvas.row-spark', null, {'data-symbol': i['symbol'].s ?? ''})),
            ], {'data-tip': i['error'].s ?? ''}, {'click': (_) => _open(i['symbol'].s!)}),
        ]),
      ])),
    ]);
    final inst = _instruments;
    for (final c in qsa(_board, 'canvas')) {
      final data = inst[c.getAttribute('data-symbol') ?? ''];
      if (data != null) drawSpark(c as HTMLCanvasElement, [for (final v in data['spark'].list) v.d], area: false, lineWidth: 1.2);
    }
  }

  // ---------------------------------------------------------------- macro

  void _renderMacro() {
    final m = _macroData;
    if (m == null) return;
    if (m['error'].truthy) {
      replace(_macro, [h('h3', 'Macro'), h('div.error.small', m['error'].s)]);
      return;
    }
    final curveCanvas = h('canvas.curve-chart') as HTMLCanvasElement;
    replace(_macro, [
      h('div.title-row', [
        h('h3', 'Macro dashboard'),
        h('span.muted.small', 'As of ${m['as_of'].s ?? '–'} · sources: ${m['sources'].list.map((s) => s.s).join(', ')}'),
      ]),
      h('div.macro-layout', [
        h('div.curve-box', [
          h('div.title-row', [h('h4', 'Treasury yield curve'), h('div.curve-legend', [
            h('span.lg.now', 'Now'), h('span.lg.m1', '1M ago'), h('span.lg.y1', '1Y ago'),
          ])]),
          curveCanvas,
          h('div.curve-table', [
            for (final c in m['curve'].list)
              h('div.curve-cell', [h('span.muted.tiny', c['tenor'].s), h('b', n(c['now'].d)),
                h((c['now'].d ?? 0) >= (c['year_ago'].d ?? 0) ? 'span.up.tiny' : 'span.down.tiny',
                    c['year_ago'].d == null || c['now'].d == null ? '' : signed(c['now'].d! - c['year_ago'].d!))]),
          ]),
        ]),
        h('div.macro-groups', [
          for (final g in m['groups'].list)
            h('div.macro-group', [
              h('h4', g['name'].s),
              h('div.macro-cards', [for (final i in g['indicators'].list) _macroCard(i)]),
            ]),
        ]),
      ]),
    ]);
    Timer.run(() => _drawCurve(curveCanvas, m['curve'].list));
    for (final c in qsa(_macro, 'canvas.macro-spark')) {
      final id = c.getAttribute('data-id');
      for (final g in m['groups'].list) {
        for (final i in g['indicators'].list) {
          if (i['id'].s == id) drawSpark(c as HTMLCanvasElement, [for (final v in i['spark'].list) v.d], color: '#38bdf8', lineWidth: 1.3);
        }
      }
    }
  }

  HTMLElement _macroCard(J i) {
    final chg = i['change'].d;
    final better = i['better'].s ?? '';
    final good = chg == null || chg == 0 || better.isEmpty ? null : (better == 'up') == (chg > 0);
    return h('div.macro-card', [
      h('div.macro-name', i['name'].s),
      h('div.macro-val', [
        h('b', '${n(i['value'].d, (i['value'].d ?? 0).abs() >= 100 ? 0 : 2)}${i['unit'].s == '%' || (i['unit'].s ?? '').startsWith('%') ? '%' : ''}'),
        if (chg != null) h(good == null ? 'span.muted' : good ? 'span.up' : 'span.down', ' ${signed(chg, 2)}'),
      ]),
      h('canvas.macro-spark', null, {'data-id': i['id'].s ?? ''}),
      h('div.muted.tiny', '${i['date'].s} · 1y ago ${n(i['year_ago'].d)} · ${i['unit'].s}'),
    ], {'data-tip': [i['note'].s, 'Source: ${i['source'].s ?? 'FRED'} (${i['id'].s}). Click for history.'].whereType<String>().join('\n')}, {
      'click': (_) => _seriesDialog(i['id'].s!, i['name'].s ?? '', i['unit'].s ?? ''),
    });
  }

  void _drawCurve(HTMLCanvasElement canvas, List<J> curve) {
    if (curve.isEmpty) return;
    final dpr = window.devicePixelRatio;
    final r = canvas.getBoundingClientRect();
    final w = r.width, hgt = r.height;
    if (w <= 0) return;
    canvas.width = (w * dpr).round();
    canvas.height = (hgt * dpr).round();
    final ctx = canvas.getContext('2d') as CanvasRenderingContext2D;
    ctx.setTransform(dpr.toJS, 0, 0, dpr, 0, 0);
    final vals = [for (final c in curve) for (final k in ['now', 'month_ago', 'year_ago']) c[k].d].whereType<double>();
    var lo = vals.reduce(math.min), hi = vals.reduce(math.max);
    final pad = (hi - lo) * 0.15 + 0.05;
    lo -= pad;
    hi += pad;
    const left = 30.0, bottom = 18.0, top = 6.0;
    double x(int i) => left + i / (curve.length - 1) * (w - left - 8);
    double y(double v) => top + (hi - v) / (hi - lo) * (hgt - top - bottom);
    final styles = window.getComputedStyle(document.documentElement!);
    final muted = styles.getPropertyValue('--text-muted').trim();
    ctx.font = '10px ui-sans-serif, system-ui, sans-serif';
    ctx.fillStyle = muted.toJS;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    for (var k = 0; k <= 3; k++) {
      final v = lo + (hi - lo) * k / 3;
      ctx.fillText(v.toStringAsFixed(1), left - 4, y(v));
      ctx.strokeStyle = 'rgba(148,163,184,0.12)'.toJS;
      ctx.beginPath();
      ctx.moveTo(left, y(v));
      ctx.lineTo(w - 8, y(v));
      ctx.stroke();
    }
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    for (var i = 0; i < curve.length; i++) {
      ctx.fillText(curve[i]['tenor'].s ?? '', x(i), hgt - bottom + 4);
    }
    for (final (k, color, dash) in [('year_ago', '#64748b', true), ('month_ago', '#a78bfa', true), ('now', '#38bdf8', false)]) {
      ctx.strokeStyle = color.toJS;
      ctx.lineWidth = k == 'now' ? 2.2 : 1.4;
      ctx.setLineDash((dash ? [4.toJS, 3.toJS] : <JSNumber>[]).toJS);
      ctx.beginPath();
      var started = false;
      for (var i = 0; i < curve.length; i++) {
        final v = curve[i][k].d;
        if (v == null) continue;
        started ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v));
        started = true;
      }
      ctx.stroke();
    }
    ctx.setLineDash(<JSNumber>[].toJS);
  }

  /// Full history of a macro series in an interactive chart (reuses the chart engine).
  Future<void> _seriesDialog(String id, String name, String unit) async {
    final host = h('div.series-chart');
    Dialog.open('$name ($unit)', h('div', [host, h('div.muted.tiny', 'Source: FRED / Treasury / BLS · id $id · drag to pan, scroll to zoom')]),
        width: '880px');
    try {
      final r = await request('/api/macro/series/$id');
      final bars = [
        for (final p in r['points'].list)
          if (p['value'].d != null) Bar(p['time'].i!, p['value'].d!, p['value'].d!, p['value'].d!, p['value'].d!, 0),
      ];
      final engine = ChartEngine(host)
        ..showVolume = false
        ..chartType = ChartType.area;
      engine.setBars(bars, intraday: false);
      engine.setVisibleRange(0, bars.length + 2.0);
    } catch (e) {
      replace(host, h('div.error.pad', '$e'));
    }
  }

  // ---------------------------------------------------------------- side column

  Future<void> _loadSide() async {
    final results = await Future.wait([
      attempt(request('/api/calendar/economic?days=1')),
      attempt(request('/api/calendar/earnings?days=5&universe=sp500')),
      attempt(api.news(null, limit: 20)),
    ]);
    final [econ, earn, news] = results;
    replace(_side, [
      h('section.mk-card', [
        h('div.title-row', [h('h3', "Today's economy"), h('button.link.small', 'Calendar ›', {'type': 'button'}, {'click': (_) => store.page.value = 'calendar'})]),
        if (econ == null || econ['events'].list.isEmpty) h('div.muted.small', 'No US releases scheduled today.'),
        for (final e in econ?['events'].list ?? const <J>[])
          h('div.econ-row', [
            h('span.muted.tiny', e['time_gmt'].s == null ? '' : '${e['time_gmt'].s} GMT'),
            h('span.ellipsis', e['event'].s),
            h('span.tiny', [
              if (e['actual'].s != null) h('b', e['actual'].s),
              if (e['consensus'].s != null) h('span.muted', ' est ${e['consensus'].s}'),
              if (e['previous'].s != null) h('span.muted', ' prev ${e['previous'].s}'),
            ]),
          ]),
      ]),
      h('section.mk-card', [
        h('div.title-row', [h('h3', 'Earnings this week'), h('button.link.small', 'All ›', {'type': 'button'}, {'click': (_) => store.page.value = 'calendar'})]),
        if (earn == null || earn['events'].list.isEmpty) h('div.muted.small', 'No S&P 500 reports in the next few days.'),
        for (final e in (earn?['events'].list ?? const <J>[]).take(16))
          h('div.earn-row', [
            h('span.muted.tiny', _dayLabel(e['date'].s ?? '')),
            h('b', e['symbol'].s),
            h('span.time-chip', e['time'].s ?? '–'),
            h('span.muted.small.ellipsis', e['name'].s),
            h('span.muted.tiny', compact(e['market_cap'].d)),
          ], null, {'click': (_) => _open(e['symbol'].s!)}),
      ]),
      h('section.mk-card.mk-news', [
        h('h3', 'Market news'),
        for (final it in news?['items'].list ?? const <J>[])
          h('a.news-item', [
            h('div.news-head', it['headline'].s ?? ''),
            h('div.muted.tiny', [ago(it['published_at'].s ?? ''), ' · ${it['source'].s ?? ''}',
              for (final s in it['symbols'].list.take(3)) h('span.news-sym', s.s)]),
          ], {'href': it['url'].s ?? '#', 'target': '_blank', 'rel': 'noreferrer'}),
      ]),
    ]);
  }

  String _dayLabel(String iso) {
    final d = DateTime.tryParse(iso);
    if (d == null) return iso;
    const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    return '${days[d.weekday - 1]} ${d.day}';
  }

  void _open(String sym) {
    store.activeChart.symbol.value = sym;
    store.page.value = 'chart';
  }
}
