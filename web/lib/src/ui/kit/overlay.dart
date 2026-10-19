// Floating UI: anchored menus/popovers, context menus, modal dialogs (incl. in-app confirm and
// prompt, never browser dialogs), toasts and tooltips. Everything lives in one overlay layer.
import 'dart:async';
import 'dart:js_interop';
import 'dart:math' as math;

import 'package:web/web.dart';

import '../dom.dart';
import 'icons.dart';

HTMLElement get _layer {
  final existing = document.getElementById('overlay-layer');
  if (existing != null) return existing as HTMLElement;
  final el = h('div.overlay-layer');
  el.id = 'overlay-layer';
  document.body!.append(el);
  return el;
}

// ------------------------------------------------------------------------------- positioning

/// Places [el] next to [anchor] (below, flipping above / clamping to the viewport).
void placeNear(HTMLElement el, {Element? anchor, double? x, double? y, String align = 'start', double gap = 4}) {
  el.style
    ..left = '0px'
    ..top = '0px';
  final vw = window.innerWidth.toDouble(), vh = window.innerHeight.toDouble();
  final r = el.getBoundingClientRect();
  double left, top;
  if (anchor != null) {
    final a = anchor.getBoundingClientRect();
    left = align == 'end' ? a.right - r.width : align == 'center' ? a.left + a.width / 2 - r.width / 2 : a.left;
    top = a.bottom + gap;
    if (top + r.height > vh - 8 && a.top - gap - r.height > 8) top = a.top - gap - r.height;
  } else {
    left = x ?? 0;
    top = y ?? 0;
    if (left + r.width > vw - 8) left = left - r.width;
    if (top + r.height > vh - 8) top = math.max(8, top - r.height);
  }
  left = left.clamp(8.0, math.max(8.0, vw - r.width - 8));
  top = top.clamp(8.0, math.max(8.0, vh - r.height - 8));
  el.style
    ..left = '${left}px'
    ..top = '${top}px';
}

// ------------------------------------------------------------------------------- popovers & menus

class Popover {
  final HTMLElement el;
  final List<void Function()> _closeHooks = [];
  bool _open = true;
  late final JSFunction _outside, _key, _resize;
  static final List<Popover> _stack = [];

  Popover._(this.el, void Function()? onClose) {
    if (onClose != null) _closeHooks.add(onClose);
    _outside = ((MouseEvent e) {
      final t = e.target;
      if (t != null && el.contains(t as Node)) return;
      // a click inside a child popover must not close its parent
      for (final p in _stack) {
        if (!identical(p, this) && p.el.contains(t as Node?)) return;
      }
      close();
    }).toJS;
    _key = ((KeyboardEvent e) {
      if (e.key == 'Escape' && identical(_stack.lastOrNull, this)) {
        e.stopPropagation();
        close();
      }
    }).toJS;
    _resize = ((Event _) => close()).toJS;
    Timer.run(() {
      if (!_open) return;
      document.addEventListener('mousedown', _outside);
      window.addEventListener('resize', _resize);
    });
    document.addEventListener('keydown', _key, true.toJS);
    _stack.add(this);
  }

  /// Opens [content] in a floating panel anchored to [anchor] (or at x/y).
  static Popover open(HTMLElement content,
      {Element? anchor, double? x, double? y, String align = 'start', String cls = '', void Function()? onClose}) {
    final el = h('div.popover${cls.isEmpty ? '' : '.$cls'}', content, {'role': 'dialog'});
    _layer.append(el);
    placeNear(el, anchor: anchor, x: x, y: y, align: align);
    return Popover._(el, onClose);
  }

  bool get isOpen => _open;

  void addCloseHook(void Function() fn) => _closeHooks.add(fn);

  void reposition({Element? anchor, double? x, double? y, String align = 'start'}) =>
      placeNear(el, anchor: anchor, x: x, y: y, align: align);

  void close() {
    if (!_open) return;
    _open = false;
    _stack.remove(this);
    document.removeEventListener('mousedown', _outside);
    document.removeEventListener('keydown', _key, true.toJS);
    window.removeEventListener('resize', _resize);
    el.remove();
    for (final hook in _closeHooks) {
      hook();
    }
  }

  static void closeAll() {
    for (final p in List.of(_stack)) {
      p.close();
    }
  }
}

