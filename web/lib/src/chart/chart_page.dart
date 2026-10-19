// The chart workbench page (TradingView-style): top toolbar, grouped drawing tools on the left,
// a grid of 1–4 synced charts, the bottom range/time bar, keyboard shortcuts and layouts.
import 'dart:async';
import 'dart:js_interop';
import 'dart:js_interop_unsafe';

import 'package:web/web.dart';

import '../core/chartmath.dart';
import '../core/layers.dart';
import '../core/json.dart';
import '../core/models.dart';
import '../core/time.dart';
import '../ui/api.dart';
import '../ui/dom.dart';
import '../ui/kit/icons.dart';
import '../ui/kit/overlay.dart';
import '../ui/kit/state.dart';
import '../ui/pickers.dart';
import '../ui/store.dart';
import 'chart_panel.dart';
import 'drawing_ops.dart';

class ToolDef {
  final String tool, label, iconName;
  final String? shortcut;
  const ToolDef(this.tool, this.label, this.iconName, [this.shortcut]);
}

const toolGroups = <String, List<ToolDef>>{
  'lines': [
    ToolDef('trendline', 'Trend line', 'trendline', 'Alt+T'),
    ToolDef('ray', 'Ray', 'ray'),
    ToolDef('extended_line', 'Extended line', 'extended'),
    ToolDef('horizontal_line', 'Horizontal line', 'hline', 'Alt+H'),
    ToolDef('horizontal_ray', 'Horizontal ray', 'hray', 'Alt+J'),
    ToolDef('vertical_line', 'Vertical line', 'vline', 'Alt+V'),
    ToolDef('parallel_channel', 'Parallel channel (3 clicks)', 'channel'),
  ],
  'fib': [ToolDef('fib_retracement', 'Fib retracement', 'fib', 'Alt+F')],
  'shapes': [ToolDef('rectangle', 'Rectangle / zone', 'rect', 'Alt+Shift+R'), ToolDef('arrow', 'Arrow', 'arrow')],
  'text': [
    ToolDef('text', 'Text', 'text'),
    ToolDef('callout', 'Callout (point, then label)', 'callout'),
    ToolDef('marker', 'Marker', 'marker'),
  ],
  'measure': [
    ToolDef('price_range', 'Price range / measure', 'measure'),
    ToolDef('long_position', 'Long position (entry, then target)', 'long', 'Alt+L'),
    ToolDef('short_position', 'Short position (entry, then target)', 'short', 'Alt+S'),
  ],
};

ToolDef? toolDef(String tool) {
  for (final g in toolGroups.values) {
    for (final t in g) {
      if (t.tool == tool) return t;
    }
  }
  return null;
}

const favoriteIntervals = ['1m', '5m', '15m', '1h', '1d', '1wk'];

class ChartPage {
  final HTMLElement root = h('div.chart-page');
  final HTMLElement _toolbar = h('div.chart-toolbar');
  final HTMLElement _draw = h('nav.draw-tools', null, {'aria-label': 'Drawing tools'});
  final HTMLElement _grid = h('div.chart-grid');
  final HTMLElement _bottom = h('div.bottom-bar');
  final HTMLElement _clockEl = h('span.clock', null, {'id': 'market-clock'});
  final HTMLElement dockSlot;
  final List<ChartPanel> panels = [];
  final Scope _scope = Scope();
  String _groupTool(String key) {
    final saved = store.pref<Map<String, Object?>>('draw.groups')[key];
    return saved is String && toolDef(saved) != null ? saved : toolGroups[key]!.first.tool;
  }
  Timer? _clock;
  final Scope _chartScope = Scope();

