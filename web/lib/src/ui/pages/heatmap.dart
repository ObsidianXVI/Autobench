// Market heatmap (FinViz-style): sectors → industries → stocks as a squarified treemap sized by
// market cap and colored by the period's change. Drill into a sector, hover for peers, click to chart.
import 'dart:async';
import 'dart:js_interop';
import 'dart:js_interop_unsafe';
import 'dart:math' as math;

import 'package:web/web.dart';

import '../../core/chartmath.dart';
import '../../core/format.dart';
import '../../core/json.dart';
import '../../core/treemap.dart';
import '../api.dart';
import '../dom.dart';
import '../kit/icons.dart';
import '../kit/overlay.dart';
import '../store.dart';

const periodSpan = {'1d': 3.0, '1w': 6.0, '1m': 10.0, '3m': 20.0, '6m': 30.0, 'ytd': 40.0, '1y': 50.0};

class _Tile {
  final Rect r;
  final J stock;
  final String sector, industry;
  final List<J> peers;
  _Tile(this.r, this.stock, this.sector, this.industry, this.peers);
}

class _Header {
  final Rect r;
  final String sector;
  _Header(this.r, this.sector);
}

class HeatmapPage {
  final HTMLElement root = h('div.page.heatmap-page');
  final HTMLElement _controls = h('div.hm-controls');
  final HTMLElement _wrap = h('div.hm-wrap');
  final HTMLCanvasElement _canvas = h('canvas.hm-canvas') as HTMLCanvasElement;
  final HTMLElement _tip = h('div.hm-tip');
  final HTMLElement _overlay = h('div.hm-overlay');
  J? _data;
  String? _error;
  bool _loading = false;
  String? _zoomSector;
  String _highlight = '';
  final List<_Tile> _tiles = [];
  final List<_Header> _headers = [];
  _Tile? _hover;
  int _seq = 0;
  Timer? _refresh;

  HeatmapPage() {
    _wrap.append(_canvas);
    _wrap.append(_tip);
    _wrap.append(_overlay);
    _tip.isHidden = true;
    append(root, [_controls, _wrap]);
    ResizeObserver(((JSArray<JSObject> _, JSObject _) => redraw()).toJS).observe(_wrap);
    _canvas.addEventListener('mousemove', ((MouseEvent e) => _onMove(e)).toJS);
    _canvas.addEventListener('mouseleave', ((Event _) {
      _hover = null;
      _tip.isHidden = true;
      redraw();
    }).toJS);
    _canvas.addEventListener('click', ((MouseEvent e) => _onClick(e)).toJS);
    _canvas.addEventListener('contextmenu', ((MouseEvent e) {
      e.preventDefault();
      final t = _hover;
      if (t == null) return;
      final sym = t.stock['symbol'].s!;
      showMenu([
        MenuItem('Open $sym chart', iconName: 'chart', onSelect: () => _open(sym)),
        MenuItem('Add to ${store.watchlist.value}', iconName: 'list', onSelect: () async {
          await request('/api/watchlists/${Uri.encodeComponent(store.watchlist.value)}', method: 'PUT', body: {'add': [sym]});
          store.watchlistVersion.value++;
          toast('$sym added', kind: 'success', ms: 1500);
        }),
        MenuItem('Zoom into ${t.sector}', iconName: 'search', onSelect: () {
          _zoomSector = t.sector;
          _renderControls();
          redraw();
        }),
      ], x: clientXY(e).$1, y: clientXY(e).$2);
    }).toJS);
    _renderControls();
  }

  Map<String, Object?> get _st => store.heatmapState.value;
  String get _period => (_st['period'] as String?) ?? '1d';
  String get _universe => (_st['universe'] as String?) ?? 'sp500';

  void onShow() {
    _renderControls();
    _load();
    _refresh ??= Timer.periodic(const Duration(seconds: 60), (_) {
      if (!root.isHidden && root.isConnected && _period == '1d') _load(quiet: true);
    });
  }

  void _set(Map<String, Object?> patch) {
    store.heatmapState.value = {..._st, ...patch};
    store.save();
    _renderControls();
    _load();
  }

