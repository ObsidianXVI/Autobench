// Financier mode (Mode → Financier): Financier's Flutter console, served by the backend at /financier/
// and framed here. The two talk by postMessage (same origin only):
//   Autobench → Financier  {source:'autobench', type:'theme', brightness}
//                          {source:'autobench', type:'navigate', destination, section}
//                          {source:'autobench', type:'refresh'}
//   Financier → Autobench  {source:'financier', type:'ready' | 'route', destination, section}
//                          {source:'financier', type:'openSymbol', symbol}
//                          {source:'financier', type:'openPage', page, tab}
import 'dart:js_interop';

import 'package:web/web.dart';

import '../core/json.dart';
import 'api.dart';
import 'dom.dart';
import 'kit/icons.dart';
import 'store.dart';

/// Financier's top-level pages (its AppDestination names) with their labels.
const financierPages = [
  ('overview', 'Overview'),
  ('dataPlane', 'Data Plane'),
  ('flowsPlane', 'Flows Plane'),
  ('controlPlane', 'Control Plane'),
  ('foresight', 'Foresight'),
  ('hindsight', 'Hindsight'),
  ('portfolio', 'Portfolio'),
];

bool get financierMode => store.pref<String>('mode') == 'financier';

/// Switches the Mode selector (and optionally opens a Financier page).
void setMode(String mode, {String? destination, int? section}) {
  if (destination != null) {
    store.setPref('financier.destination', destination);
    store.setPref('financier.section', section ?? 0);
  }
  if (store.pref<String>('mode') != mode) store.setPref('mode', mode);
}

class FinancierFrame {
  final HTMLElement root = h('div.page.fin-page');
  final void Function(String symbol) onOpenSymbol;
  final void Function(String page, String? tab) onOpenPage;
  HTMLIFrameElement? _frame;
  bool _ready = false;
  bool _checked = false;
  String? _theme;
  (String, int)? _pendingNav;
  String? _shown; // the page the frame last reported (or was sent to)

  FinancierFrame({required this.onOpenSymbol, required this.onOpenPage}) {
    window.addEventListener('message', ((MessageEvent e) => _onMessage(e)).toJS);
  }

  String get destination => store.pref<String>('financier.destination');
  int get _section => (store.pref<Object?>('financier.section') as num?)?.toInt() ?? 0;

  /// The Financier page on screen, for the quick chat with Claude.
  Map<String, Object?> agentContext() => {
        'mode': 'financier',
        'financier_page': destination,
        'financier_section': store.pref<Object?>('financier.section'),
      };

  /// Shown (Mode switched to Financier). Creates the frame on first use.
  Future<void> onShow({required bool dark}) async {
    _theme = dark ? 'dark' : 'light';
    if (_frame != null) {
      _post('theme', {'brightness': _theme});
      // asked for another page while hidden (a link elsewhere in Autobench)
      if (_ready && destination != _shown) _pendingNav = (destination, _section);
      _navigateIfWanted();
      return;
    }
    if (!_checked) {
      replace(root, h('div.loading-block', [h('div.spinner'), h('div.muted', 'Opening Financier…')]));
      final st = await attempt(request('/api/financier/status'));
      _checked = true;
      if (st != null && st['ui']['built'].v == false) {
        _checked = false; // look again next time (it may have been built meanwhile)
        replace(root, _notBuilt(st['ui']['build'].s ?? ''));
        return;
      }
    }
    final dest = destination;
    final frame = h('iframe.fin-frame', null, {
      'src': 'financier/?host=autobench&theme=$_theme',
      'title': 'Financier',
      'allow': 'clipboard-read; clipboard-write',
    }) as HTMLIFrameElement;
    _frame = frame;
    _ready = false;
    _pendingNav = (dest, _section);
    replace(root, frame);
  }

  /// Autobench's theme changed.
  void setTheme(bool dark) {
    _theme = dark ? 'dark' : 'light';
    _post('theme', {'brightness': _theme});
  }

  /// Opens a Financier page (from a link elsewhere in Autobench, or the agent).
  void navigate(String destination, {int section = 0}) {
    store.setPref('financier.destination', destination);
    store.setPref('financier.section', section);
    _pendingNav = (destination, section);
    _navigateIfWanted();
  }

  /// Financier re-reads its workspace (e.g. after Autobench imported a statement).
  void refresh() => _post('refresh', const {});

  void _navigateIfWanted() {
    final nav = _pendingNav;
    if (nav == null || !_ready) return;
    _pendingNav = null;
    _shown = nav.$1;
    _post('navigate', {'destination': nav.$1, 'section': nav.$2});
  }

  void _post(String type, Map<String, Object?> data) {
    final w = _frame?.contentWindow;
    if (w == null || !_ready) return;
    w.postMessage({'source': 'autobench', 'type': type, ...data}.jsify(), window.location.origin.toJS);
  }

  void _onMessage(MessageEvent e) {
    final frame = _frame;
    if (frame == null || e.origin != window.location.origin) return;
    // only our own frame (not another window or a nested frame)
    final src = e.source as JSAny?, win = frame.contentWindow as JSAny?;
    if (src == null || win == null || !src.strictEquals(win).toDart) return;
    final data = e.data.dartify();
    if (data is! Map || data['source'] != 'financier') return;
    final d = J(data);
    switch (d['type'].s) {
      case 'ready':
        _ready = true;
        _shown = d['destination'].s;
        _post('theme', {'brightness': _theme ?? 'dark'});
        _navigateIfWanted();
      case 'route':
        final dest = d['destination'].s;
        if (dest != null) {
          _shown = dest;
          store.setPref('financier.destination', dest);
        }
        final sec = d['section'].i;
        if (sec != null) store.setPref('financier.section', sec);
      case 'openSymbol':
        final sym = d['symbol'].s;
        if (sym != null && sym.isNotEmpty) onOpenSymbol(sym.toUpperCase());
      case 'openPage':
        final page = d['page'].s;
        if (page != null) onOpenPage(page, d['tab'].s);
    }
  }

  HTMLElement _notBuilt(String cmd) => h('div.empty-state.tall', [
        icon('wallet', size: 30),
        h('div', 'Financier is not built yet'),
        h('div.small', [
          'Autobench serves Financier\'s web build from ',
          h('code', 'tools/Financier/app/build/web'),
          ' (or AUTOBENCH_FINANCIER_DIR). Build it once:',
        ]),
        h('code.fin-cmd', cmd),
        h('button.btn', [icon('refresh', size: 14), 'Check again'], {'type': 'button'}, {
          'click': (_) => onShow(dark: _theme != 'light'),
        }),
      ]);
}