class MenuItem {
  final String label;
  final String? iconName;
  final String? shortcut;
  final void Function()? onSelect;
  final bool checked;
  final bool disabled;
  final bool danger;
  final bool divider;
  final String? header;
  final List<MenuItem>? submenu;
  final String? hint;

  const MenuItem(this.label,
      {this.iconName, this.shortcut, this.onSelect, this.checked = false, this.disabled = false, this.danger = false,
      this.submenu, this.hint})
      : divider = false,
        header = null;
  const MenuItem.divider()
      : label = '',
        iconName = null,
        shortcut = null,
        onSelect = null,
        checked = false,
        disabled = true,
        danger = false,
        divider = true,
        header = null,
        submenu = null,
        hint = null;
  const MenuItem.header(String text)
      : label = '',
        iconName = null,
        shortcut = null,
        onSelect = null,
        checked = false,
        disabled = true,
        danger = false,
        divider = false,
        header = text,
        submenu = null,
        hint = null;
}

/// A keyboard-navigable menu. Returns the popover (already open).
Popover showMenu(List<MenuItem> items, {Element? anchor, double? x, double? y, String align = 'start', String cls = ''}) {
  late Popover pop;
  final rows = <HTMLElement>[];
  var active = -1;
  Popover? sub;
  var subOwner = -1;
  Timer? switchTimer; // hover intent: a pointer crossing other rows on its way into an open submenu keeps it

  void setActive(int i) {
    if (active >= 0 && active < rows.length) rows[active].classList.remove('active');
    active = i;
    if (i >= 0 && i < rows.length) {
      rows[i].classList.add('active');
      rows[i].scrollIntoView(ScrollIntoViewOptions(block: 'nearest'));
    }
  }

  final list = h('div.menu-items', null, {'role': 'menu'});
  for (final it in items) {
    if (it.divider) {
      list.append(h('div.menu-sep'));
      continue;
    }
    if (it.header != null) {
      list.append(h('div.menu-header', it.header));
      continue;
    }
    final row = h('div.menu-item${it.danger ? '.danger' : ''}${it.disabled ? '.disabled' : ''}', [
      h('span.menu-icon', it.checked ? icon('check', size: 15) : it.iconName != null ? icon(it.iconName!, size: 16) : null),
      h('span.menu-label', [it.label, if (it.hint != null) h('span.menu-hint', it.hint)]),
      if (it.shortcut != null) h('span.menu-shortcut', it.shortcut),
      if (it.submenu != null) h('span.menu-sub', icon('chev-right', size: 14)),
    ], {
      'role': 'menuitem',
      'tabindex': '-1'
    });
    final index = rows.length;
    rows.add(row);
    if (it.disabled) {
      list.append(row);
      continue;
    }
    void enter() {
      switchTimer?.cancel();
      setActive(index);
      sub?.close();
      sub = null;
      subOwner = -1;
      if (it.submenu != null) {
        final s = showMenu(it.submenu!, anchor: row, align: 'start', cls: 'submenu');
        s.el.addEventListener('mouseenter', ((Event _) {
          switchTimer?.cancel();
          setActive(index);
        }).toJS);
        sub = s;
        subOwner = index;
      }
    }

    row.addEventListener('mouseenter', ((Event _) {
      switchTimer?.cancel();
      if (subOwner == index && sub != null && sub!.isOpen) return;
      if (sub != null && sub!.isOpen) {
        switchTimer = Timer(const Duration(milliseconds: 300), enter);
      } else {
        enter();
      }
    }).toJS);
    row.addEventListener('click', ((MouseEvent e) {
      e.stopPropagation();
      if (it.submenu != null) return;
      Popover.closeAll();
      it.onSelect?.call();
    }).toJS);
    list.append(row);
  }
  pop = Popover.open(h('div.menu', list), anchor: anchor, x: x, y: y, align: align, cls: 'menu-pop $cls'.trim());
  if (cls == 'submenu' && anchor != null) {
    final a = anchor.getBoundingClientRect();
    final r = pop.el.getBoundingClientRect();
    final left = a.right + r.width > window.innerWidth ? a.left - r.width : a.right;
    pop.el.style
      ..left = '${left}px'
      ..top = '${math.min(a.top - 4, window.innerHeight - r.height - 8)}px';
  }
  final keys = ((KeyboardEvent e) {
    if (!pop.isOpen || sub != null && sub!.isOpen) return;
    final enabled = [for (var i = 0; i < items.where((x) => !x.divider && x.header == null).length; i++) i];
    if (e.key == 'ArrowDown' || e.key == 'ArrowUp') {
      e.preventDefault();
      if (enabled.isEmpty) return;
      var next = active;
      for (var k = 0; k < rows.length; k++) {
        next = (next + (e.key == 'ArrowDown' ? 1 : -1)) % rows.length;
        if (next < 0) next += rows.length;
        if (!rows[next].classList.contains('disabled')) break;
      }
      setActive(next);
    } else if (e.key == 'Enter' && active >= 0) {
      e.preventDefault();
      rows[active].click();
    }
  }).toJS;
  document.addEventListener('keydown', keys);
  pop.addCloseHook(() {
    switchTimer?.cancel();
    document.removeEventListener('keydown', keys);
    sub?.close();
  });
  return pop;
}

