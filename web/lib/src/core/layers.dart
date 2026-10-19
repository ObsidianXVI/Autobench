// Chart layers (pure Dart, no browser APIs). Each symbol has a SymbolConfig: an ordered list of
// layers (top of the list = top of the drawing order). A layer holds items – the price chart,
// volume, indicators and other symbols' series – plus the drawings whose `layer` field names it.
// Layers and items can be hidden, locked (drawings), reordered and moved between layers.
import 'dart:math' as math;

import 'json.dart';
import 'models.dart';

enum ItemKind { price, volume, indicator, compare }

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
    this.symbol = '',
    this.display = 'line',
  }) : params = params ?? {};

  factory LayerItem.price({String chartType = 'candles'}) =>
      LayerItem(id: 'price', kind: ItemKind.price, chartType: chartType, pane: 'main');
  factory LayerItem.volume({String pane = 'auto'}) => LayerItem(id: 'volume', kind: ItemKind.volume, pane: pane);
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
  final List<LayerItem> items;

  Layer({required this.id, required this.name, this.visible = true, this.locked = false, List<LayerItem>? items})
      : items = items ?? [];

  Map<String, Object?> toJson() => {
        'id': id, 'name': name, 'visible': visible, 'locked': locked,
        'items': [for (final i in items) i.toJson()],
      };

  factory Layer.fromJson(J j) => Layer(
        id: j['id'].s ?? newId('l'),
        name: j['name'].s ?? 'Layer',
        visible: j['visible'].b ?? true,
        locked: j['locked'].b ?? false,
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

  /// Repairs invariants: exactly one price item, ids unique, active layer exists, at least one layer.
  void normalize() {
    if (layers.isEmpty) layers.add(Layer(id: mainLayerId, name: 'Main'));
    final seen = <String>{};
    var hasPrice = false;
    for (final l in layers) {
      l.items.removeWhere((i) {
        if (i.kind == ItemKind.price) {
          if (hasPrice) return true;
          hasPrice = true;
        }
        if (!seen.add(i.id)) return true;
        return false;
      });
    }
    if (!hasPrice) bottomLayer.items.insert(0, LayerItem.price());
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

  LayerItem get price {
    for (final l in layers) {
      for (final i in l.items) {
        if (i.kind == ItemKind.price) return i;
      }
    }
    normalize(); // adds one
    return price;
  }

  LayerItem? get volume {
    for (final l in layers) {
      for (final i in l.items) {
        if (i.kind == ItemKind.volume) return i;
      }
    }
    return null;
  }

  /// Items that are drawn: visible items in visible layers, bottom layer first.
  List<LayerItem> get shown => [
        for (final l in layers.reversed)
          if (l.visible)
            for (final i in l.items)
              if (i.visible) i,
      ];

  /// All items (any visibility), bottom layer first.
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

  /// Removes a layer; returns its items so the caller can delete its drawings too. The last
  /// remaining layer cannot be removed; the price item moves to the layer below if needed.
  bool removeLayer(String id) {
    if (layers.length <= 1) return false;
    final l = layer(id);
    if (l == null) return false;
    final idx = layers.indexOf(l);
    layers.remove(l);
    final price = l.items.where((i) => i.kind == ItemKind.price).firstOrNull;
    if (price != null) layers[math.min(idx, layers.length - 1)].items.insert(0, price);
    if (activeLayer == id) activeLayer = layers[math.min(idx, layers.length - 1)].id;
    return true;
  }

  Layer duplicateLayer(String id) {
    final src = layer(id)!;
    final copy = Layer(id: newId('l'), name: '${src.name} copy', visible: src.visible, locked: src.locked, items: [
      for (final i in src.items)
        if (i.kind != ItemKind.price) LayerItem.fromJson(J({...i.toJson(), 'id': newId('i')})),
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
    if (f == null || f.$2.kind == ItemKind.price) return false; // the price chart can be hidden, not removed
    f.$1.items.remove(f.$2);
    // items attached to the removed one fall back to their default pane
    for (final i in all) {
      if (i.pane == 'with:$itemId') i.pane = 'auto';
    }
    return true;
  }

  void moveItem(String itemId, String toLayer, {int? index}) {
    final f = find(itemId);
    final target = layer(toLayer);
    if (f == null || target == null) return;
    f.$1.items.remove(f.$2);
    final at = index == null ? target.items.length : index.clamp(0, target.items.length);
    target.items.insert(at, f.$2);
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
