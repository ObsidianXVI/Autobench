// Quick chat (⌘K / Ctrl+K on any page): a panel at the side for a few words with the agent session,
// sending what is on screen along with the message. Esc (or ⌘K again) puts it away; a reply keeps
// streaming while it is closed.
import 'dart:js_interop';

import 'package:web/web.dart';

import '../core/json.dart';
import 'api.dart';
import 'claude_chat.dart';
import 'dom.dart';
import 'events.dart';
import 'kit/icons.dart';
import 'kit/overlay.dart';
import 'store.dart';

class QuickChat {
  final Map<String, Object?> Function() view;
  late final HTMLElement _el;
  final HTMLElement _title = h('div.qc-title');
  late final ChatView _chat;
  HTMLElement? _returnFocus;

  QuickChat(this.view) {
    _chat = ChatView(quick: true, view: view);
    _el = h('section.qc', [
      h('header.qc-head', [
        icon('bot', size: 16),
        _title,
        h('span.spacer'),
        h('button.icon-btn', icon('external', size: 15), {'type': 'button', 'data-tip': 'Open on the Claude page', 'aria-label': 'Open on the Claude page'},
            {'click': (_) => _openPage()}),
        h('button.icon-btn', icon('x', size: 16), {'type': 'button', 'data-tip': 'Close (Esc)', 'aria-label': 'Close quick chat'},
            {'click': (_) => close()}),
      ]),
      _chat.thread,
      _chat.composer,
    ], {
      'role': 'dialog',
      'aria-label': 'Quick chat with Claude',
      'hidden': '',
    });
    document.body!.append(_el);
    // Esc closes it wherever focus is (a dialog opened over it closes first)
    document.addEventListener('keydown', ((KeyboardEvent e) {
      if (e.key == 'Escape' && isOpen && document.querySelector('.backdrop') == null) {
        e.preventDefault();
        e.stopPropagation();
        close();
      }
    }).toJS, true.toJS);
    store.page.listen((_) {
      if (isOpen) _chat.renderComposer();
    });
  }

  bool get isOpen => !_el.isHidden;

  void toggle() => isOpen ? close() : open();

  Future<void> open() async {
    final active = document.activeElement;
    _returnFocus = active != null && active.isA<HTMLElement>() ? active as HTMLElement : null;
    _el.isHidden = false;
    _chat.renderComposer();
    _chat.focus();
    await loadClaudeStatus();
    await _loadAgent();
    if (isOpen) _chat.focus();
  }

  void close() {
    _el.isHidden = true;
    _returnFocus?.focus();
    _returnFocus = null;
  }

  Future<void> _loadAgent() async {
    final id = claudeStatus.value['agent'].s;
    J? s;
    if (id != null) {
      try {
        s = await request('/api/claude/sessions/${encSession(id)}');
      } on ApiError {
        s = null;
      }
    }
    _renderTitle(s);
    _chat.setSession(s);
    if (s == null) _offerSessions();
  }

  void _renderTitle(J? s) {
    replace(_title, [
      h('b', 'Claude'),
      if (s != null) h('span.muted.small', ' · ${s['name'].s ?? s['title'].s ?? s['id'].s!.substring(0, 8)}'),
      if (s != null && !s['live'].isNull)
        h('span.muted.tiny', ' · open in a terminal: continuing a copy', {'data-tip': 'The original session is left as it is'}),
    ]);
  }

  /// No agent session yet: pick one of the recent sessions right here.
  Future<void> _offerSessions() async {
    List<J> recent;
    try {
      recent = (await request('/api/claude/sessions?limit=8'))['sessions'].list;
    } on ApiError {
      return;
    }
    if (_chat.session != null || recent.isEmpty) return;
    replace(_chat.thread, h('div.qc-pick', [
      h('div', [h('b', 'Which session should the agent be?'), h('div.muted.small', 'You can change it on the Claude page.')]),
      for (final s in recent)
        h('button.cc-row', [
          h('b.cc-row-name', s['name'].s ?? s['title'].s ?? s['id'].s!.substring(0, 8)),
          h('div.muted.tiny', s['project'].s ?? ''),
        ], {'type': 'button'}, {
          'click': (_) async {
            try {
              await request('/api/claude/agent', method: 'PUT', body: {'session': s['id'].s});
              await loadClaudeStatus();
              await _loadAgent();
              _chat.focus();
            } on ApiError catch (e) {
              toast(e.message, kind: 'error', title: 'Claude');
            }
          },
        }),
    ]));
  }

  void _openPage() {
    final id = _chat.id;
    if (id != null) store.setPref('claude.session', id);
    close();
    store.page.value = 'claude';
  }

  void onEvent(ServerEvent e) {
    if (e.type == 'claude.agent') {
      loadClaudeStatus().then((_) {
        // the chat already follows a copy it made; only reload when the agent changed otherwise
        if (claudeStatus.value['agent'].s != _chat.id) _loadAgent();
      });
      return;
    }
    _chat.onEvent(e);
  }
}