// ------------------------------------------------------------------------------- dialogs

class DialogAction {
  final String label;
  final bool primary, danger;
  /// Return false to keep the dialog open (e.g. validation failed).
  final FutureOr<bool> Function()? onPressed;
  const DialogAction(this.label, {this.primary = false, this.danger = false, this.onPressed});
}

class Dialog {
  final HTMLElement backdrop;
  final HTMLElement panel;
  final void Function()? onClose;
  late final JSFunction _key;
  bool _open = true;

  Dialog._(this.backdrop, this.panel, this.onClose) {
    _key = ((KeyboardEvent e) {
      if (e.key == 'Escape') {
        e.stopPropagation();
        close();
      }
    }).toJS;
    document.addEventListener('keydown', _key, true.toJS);
  }

  static Dialog open(String title, Object? body,
      {List<DialogAction> actions = const [], String width = '420px', void Function()? onClose, String cls = ''}) {
    late Dialog d;
    final footer = actions.isEmpty
        ? null
        : h('footer.dialog-actions', [
            for (final a in actions)
              h('button.btn${a.primary ? '.primary' : ''}${a.danger ? '.danger' : ''}', a.label, {'type': 'button'}, {
                'click': (_) async {
                  final keep = a.onPressed == null ? true : await a.onPressed!();
                  if (keep) d.close();
                },
              }),
          ]);
    final panel = h('div.dialog${cls.isEmpty ? '' : '.$cls'}', [
      h('header.dialog-head', [
        h('h3', title),
        h('button.icon-btn', icon('x', size: 16), {'aria-label': 'Close', 'type': 'button'}, {'click': (_) => d.close()}),
      ]),
      h('div.dialog-body', body),
      footer,
    ], {
      'role': 'dialog',
      'aria-modal': 'true',
      'aria-label': title
    });
    panel.style.width = width;
    final backdrop = h('div.backdrop', panel, null, {
      'mousedown': (e) {
        if (identical(e.target, e.currentTarget)) d.close();
      },
    });
    _layer.append(backdrop);
    d = Dialog._(backdrop, panel, onClose);
    Timer.run(() {
      final focus = panel.querySelector('[autofocus], input, select, textarea, button.primary') as HTMLElement?;
      focus?.focus();
    });
    return d;
  }

  void close() {
    if (!_open) return;
    _open = false;
    document.removeEventListener('keydown', _key, true.toJS);
    backdrop.remove();
    onClose?.call();
  }
}

/// In-app confirmation (never a browser `confirm`).
Future<bool> confirmDialog(String title, String message, {String confirm = 'Confirm', bool danger = false}) {
  final c = Completer<bool>();
  Dialog.open(title, h('p.dialog-text', message), actions: [
    DialogAction('Cancel', onPressed: () {
      c.complete(false);
      return true;
    }),
    DialogAction(confirm, primary: !danger, danger: danger, onPressed: () {
      c.complete(true);
      return true;
    }),
  ], onClose: () {
    if (!c.isCompleted) c.complete(false);
  });
  return c.future;
}

/// In-app text prompt. Resolves to null when cancelled.
Future<String?> promptDialog(String title, {String label = '', String initial = '', String confirm = 'OK',
    String? placeholder}) {
  final c = Completer<String?>();
  final input = h('input.input', null, {'value': initial, 'autofocus': '', 'placeholder': placeholder ?? ''})
      as HTMLInputElement;
  input.value = initial;
  late Dialog d;
  void submit() {
    if (!c.isCompleted) c.complete(input.value.trim());
    d.close();
  }

  input.addEventListener('keydown', ((KeyboardEvent e) {
    if (e.key == 'Enter') submit();
  }).toJS);
  d = Dialog.open(title, h('label.field-col', [if (label.isNotEmpty) h('span', label), input]), actions: [
    DialogAction('Cancel'),
    DialogAction(confirm, primary: true, onPressed: () {
      if (!c.isCompleted) c.complete(input.value.trim());
      return true;
    }),
  ], onClose: () {
    if (!c.isCompleted) c.complete(null);
  });
  Timer.run(() => input.select());
  return c.future;
}