  void _renderControls() {
    final search = h('input.input', null, {'placeholder': 'Highlight ticker', 'aria-label': 'Highlight ticker', 'value': _highlight})
        as HTMLInputElement;
    search.value = _highlight;
    search.addEventListener('input', ((Event _) {
      _highlight = search.value.trim().toUpperCase();
      redraw();
    }).toJS);
    final span = periodSpan[_period] ?? 3;
    replace(_controls, [
      h('div.seg', [
        for (final (k, label) in [('sp500', 'S&P 500'), ('all', 'All US (top 1,000)')])
          h('button.seg-btn${k == _universe ? '.on' : ''}', label, {'type': 'button'}, {'click': (_) => _set({'universe': k})}),
      ]),
      h('div.seg', [
        for (final p in ['1d', '1w', '1m', '3m', '6m', 'ytd', '1y'])
          h('button.seg-btn${p == _period ? '.on' : ''}', p.toUpperCase(), {'type': 'button'}, {'click': (_) => _set({'period': p})}),
      ]),
      if (_zoomSector != null)
        h('div.crumbs', [
          h('button.link', 'All sectors', {'type': 'button'}, {
            'click': (_) {
              _zoomSector = null;
              _renderControls();
              redraw();
            },
          }),
          icon('chev-right', size: 12),
          h('b', _zoomSector),
        ]),
      h('span.spacer'),
      h('div.search-mini', [icon('search', size: 14), search]),
      h('div.hm-legend', [
        for (final v in [-span, -span * 2 / 3, -span / 3, 0.0, span / 3, span * 2 / 3, span])
          h('span.hm-swatch', '${v > 0 ? '+' : ''}${v.toStringAsFixed(v.abs() < 10 ? 1 : 0)}%',
              {'style': 'background:${changeColor(v, span)}'}),
      ]),
    ]);
  }

  Future<void> _load({bool quiet = false}) async {
    final seq = ++_seq;
    if (!quiet) {
      _loading = true;
      _renderOverlay();
    }
    try {
      final d = await request('/api/heatmap?universe=$_universe&period=$_period&limit=${_universe == 'all' ? 1000 : 600}');
      if (seq != _seq) return;
      _data = d;
      _error = null;
      if (d['building'].b == true) {
        Timer(const Duration(seconds: 5), () {
          if (!root.isHidden) _load();
        });
      }
    } catch (e) {
      if (seq != _seq) return;
      _error = '$e';
    }
    _loading = false;
    _renderOverlay();
    redraw();
  }

  void _renderOverlay() {
    final building = _data?['building'].b == true;
    replace(_overlay, [
      if (_loading && _data == null) h('div.loading-block', [h('div.spinner'), h('div.muted', 'Loading heatmap…')]),
      if (_error != null) h('div.error-block', [icon('info', size: 18), h('div', _error)]),
      if (building)
        h('div.loading-block', [h('div.spinner'), h('div.muted', 'Building the market universe (first run takes about a minute)…')]),
    ]);
    _overlay.isHidden = _overlay.children.length == 0;
  }

  // ---------------------------------------------------------------- drawing

