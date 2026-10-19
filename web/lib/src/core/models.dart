// Typed views of the REST API payloads the UI works with directly. Less central payloads
// (technical summary, fundamentals, track record) are read through [J].
import 'json.dart';

class Bar {
  final int time;
  final double open, high, low, close, volume;
  const Bar(this.time, this.open, this.high, this.low, this.close, this.volume);

  factory Bar.fromJson(J j) =>
      Bar(j['time'].i ?? 0, j['open'].d ?? 0, j['high'].d ?? 0, j['low'].d ?? 0, j['close'].d ?? 0, j['volume'].d ?? 0);
}

class Point {
  final int? time;
  final double? price;
  const Point(this.time, this.price);

  factory Point.fromJson(J j) => Point(j['time'].i, j['price'].d);
  Map<String, Object?> toJson() => {'time': time, 'price': price};
}

/// Drawing types the backend accepts (same names as the MCP draw_annotations tool).
const annotationTypes = [
  'trendline', 'ray', 'extended_line', 'horizontal_line', 'horizontal_ray', 'vertical_line',
  'rectangle', 'parallel_channel', 'fib_retracement', 'price_range', 'polyline', 'arrow',
  'text', 'marker', 'long_position', 'short_position', 'callout',
];

class Annotation {
  final String id;
  final String symbol;
  final String? interval;
  final String type;
  final List<Point> points;
  final String? text;
  final J style;
  final String author; // "agent" | "user"
  final String? tag;
  final String? layer; // chart layer id (null = main)
  final bool visible;

  const Annotation({
    required this.id,
    required this.symbol,
    this.interval,
    required this.type,
    required this.points,
    this.text,
    this.style = const J(null),
    this.author = 'user',
    this.tag,
    this.layer,
    this.visible = true,
  });

  factory Annotation.fromJson(J j) => Annotation(
        id: j['id'].s ?? '',
        symbol: j['symbol'].s ?? '',
        interval: j['interval'].s,
        type: j['type'].s ?? 'trendline',
        points: [for (final p in j['points'].list) Point.fromJson(p)],
        text: j['text'].s,
        style: j['style'],
        author: j['author'].s ?? 'user',
        tag: j['tag'].s,
        layer: j['layer'].s,
        visible: j['visible'].b ?? true,
      );
}

class IndicatorRef {
  final String name;
  final Map<String, Object> params;
  const IndicatorRef(this.name, this.params);

  factory IndicatorRef.fromJson(J j) => IndicatorRef(j['name'].s ?? '', {
        for (final e in j['params'].map.entries)
          if (e.value.v is num || e.value.v is String) e.key: e.value.v!,
      });
  Map<String, Object?> toJson() => {'name': name, 'params': params};

  String get label => params.isEmpty ? name : '$name(${params.values.join(',')})';
}

class IndicatorSpec {
  final String name, title, category, description;
  final bool overlay;
  final Map<String, Object?> params;
  const IndicatorSpec(this.name, this.title, this.category, this.description, this.overlay, this.params);

  factory IndicatorSpec.fromJson(J j) => IndicatorSpec(
        j['name'].s ?? '',
        j['title'].s ?? j['name'].s ?? '',
        j['category'].s ?? '',
        j['description'].s ?? '',
        j['overlay'].b ?? false,
        {for (final e in j['params'].map.entries) e.key: e.value.v},
      );
}

class IndicatorResult {
  final String name, label;
  final bool overlay;
  final List<String> outputs;
  final List<double> levels;
  final Map<String, List<double?>> values;
  const IndicatorResult(this.name, this.label, this.overlay, this.outputs, this.levels, this.values);

  factory IndicatorResult.fromJson(J j) => IndicatorResult(
        j['name'].s ?? '',
        j['label'].s ?? j['name'].s ?? '',
        j['overlay'].b ?? false,
        [for (final o in j['outputs'].list) o.s ?? ''],
        [for (final l in j['levels'].list) ?l.d],
        {for (final e in j['values'].map.entries) e.key: [for (final v in e.value.list) v.d]},
      );
}

class ChartViewState {
  final String symbol;
  final String interval;
  final int? fromTime, toTime;
  final List<IndicatorRef> indicators;
  final String? updatedBy;
  const ChartViewState({
    this.symbol = 'SPY',
    this.interval = '1d',
    this.fromTime,
    this.toTime,
    this.indicators = const [],
    this.updatedBy,
  });

  factory ChartViewState.fromJson(J j) => ChartViewState(
        symbol: j['symbol'].s ?? 'SPY',
        interval: j['interval'].s ?? '1d',
        fromTime: j['from_time'].i,
        toTime: j['to_time'].i,
        indicators: [for (final r in j['indicators'].list) IndicatorRef.fromJson(r)],
        updatedBy: j['updated_by'].s,
      );

  /// A copy with the given fields replaced; changing symbol or interval resets the visible range.
  ChartViewState copyWith({String? symbol, String? interval, List<IndicatorRef>? indicators}) {
    final resetRange = (symbol != null && symbol != this.symbol) || interval != null;
    return ChartViewState(
      symbol: symbol ?? this.symbol,
      interval: interval ?? this.interval,
      fromTime: resetRange ? null : fromTime,
      toTime: resetRange ? null : toTime,
      indicators: indicators ?? this.indicators,
      updatedBy: 'user',
    );
  }
}

class Quote {
  final String symbol, source;
  final String? feed;
  final double price;
  final double? change, changePct, dayHigh, dayLow, volume;
  const Quote(this.symbol, this.source, this.feed, this.price, this.change, this.changePct,
      [this.dayHigh, this.dayLow, this.volume]);

  factory Quote.fromJson(J j) => Quote(j['symbol'].s ?? '', j['source'].s ?? '', j['feed'].s, j['price'].d ?? 0,
      j['change'].d, j['change_pct'].d, j['day_high'].d, j['day_low'].d, j['volume'].d);
}

class SymbolMatch {
  final String symbol, name, exchange;
  const SymbolMatch(this.symbol, this.name, this.exchange);
  factory SymbolMatch.fromJson(J j) => SymbolMatch(j['symbol'].s ?? '', j['name'].s ?? '', j['exchange'].s ?? '');
}

class Note {
  final String id, title, content, author, updatedAt;
  final String? symbol;
  final List<String> tags;
  const Note(this.id, this.title, this.content, this.author, this.updatedAt, this.symbol, this.tags);

  factory Note.fromJson(J j) => Note(j['id'].s ?? '', j['title'].s ?? '', j['content'].s ?? '', j['author'].s ?? 'user',
      j['updated_at'].s ?? '', j['symbol'].s, [for (final t in j['tags'].list) t.s ?? '']);
}

class Watchlist {
  final String name;
  final List<String> symbols;
  const Watchlist(this.name, this.symbols);
  factory Watchlist.fromJson(J j) => Watchlist(j['name'].s ?? '', [for (final s in j['symbols'].list) s.s ?? '']);
}

const intervals = ['1m', '5m', '15m', '30m', '1h', '1d', '1wk', '1mo'];

/// History loaded per interval (enough for long indicators without slow requests).
const lookback = {
  '1m': '5d', '5m': '30d', '15m': '45d', '30m': '55d', '1h': '1y', '1d': '5y', '1wk': '20y', '1mo': 'max',
};

const intervalSeconds = {
  '1m': 60, '5m': 300, '15m': 900, '30m': 1800, '1h': 3600, '1d': 86400, '1wk': 604800, '1mo': 2592000,
};

bool isIntraday(String interval) => interval != '1mo' && (interval.endsWith('m') || interval.endsWith('h'));
