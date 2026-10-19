// Layers panel: the active chart symbol's layers (top first). Each layer holds series items (price,
// volume, indicators, other symbols) and drawings; layers and items can be shown/hidden, locked,
// renamed, reordered and moved between layers by drag and drop. Everything is remembered per symbol.

import 'package:web/web.dart';

import '../../chart/chart_panel.dart';
import '../../chart/drawing_ops.dart';
import '../../core/layers.dart';
import '../../core/models.dart';
import '../api.dart';
import '../dom.dart';
import '../kit/icons.dart';
import '../kit/overlay.dart';
import '../kit/state.dart';
import '../pickers.dart';
import '../store.dart';

const _kindIcon = {
  ItemKind.price: 'candles',
  ItemKind.volume: 'chart',
  ItemKind.indicator: 'fx',
  ItemKind.compare: 'compare',
};

class LayersPanel {
  final HTMLElement root = h('div.panel.layers-panel');
  final HTMLElement _head = h('div.panel-head');
  final HTMLElement _body = h('div.panel-scroll.layers-body');
  final Scope _scope = Scope();
  final Set<String> _collapsed = {};
  String? _dragKind; // 'layer' | 'item' | 'drawing'
  String? _dragId;

  LayersPanel() {
    append(root, [_head, _body]);
    void track() {
      for (final c in store.charts) {
        _scope.listen(c.symbol, (_) => _load());
      }
    }

    track();
    _scope.listen(store.layoutVersion, (_) => track());
    _scope.listen(store.active, (_) => _load());
    _scope.listen(store.configVersion, (_) {
      if (store.lastConfigChanged == symbol) _render();
    });
    _scope.listen(store.drawingsVersion, (_) => _render());
    _scope.listen(store.selected, (_) => _render());
    _load();
  }

  void dispose() => _scope.dispose();

  String get symbol => store.activeChart.symbol.value;

  Future<void> _load() async {
    _render();
    await store.config(symbol);
    _render();
  }

  void _edit(void Function(SymbolConfig c) change, String label) => store.editConfig(symbol, change, label: label);

  // ---------------------------------------------------------------- render

  void _render() {
    final cfg = store.configNow(symbol);
    replace(_head, [
      h('span.panel-title.static', ['Layers', h('span.muted.small', ' · $symbol')]),
      h('span.spacer'),
      h('button.icon-btn', icon('plus', size: 16), {'type': 'button', 'data-tip': 'New layer', 'aria-label': 'New layer'},
          {'click': (_) => _newLayer()}),
      h('button.icon-btn', icon('more', size: 16), {'type': 'button', 'data-tip': 'Layer options', 'aria-label': 'Layer options'},
          {'click': (e) => _panelMenu(e.currentTarget as Element)}),
    ]);
    if (cfg == null) {
      replace(_body, h('div.muted.pad.small', 'Loading layers…'));
      return;
    }
    final drawings = store.drawingsFor(symbol);
    replace(_body, [
      for (final l in cfg.layers) _layer(cfg, l, [for (final a in drawings) if (cfg.layerOfDrawing(a.layer, a.author) == l) a]),
      h('div.muted.tiny.pad', 'New indicators, symbols and drawings go to the active layer. Drag rows between layers; '
          'drag a layer header to reorder.'),
    ]);
  }

