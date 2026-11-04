// Chart layers (pure Dart, no browser APIs). Each symbol has a SymbolConfig: an ordered list of
// layers (top of the list = top of the drawing order). A layer holds items – the price chart,
// volume, indicators and other symbols' series – plus the drawings whose `layer` field names it.
// Layers and items can be hidden, locked (drawings), reordered and moved between layers.
//
// Timeframes: a layer can be tied to one interval, and any number of layers can hold their own
// price chart ("chart layers"). At a given interval the chart shows exactly one chart layer – the
// one tied to that interval, else the one tied to none (the default), else the bottom one – with
// its series; the other chart layers' series are hidden. Layers without a price chart add their
// series and drawings on every interval, or only on theirs when tied to one. So a "Daily" layer
// (price, SMA 50/200) and an "Entry" layer (price on 30m, VWAP, RSI) switch with the interval.
import 'dart:math' as math;

import 'json.dart';
import 'models.dart';

enum ItemKind { price, volume, indicator, compare }

/// How one output line of an indicator is drawn. Unset fields fall back to the automatic look.
class OutputStyle {
  String? color;
  double? width;
  String? line; // solid | dashed | dotted
  String? plot; // line | histogram | dots
  bool visible;

  OutputStyle({this.color, this.width, this.line, this.plot, this.visible = true});

  bool get isDefault => color == null && width == null && line == null && plot == null && visible;

  OutputStyle copy() => OutputStyle(color: color, width: width, line: line, plot: plot, visible: visible);

  Map<String, Object?> toJson() => {
        if (color != null) 'color': color,
        if (width != null) 'width': width,
        if (line != null) 'line': line,
        if (plot != null) 'plot': plot,
        if (!visible) 'visible': false,
      };

  factory OutputStyle.fromJson(J j) =>
      OutputStyle(color: j['color'].s, width: j['width'].d, line: j['line'].s, plot: j['plot'].s, visible: j['visible'].b ?? true);
}

/// Where an item is drawn: the main price pane, its own new pane, or the pane of another item.
/// 'auto' = main pane for overlay indicators, a new pane for oscillators.
const placements = ['auto', 'main', 'new'];

class LayerItem {
  final String id;
  final ItemKind kind;
  bool visible;
  String pane; // auto | main | new | with:<itemId>
  String? color;
  // price
  String chartType;
  // indicator
  String name;
  Map<String, Object> params;
  Map<String, OutputStyle> styles; // per output
  List<double>? levels; // reference lines (null = the indicator's own)
  // compare
  String symbol;
  String display; // line | area | candles (own pane); overlays are always lines

  LayerItem({
    required this.id,
    required this.kind,
    this.visible = true,
    this.pane = 'auto',
    this.color,
    this.chartType = 'candles',
    this.name = '',
    Map<String, Object>? params,
    Map<String, OutputStyle>? styles,
    this.levels,
    this.symbol = '',
    this.display = 'line',
  })  : params = params ?? {},
        styles = styles ?? {};

  /// The style of [output], created on demand.
  OutputStyle style(String output) => styles.putIfAbsent(output, OutputStyle.new);

  factory LayerItem.price({String id = 'price', String chartType = 'candles'}) =>
      LayerItem(id: id, kind: ItemKind.price, chartType: chartType, pane: 'main');
  factory LayerItem.volume({String id = 'volume', String pane = 'auto'}) => LayerItem(id: id, kind: ItemKind.volume, pane: pane);
  factory LayerItem.indicator(String id, String name, [Map<String, Object>? params]) =>
      LayerItem(id: id, kind: ItemKind.indicator, name: name, params: params);
  factory LayerItem.compare(String id, String symbol, {String pane = 'auto', String display = 'line'}) =>
      LayerItem(id: id, kind: ItemKind.compare, symbol: symbol, pane: pane, display: display);

  IndicatorRef get ref => IndicatorRef(name, params);

  String get label => switch (kind) {
        ItemKind.price => 'Price · ${_chartTypeLabel(chartType)}',
        ItemKind.volume => 'Volume',
        ItemKind.indicator => ref.label,
        ItemKind.compare => '$symbol${pane == 'new' ? ' · ${display}' : ' · compare'}',
      };