  ChartPage(this.dockSlot) {
    append(root, [
      _toolbar,
      h('div.chart-body', [_draw, _grid, dockSlot]),
      _bottom,
    ]);
    _scope.listen(store.grid, (_) => _buildGrid());
    _scope.listen(store.layoutVersion, (_) => _buildGrid());
    _scope.listen(store.active, (_) => _onActiveChanged());
    _scope.listen(store.configVersion, (_) {
      if (store.lastConfigChanged == chart.symbol.value) {
        _renderToolbar();
        _renderBottom();
      }
    });
    _scope.listen(store.tool, (_) => _renderDrawTools());
    _scope.listen(store.magnet, (_) => _renderDrawTools());
    _scope.listen(store.stayInDrawMode, (_) => _renderDrawTools());
    _scope.listen(store.drawingsHidden, (_) => _renderDrawTools());
    _scope.listen(store.drawingsLocked, (_) => _renderDrawTools());
    _scope.listen(store.undoVersion, (_) => _renderToolbar());
    _scope.listen(store.layoutName, (_) => _renderToolbar());
    _scope.listen(store.syncSymbol, (_) => _renderToolbar());
    _scope.listen(store.syncCrosshair, (_) => _renderToolbar());
    _scope.listen(store.syncInterval, (_) => _renderToolbar());
    _scope.listen(store.syncRange, (_) => _renderToolbar());
    _buildGrid();
    _renderDrawTools();
    _clock = Timer.periodic(const Duration(seconds: 1), (_) => _renderClock());
    window.addEventListener('keydown', ((KeyboardEvent e) => _onKey(e)).toJS);
    window.addEventListener('open-symbol-search', ((Event _) => _symbolSearch()).toJS);
    _exposeHook();
  }

  void dispose() {
    _clock?.cancel();
    _scope.dispose();
    _chartScope.dispose();
  }

  ChartPanel? get activePanel => panels.isEmpty ? null : panels[store.active.value.clamp(0, panels.length - 1)];
  ChartState get chart => store.activeChart;

  bool get visible => store.page.value == 'chart';

  // ---------------------------------------------------------------- grid

  void _buildGrid() {
    for (final p in panels) {
      p.dispose();
    }
    panels.clear();
    _chartScope.dispose();
    final n = chartsFor(store.grid.value);
    _grid.className = 'chart-grid grid-${store.grid.value}';
    for (var i = 0; i < n; i++) {
      final p = ChartPanel(store.charts[i], i, onActivate: (panel) {
        if (store.active.value != panel.index) store.active.value = panel.index;
      });
      panels.add(p);
      _grid.append(p.root);
      final st = store.charts[i];
      for (final o in st.all) {
        _chartScope.add(o.listen((_) {
          store.save();
          if (identical(st, store.activeChart)) _renderToolbar();
        }));
      }
      // symbol / interval sync across charts
      _chartScope.add(st.symbol.listen((s) {
        if (!store.syncSymbol.value) return;
        for (final other in store.charts.take(n)) {
          if (!identical(other, st)) other.symbol.value = s;
        }
      }));
      _chartScope.add(st.interval.listen((iv) {
        if (!store.syncInterval.value) return;
        for (final other in store.charts.take(n)) {
          if (!identical(other, st)) other.interval.value = iv;
        }
      }));
    }
    _onActiveChanged();
  }

  void _onActiveChanged() {
    for (final p in panels) {
      p.active = panels.length > 1 && p.index == store.active.value;
    }
    _renderToolbar();
    _renderBottom();
  }

  // ---------------------------------------------------------------- top toolbar

  HTMLElement _tbButton(Object content, String tip, void Function(Event) onClick, {bool on = false, String cls = ''}) =>
      h('button.tb${on ? '.on' : ''}${cls.isEmpty ? '' : '.$cls'}', content,
          {'type': 'button', 'data-tip': tip, 'aria-label': tip}, {'click': onClick});

  SymbolConfig? get _cfg => store.configNow(chart.symbol.value);

  void _editCfg(void Function(SymbolConfig) change, String label) => store.editConfig(chart.symbol.value, change, label: label);