  HTMLElement _layer(SymbolConfig cfg, Layer l, List<Annotation> drawings) {
    final active = cfg.activeLayer == l.id;
    final collapsed = _collapsed.contains('$symbol:${l.id}');
    final count = l.items.length + drawings.length;
    final head = h('div.layer-head${active ? '.active' : ''}${l.visible ? '' : '.dim'}', [
      h('button.icon-btn.small', icon(collapsed ? 'chev-right' : 'chev-down', size: 13),
          {'type': 'button', 'aria-label': collapsed ? 'Expand' : 'Collapse'}, {
        'click': (e) {
          e.stopPropagation();
          final k = '$symbol:${l.id}';
          collapsed ? _collapsed.remove(k) : _collapsed.add(k);
          _render();
        },
      }),
      h('button.icon-btn.small', icon(l.visible ? 'eye' : 'eye-off', size: 14),
          {'type': 'button', 'aria-label': '${l.visible ? 'Hide' : 'Show'} layer ${l.name}', 'data-tip': l.visible ? 'Hide layer' : 'Show layer'}, {
        'click': (e) {
          e.stopPropagation();
          _edit((c) => c.layer(l.id)?.visible = !l.visible, '${l.visible ? 'Hide' : 'Show'} ${l.name}');
        },
      }),
      h('button.icon-btn.small${l.locked ? '.on' : ''}', icon(l.locked ? 'lock' : 'unlock', size: 13),
          {'type': 'button', 'aria-label': '${l.locked ? 'Unlock' : 'Lock'} layer ${l.name}', 'data-tip': l.locked ? 'Unlock drawings' : 'Lock drawings'}, {
        'click': (e) {
          e.stopPropagation();
          _edit((c) => c.layer(l.id)?.locked = !l.locked, '${l.locked ? 'Unlock' : 'Lock'} ${l.name}');
        },
      }),
      h('span.layer-name', [if (l.id == agentLayerId) h('span.badge-agent', '✦'), ' ', l.name]),
      if (active) h('span.tag', 'active'),
      h('span.spacer'),
      h('span.muted.tiny', '$count'),
      h('button.icon-btn.small', icon('more', size: 14), {'type': 'button', 'aria-label': 'Layer ${l.name} menu'},
          {'click': (e) {
            e.stopPropagation();
            _layerMenu(cfg, l, drawings, e.currentTarget as Element);
          }}),
    ], {
      'draggable': 'true',
      'data-layer': l.id,
      'data-tip': active ? 'Active layer: new items and drawings go here' : 'Click to make this the active layer',
    }, {
      'click': (_) => _edit((c) => c.activeLayer = l.id, 'Activate ${l.name}'),
      'dblclick': (_) => _rename(l),
      'dragstart': (e) => _startDrag(e as DragEvent, 'layer', l.id),
      'dragover': (e) => _over(e as DragEvent),
      'dragleave': (e) => (e.currentTarget as Element).classList.remove('drop-target'),
      'drop': (e) => _dropOnLayer(e as DragEvent, l.id),
    });
    return h('div.layer${active ? '.active' : ''}', [
      head,
      if (!collapsed) ...[
        for (final (i, item) in l.items.indexed) _item(cfg, l, item, i),
        for (final a in drawings) _drawing(l, a),
        if (count == 0) h('div.layer-empty.muted.tiny', 'Empty – add indicators or drawings while this layer is active, or drop rows here'),
      ],
    ]);
  }

  HTMLElement _item(SymbolConfig cfg, Layer l, LayerItem item, int index) {
    final shown = cfg.itemShown(item);
    return h('div.layer-row${shown ? '' : '.dim'}', [
      h('button.icon-btn.small', icon(item.visible ? 'eye' : 'eye-off', size: 13),
          {'type': 'button', 'aria-label': '${item.visible ? 'Hide' : 'Show'} ${item.label}'}, {
        'click': (e) {
          e.stopPropagation();
          _edit((c) => c.find(item.id)?.$2.visible = !item.visible, '${item.visible ? 'Hide' : 'Show'} ${item.label}');
        },
      }),
      h('span.row-icon', icon(_kindIcon[item.kind]!, size: 14)),
      h('span.ellipsis', item.label),
      if (item.color != null) h('span.dot', null, {'style': 'background:${item.color}'}),
      h('span.spacer'),
      h('span.muted.tiny', _paneLabel(cfg, item)),
      h('button.icon-btn.small', icon('settings', size: 13), {'type': 'button', 'aria-label': 'Settings for ${item.label}'},
          {'click': (e) {
            e.stopPropagation();
            editLayerItem(symbol, item.id);
          }}),
      if (item.kind != ItemKind.price)
        h('button.icon-btn.small', icon('x', size: 13), {'type': 'button', 'aria-label': 'Remove ${item.label}'}, {
          'click': (e) {
            e.stopPropagation();
            _edit((c) => c.removeItem(item.id), 'Remove ${item.label}');
          },
        }),
    ], {
      'draggable': 'true',
      'data-item': item.id,
    }, {
      'dragstart': (e) => _startDrag(e as DragEvent, 'item', item.id),
      'dragover': (e) => _over(e as DragEvent),
      'dragleave': (e) => (e.currentTarget as Element).classList.remove('drop-target'),
      'drop': (e) => _dropOnItem(e as DragEvent, l.id, index),
      'dblclick': (_) => editLayerItem(symbol, item.id),
    });
  }

  String _paneLabel(SymbolConfig cfg, LayerItem item) {
    if (item.kind == ItemKind.price) return 'main';
    if (item.pane.startsWith('with:')) return 'with ${cfg.find(item.pane.substring(5))?.$2.label ?? '?'}';
    return switch (item.pane) {
      'main' => 'main',
      'new' => 'own pane',
      _ => item.kind == ItemKind.volume || item.kind == ItemKind.compare
          ? 'main'
          : (store.specs.value.where((s) => s.name == item.name).firstOrNull?.overlay ?? false)
              ? 'main'
              : 'own pane',
    };
  }