  Map<String, Object?> toJson() => {
        'id': id,
        'kind': kind.name,
        'visible': visible,
        'pane': pane,
        if (color != null) 'color': color,
        if (kind == ItemKind.price) 'chartType': chartType,
        if (kind == ItemKind.indicator) 'name': name,
        if (kind == ItemKind.indicator) 'params': params,
        if (kind == ItemKind.indicator && styles.values.any((x) => !x.isDefault))
          'styles': {for (final e in styles.entries) if (!e.value.isDefault) e.key: e.value.toJson()},
        if (kind == ItemKind.indicator && levels != null) 'levels': levels,
        if (kind == ItemKind.compare) 'symbol': symbol,
        if (kind == ItemKind.compare) 'display': display,
      };

  factory LayerItem.fromJson(J j) {
    final kind = ItemKind.values.firstWhere((k) => k.name == j['kind'].s, orElse: () => ItemKind.indicator);
    return LayerItem(
      id: j['id'].s ?? newId('i'),
      kind: kind,
      visible: j['visible'].b ?? true,
      pane: j['pane'].s ?? (kind == ItemKind.price ? 'main' : 'auto'),
      color: j['color'].s,
      chartType: j['chartType'].s ?? 'candles',
      name: j['name'].s ?? '',
      params: {
        for (final e in j['params'].map.entries)
          if (e.value.v is num || e.value.v is String) e.key: e.value.v!,
      },
      styles: {for (final e in j['styles'].map.entries) e.key: OutputStyle.fromJson(e.value)},
      levels: j['levels'].v is List ? [for (final l in j['levels'].list) ?l.d] : null,
      symbol: j['symbol'].s ?? '',
      display: j['display'].s ?? 'line',
    );
  }
}

String _chartTypeLabel(String t) => switch (t) {
      'hollow' => 'Hollow candles',
      'bars' => 'Bars',
      'line' => 'Line',
      'area' => 'Area',
      'heikin' => 'Heikin Ashi',
      _ => 'Candles',
    };

class Layer {
  final String id;
  String name;
  bool visible;
  bool locked;
  String? interval; // tied to this interval only (null = every interval)
  final List<LayerItem> items;

  Layer({required this.id, required this.name, this.visible = true, this.locked = false, this.interval, List<LayerItem>? items})
      : items = items ?? [];

  /// Holds its own price chart.
  bool get hasPrice => items.any((i) => i.kind == ItemKind.price);
  LayerItem? get priceItem => items.where((i) => i.kind == ItemKind.price).firstOrNull;
  LayerItem? get volumeItem => items.where((i) => i.kind == ItemKind.volume).firstOrNull;

  Map<String, Object?> toJson() => {
        'id': id, 'name': name, 'visible': visible, 'locked': locked,
        if (interval != null) 'interval': interval,
        'items': [for (final i in items) i.toJson()],
      };

  factory Layer.fromJson(J j) => Layer(
        id: j['id'].s ?? newId('l'),
        name: j['name'].s ?? 'Layer',
        visible: j['visible'].b ?? true,
        locked: j['locked'].b ?? false,
        interval: (j['interval'].s ?? '').isEmpty ? null : j['interval'].s,
        items: [for (final i in j['items'].list) LayerItem.fromJson(i)],
      );
}

const mainLayerId = 'main';
const agentLayerId = 'agent';
const configVersion = 1;

class SymbolConfig {
  final List<Layer> layers; // top first
  String activeLayer;
  String scale; // linear | log | percent

  SymbolConfig({required this.layers, this.activeLayer = mainLayerId, this.scale = 'linear'});

  Map<String, Object?> toJson() => {
        'v': configVersion,
        'layers': [for (final l in layers) l.toJson()],
        'activeLayer': activeLayer,
        'scale': scale,
      };

  factory SymbolConfig.fromJson(J j) {
    final c = SymbolConfig(
      layers: [for (final l in j['layers'].list) Layer.fromJson(l)],
      activeLayer: j['activeLayer'].s ?? mainLayerId,
      scale: j['scale'].s ?? 'linear',
    );
    c.normalize();
    return c;
  }

  SymbolConfig clone() => SymbolConfig.fromJson(J(toJson()));