  void _renderToolbar() {
    final c = chart;
    final iv = c.interval.value;
    final cfg = _cfg;
    final ct = cfg?.priceAt(store.activeChart.interval.value).chartType ?? 'candles';
    final favorites = store.prefList('chart.favoriteIntervals');
    final ctIcon = ct == 'candles' ? 'candles' : ct == 'heikin' ? 'heikin' : ct;
    replace(_toolbar, [
      _tbButton([icon('search', size: 15), h('b', c.symbol.value)], 'Symbol search (or just start typing)',
          (_) => _symbolSearch(), cls: 'symbol-btn'),
      _tbButton(icon('plus', size: 16), 'Compare or add symbol', (_) => _compare()),
      h('span.tb-sep'),
      for (final f in intervals.where(favorites.contains))
        _tbButton(intervalLabel(f), 'Interval $f', (_) => c.interval.value = f, on: f == iv, cls: 'iv'),
      if (!favorites.contains(iv)) _tbButton(intervalLabel(iv), 'Interval $iv', (_) {}, on: true, cls: 'iv'),
      _tbButton(icon('chev-down', size: 14), 'More intervals (or type a number); ★ toggles the toolbar', (e) {
        showMenu([
          for (final x in intervals)
            MenuItem(intervalLabel(x), checked: x == iv, hint: favorites.contains(x) ? '★' : '☆',
                onSelect: () => c.interval.value = x),
          const MenuItem.divider(),
          MenuItem('Toolbar intervals', iconName: 'star', submenu: [
            for (final x in intervals)
              MenuItem(intervalLabel(x), checked: favorites.contains(x), onSelect: () {
                final next = favorites.contains(x) ? (favorites.toList()..remove(x)) : [...favorites, x];
                store.setPref('chart.favoriteIntervals', next);
                _renderToolbar();
              }),
          ]),
        ], anchor: e.currentTarget as Element);
      }, cls: 'narrow'),
      h('span.tb-sep'),
      _tbButton(icon(ctIcon, size: 18), 'Chart type', (e) {
        showMenu([
          for (final (k, label) in chartTypes)
            MenuItem(label, iconName: k == 'heikin' ? 'heikin' : k, checked: k == ct,
                onSelect: () => _editCfg((cfg) => cfg.priceAt(store.activeChart.interval.value).chartType = k, 'Chart type')),
        ], anchor: e.currentTarget as Element);
      }),
      _tbButton([icon('fx', size: 17), h('span', 'Indicators')], 'Indicators (/)', (_) => _indicators(), cls: 'wide'),
      _tbButton(icon('alarm', size: 17), 'Create alert (Alt+A)', (_) => showAlertDialog(c.symbol.value,
          price: activePanel?.bars.isNotEmpty == true ? activePanel!.bars.last.close : null)),
      h('span.tb-sep'),
      _tbButton(icon('grid-${store.grid.value}', size: 17), 'Layout: charts and sync', (e) => _gridMenu(e.currentTarget as Element)),
      _tbButton(icon('undo', size: 17), store.undo.canUndo ? 'Undo ${store.undo.undoLabel} (⌘Z)' : 'Nothing to undo',
          (_) => undoLast(), cls: store.undo.canUndo ? '' : 'disabled'),
      _tbButton(icon('redo', size: 17), store.undo.canRedo ? 'Redo ${store.undo.redoLabel} (⇧⌘Z)' : 'Nothing to redo',
          (_) => redoLast(), cls: store.undo.canRedo ? '' : 'disabled'),
      h('span.spacer'),
      _tbButton([icon('folder', size: 16), h('span', store.layoutName.value ?? 'Unsaved layout'), icon('chev-down', size: 13)],
          'Save / load layouts', (e) => _layoutMenu(e.currentTarget as Element), cls: 'wide'),
      _tbButton(icon('settings', size: 17), 'Chart settings', (_) => showChartSettings()),
      _tbButton(icon('camera', size: 17), 'Screenshot', (_) => activePanel?.screenshot()),
      _tbButton(icon('fullscreen', size: 17), 'Fullscreen', (_) => _fullscreen()),
    ]);
  }

  void _symbolSearch([String initial = '']) =>
      openSymbolSearch(initial: initial, onPick: (s) => chart.symbol.value = s);