// ------------------------------------------------------------------------------- toasts

HTMLElement get _toasts {
  final existing = document.getElementById('toast-stack');
  if (existing != null) return existing as HTMLElement;
  final el = h('div.toast-stack', null, {'role': 'status', 'aria-live': 'polite'});
  el.id = 'toast-stack';
  document.body!.append(el);
  return el;
}

/// Shows a toast. [kind]: info | success | warn | error | alert. Returns a function that closes it.
void Function() toast(String message,
    {String kind = 'info', String? title, String? actionLabel, void Function()? onAction, int ms = 5000}) {
  late HTMLElement el;
  Timer? timer;
  void close() {
    timer?.cancel();
    el.classList.add('leaving');
    Timer(const Duration(milliseconds: 180), () => el.remove());
  }

  el = h('div.toast.$kind', [
    h('span.toast-icon', icon(kind == 'success' ? 'check' : kind == 'error' ? 'x' : kind == 'alert' ? 'alarm' : 'info',
        size: 16)),
    h('div.toast-body', [if (title != null) h('b', title), h('div', message)]),
    if (actionLabel != null)
      h('button.btn.small', actionLabel, {'type': 'button'}, {
        'click': (_) {
          onAction?.call();
          close();
        },
      }),
    h('button.icon-btn.small', icon('x', size: 14), {'aria-label': 'Dismiss', 'type': 'button'}, {'click': (_) => close()}),
  ]);
  _toasts.append(el);
  while (_toasts.children.length > 5) {
    _toasts.children.item(0)?.remove();
  }
  if (ms > 0) timer = Timer(Duration(milliseconds: ms), close);
  return close;
}

// ------------------------------------------------------------------------------- tooltips

HTMLElement? _tip;
Timer? _tipTimer;

/// Installs the global tooltip handler: any element with data-tip="…" shows a styled tooltip
/// (data-tip-pos="right|top|bottom|left").
void installTooltips() {
  document.addEventListener('mouseover', ((MouseEvent e) {
    final t = e.target;
    if (t == null || !(t as JSObject).isA<Element>()) return;
    final el = (t as Element).closest('[data-tip]');
    if (el == null) return;
    _tipTimer?.cancel();
    _tipTimer = Timer(const Duration(milliseconds: 350), () => _showTip(el));
  }).toJS);
  document.addEventListener('mouseout', ((MouseEvent e) {
    final t = e.target;
    if (t == null || !(t as JSObject).isA<Element>()) return;
    if ((t as Element).closest('[data-tip]') == null) return;
    _tipTimer?.cancel();
    _tip?.remove();
    _tip = null;
  }).toJS);
  document.addEventListener('mousedown', ((Event _) {
    _tipTimer?.cancel();
    _tip?.remove();
    _tip = null;
  }).toJS);
}

void _showTip(Element el) {
  if (!el.isConnected) return;
  _tip?.remove();
  final text = el.getAttribute('data-tip') ?? '';
  if (text.isEmpty) return;
  final tip = h('div.tooltip', text);
  _layer.append(tip);
  final a = el.getBoundingClientRect();
  final r = tip.getBoundingClientRect();
  final pos = el.getAttribute('data-tip-pos') ?? 'bottom';
  double left, top;
  switch (pos) {
    case 'right':
      left = a.right + 8;
      top = a.top + a.height / 2 - r.height / 2;
    case 'left':
      left = a.left - r.width - 8;
      top = a.top + a.height / 2 - r.height / 2;
    case 'top':
      left = a.left + a.width / 2 - r.width / 2;
      top = a.top - r.height - 6;
    default:
      left = a.left + a.width / 2 - r.width / 2;
      top = a.bottom + 6;
  }
  tip.style
    ..left = '${left.clamp(4.0, math.max(4.0, window.innerWidth - r.width - 4))}px'
    ..top = '${top.clamp(4.0, math.max(4.0, window.innerHeight - r.height - 4))}px';
  _tip = tip;
}