  /// The built-in starting point: price, volume, SMA 50/200 and RSI in one "Main" layer.
  factory SymbolConfig.builtin() => SymbolConfig(layers: [
        Layer(id: mainLayerId, name: 'Main', items: [
          LayerItem.price(),
          LayerItem.volume(),
          LayerItem.indicator('sma50', 'sma', {'length': 50}),
          LayerItem.indicator('sma200', 'sma', {'length': 200}),
          LayerItem.indicator('rsi', 'rsi'),
        ]),
      ]);

  /// Builds a config from the old per-chart state (before layers existed).
  factory SymbolConfig.fromLegacy({List<IndicatorRef> indicators = const [], String chartType = 'candles',
      String scale = 'linear', List<String> compares = const [], List<String> hidden = const []}) {
    final items = <LayerItem>[LayerItem.price(chartType: chartType), LayerItem.volume()];
    for (final r in indicators) {
      items.add(LayerItem.indicator(newId('i'), r.name, Map.of(r.params)));
    }
    for (final s in compares) {
      items.add(LayerItem.compare(newId('c'), s));
    }
    return SymbolConfig(layers: [Layer(id: mainLayerId, name: 'Main', items: items)], scale: scale);
  }

  /// Repairs invariants: at least one layer and one price chart, at most one price chart and one
  /// volume per layer, unique ids (a repeated price/volume id is renamed, other repeats dropped),
  /// and an active layer that exists.
  void normalize() {
    if (layers.isEmpty) layers.add(Layer(id: mainLayerId, name: 'Main'));
    final seen = <String>{};
    for (final l in layers) {
      var price = false, volume = false;
      final keep = <LayerItem>[];
      for (var i in l.items) {
        if (i.kind == ItemKind.price) {
          if (price) continue;
          price = true;
        } else if (i.kind == ItemKind.volume) {
          if (volume) continue;
          volume = true;
        }
        if (seen.contains(i.id)) {
          if (i.kind != ItemKind.price && i.kind != ItemKind.volume) continue;
          i = LayerItem.fromJson(J({...i.toJson(), 'id': newId(i.kind == ItemKind.price ? 'p' : 'v')}));
        }
        seen.add(i.id);
        keep.add(i);
      }
      l.items
        ..clear()
        ..addAll(keep);
    }
    if (!layers.any((l) => l.hasPrice)) bottomLayer.items.insert(0, LayerItem.price(id: seen.contains('price') ? newId('p') : 'price'));
    if (!layers.any((l) => l.id == activeLayer)) activeLayer = layers.first.id;
  }

  Layer get bottomLayer => layers.last;

  Layer? layer(String id) {
    for (final l in layers) {
      if (l.id == id) return l;
    }
    return null;
  }

  Layer get active => layer(activeLayer) ?? layers.first;

  (Layer, LayerItem)? find(String itemId) {
    for (final l in layers) {
      for (final i in l.items) {
        if (i.id == itemId) return (l, i);
      }
    }
    return null;
  }

  /// The first price chart, whatever the interval (the default chart's when there is only one).
  LayerItem get price {
    for (final l in layers) {
      final p = l.priceItem;
      if (p != null) return p;
    }
    normalize(); // adds one
    return price;
  }

  /// The first volume item, whatever the interval.
  LayerItem? get volume {
    for (final l in layers) {
      final v = l.volumeItem;
      if (v != null) return v;
    }
    return null;
  }

  // ---------------------------------------------------------------- timeframes

  List<Layer> get chartLayers => [for (final l in layers) if (l.hasPrice) l];

  /// More than one layer holds a price chart (the chart then depends on the interval).
  bool get multiChart => chartLayers.length > 1;

  /// The chart layer shown at [interval]: the one tied to it, else the default (tied to none), else
  /// the bottom one. Top-most wins a tie.
  Layer chartLayerFor(String interval) {
    var charts = chartLayers;
    if (charts.isEmpty) {
      normalize();
      charts = chartLayers;
    }
    return charts.where((l) => l.interval == interval).firstOrNull ??
        charts.where((l) => l.interval == null).firstOrNull ??
        charts.last;
  }

  /// Whether [l]'s series are part of the chart at [interval] (visibility aside).
  bool appliesAt(Layer l, String interval) =>
      l.hasPrice ? identical(l, chartLayerFor(interval)) : (l.interval == null || l.interval == interval);