  /// Adds another symbol to the active layer: compared on the price pane, or as its own chart.
  void _compare() => openSymbolSearch(title: 'Add symbol to the active layer', onPick: (s) {
        final cfg = _cfg;
        if (cfg == null || s == chart.symbol.value) return;
        if (cfg.all.any((i) => i.kind == ItemKind.compare && i.symbol == s)) return;
        _editCfg((c) {
          c.addItem(LayerItem.compare(newId('c'), s));
          if (c.scale == 'linear') c.scale = 'percent';
        }, 'Add $s');
      });

  void _indicators() {
    final cfg = _cfg;
    if (cfg == null) return;
    openIndicatorPicker(
        current: [for (final (_, r) in cfg.indicatorItemsAt(store.activeChart.interval.value)) r],
        onAdd: (r) => _editCfg((c) => c.addItem(LayerItem.indicator(newId('i'), r.name, Map.of(r.params))), 'Add ${r.name}'));
  }

  void _gridMenu(Element anchor) {
    final g = store.grid.value;
    showMenu([
      const MenuItem.header('Charts'),
      MenuItem('1 chart', iconName: 'grid-1', checked: g == '1', onSelect: () => store.setGrid('1')),
      MenuItem('2 charts, side by side', iconName: 'grid-2v', checked: g == '2v', onSelect: () => store.setGrid('2v')),
      MenuItem('2 charts, stacked', iconName: 'grid-2h', checked: g == '2h', onSelect: () => store.setGrid('2h')),
      MenuItem('3 charts', iconName: 'grid-3', checked: g == '3', onSelect: () => store.setGrid('3')),
      MenuItem('4 charts', iconName: 'grid-4', checked: g == '4', onSelect: () => store.setGrid('4')),
      const MenuItem.header('Sync across charts'),
      MenuItem('Symbol', checked: store.syncSymbol.value, onSelect: () => _toggle(store.syncSymbol)),
      MenuItem('Interval', checked: store.syncInterval.value, onSelect: () => _toggle(store.syncInterval)),
      MenuItem('Crosshair', checked: store.syncCrosshair.value, onSelect: () => _toggle(store.syncCrosshair)),
      MenuItem('Date range', checked: store.syncRange.value, onSelect: () => _toggle(store.syncRange)),
    ], anchor: anchor);
  }

  void _toggle(Obs<bool> o) {
    o.value = !o.value;
    store.save();
  }

  Future<void> _layoutMenu(Element anchor) async {
    List<J> layouts = const [];
    try {
      layouts = (await request('/api/layouts')).list;
    } catch (_) {}
    final current = store.layoutName.value;
    showMenu([
      MenuItem('Save layout', iconName: 'save', shortcut: '⌘S', onSelect: saveLayout),
      MenuItem('Save layout as…', iconName: 'save', onSelect: () => saveLayout(asNew: true)),
      if (layouts.isNotEmpty) const MenuItem.header('Load layout'),
      for (final l in layouts)
        MenuItem(l['name'].s ?? '', iconName: 'folder', checked: l['name'].s == current, hint: l['summary'].s,
            onSelect: () => loadLayout(l['name'].s!)),
      if (current != null) ...[
        const MenuItem.divider(),
        MenuItem('Delete "$current"', iconName: 'trash', danger: true, onSelect: () async {
          if (!await confirmDialog('Delete layout', 'Delete the saved layout "$current"?', confirm: 'Delete', danger: true)) {
            return;
          }
          await request('/api/layouts/${Uri.encodeComponent(current)}', method: 'DELETE');
          store.layoutName.value = null;
          store.save();
        }),
      ],
    ], anchor: anchor);
  }