  HTMLElement _drawing(Layer l, Annotation a) {
    final locked = a.style['locked'].b == true;
    final selected = store.selected.value == a.id;
    return h('div.layer-row.drawing${selected ? '.selected' : ''}${a.visible ? '' : '.dim'}', [
      h('button.icon-btn.small', icon(a.visible ? 'eye' : 'eye-off', size: 13),
          {'type': 'button', 'aria-label': '${a.visible ? 'Hide' : 'Show'} drawing'}, {
        'click': (e) {
          e.stopPropagation();
          updateDrawing(a, {'visible': !a.visible}, label: a.visible ? 'Hide drawing' : 'Show drawing');
        },
      }),
      h('span.row-icon', icon('edit', size: 13)),
      h('span.ellipsis', [
        h('span.obj-type', a.type.replaceAll('_', ' ')),
        if (a.text != null && a.text!.isNotEmpty) h('span.muted', ' · ${a.text}'),
      ]),
      if (a.author == 'agent') h('span.badge-agent', '✦'),
      h('span.spacer'),
      h('button.icon-btn.small${locked ? '.on' : ''}', icon(locked ? 'lock' : 'unlock', size: 12),
          {'type': 'button', 'aria-label': locked ? 'Unlock drawing' : 'Lock drawing'}, {
        'click': (e) {
          e.stopPropagation();
          final style = {if (a.style.v is Map) ...Map<String, Object?>.from(a.style.v as Map), 'locked': !locked};
          updateDrawing(a, {'style': style}, label: locked ? 'Unlock drawing' : 'Lock drawing');
        },
      }),
      h('button.icon-btn.small', icon('trash', size: 13), {'type': 'button', 'aria-label': 'Delete drawing'}, {
        'click': (e) {
          e.stopPropagation();
          deleteDrawing(a);
        },
      }),
    ], {
      'draggable': 'true',
    }, {
      'click': (_) => store.selected.value = a.id,
      'dragstart': (e) => _startDrag(e as DragEvent, 'drawing', a.id),
    });
  }

  // ---------------------------------------------------------------- drag & drop

  void _startDrag(DragEvent e, String kind, String id) {
    e.stopPropagation();
    _dragKind = kind;
    _dragId = id;
    e.dataTransfer?.effectAllowed = 'move';
    e.dataTransfer?.setData('text/plain', id);
  }

  void _over(DragEvent e) {
    if (_dragKind == null) return;
    e.preventDefault();
    (e.currentTarget as Element).classList.add('drop-target');
  }

  void _dropOnLayer(DragEvent e, String layerId) {
    e.preventDefault();
    e.stopPropagation();
    (e.currentTarget as Element).classList.remove('drop-target');
    final kind = _dragKind, id = _dragId;
    _dragKind = _dragId = null;
    if (kind == null || id == null) return;
    switch (kind) {
      case 'layer':
        _edit((c) => c.placeLayerBefore(id, layerId), 'Reorder layers');
      case 'item':
        _edit((c) => c.moveItem(id, layerId), 'Move to layer');
      case 'drawing':
        _moveDrawing(id, layerId);
    }
  }

  void _dropOnItem(DragEvent e, String layerId, int index) {
    e.preventDefault();
    e.stopPropagation();
    (e.currentTarget as Element).classList.remove('drop-target');
    final kind = _dragKind, id = _dragId;
    _dragKind = _dragId = null;
    if (kind == 'item' && id != null) {
      _edit((c) => c.moveItem(id, layerId, index: index), 'Move item');
    } else if (kind == 'drawing' && id != null) {
      _moveDrawing(id, layerId);
    } else if (kind == 'layer' && id != null) {
      _edit((c) => c.placeLayerBefore(id, layerId), 'Reorder layers');
    }
  }

  void _moveDrawing(String id, String layerId) {
    final a = store.drawingById(id);
    if (a == null) return;
    updateDrawing(a, {'layer': layerId == mainLayerId ? '' : layerId}, label: 'Move drawing to layer');
  }

  // ---------------------------------------------------------------- menus & actions

  Future<void> _newLayer() async {
    final name = await promptDialog('New layer', label: 'Name', initial: 'Layer ${(store.configNow(symbol)?.layers.length ?? 0) + 1}');
    if (name == null || name.isEmpty) return;
    _edit((c) => c.addLayer(name), 'New layer');
  }

  Future<void> _rename(Layer l) async {
    final name = await promptDialog('Rename layer', label: 'Name', initial: l.name);
    if (name == null || name.isEmpty || name == l.name) return;
    _edit((c) => c.layer(l.id)?.name = name, 'Rename layer');
  }

