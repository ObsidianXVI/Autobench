// Right-hand widget dock (TradingView-style): an icon rail that opens/collapses panels, with a
// draggable left edge to resize the open panel.
import 'dart:js_interop';

import 'package:web/web.dart';

import '../dom.dart';
import '../kit/icons.dart';
import '../kit/state.dart';
import '../store.dart';
import 'agent.dart';
import 'alerts.dart';
import 'news.dart';
import 'watchlist.dart';

class _PanelDef {
  final String key, label, iconName;
  final Object Function() create;
  _PanelDef(this.key, this.label, this.iconName, this.create);
}

class Dock {
  final HTMLElement root = h('div.dock');
  final HTMLElement _resizer = h('div.dock-resizer', null, {'aria-hidden': 'true'});
  final HTMLElement _host = h('div.dock-host');
  final HTMLElement _rail = h('nav.dock-rail', null, {'aria-label': 'Panels'});
  final Scope _scope = Scope();
  final Map<String, Object> _panels = {};
  late final List<_PanelDef> _defs;
  final void Function(String symbol) onCreateAlert;

  Dock({required this.onCreateAlert}) {
    _defs = [
      _PanelDef('watchlist', 'Watchlist & details', 'list', () => WatchlistPanel()),
      _PanelDef('alerts', 'Alerts', 'alarm', () => AlertsPanel(onCreate: onCreateAlert)),
      _PanelDef('news', 'News', 'news', () => NewsPanel()),
      _PanelDef('agent', 'Agent activity & analyses', 'bot', () => AgentPanel()),
      _PanelDef('notes', 'Notes', 'note', () => NotesPanel()),
      _PanelDef('objects', 'Object tree (drawings)', 'layers', () => ObjectsPanel()),
    ];
    append(root, [_resizer, _host, _rail]);
    _scope.listen(store.panel, (_) => _render());
    _scope.listen(store.panelWidth, (w) => _host.style.width = '${w}px');
    _scope.listen(store.unseenAgent, (_) => _renderRail());
    _scope.listen(store.alerts, (_) => _renderRail());
    _host.style.width = '${store.panelWidth.value}px';
    _bindResize();
    _render();
  }

  HTMLElement _rootOf(Object panel) => switch (panel) {
        WatchlistPanel p => p.root,
        AlertsPanel p => p.root,
        NewsPanel p => p.root,
        AgentPanel p => p.root,
        NotesPanel p => p.root,
        ObjectsPanel p => p.root,
        _ => h('div'),
      };

  void _render() {
    final key = store.panel.value;
    root.classList.toggle('collapsed', key == null);
    if (key != null) {
      final def = _defs.firstWhere((d) => d.key == key, orElse: () => _defs.first);
      final panel = _panels.putIfAbsent(def.key, def.create);
      replace(_host, _rootOf(panel));
      if (key == 'agent') store.unseenAgent.value = 0;
    }
    _renderRail();
  }

  void _renderRail() {
    final key = store.panel.value;
    final triggered = store.alerts.value.where((a) => a.status == 'triggered').length;
    replace(_rail, [
      for (final d in _defs)
        h('button.rail-btn${d.key == key ? '.on' : ''}', [
          icon(d.iconName, size: 20),
          if (d.key == 'agent' && store.unseenAgent.value > 0) h('span.rail-badge.agent', '${store.unseenAgent.value}'),
          if (d.key == 'alerts' && triggered > 0) h('span.rail-badge', '$triggered'),
        ], {
          'type': 'button',
          'data-tip': d.label,
          'data-tip-pos': 'left',
          'aria-label': d.label,
          'aria-pressed': '${d.key == key}',
        }, {
          'click': (_) {
            store.panel.value = store.panel.value == d.key ? null : d.key;
            store.save();
          },
        }),
    ]);
  }

  void _bindResize() {
    double? startX, startW;
    _resizer.addEventListener('pointerdown', ((PointerEvent e) {
      startX = e.clientX.toDouble();
      startW = store.panelWidth.value;
      _resizer.setPointerCapture(e.pointerId);
      root.classList.add('resizing');
    }).toJS);
    _resizer.addEventListener('pointermove', ((PointerEvent e) {
      if (startX == null) return;
      store.panelWidth.value = (startW! - (e.clientX - startX!)).clamp(240.0, 640.0);
    }).toJS);
    _resizer.addEventListener('pointerup', ((PointerEvent e) {
      startX = null;
      root.classList.remove('resizing');
      store.save();
    }).toJS);
  }
}