  Future<void> saveLayout({bool asNew = false}) async {
    var name = store.layoutName.value;
    if (name == null || asNew) {
      name = await promptDialog('Save layout', label: 'Layout name', initial: asNew ? '' : 'My layout');
      if (name == null || name.isEmpty) return;
    }
    final summary = [for (final c in store.charts.take(chartsFor(store.grid.value))) '${c.symbol.value} ${intervalLabel(c.interval.value)}']
        .join(' · ');
    try {
      await request('/api/layouts/${Uri.encodeComponent(name)}', method: 'PUT',
          body: {'data': store.layoutJson(), 'summary': summary});
      store.layoutName.value = name;
      store.save();
      toast('Layout "$name" saved', kind: 'success', ms: 2000);
    } catch (e) {
      toast('$e', kind: 'error');
    }
  }

  Future<void> loadLayout(String name) async {
    try {
      final l = await request('/api/layouts/${Uri.encodeComponent(name)}');
      store.applyLayout(l['data']);
      store.layoutName.value = name;
      store.save();
    } catch (e) {
      toast('$e', kind: 'error');
    }
  }

  void _fullscreen() {
    if (document.fullscreenElement != null) {
      document.exitFullscreen();
    } else {
      root.requestFullscreen();
    }
  }

  // ---------------------------------------------------------------- drawing toolbar

  void _renderDrawTools() {
    final tool = store.tool.value;
    HTMLElement toolBtn(String ic, String tip, bool on, void Function(Event) fn, {String cls = ''}) =>
        h('button.dt${on ? '.on' : ''}${cls.isEmpty ? '' : '.$cls'}', icon(ic, size: 19),
            {'type': 'button', 'data-tip': tip, 'data-tip-pos': 'right', 'aria-label': tip}, {'click': fn});
    replace(_draw, [
      toolBtn('cursor', 'Select / move (Esc)', tool == 'select', (_) => store.tool.value = 'select'),
      for (final e in toolGroups.entries) _group(e.key, e.value, tool),
      h('div.dt-sep'),
      toolBtn('magnet', 'Magnet: snap to OHLC (${store.magnet.value ? 'on' : 'off'})', store.magnet.value, (_) {
        store.magnet.value = !store.magnet.value;
        store.save();
      }),
      toolBtn('edit', 'Stay in drawing mode (${store.stayInDrawMode.value ? 'on' : 'off'})', store.stayInDrawMode.value, (_) {
        store.stayInDrawMode.value = !store.stayInDrawMode.value;
        store.save();
      }),
      toolBtn(store.drawingsLocked.value ? 'lock' : 'unlock', 'Lock all drawings', store.drawingsLocked.value,
          (_) => store.drawingsLocked.value = !store.drawingsLocked.value),
      toolBtn(store.drawingsHidden.value ? 'eye-off' : 'eye', 'Hide all drawings', store.drawingsHidden.value,
          (_) => store.drawingsHidden.value = !store.drawingsHidden.value),
      toolBtn('trash', 'Remove drawings…', false, (e) {
        final sym = chart.symbol.value;
        showMenu([
          MenuItem('Remove my drawings on $sym', onSelect: () => removeDrawings(sym, 'user')),
          MenuItem("Remove the agent's drawings on $sym", onSelect: () => removeDrawings(sym, 'agent')),
          MenuItem('Remove all drawings on $sym', danger: true, onSelect: () => removeDrawings(sym, null)),
        ], anchor: e.currentTarget as Element, align: 'start');
      }),
    ]);
  }

  HTMLElement _group(String key, List<ToolDef> tools, String active) {
    final current = tools.firstWhere((t) => t.tool == active, orElse: () => toolDef(_groupTool(key))!);
    final on = tools.any((t) => t.tool == active);
    final tip = '${current.label}${current.shortcut != null ? ' (${current.shortcut})' : ''}';
    return h('div.dt-group', [
      h('button.dt${on ? '.on' : ''}', icon(current.iconName, size: 19),
          {'type': 'button', 'data-tip': tip, 'data-tip-pos': 'right', 'aria-label': current.label}, {
        'click': (_) => store.tool.value = on ? 'select' : current.tool,
      }),
      if (tools.length > 1)
        h('button.dt-more', icon('chev-right', size: 10), {'type': 'button', 'aria-label': 'More ${key} tools'}, {
          'click': (e) {
            showMenu([
              for (final t in tools)
                MenuItem(t.label, iconName: t.iconName, shortcut: t.shortcut, checked: t.tool == active, onSelect: () {
                  store.setPref('draw.groups', {...store.pref<Map<String, Object?>>('draw.groups'), key: t.tool});
                  store.tool.value = t.tool;
                }),
            ], anchor: (e.currentTarget as Element).parentElement, align: 'start', cls: 'tool-flyout');
          },
        }),
    ]);
  }

