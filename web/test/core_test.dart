// Unit tests for the browser-independent core (time zones, scales, mapping, formatting, markdown).
// Run: dart run test/core_test.dart   (no test framework needed; exits non-zero on failure)
import 'dart:io';

import '../lib/src/core/format.dart';
import '../lib/src/core/geometry.dart';
import '../lib/src/core/json.dart';
import '../lib/src/core/mapper.dart';
import '../lib/src/core/markdown.dart';
import '../lib/src/core/models.dart';
import '../lib/src/core/ticks.dart';
import '../lib/src/core/time.dart';

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

void main() {
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

  stdout.writeln('$_passes passed, $_failures failed');
  if (_failures > 0) exit(1);
}