  /// Whether [l]'s drawings belong on the chart at [interval] (visibility aside). Drawings only follow
  /// the layer's own interval tie, so key levels kept in an untied chart layer stay on every interval.
  bool drawingsApplyAt(Layer l, String interval) => l.interval == null || l.interval == interval;

  LayerItem priceAt(String interval) => chartLayerFor(interval).priceItem!;

  LayerItem? volumeAt(String interval) {
    for (final l in layers) {
      if (!appliesAt(l, interval)) continue;
      final v = l.volumeItem;
      if (v != null) return v;
    }
    return null;
  }

  /// Items drawn at [interval]: visible items of visible layers that apply there, bottom layer first.
  List<LayerItem> shownAt(String interval) => [
        for (final l in layers.reversed)
          if (l.visible && appliesAt(l, interval))
            for (final i in l.items)
              if (i.visible) i,
      ];

  /// Items of the layers that apply at [interval] (any visibility), bottom layer first.
  List<LayerItem> allAt(String interval) => [for (final l in layers.reversed) if (appliesAt(l, interval)) ...l.items];

  bool itemShownAt(LayerItem item, String interval) {
    final f = find(item.id);
    return f != null && f.$1.visible && item.visible && appliesAt(f.$1, interval);
  }

  List<(LayerItem, IndicatorRef)> indicatorItemsAt(String interval) => [
        for (final i in allAt(interval))
          if (i.kind == ItemKind.indicator) (i, i.ref),
      ];

  /// Gives layer [layerId] its own price chart (and volume) tied to [interval]; a layer that already
  /// has one is just re-tied.
  void addChart(String layerId, String interval) {
    final l = layer(layerId);
    if (l == null) return;
    l.interval = interval;
    if (!l.hasPrice) {
      l.items.insert(0, LayerItem.price(id: newId('p'), chartType: chartLayerFor(interval).priceItem!.chartType));
      if (l.volumeItem == null) l.items.insert(1, LayerItem.volume(id: newId('v')));
    }
  }

  /// Removes a layer's price chart (and its volume); the last price chart stays.
  bool removeChart(String layerId) {
    final l = layer(layerId);
    if (l == null || !l.hasPrice || chartLayers.length <= 1) return false;
    l.items.removeWhere((i) => i.kind == ItemKind.price || i.kind == ItemKind.volume);
    return true;
  }

  // ---------------------------------------------------------------- any interval

  /// Visible items of visible layers, whatever the interval, bottom layer first.
  List<LayerItem> get shown => [
        for (final l in layers.reversed)
          if (l.visible)
            for (final i in l.items)
              if (i.visible) i,
      ];

  /// All items (any visibility or interval), bottom layer first.
  List<LayerItem> get all => [for (final l in layers.reversed) ...l.items];

  bool itemShown(LayerItem item) {
    final f = find(item.id);
    return f != null && f.$1.visible && item.visible;
  }

  /// Layer a drawing belongs to (unknown/empty ids fall back to the main layer).
  Layer layerOfDrawing(String? layerId, String author) {
    final id = (layerId == null || layerId.isEmpty) ? mainLayerId : layerId;
    return layer(id) ?? layer(mainLayerId) ?? bottomLayer;
  }

  /// Makes sure every drawing layer id has a layer entry (the agent's drawings get an "Agent" layer).
  bool ensureDrawingLayers(Iterable<String?> layerIds) {
    var changed = false;
    for (final raw in layerIds) {
      final id = (raw == null || raw.isEmpty) ? mainLayerId : raw;
      if (layer(id) != null) continue;
      layers.insert(0, Layer(id: id, name: id == agentLayerId ? 'Agent' : id == mainLayerId ? 'Main' : id));
      changed = true;
    }
    return changed;
  }

  /// The agent's layer, created on top when missing.
  Layer agentLayer() {
    final existing = layer(agentLayerId);
    if (existing != null) return existing;
    final l = Layer(id: agentLayerId, name: 'Agent');
    layers.insert(0, l);
    return l;
  }

  // ---------------------------------------------------------------- operations

  Layer addLayer(String name, {int at = 0}) {
    final l = Layer(id: newId('l'), name: name);
    layers.insert(at.clamp(0, layers.length), l);
    activeLayer = l.id;
    return l;
  }