  void redraw() {
    final rect = _wrap.getBoundingClientRect();
    final w = rect.width, hgt = rect.height;
    if (w <= 0 || hgt <= 0) return;
    final dpr = window.devicePixelRatio;
    _canvas.width = (w * dpr).round();
    _canvas.height = (hgt * dpr).round();
    final ctx = _canvas.getContext('2d') as CanvasRenderingContext2D;
    ctx.setTransform(dpr.toJS, 0, 0, dpr, 0, 0);
    final styles = window.getComputedStyle(document.documentElement!);
    final bg = styles.getPropertyValue('--bg').trim();
    ctx.fillStyle = (bg.isEmpty ? '#070c17' : bg).toJS;
    ctx.fillRect(0, 0, w, hgt);
    _tiles.clear();
    _headers.clear();
    final d = _data;
    if (d == null) return;
    var sectors = d['sectors'].list;
    if (_zoomSector != null) sectors = sectors.where((s) => s['name'].s == _zoomSector).toList();
    if (sectors.isEmpty) return;
    final span = periodSpan[_period] ?? 3;
    final sRects = squarify([for (final s in sectors) s['market_cap'].d ?? 0], Rect(0, 0, w, hgt));
    for (var si = 0; si < sectors.length; si++) {
      final s = sectors[si];
      final sr = sRects[si];
      if (sr.w < 2 || sr.h < 2) continue;
      final name = s['name'].s ?? '';
      const headH = 17.0;
      final showHead = sr.h > 40 && sr.w > 40;
      _headers.add(_Header(Rect(sr.x, sr.y, sr.w, showHead ? headH : 0), name));
      ctx.fillStyle = '#0f1420'.toJS;
      ctx.fillRect(sr.x, sr.y, sr.w, sr.h);
      if (showHead) {
        ctx.save();
        ctx.beginPath();
        ctx.rect(sr.x, sr.y, sr.w, headH);
        ctx.clip();
        ctx.fillStyle = '#1b2232'.toJS;
        ctx.fillRect(sr.x, sr.y, sr.w, headH);
        ctx.font = '600 11px ui-sans-serif, system-ui, sans-serif';
        ctx.textBaseline = 'middle';
        ctx.textAlign = 'left';
        ctx.fillStyle = '#e5e7eb'.toJS;
        final v = s['value'].d;
        ctx.fillText('${name.toUpperCase()}${v != null ? '  ${signed(v)}%' : ''}', sr.x + 5, sr.y + headH / 2 + 0.5);
        ctx.restore();
      }
      final body = sr.inset(1, showHead ? headH : 1, 1, 1);
      final industries = s['industries'].list;
      final iRects = squarify([for (final i in industries) i['market_cap'].d ?? 0], body);
      for (var ii = 0; ii < industries.length; ii++) {
        final ind = industries[ii];
        final ir = iRects[ii];
        if (ir.w < 1 || ir.h < 1) continue;
        final stocks = ind['stocks'].list;
        final showInd = ir.h > 46 && ir.w > 70 && _zoomSector != null;
        final inner = ir.inset(1, showInd ? 13 : 1, 1, 1);
        if (showInd) {
          ctx.font = '10px ui-sans-serif, system-ui, sans-serif';
          ctx.fillStyle = '#9ca3af'.toJS;
          ctx.textBaseline = 'middle';
          ctx.textAlign = 'left';
          ctx.save();
          ctx.beginPath();
          ctx.rect(ir.x, ir.y, ir.w, 13);
          ctx.clip();
          ctx.fillText((ind['name'].s ?? '').toUpperCase(), ir.x + 3, ir.y + 7);
          ctx.restore();
        }
        final stRects = squarify([for (final st in stocks) st['market_cap'].d ?? 0], inner);
        for (var k = 0; k < stocks.length; k++) {
          final st = stocks[k];
          final r = stRects[k];
          if (r.w < 0.5 || r.h < 0.5) continue;
          final v = st['value'].d;
          final sym = st['symbol'].s ?? '';
          final highlighted = _highlight.isNotEmpty && sym.startsWith(_highlight);
          ctx.fillStyle = changeColor(v, span).toJS;
          ctx.fillRect(r.x + 0.5, r.y + 0.5, math.max(0, r.w - 1), math.max(0, r.h - 1));
          if (identical(_hover?.stock.v, st.v) || highlighted) {
            ctx.strokeStyle = (highlighted ? '#facc15' : '#ffffff').toJS;
            ctx.lineWidth = 2;
            ctx.strokeRect(r.x + 1, r.y + 1, math.max(0, r.w - 2), math.max(0, r.h - 2));
          }
          _label(ctx, r, sym, v);
          _tiles.add(_Tile(r, st, name, ind['name'].s ?? '', stocks));
        }
      }
    }
  }

  void _label(CanvasRenderingContext2D ctx, Rect r, String sym, double? v) {
    final size = math.min(math.min(r.w / (sym.length * 0.62 + 0.8), r.h / 2.4), 26.0);
    if (size < 7) return;
    ctx.save();
    ctx.beginPath();
    ctx.rect(r.x, r.y, r.w, r.h);
    ctx.clip();
    ctx.fillStyle = '#ffffff'.toJS;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.font = '600 ${size.toStringAsFixed(1)}px ui-sans-serif, system-ui, sans-serif';
    final cx = r.x + r.w / 2, cy = r.y + r.h / 2;
    final showVal = v != null && r.h > size * 2.4;
    ctx.fillText(sym, cx, showVal ? cy - size * 0.45 : cy);
    if (showVal) {
      ctx.font = '${(size * 0.72).toStringAsFixed(1)}px ui-sans-serif, system-ui, sans-serif';
      ctx.fillText('${signed(v)}%', cx, cy + size * 0.55);
    }
    ctx.restore();
  }

