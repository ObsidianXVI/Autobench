// Unit tests for the browser-independent core (time zones, scales, mapping, formatting, markdown).
// Run: dart run test/core_test.dart   (no test framework needed; exits non-zero on failure)
import 'dart:io';

import '../lib/src/core/chartmath.dart';
import '../lib/src/core/format.dart';
import '../lib/src/core/geometry.dart';
import '../lib/src/core/json.dart';
import '../lib/src/core/layers.dart';
import '../lib/src/core/mapper.dart';
import '../lib/src/core/markdown.dart';
import '../lib/src/core/models.dart';
import '../lib/src/core/ticks.dart';
import '../lib/src/core/time.dart';
import '../lib/src/core/trading.dart';
import '../lib/src/core/treemap.dart';
import '../lib/src/core/undo.dart';

var _failures = 0, _passes = 0;
String _group = '';

void group(String name, void Function() body) {
  _group = name;
  body();
}

void check(String what, Object? actual, Object? expected) {
  final ok = actual is double && expected is num
      ? (actual - expected).abs() < 1e-9
      : actual.toString() == expected.toString();
  if (ok) {
    _passes++;
  } else {
    _failures++;
    stderr.writeln('FAIL [$_group] $what\n  expected: $expected\n  actual:   $actual');
  }
}

int utc(int y, int m, int d, [int h = 0, int min = 0]) => DateTime.utc(y, m, d, h, min).millisecondsSinceEpoch ~/ 1000;

final _pending = <Future<void>>[];