  /// Removes a layer. The last remaining layer cannot be removed; the last price chart moves to the
  /// layer below.
  bool removeLayer(String id) {
    if (layers.length <= 1) return false;
    final l = layer(id);
    if (l == null) return false;
    final idx = layers.indexOf(l);
    layers.remove(l);
    final price = l.priceItem;
    if (price != null && !layers.any((x) => x.hasPrice)) layers[math.min(idx, layers.length - 1)].items.insert(0, price);
    if (activeLayer == id) activeLayer = layers[math.min(idx, layers.length - 1)].id;
    return true;
  }

  Layer duplicateLayer(String id) {
    final src = layer(id)!;
    final copy = Layer(id: newId('l'), name: '${src.name} copy', visible: src.visible, locked: src.locked, interval: src.interval, items: [
      for (final i in src.items) LayerItem.fromJson(J({...i.toJson(), 'id': newId('i')})),
    ]);
    layers.insert(layers.indexOf(src), copy);
    return copy;
  }

  void moveLayer(String id, int delta) {
    final l = layer(id);
    if (l == null) return;
    final i = layers.indexOf(l);
    final j = (i + delta).clamp(0, layers.length - 1);
    layers.removeAt(i);
    layers.insert(j, l);
  }

  /// Moves layer [id] to just above layer [beforeId] (drag and drop).
  void placeLayerBefore(String id, String beforeId) {
    if (id == beforeId) return;
    final l = layer(id);
    if (l == null) return;
    layers.remove(l);
    final target = layer(beforeId);
    layers.insert(target == null ? layers.length : layers.indexOf(target), l);
  }

  void addItem(LayerItem item, {String? layerId}) {
    final l = layerId == null ? active : (layer(layerId) ?? active);
    l.items.add(item);
  }

  bool removeItem(String itemId) {
    final f = find(itemId);
    if (f == null) return false;
    if (f.$2.kind == ItemKind.price && chartLayers.length <= 1) return false; // the last price chart can be hidden, not removed
    f.$1.items.remove(f.$2);
    // items attached to the removed one fall back to their default pane
    for (final i in all) {
      if (i.pane == 'with:$itemId') i.pane = 'auto';
    }
    return true;
  }

  /// Moves an item to another layer; false when the target already has a price chart / volume.
  bool moveItem(String itemId, String toLayer, {int? index}) {
    final f = find(itemId);
    final target = layer(toLayer);
    if (f == null || target == null) return false;
    if (!identical(f.$1, target) &&
        ((f.$2.kind == ItemKind.price && target.hasPrice) || (f.$2.kind == ItemKind.volume && target.volumeItem != null))) {
      return false;
    }
    f.$1.items.remove(f.$2);
    final at = index == null ? target.items.length : index.clamp(0, target.items.length);
    target.items.insert(at, f.$2);
    return true;
  }

  /// Indicator refs of every indicator item (any visibility), in [all] order.
  List<(LayerItem, IndicatorRef)> get indicatorItems => [
        for (final i in all)
          if (i.kind == ItemKind.indicator) (i, i.ref),
      ];
}

/// Assigns pane indexes (0 = main price pane) to the shown items. [isOverlay] tells whether an
/// indicator draws on price by default. Items placed 'with:<id>' share that item's pane when it is
/// shown, otherwise they get their own.
Map<String, int> assignPanes(List<LayerItem> shown, bool Function(String indicator) isOverlay) {
  final out = <String, int>{};
  var next = 1;
  int resolve(LayerItem i) {
    switch (i.pane) {
      case 'main':
        return 0;
      case 'new':
        return next++;
      case 'auto':
        if (i.kind == ItemKind.price || i.kind == ItemKind.volume) return 0;
        if (i.kind == ItemKind.compare) return 0;
        return isOverlay(i.name) ? 0 : next++;
    }
    return -1;
  }

  // first pass: everything not attached to another item
  for (final i in shown) {
    if (!i.pane.startsWith('with:')) out[i.id] = resolve(i);
  }
  for (final i in shown) {
    if (!i.pane.startsWith('with:')) continue;
    final target = i.pane.substring(5);
    out[i.id] = out[target] ?? next++;
  }
  return out;
}

final _rng = math.Random();

String newId(String prefix) =>
    '$prefix${DateTime.now().microsecondsSinceEpoch.toRadixString(36)}${_rng.nextInt(1 << 20).toRadixString(36)}';