  (double, double) _local(MouseEvent e) {
    final r = _canvas.getBoundingClientRect();
    final o = e as JSObject;
    return ((o['clientX'] as JSNumber).toDartDouble - r.left, (o['clientY'] as JSNumber).toDartDouble - r.top);
  }

  void _onMove(MouseEvent e) {
    final (x, y) = _local(e);
    final hit = _tiles.where((t) => t.r.contains(x, y)).firstOrNull;
    final onHeader = _headers.any((hd) => hd.r.h > 0 && hd.r.contains(x, y));
    _canvas.style.cursor = hit != null || onHeader ? 'pointer' : 'default';
    if (!identical(hit, _hover)) {
      _hover = hit;
      redraw();
    }
    if (hit == null) {
      _tip.isHidden = true;
      return;
    }
    _renderTip(hit);
    final wr = _wrap.getBoundingClientRect();
    final tr = _tip.getBoundingClientRect();
    var left = x + 16, top = y + 16;
    if (left + tr.width > wr.width - 8) left = x - tr.width - 16;
    if (top + tr.height > wr.height - 8) top = math.max(8, wr.height - tr.height - 8);
    _tip.style
      ..left = '${left}px'
      ..top = '${top}px';
  }

  void _renderTip(_Tile t) {
    final st = t.stock;
    final sym = st['symbol'].s ?? '';
    final metric = _data?['metric'].s ?? 'change_pct';
    final peers = [...t.peers]..sort((a, b) => (b['market_cap'].d ?? 0).compareTo(a['market_cap'].d ?? 0));
    final span = periodSpan[_period] ?? 3;
    replace(_tip, [
      h('div.hm-tip-head', [
        h('b', sym),
        h('span.muted.small', st['name'].s ?? ''),
      ]),
      h('div.hm-tip-sub.muted.small', '${t.sector} · ${t.industry}'),
      h('div.hm-tip-price', [
        h('span.big', st['price'].d == null ? '' : fmtPrice(st['price'].d!)),
        h((st['change_pct'].d ?? 0) >= 0 ? 'span.up' : 'span.down', st['change_pct'].d == null ? '' : '${signed(st['change_pct'].d!)}% today'),
        if (metric != 'change_pct' && st['value'].d != null)
          h((st['value'].d ?? 0) >= 0 ? 'span.up' : 'span.down', '${signed(st['value'].d!)}% ${_period.toUpperCase()}'),
        h('span.muted.small', 'Cap ${compact(st['market_cap'].d)}'),
      ]),
      h('table.hm-peers', [
        for (final p in peers.take(10))
          h('tr${p['symbol'].s == sym ? '.me' : ''}', [
            h('td', p['symbol'].s),
            h('td.num', p['price'].d == null ? '' : fmtPrice(p['price'].d!)),
            h('td.num', h('span.hm-chip', p['value'].d == null ? '–' : '${signed(p['value'].d!)}%',
                {'style': 'background:${changeColor(p['value'].d, span)}'})),
          ]),
      ]),
    ]);
    _tip.isHidden = false;
  }

  void _onClick(MouseEvent e) {
    final (x, y) = _local(e);
    final header = _headers.where((hd) => hd.r.h > 0 && hd.r.contains(x, y)).firstOrNull;
    if (header != null) {
      _zoomSector = _zoomSector == null ? header.sector : null;
      _renderControls();
      redraw();
      return;
    }
    final t = _tiles.where((t) => t.r.contains(x, y)).firstOrNull;
    if (t != null) _open(t.stock['symbol'].s!);
  }

  void _open(String sym) {
    store.activeChart.symbol.value = sym;
    store.page.value = 'chart';
  }
}