Future<void> main() async {
  group('new york offset', () {
    // 2026: DST from Sun Mar 8 07:00 UTC to Sun Nov 1 06:00 UTC
    check('winter', nyOffsetSeconds(utc(2026, 1, 15, 15)), -5 * 3600);
    check('summer', nyOffsetSeconds(utc(2026, 7, 1, 15)), -4 * 3600);
    check('just before spring forward', nyOffsetSeconds(utc(2026, 3, 8, 6, 59)), -5 * 3600);
    check('at spring forward', nyOffsetSeconds(utc(2026, 3, 8, 7)), -4 * 3600);
    check('just before fall back', nyOffsetSeconds(utc(2026, 11, 1, 5, 59)), -4 * 3600);
    check('at fall back', nyOffsetSeconds(utc(2026, 11, 1, 6)), -5 * 3600);
    check('2024 rule (Mar 10)', nyOffsetSeconds(utc(2024, 3, 10, 8)), -4 * 3600);
  });

  group('civil time and labels', () {
    final open = utc(2026, 10, 16, 13, 30); // 09:30 EDT
    final c = civil(open, exchangeTime: true);
    check('exchange hour', '${c.hour}:${c.minute}', '9:30');
    check('utc for daily', civil(utc(2026, 10, 16), exchangeTime: false).day, 16);
    check('daily label', formatBarTime(utc(2026, 10, 16), false), 'Oct 16, 2026');
    check('intraday label', formatBarTime(open, true), 'Oct 16, 09:30');
    check('ago minutes', ago('2026-10-19T10:00:00', now: DateTime.utc(2026, 10, 19, 10, 5)), '5m ago');
    check('ago days', ago('2026-10-17T10:00:00Z', now: DateTime.utc(2026, 10, 19, 10, 5)), '2d ago');
  });

  group('time mapper', () {
    final m = TimeMapper()..set([utc(2026, 1, 5), utc(2026, 1, 6), utc(2026, 1, 7), utc(2026, 1, 8)], 86400, false);
    check('exact bar', m.logical(utc(2026, 1, 6)), 1.0);
    check('between bars', m.logical(utc(2026, 1, 6, 12)), 1.5);
    check('one week after last bar ≈ 5 trading days', m.logical(utc(2026, 1, 15)), 3 + 5.0);
    check('before first bar', m.logical(utc(2026, 1, 4)), -5 / 7);
    check('round trip inside', m.time(1.5), utc(2026, 1, 6, 12));
    check('round trip future', m.time(m.logical(utc(2026, 2, 2))!), utc(2026, 2, 2));
    check('empty', TimeMapper().logical(0), null);
  });

  group('price ticks', () {
    final t = niceTicks(101.3, 118.9, 6);
    check('step', t.step, 5.0);
    check('values', t.values, [105.0, 110.0, 115.0]);
    check('small step', niceTicks(0.012, 0.031, 4).step, 0.005);
    check('degenerate', niceTicks(5, 5, 4).values, []);
    check('axis price ≥2 decimals', axisPrice(250, 10), '250.00');
    check('axis price finer step', axisPrice(0.0125, 0.0005), '0.0125');
  });

  group('time ticks', () {
    // business days across a month and a year boundary
    final days = <int>[];
    var d = DateTime.utc(2025, 11, 3);
    while (days.length < 60) {
      if (d.weekday <= 5) days.add(d.millisecondsSinceEpoch ~/ 1000);
      d = d.add(const Duration(days: 1));
    }
    final civ = [for (final t in days) civil(t, exchangeTime: false)];
    final ticks = timeTicks(civ, 0, days.length - 1, 15, false);
    final labels = ticks.map((t) => t.label).toList();
    check('year boundary labelled', labels.contains('2026'), true);
    check('month boundary labelled', labels.contains('Dec'), true);
    check('spacing respected', [for (var i = 1; i < ticks.length; i++) ticks[i].index - ticks[i - 1].index >= 15].every((x) => x), true);
  });

  group('formatting', () {
    check('compact billions', compact(2.345e9), '2.35B');
    check('compact trims zeros', compact(1.5e6), '1.5M');
    check('compact small', compact(42), '42');
    check('pct', pct(1.234), '+1.23%');
    check('pct negative', pct(-0.5), '-0.50%');
    check('fmt', fmt(0.12345), '0.1235');
    check('fmt null', fmt(null), '–');
    check('fmtPrice', fmtPrice(1234.56), '1234.6');
  });

  group('geometry', () {
    check('segment distance', segDist(const XY(5, 5), const XY(0, 0), const XY(10, 0)), 5.0);
    check('past segment end', segDist(const XY(13, 4), const XY(0, 0), const XY(10, 0)), 5.0);
    check('line extension', lineAt(const XY(0, 0), const XY(10, 10), 20).y, 20.0);
    check('hex alpha', hexAlpha('#22c55e', 0.5), 'rgba(34,197,94,0.5)');
  });

  group('json and models', () {
    final j = J({'a': {'b': [1, 2.5, 'x']}});
    check('nested', j['a']['b'][1].d, 2.5);
    check('missing', j['a']['zz']['q'].d, null);
    check('wrong type', j['a']['b'][2].d, null);
    final view = ChartViewState.fromJson(J({
      'symbol': 'AMD', 'interval': '1h', 'from_time': 100, 'indicators': [{'name': 'sma', 'params': {'length': 20}}],
    }));
    check('view', '${view.symbol} ${view.interval} ${view.fromTime} ${view.indicators.first.label}', 'AMD 1h 100 sma(20)');
    check('changing symbol resets range', view.copyWith(symbol: 'NVDA').fromTime, null);
    check('changing indicators keeps range', view.copyWith(indicators: const []).fromTime, 100);
    check('intraday', [isIntraday('5m'), isIntraday('1h'), isIntraday('1d'), isIntraday('1mo')], [true, true, false, false]);
  });

  group('markdown', () {
    final blocks = parseMarkdown('## Plan\nBuy **dips** to `ema(20)`\n\n- one\n- two\n\n| a | b |\n|---|---|\n| 1 | 2 |');
    check('block kinds', blocks.map((b) => b.runtimeType).toList(), [Heading, Paragraph, ListBlock, Table]);
    final para = blocks[1] as Paragraph;
    check('inline kinds', para.lines.first.map((i) => i.runtimeType).toList(), [Plain, Bold, Plain, Code]);
    check('list items', (blocks[2] as ListBlock).items.length, 2);
    check('table rows', (blocks[3] as Table).rows.length, 1);
    check('ordered list', (parseMarkdown('1. a\n2. b').single as ListBlock).ordered, true);
  });

  group('treemap', () {
    const bounds = Rect(0, 0, 600, 400);
    final weights = <double>[6, 6, 4, 3, 2, 2, 1, 0];
    final rects = squarify(weights, bounds);
    final total = weights.reduce((a, b) => a + b);
    var areaOk = true, inside = true, worstAspect = 0.0;
    for (var i = 0; i < weights.length; i++) {
      final r = rects[i];
      if ((r.area - weights[i] / total * bounds.area).abs() > 1e-6) areaOk = false;
      if (r.x < -1e-9 || r.y < -1e-9 || r.x + r.w > 600 + 1e-6 || r.y + r.h > 400 + 1e-6) inside = false;
      if (r.area > 0) worstAspect = [worstAspect, r.w / r.h, r.h / r.w].reduce((a, b) => a > b ? a : b);
    }
    check('areas proportional to weights', areaOk, true);
    check('all rects inside bounds', inside, true);
    check('reasonable aspect ratios', worstAspect < 4, true);
    var overlap = false;
    for (var i = 0; i < rects.length; i++) {
      for (var j = i + 1; j < rects.length; j++) {
        final a = rects[i], b = rects[j];
        final ox = (a.x + a.w).clamp(0, 1e9) - (a.x > b.x ? a.x : b.x);
        final w = (a.x + a.w < b.x + b.w ? a.x + a.w : b.x + b.w) - (a.x > b.x ? a.x : b.x);
        final h = (a.y + a.h < b.y + b.h ? a.y + a.h : b.y + b.h) - (a.y > b.y ? a.y : b.y);
        if (w > 1e-6 && h > 1e-6 && ox > 0) overlap = true;
      }
    }
    check('no overlaps', overlap, false);
    check('zero weight -> empty', rects.last.area, 0.0);
    check('empty input', squarify([], bounds), []);
  });

  group('chart math', () {
    final bars = [const Bar(0, 10, 12, 9, 11, 100), const Bar(1, 11, 13, 10, 12, 100)];
    final ha = heikinAshi(bars);
    check('HA close = ohlc4', ha[0].close, (10 + 12 + 9 + 11) / 4);
    check('HA first open = (o+c)/2', ha[0].open, 10.5);
    check('HA second open = prev (o+c)/2', ha[1].open, (ha[0].open + ha[0].close) / 2);
    check('HA high ≥ open/close', ha[1].high >= ha[1].open && ha[1].high >= ha[1].close, true);
    const log = PriceTransform(ScaleMode.log);
    check('log round trip', log.from(log.to(123.4)), 123.4);
    const pct = PriceTransform(ScaleMode.percent, 200);
    check('percent', pct.to(220), 10.0);
    check('percent inverse', pct.from(-50), 100.0);
    final last = utc(2026, 10, 16);
    check('YTD start', rangeStart('YTD', last), utc(2026, 1, 1));
    check('1Y start', rangeStart('1Y', last), utc(2025, 10, 16));
    check('All', rangeStart('All', last), null);
    final b = [for (var i = 0; i < 5; i++) Bar(i * 10, 1, 1, 1, 1, 1)];
    check('first index at/after', [firstIndexAtOrAfter(b, 15), firstIndexAtOrAfter(b, 20), firstIndexAtOrAfter(b, 99)], [2, 2, 5]);
  });

  group('market session', () {
    check('open', marketSession(DateTime.utc(2026, 10, 16, 15)).state, 'open'); // 11:00 EDT Friday
    check('pre', marketSession(DateTime.utc(2026, 10, 16, 12)).state, 'pre'); // 08:00 EDT
    check('post', marketSession(DateTime.utc(2026, 10, 16, 21)).state, 'post'); // 17:00 EDT
    check('weekend', marketSession(DateTime.utc(2026, 10, 17, 15)).state, 'closed');
    check('holiday', marketSession(DateTime.utc(2026, 11, 26, 15)).label, 'Holiday'); // Thanksgiving
    check('early close', marketSession(DateTime.utc(2026, 11, 27, 18, 30)).state, 'post'); // 13:30 EST
    check('next change is the close', marketSession(DateTime.utc(2026, 10, 16, 15)).nextChange, utc(2026, 10, 16, 20));
  });

  group('colors & csv', () {
    check('neutral', changeColor(0, 3), 'rgb(65,69,84)');
    check('full green', changeColor(5, 3), 'rgb(48,204,90)');
    check('full red', changeColor(-3, 3), 'rgb(246,53,56)');
    check('null', changeColor(null, 3), '#3b4252');
    check('csv quoting', toCsv(['a', 'b'], [[1, 'x,y'], [null, 'say "hi"']]), 'a,b\n1,"x,y"\n,"say ""hi"""');
  });

  group('undo stack', () {
    final log = <String>[];
    final u = UndoStack(limit: 2);
    for (final n in ['a', 'b', 'c']) {
      u.push(Command(n, undo: () async => log.add('undo $n'), redo: () async => log.add('redo $n')));
    }
    check('limit drops oldest', u.undoLabel, 'c');
    Future<void> run() async {
      await u.undo();
      await u.undo();
      check('stops at limit', await u.undo(), null);
      await u.redo();
      check('order', log, ['undo c', 'undo b', 'redo b']);
      u.push(Command('d', undo: () async {}, redo: () async {}));
      check('new op clears redo', u.canRedo, false);
    }

    _pending.add(run());
  });

  group('layers', () {
    final c = SymbolConfig.builtin();
    check('builtin items', c.layers.single.items.map((i) => i.id).toList(), ['price', 'volume', 'sma50', 'sma200', 'rsi']);
    final round = SymbolConfig.fromJson(J(c.toJson()));
    check('json round trip', round.toJson().toString(), c.toJson().toString());
    final overlay = {'sma', 'ema', 'bbands'};
    bool isOv(String n) => overlay.contains(n);
    check('auto panes', assignPanes(c.shown, isOv), {'price': 0, 'volume': 0, 'sma50': 0, 'sma200': 0, 'rsi': 1});

    final l2 = c.addLayer('Momentum');
    check('new layer on top and active', [c.layers.first.id == l2.id, c.activeLayer == l2.id], [true, true]);
    c.addItem(LayerItem.indicator('macd', 'macd'));
    c.addItem(LayerItem.compare('cmp', 'QQQ', pane: 'new', display: 'candles'));
    c.addItem(LayerItem.indicator('sig', 'sma', {'length': 9})..pane = 'with:macd');
    final panes = assignPanes(c.shown, isOv);
    // bottom layer first: rsi gets pane 1, then the top layer's macd 2 and QQQ 3; the SMA joins macd
    check('panes across layers', [panes['rsi'], panes['macd'], panes['cmp'], panes['sig']], [1, 2, 3, 2]);
    c.find('volume')!.$2.pane = 'new';
    check('volume in its own pane', assignPanes(c.shown, isOv)['volume'], 1);
    c.find('volume')!.$2.pane = 'auto';

    l2.visible = false;
    check('hidden layer drops its items', c.shown.map((i) => i.id).toList(), ['price', 'volume', 'sma50', 'sma200', 'rsi']);
    check('attached item falls back to own pane when target hidden', assignPanes([
      ...c.shown, c.find('sig')!.$2,
    ], isOv)['sig'], 2);
    l2.visible = true;

    check('price cannot be removed', c.removeItem('price'), false);
    c.find('price')!.$2.visible = false;
    check('price can be hidden', c.shown.any((i) => i.kind == ItemKind.price), false);
    c.find('price')!.$2.visible = true;
    check('removing a target re-homes attached items', [c.removeItem('macd'), c.find('sig')!.$2.pane], [true, 'auto']);

    c.moveItem('rsi', l2.id, index: 0);
    check('move item between layers', [c.find('rsi')!.$1.id == l2.id, l2.items.first.id], [true, 'rsi']);
    c.moveLayer(l2.id, 1);
    check('reorder layers', c.layers.map((l) => l.id).toList(), [mainLayerId, l2.id]);
    c.placeLayerBefore(l2.id, mainLayerId);
    check('drag layer above another', c.layers.first.id, l2.id);

    c.moveItem('price', l2.id);
    check('remove layer keeps the price chart', [c.removeLayer(l2.id), c.price.id, c.layers.length], [true, 'price', 1]);
    check('cannot remove the last layer', c.removeLayer(c.layers.single.id), false);

    final d = c.duplicateLayer(mainLayerId);
    check('duplicate copies every item with new ids, price chart included', [
      d.hasPrice, d.items.every((i) => c.find(i.id)!.$1 == d), d.items.length,
    ], [true, true, c.layer(mainLayerId)!.items.length]);

    final legacy = SymbolConfig.fromLegacy(indicators: [const IndicatorRef('ema', {'length': 21})], chartType: 'line',
        scale: 'log', compares: ['SPY']);
    check('legacy migration', [legacy.price.chartType, legacy.scale, legacy.all.map((i) => i.kind.name).toList()],
        ['line', 'log', ['price', 'volume', 'indicator', 'compare']]);

    final broken = SymbolConfig.fromJson(J({'layers': [], 'activeLayer': 'gone'}));
    check('normalize repairs', [broken.layers.length, broken.price.kind, broken.activeLayer], [1, ItemKind.price, mainLayerId]);
    final dupes = SymbolConfig.fromJson(J({'layers': [
      {'id': 'a', 'items': [{'id': 'p1', 'kind': 'price'}, {'id': 'x', 'kind': 'indicator', 'name': 'rsi'}]},
      {'id': 'b', 'items': [{'id': 'p2', 'kind': 'price'}, {'id': 'x', 'kind': 'indicator', 'name': 'rsi'}]},
    ]}));
    check('one price per layer, unique ids', [dupes.all.where((i) => i.kind == ItemKind.price).length, dupes.all.length], [2, 3]);
    final twice = SymbolConfig.fromJson(J({'layers': [
      {'id': 'a', 'items': [{'id': 'price', 'kind': 'price'}, {'id': 'price', 'kind': 'price'}]},
      {'id': 'b', 'items': [{'id': 'price', 'kind': 'price'}, {'id': 'volume', 'kind': 'volume'}, {'id': 'v2', 'kind': 'volume'}]},
    ]}));
    check('repeated price ids are renamed, extra prices/volumes in a layer dropped', [
      twice.layer('a')!.items.length, twice.layer('b')!.items.length, twice.layer('b')!.priceItem!.id != 'price',
    ], [1, 2, true]);

    final withDrawings = SymbolConfig.builtin();
    check('drawing layers are created', [withDrawings.ensureDrawingLayers([null, 'agent', 'ideas']),
      withDrawings.layers.map((l) => l.name).toList()], [true, ['ideas', 'Agent', 'Main']]);
    check('unknown drawing layer falls back to main', withDrawings.layerOfDrawing('nope', 'user').id, mainLayerId);
  });

  group('indicator styles', () {
    final it = LayerItem.indicator('m', 'macd', {'fast': 8})
      ..levels = [0, 1.5];
    it.style('macd').color = '#ff0000';
    it.style('signal')
      ..line = 'dotted'
      ..width = 2;
    it.style('histogram').visible = false;
    it.style('unused'); // default styles are not stored
    final j = it.toJson();
    check('styles stored sparsely', (j['styles'] as Map).keys.toList(), ['macd', 'signal', 'histogram']);
    final back = LayerItem.fromJson(J(j));
    check('styles round trip', [
      back.styles['macd']!.color, back.styles['signal']!.line, back.styles['signal']!.width, back.styles['histogram']!.visible,
      back.levels, back.params['fast'],
    ], ['#ff0000', 'dotted', 2.0, false, [0.0, 1.5], 8]);
    check('no styles, no keys', LayerItem.indicator('r', 'rsi').toJson().containsKey('styles'), false);
    final copy = back.styles['signal']!.copy()..width = 3;
    check('copy is independent', back.styles['signal']!.width, 2.0);
    check('copy keeps fields', [copy.line, copy.width], ['dotted', 3.0]);
  });

  group('layer timeframes', () {
    bool isOv(String n) => n == 'vwap' || n == 'sma';
    final c = SymbolConfig.builtin(); // Main: price, volume, sma50, sma200, rsi (untied)
    c.layer(mainLayerId)!.interval = '1d';
    final entry = c.addLayer('Entry');
    c.addChart(entry.id, '30m');
    c.addItem(LayerItem.indicator('vwap', 'vwap'), layerId: entry.id);
    final levels = c.addLayer('Levels'); // no chart, no tie: on every interval
    c.addItem(LayerItem.indicator('ema', 'ema', {'length': 21}), layerId: levels.id);
    final scalp = c.addLayer('Scalp')..interval = '5m'; // no chart, tied to 5m
    c.addItem(LayerItem.indicator('atr', 'atr'), layerId: scalp.id);
    List<String> ids(String iv) => [for (final i in c.shownAt(iv)) i.id];

    check('two chart layers', [c.multiChart, c.chartLayers.length, entry.hasPrice, entry.volumeItem != null], [true, 2, true, true]);
    check('1d shows Main and untied layers', ids('1d'), ['price', 'volume', 'sma50', 'sma200', 'rsi', 'ema']);
    check('30m shows Entry instead of Main', ids('30m'),
        [entry.priceItem!.id, entry.volumeItem!.id, 'vwap', 'ema']);
    check('chart layer per interval', [c.chartLayerFor('1d').id, c.chartLayerFor('30m').id, c.priceAt('30m').id],
        [mainLayerId, entry.id, entry.priceItem!.id]);
    check('an interval with no chart layer falls back to the bottom one', c.chartLayerFor('1h').id, mainLayerId);
    check('a tied layer without a chart only adds to its interval', [ids('5m').contains('atr'), ids('1d').contains('atr')], [true, false]);
    check('indicators to compute follow the interval', [for (final (i, _) in c.indicatorItemsAt('30m')) i.id], ['vwap', 'ema']);
    check('panes at 30m', assignPanes(c.shownAt('30m'), isOv)['vwap'], 0);
    check('volume follows the chart layer', [c.volumeAt('1d')!.id, c.volumeAt('30m')!.id], ['volume', entry.volumeItem!.id]);
    check('item shown only at its interval', [c.itemShownAt(c.find('rsi')!.$2, '1d'), c.itemShownAt(c.find('rsi')!.$2, '30m')], [true, false]);
    check('drawings follow the interval tie only', [
      c.drawingsApplyAt(c.layer(mainLayerId)!, '30m'), c.drawingsApplyAt(entry, '1d'), c.drawingsApplyAt(levels, '30m'),
    ], [false, false, true]);
    c.layer(mainLayerId)!.interval = null; // untied: the default chart for every other interval
    check('untied chart layer is the default', [c.chartLayerFor('1h').id, c.chartLayerFor('30m').id, c.drawingsApplyAt(c.layer(mainLayerId)!, '30m')],
        [mainLayerId, entry.id, true]);
    check('a layer holds one price chart', c.moveItem('price', entry.id), false);
    check('price chart of a non-last chart layer can be removed', [c.removeChart(entry.id), entry.hasPrice, entry.volumeItem], [true, false, null]);
    check('the last price chart cannot be removed', [c.removeChart(mainLayerId), c.removeItem('price')], [false, false]);
    c.addChart(entry.id, '30m');
    final round = SymbolConfig.fromJson(J(c.toJson()));
    check('interval survives json', [round.layer(entry.id)!.interval, round.chartLayerFor('30m').id], ['30m', entry.id]);
    c.removeLayer(entry.id);
    check('removing a chart layer leaves the others', [c.chartLayers.length, c.chartLayerFor('30m').id], [1, mainLayerId]);
  });

  group('trading', () {
    check('shares for 1% risk', sharesForRisk(100000, 1, 50, 48), 500);
    check('no risk distance → 0', sharesForRisk(100000, 1, 50, 50), 0);
    check('shares for value', sharesForValue(1000, 333), 3);
    final bars = [for (final t in [100, 200, 300]) Bar(t, 1, 1, 1, 1, 0)];
    check('bar for time', [barIndexForTime(bars, 50), barIndexForTime(bars, 100), barIndexForTime(bars, 250), barIndexForTime(bars, 999)],
        [-1, 0, 1, 2]);
    final fills = [
      TFill(execId: 'a', symbol: 'X', side: 'buy', qty: 1, price: 1, time: 150),
      TFill(execId: 'b', symbol: 'X', side: 'sell', qty: 1, price: 1, time: 170),
      TFill(execId: 'c', symbol: 'X', side: 'buy', qty: 1, price: 1, time: 10),
    ];
    final by = fillsByBar(bars, fills);
    check('fills grouped by bar', [by.keys.toList(), by[0]!.length], [[0], 2]);
    check('money', [money(1234.5), money(-12), money(0), money(99.999, sign: false)], ['+\$1,234.50', '−\$12.00', '\$0.00', '\$100.00']);
    check('holding', [holdingText(90), holdingText(3 * 3600 + 300), holdingText(2 * 86400 + 3600)], ['1m', '3h 05m', '2d 1h']);
    final o = TOrder.fromJson(J({'id': '7', 'symbol': 'AAPL', 'side': 'sell', 'type': 'stop', 'stop_price': 95.0, 'qty': 10.0,
      'status': 'pending', 'parent_id': '6', 'role': 'stop_loss'}));
    check('stop order line', [o.linePrice, o.priceField, o.roleLabel, o.typeLabel, o.held, o.isOpen], [95.0, 'stop_price', 'SL', 'STP', true, true]);
    final t = TTicket.fromJson(J({'id': 't', 'symbol': 'MSFT', 'source': 'agent', 'status': 'pending',
      'spec': {'side': 'buy', 'qty': 5.0, 'order_type': 'limit', 'limit_price': 410.5, 'stop_loss': 400.0}, 'check': {'intent': 'open'}}));
    check('ticket summary', [t.summary, t.entryPrice, t.stopLoss], ['BUY 5 MSFT LMT @ 410.50', 410.5, 400.0]);
    final p = TPosition.fromJson(J({'symbol': 'X', 'qty': -10.0, 'avg_cost': 50.0}));
    check('short P&L', [p.isLong, p.pnlAt(45)], [false, 50.0]);
  });

  for (final f in _pending) {
    await f;
  }
  stdout.writeln('$_passes passed, $_failures failed');
  if (_failures > 0) exit(1);
}