  // ---------------------------------------------------------------- bottom bar

  void _renderBottom() {
    final scale = _cfg?.scale ?? 'linear';
    replace(_bottom, [
      for (final r in ['1D', '5D', '1M', '3M', '6M', 'YTD', '1Y', '5Y', 'All'])
        h('button.range-btn', r, {'type': 'button', 'data-tip': 'Show $r (switches to ${rangeInterval[r]})'}, {
          'click': (_) => _applyRange(r),
        }),
      h('button.range-btn', icon('calendar', size: 14), {'type': 'button', 'data-tip': 'Go to date', 'aria-label': 'Go to date'},
          {'click': (e) => _goToDate(e.currentTarget as Element)}),
      h('span.spacer'),
      _clockEl,
      h('span.tb-sep'),
      for (final (k, label, tip) in [('percent', '%', 'Percent scale'), ('log', 'log', 'Logarithmic scale'), ('linear', 'auto', 'Regular scale')])
        h('button.range-btn${scale == k ? '.on' : ''}', label, {'type': 'button', 'data-tip': tip}, {
          'click': (_) => _editCfg((c) => c.scale = k, 'Price scale'),
        }),
    ]);
    _renderClock();
  }

  void _applyRange(String r) {
    final want = rangeInterval[r]!;
    final c = chart;
    // keep daily-or-longer intervals for long ranges the user already chose
    final keep = !isIntraday(c.interval.value) && !isIntraday(want) && r != 'All' && r != '5Y';
    if (!keep && c.interval.value != want) {
      c.interval.value = want;
      Timer(const Duration(milliseconds: 900), () => activePanel?.showRange(r));
    } else {
      activePanel?.showRange(r);
    }
  }

  void _goToDate(Element anchor) {
    final input = h('input.input', null, {'type': 'date', 'aria-label': 'Date'}) as HTMLInputElement;
    late Popover pop;
    void go() {
      final d = DateTime.tryParse(input.value);
      pop.close();
      if (d == null) return;
      final t = DateTime.utc(d.year, d.month, d.day).millisecondsSinceEpoch ~/ 1000;
      final p = activePanel;
      if (p == null || p.bars.isEmpty) return;
      if (t < p.bars.first.time) {
        toast('Data for ${chart.symbol.value} starts ${formatBarTime(p.bars.first.time, p.intraday)}; try a longer interval',
            kind: 'warn');
        return;
      }
      final span = (p.engine.visibleRange.$2 - p.engine.visibleRange.$1);
      final i = firstIndexAtOrAfter(p.bars, t).toDouble();
      p.engine.setVisibleRange(i - span / 2, i + span / 2, notify: true);
    }

    input.addEventListener('keydown', ((KeyboardEvent e) {
      e.stopPropagation();
      if (e.key == 'Enter') go();
    }).toJS);
    pop = Popover.open(h('div.goto', [input, h('button.btn.primary.small', 'Go', {'type': 'button'}, {'click': (_) => go()})]),
        anchor: anchor);
    Timer.run(() => input.focus());
  }

  void _renderClock() {
    final el = _clockEl;
    final now = DateTime.now().toUtc();
    final c = civil(now.millisecondsSinceEpoch ~/ 1000, exchangeTime: true);
    final s = marketSession(now);
    String two(int v) => v.toString().padLeft(2, '0');
    var next = '';
    if (s.nextChange != null) {
      final left = s.nextChange! - now.millisecondsSinceEpoch ~/ 1000;
      next = ' · ${s.state == 'open' ? 'closes' : 'opens'} in ${left ~/ 3600}h ${two((left % 3600) ~/ 60)}m';
    }
    el.className = 'clock ${s.state}';
    el.textContent = '${two(c.hour)}:${two(c.minute)}:${two(now.second)} New York · ${s.label}$next';
  }