  void _layerMenu(SymbolConfig cfg, Layer l, List<Annotation> drawings, Element anchor) {
    final i = cfg.layers.indexOf(l);
    showMenu([
      MenuItem('Make active', iconName: 'check', disabled: cfg.activeLayer == l.id,
          onSelect: () => _edit((c) => c.activeLayer = l.id, 'Activate ${l.name}')),
      MenuItem('Rename…', iconName: 'edit', onSelect: () => _rename(l)),
      MenuItem('Add indicator…', iconName: 'fx', onSelect: () {
        _edit((c) => c.activeLayer = l.id, 'Activate ${l.name}');
        openIndicatorPicker(
            current: [for (final (_, r) in cfg.indicatorItems) r],
            onAdd: (r) => _edit((c) => c.addItem(LayerItem.indicator(newId('i'), r.name, Map.of(r.params)), layerId: l.id),
                'Add ${r.name}'));
      }),
      MenuItem('Add symbol…', iconName: 'compare', onSelect: () => openSymbolSearch(title: 'Add a symbol to ${l.name}', onPick: (s) {
            if (s == symbol) return;
            _edit((c) => c.addItem(LayerItem.compare(newId('c'), s, pane: 'new'), layerId: l.id), 'Add $s');
          })),
      if (cfg.volume == null)
        MenuItem('Add volume', iconName: 'chart', onSelect: () => _edit((c) => c.addItem(LayerItem.volume(), layerId: l.id), 'Add volume')),
      MenuItem('Duplicate', iconName: 'copy', onSelect: () => _edit((c) => c.duplicateLayer(l.id), 'Duplicate layer')),
      MenuItem('Move up', iconName: 'chev-up', disabled: i == 0, onSelect: () => _edit((c) => c.moveLayer(l.id, -1), 'Move layer')),
      MenuItem('Move down', iconName: 'chev-down', disabled: i == cfg.layers.length - 1,
          onSelect: () => _edit((c) => c.moveLayer(l.id, 1), 'Move layer')),
      const MenuItem.divider(),
      MenuItem('Delete layer', iconName: 'trash', danger: true, disabled: cfg.layers.length <= 1, onSelect: () async {
        final what = [
          if (l.items.where((x) => x.kind != ItemKind.price).isNotEmpty) '${l.items.where((x) => x.kind != ItemKind.price).length} item(s)',
          if (drawings.isNotEmpty) '${drawings.length} drawing(s)',
        ];
        if (what.isNotEmpty &&
            !await confirmDialog('Delete layer', 'Delete "${l.name}" with ${what.join(' and ')}?', confirm: 'Delete', danger: true)) {
          return;
        }
        if (drawings.isNotEmpty) {
          try {
            await request('/api/annotations?symbol=${Uri.encodeComponent(symbol)}&layer=${Uri.encodeComponent(l.id)}', method: 'DELETE');
            await store.loadDrawings(symbol, force: true);
          } catch (e) {
            toast('$e', kind: 'error');
          }
        }
        _edit((c) => c.removeLayer(l.id), 'Delete layer');
      }),
    ], anchor: anchor, align: 'end');
  }

  void _panelMenu(Element anchor) {
    showMenu([
      MenuItem('New layer…', iconName: 'plus', onSelect: _newLayer),
      MenuItem('Show all layers', iconName: 'eye', onSelect: () => _edit((c) {
            for (final l in c.layers) {
              l.visible = true;
            }
          }, 'Show all layers')),
      const MenuItem.divider(),
      MenuItem('Save as default for new symbols', iconName: 'save', onSelect: () async {
        await store.saveAsDefault(symbol);
        toast('New symbols will start with these layers', kind: 'success', ms: 2500);
      }),
      MenuItem('Copy layers to another symbol…', iconName: 'copy', onSelect: () => openSymbolSearch(
          title: 'Copy $symbol layers to', onPick: (target) async {
            final src = store.configNow(symbol);
            if (src == null || target == symbol) return;
            await store.config(target);
            store.editConfig(target, (c) {
              final copy = src.clone();
              c.layers
                ..removeWhere((l) => l.items.isNotEmpty) // keep the target's drawing-only layers
                ..insertAll(0, copy.layers.where((l) => l.items.isNotEmpty || c.layer(l.id) == null));
              c.scale = copy.scale;
            }, label: 'Copy layers');
            toast('Copied to $target', kind: 'success', ms: 2000);
          })),
      MenuItem('Reset to default', iconName: 'refresh', onSelect: () async {
        if (await confirmDialog('Reset layers', 'Replace $symbol\'s series layers with the default template? Drawings are kept.',
            confirm: 'Reset')) {
          await store.resetToDefault(symbol);
        }
      }),
    ], anchor: anchor, align: 'end');
  }
}
