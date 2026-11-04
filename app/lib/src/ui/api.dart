// Client for the Autobench REST API.
import 'dart:convert';
import 'dart:js_interop';

import 'package:web/web.dart';

import '../core/json.dart';
import '../core/models.dart';

class ApiError implements Exception {
  final int status;
  final String message;
  ApiError(this.status, this.message);
  @override
  String toString() => message;
}

String _q(Map<String, Object?> params) {
  final kept = {
    for (final e in params.entries)
      if (e.value != null && '${e.value}'.isNotEmpty) e.key: '${e.value}',
  };
  return kept.isEmpty ? '' : '?${Uri(queryParameters: kept).query}';
}

String _enc(String s) => Uri.encodeComponent(s);

Future<J> request(String path, {String method = 'GET', Object? body}) async {
  final init = RequestInit(
    method: method,
    headers: {'Content-Type': 'application/json', 'X-Autobench': '1'}.jsify()! as JSObject,
  );
  if (body != null) init.body = jsonEncode(body).toJS;
  final res = await window.fetch(path.toJS, init).toDart;
  final text = (await res.text().toDart).toDart;
  Object? data;
  try {
    data = text.isEmpty ? null : jsonDecode(text);
  } on FormatException {
    data = null;
  }
  if (!res.ok) {
    final detail = J(data)['detail'];
    throw ApiError(res.status, detail.s ?? (detail.isNull ? res.statusText : jsonEncode(detail.v)));
  }
  return J(data);
}

/// Resolves to null instead of throwing (for optional data where a failure just means "none").
Future<T?> attempt<T>(Future<T> f) async {
  try {
    return await f;
  } catch (_) {
    return null;
  }
}

class Api {
  Future<J> health() => request('/api/health');

  Future<List<SymbolMatch>> search(String query) async =>
      [for (final r in (await request('/api/symbols/search${_q({'q': query, 'limit': 12})}')).list) SymbolMatch.fromJson(r)];

  Future<Quote> quote(String symbol) async => Quote.fromJson(await request('/api/quote/${_enc(symbol)}'));

  Future<({String source, List<Bar> bars})> bars(String symbol, String interval) async {
    final r = await request('/api/bars/${_enc(symbol)}${_q({'interval': interval, 'lookback': lookback[interval] ?? '1y'})}');
    return (source: r['source'].s ?? '', bars: [for (final b in r['bars'].list) Bar.fromJson(b)]);
  }

  Future<List<IndicatorSpec>> indicatorList() async =>
      [for (final s in (await request('/api/indicators')).list) IndicatorSpec.fromJson(s)];

  Future<({List<int> time, List<IndicatorResult> results})> indicators(
      String symbol, String interval, List<IndicatorRef> refs) async {
    final r = await request('/api/indicators/${_enc(symbol)}', method: 'POST', body: {
      'interval': interval,
      'lookback': lookback[interval] ?? '1y',
      'indicators': [for (final x in refs) x.toJson()],
    });
    return (
      time: [for (final t in r['time'].list) t.i ?? 0],
      results: [for (final x in r['results'].list) IndicatorResult.fromJson(x)],
    );
  }

  Future<J> summary(String symbol, {String interval = '1d'}) =>
      request('/api/summary/${_enc(symbol)}${_q({'interval': interval})}');
  Future<J> fundamentals(String symbol) => request('/api/fundamentals/${_enc(symbol)}');
  Future<J> news(String? symbol, {int limit = 12}) => request('/api/news${_q({'symbol': symbol, 'limit': limit})}');
  Future<J> earnings(String symbol) => request('/api/earnings/${_enc(symbol)}');
  Future<J> snapshot(String symbol) => request('/api/snapshot/${_enc(symbol)}');
  Future<Map<String, Quote>> quotes(List<String> symbols) async {
    if (symbols.isEmpty) return {};
    final r = await request('/api/quotes${_q({'symbols': symbols.join(',')})}');
    return {for (final e in r.map.entries) e.key: Quote.fromJson(e.value)};
  }

  Future<List<Bar>> barsRange(String symbol, String interval, String lookback) async {
    final r = await request('/api/bars/${_enc(symbol)}${_q({'interval': interval, 'lookback': lookback})}');
    return [for (final b in r['bars'].list) Bar.fromJson(b)];
  }

  Future<List<Annotation>> annotations(String symbol) async =>
      [for (final a in (await request('/api/annotations${_q({'symbol': symbol})}')).list) Annotation.fromJson(a)];

  Future<Annotation> createAnnotation(String symbol, String type, List<Point> points,
          {String? text, Map<String, Object?>? style, String? interval, String? tag, String? layer}) async =>
      Annotation.fromJson(await request('/api/annotations/${_enc(symbol)}', method: 'POST', body: {
        'type': type,
        'points': [for (final p in points) p.toJson()],
        'text': ?text,
        'style': ?style,
        'interval': ?interval,
        'tag': ?tag,
        'layer': ?layer,
      }));

  Future<Annotation> updateAnnotation(String id, Map<String, Object?> patch) async =>
      Annotation.fromJson(await request('/api/annotations/$id', method: 'PATCH', body: patch));

  Future<J> deleteAnnotation(String id) => request('/api/annotations/$id', method: 'DELETE');

  Future<int> deleteAnnotations(String symbol, {String? author, String? tag}) async =>
      (await request('/api/annotations${_q({'symbol': symbol, 'author': author, 'tag': tag})}', method: 'DELETE'))['deleted']
          .i ??
      0;

  Future<ChartViewState> view() async => ChartViewState.fromJson(await request('/api/view'));

  Future<ChartViewState> setView(ChartViewState v, {bool resetRange = false}) async =>
      ChartViewState.fromJson(await request('/api/view', method: 'PUT', body: {
        'symbol': v.symbol,
        'interval': v.interval,
        'indicators': [for (final r in v.indicators) r.toJson()],
        if (resetRange) 'from_time': null,
        if (resetRange) 'to_time': null,
      }));

  Future<List<J>> analyses({String? symbol}) async =>
      (await request('/api/analyses${_q({'symbol': symbol, 'limit': 50})}')).list;
  Future<J> analysis(String id) => request('/api/analyses/$id');
  Future<J> trackRecord() => request('/api/track-record');

  Future<List<Note>> notes(String symbol) async =>
      [for (final x in (await request('/api/notes${_q({'symbol': symbol})}')).list) Note.fromJson(x)];
  Future<J> createNote(String title, String content, String symbol) =>
      request('/api/notes', method: 'POST', body: {'title': title, 'content': content, 'symbol': symbol});
  Future<J> deleteNote(String id) => request('/api/notes/$id', method: 'DELETE');

  Future<List<Watchlist>> watchlists() async =>
      [for (final w in (await request('/api/watchlists')).list) Watchlist.fromJson(w)];
  Future<J> updateWatchlist(String name, {List<String>? add, List<String>? remove}) =>
      request('/api/watchlists/${_enc(name)}', method: 'PUT', body: {'add': ?add, 'remove': ?remove});
}

final api = Api();