  // ---------------------------------------------------------------- keyboard

  void _onKey(KeyboardEvent e) {
    if (!visible || typingIn(e.target) || document.querySelector('.backdrop') != null) return;
    final mod = e.metaKey || e.ctrlKey;
    final key = e.key;
    final p = activePanel;
    if (mod && (key == 'z' || key == 'Z')) {
      e.preventDefault();
      e.shiftKey ? redoLast() : undoLast();
      return;
    }
    if (mod && key == 'y') {
      e.preventDefault();
      redoLast();
      return;
    }
    if (mod && key == 's') {
      e.preventDefault();
      saveLayout();
      return;
    }
    if (mod) return;
    if (e.altKey) {
      final code = e.code; // Alt changes e.key on macOS, the physical key is stable
      final tool = switch (code) {
        'KeyT' => 'trendline',
        'KeyH' => 'horizontal_line',
        'KeyJ' => 'horizontal_ray',
        'KeyV' => 'vertical_line',
        'KeyF' => 'fib_retracement',
        'KeyL' => 'long_position',
        'KeyS' => 'short_position',
        'KeyR' when e.shiftKey => 'rectangle',
        _ => null,
      };
      if (tool != null) {
        e.preventDefault();
        store.tool.value = tool;
        return;
      }
      if (code == 'KeyR') {
        e.preventDefault();
        p?.resetView();
        return;
      }
      if (code == 'KeyA') {
        e.preventDefault();
        showAlertDialog(chart.symbol.value, price: p?.bars.isNotEmpty == true ? p!.bars.last.close : null);
        return;
      }
      return;
    }
    switch (key) {
      case 'Escape':
        if (store.tool.value != 'select') {
          store.tool.value = 'select';
        } else {
          store.selected.value = null;
        }
        p?.cancelDraft();
        return;
      case 'Delete' || 'Backspace':
        final id = store.selected.value;
        final a = id == null ? null : store.drawingById(id);
        if (a != null) {
          e.preventDefault();
          deleteDrawing(a);
        }
        return;
      case 'ArrowLeft':
        e.preventDefault();
        p?.engine.scrollBars(e.shiftKey ? -20 : -3);
        return;
      case 'ArrowRight':
        e.preventDefault();
        p?.engine.scrollBars(e.shiftKey ? 20 : 3);
        return;
      case 'ArrowUp':
        e.preventDefault();
        p?.engine.zoom(1.15);
        return;
      case 'ArrowDown':
        e.preventDefault();
        p?.engine.zoom(1 / 1.15);
        return;
      case 'End':
        p?.engine.scrollToRealtime();
        return;
      case '/':
        e.preventDefault();
        _indicators();
        return;
      case '?':
        showShortcuts();
        return;
    }
    if (key.length == 1 && RegExp(r'[0-9]').hasMatch(key)) {
      e.preventDefault();
      openIntervalPrompt(key, (iv) => chart.interval.value = iv);
    } else if (key.length == 1 && RegExp(r'[A-Za-z^]').hasMatch(key)) {
      e.preventDefault();
      _symbolSearch(key.toUpperCase());
    }
  }

  void _exposeHook() {
    final o = JSObject();
    o['panel'] = ((int i) => i < panels.length ? panels[i].hook() : null).toJS;
    o['active'] = (() => activePanel?.hook()).toJS;
    o['count'] = (() => panels.length.toJS).toJS;
    final hook = JSObject();
    hook['chart'] = o;
    globalContext['__autobench'] = hook;
    // convenience aliases used by the end-to-end tests
    hook['bars'] = (() => (activePanel?.bars.length ?? 0).toJS).toJS;
    hook['drawn'] = (() => (activePanel?.layer.annotations.where((a) => a.visible).length ?? 0).toJS).toJS;
    hook['panes'] = (() => (activePanel?.engine.paneCount ?? 0).toJS).toJS;
    hook['series'] = (() => (activePanel?.engine.series.length ?? 0).toJS).toJS;
    hook['visibleRange'] = (() {
      final r = activePanel?.engine.visibleRange ?? (0.0, 0.0);
      return [r.$1.toJS, r.$2.toJS].toJS;
    }).toJS;
    hook['xOfTime'] = ((int t) => activePanel?.layer.x(t)?.toJS).toJS;
    hook['yOfPrice'] = ((double p) => activePanel?.layer.y(p)?.toJS).toJS;
  }
}

void showShortcuts() {
  const rows = [
    ('Type a letter', 'Symbol search'),
    ('Type a number', 'Change interval (5, 15, 60, D, W)'),
    ('/', 'Indicators'),
    ('Alt+T / H / J / V', 'Trend line / horizontal line / horizontal ray / vertical line'),
    ('Alt+F', 'Fib retracement'),
    ('Alt+L / Alt+S', 'Long / short position'),
    ('Alt+Shift+R', 'Rectangle'),
    ('Alt+A', 'Create alert'),
    ('Alt+R', 'Reset chart view'),
    ('Esc', 'Cancel drawing / deselect'),
    ('Del / Backspace', 'Delete selected drawing'),
    ('⌘Z / ⇧⌘Z', 'Undo / redo'),
    ('⌘S', 'Save layout'),
    ('← → (Shift: faster)', 'Scroll'),
    ('↑ ↓', 'Zoom'),
    ('End', 'Scroll to latest'),
    ('Double-click price axis', 'Auto-scale'),
    ('Double-click drawing', 'Edit text'),
    ('Drag price/time axis', 'Scale'),
    ('Alt while dragging a point', 'Disable magnet'),
  ];
  Dialog.open('Keyboard shortcuts', h('table.shortcuts', [
    for (final (k, v) in rows) h('tr', [h('td', h('kbd', k)), h('td', v)]),
  ]), width: '520px');
}

/// Chart preferences (apply to every chart; persisted).
void showChartSettings() {
  HTMLElement toggle(String key, String label) {
    final input = h('input', null, {'type': 'checkbox'}) as HTMLInputElement;
    input.checked = store.pref<bool>(key);
    input.addEventListener('change', ((Event _) => store.setPref(key, input.checked)).toJS);
    return h('label.check-row', [input, ' $label']);
  }

  HTMLElement colorRow(String key, String label, List<String> options) {
    final row = h('div.swatch-row');
    void render() {
      final cur = store.pref<String?>(key);
      replace(row, [
        h('button.swatch-btn${cur == null ? '.on' : ''}', h('span.tiny', 'Theme'), {'type': 'button'}, {
          'click': (_) {
            store.setPref(key, null);
            render();
          },
        }),
        for (final c in options)
          h('button.swatch-btn${cur == c ? '.on' : ''}', h('span.swatch', null, {'style': 'background:$c'}),
              {'type': 'button', 'aria-label': c}, {
            'click': (_) {
              store.setPref(key, c);
              render();
            },
          }),
      ]);
    }

    render();
    return h('div.field-col', [h('span', label), row]);
  }

  Dialog.open('Chart settings', h('div.form-grid', [
    toggle('chart.grid', 'Grid lines'),
    toggle('chart.lastPriceLine', 'Last price line'),
    toggle('chart.countdown', 'Bar close countdown (intraday)'),
    toggle('chart.markers', 'Earnings markers'),
    colorRow('chart.upColor', 'Up candles', ['#12b886', '#22c55e', '#26a69a', '#2f7cf6', '#e2e8f0']),
    colorRow('chart.downColor', 'Down candles', ['#f0414f', '#ef4444', '#ef5350', '#f59e0b', '#64748b']),
    h('div.muted.small', 'Layers, indicators and the price/volume display are remembered per symbol (Layers panel); '
        'these settings apply to all charts.'),
  ]), actions: [const DialogAction('Done', primary: true)], width: '440px');
}
