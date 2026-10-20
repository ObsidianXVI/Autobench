// The chat with one Claude Code session: thread (history from the transcript plus the reply streaming
// in over the event socket) and composer. Used by the Claude page and by the quick chat (⌘K), which
// talks to the agent session and attaches what the user has open in the app.
import 'package:web/web.dart';

import '../core/json.dart';
import 'api.dart';
import 'dom.dart';
import 'events.dart';
import 'kit/icons.dart';
import 'kit/overlay.dart';
import 'kit/state.dart';
import 'store.dart';

String encSession(String s) => Uri.encodeComponent(s);

/// /api/claude/status: whether `claude` is there, how it is signed in, and the agent session.
final claudeStatus = Obs<J>(const J(null));

Future<void> loadClaudeStatus() async {
  try {
    claudeStatus.value = await request('/api/claude/status');
  } on ApiError catch (e) {
    claudeStatus.value = J({'error': e.message});
  }
}

/// Why messages cannot be sent right now (null = they can).
String? claudeBlocker() {
  final st = claudeStatus.value;
  if (st.isNull) return null;
  if (st['error'].s != null) return st['error'].s;
  if (st['enabled'].b == false) return 'Claude chat is turned off (AUTOBENCH_CLAUDE_CHAT=0 in .env).';
  if (st['available'].b == false) {
    return 'The `claude` command was not found on this Mac. Install Claude Code, or set AUTOBENCH_CLAUDE_BIN in .env and restart.';
  }
  final auth = st['auth'];
  if (!auth.isNull && auth['ok'].b == false) {
    return auth['logged_in'].b == true
        ? 'Claude Code is signed in with ${auth['method'].s ?? 'an API key'}, not your Claude subscription. Run `claude`, then /login.'
        : 'Claude Code is not signed in. Run `claude` in a terminal and /login with your Claude subscription.';
  }
  return null;
}

/// A short label for an attached view, e.g. "Chart · NVDA · 1d".
String viewLabel(J v) {
  final page = v['page'].s ?? '';
  final name = page.isEmpty ? 'App' : '${page[0].toUpperCase()}${page.substring(1)}';
  final detail = switch (page) {
    'chart' => [v['symbol'].s, v['interval'].s].whereType<String>().join(' · '),
    'screener' => v['total'].i != null ? '${v['total'].i} results' : '',
    'heatmap' => [v['universe'].s, v['period'].s].whereType<String>().join(' · '),
    'portfolio' || 'trading' => [v['account'].s, v['tab'].s].whereType<String>().join(' · '),
    _ => v['tab'].s ?? '',
  };
  return detail.isEmpty ? name : '$name · $detail';
}

class ChatView {
  final HTMLElement thread = h('div.cc-thread', null, {'aria-live': 'polite'});
  final HTMLElement composer = h('div.cc-composer');
  late final HTMLTextAreaElement input;

  /// Quick chat: sends to the agent session (the server continues a copy when it is open elsewhere)
  /// and attaches [view]. Otherwise messages go to [session], confirming a copy when it is open elsewhere.
  final bool quick;
  final Map<String, Object?>? Function()? view;
  final void Function()? onRunEnded;

  J? session;
  List<J> _history = [];
  bool _truncated = false;
  int _seq = 0;
  String? _runId;
  String? _runText;
  J? _runView;
  final List<Map<String, Object?>> _parts = [];
  J? _lastResult;
  String? _runError;
  final Scope _scope = Scope();

  ChatView({this.quick = false, this.view, this.onRunEnded}) {
    input = h('textarea.input.cc-input', null, {
      'rows': quick ? '2' : '3',
      'placeholder': quick ? 'Ask Claude…  (Enter to send, Esc to close)' : 'Message Claude…  (Enter to send, Shift+Enter for a new line)',
      'aria-label': 'Message Claude',
    }, {
      'keydown': (e) {
        final k = e as KeyboardEvent;
        if (k.key == 'Enter' && !k.shiftKey && !k.isComposing) {
          k.preventDefault();
          send();
        }
      },
    }) as HTMLTextAreaElement;
    _scope.listen(claudeStatus, (_) => renderComposer());
  }

  String? get id => session?['id'].s;
  bool get busy => _runId != null || (session?['running'].b ?? false);

  /// Shows [s] (null = none chosen).
  void setSession(J? s) {
    final changed = s?['id'].s != id;
    session = s;
    if (changed) {
      _clearRun();
      _history = [];
      _truncated = false;
      renderThread();
      reload();
    }
    renderComposer();
  }

  void focus() => input.focus();

  Future<void> reload() async {
    final sid = id;
    final seq = ++_seq;
    if (sid == null) {
      renderThread();
      return;
    }
    try {
      final r = await request('/api/claude/sessions/${encSession(sid)}/messages?limit=${quick ? 24 : 80}');
      if (seq != _seq) return;
      _history = r['messages'].list;
      _truncated = r['truncated'].b ?? false;
    } on ApiError catch (e) {
      if (seq != _seq) return;
      _history = [];
      _runError = e.message;
    }
    renderThread(scroll: true);
  }

  void _clearRun() {
    _runId = null;
    _runText = null;
    _runView = null;
    _parts.clear();
    _lastResult = null;
    _runError = null;
  }

  void _startRun(String? runId, String text, J? v) {
    _clearRun();
    _runId = runId;
    _runText = text;
    _runView = v;
  }

  // ---------------------------------------------------------------- events

  void onEvent(ServerEvent e) {
    final d = e.data;
    final sid = d['session_id'].s;
    if (e.type == 'claude.run') {
      final forkedFrom = d['forked_from'].s;
      final mine = d['run_id'].s == _runId || (sid != null && sid == id) || (forkedFrom != null && forkedFrom == id);
      if (!mine) return;
      if (forkedFrom != null && forkedFrom == id && sid != null && sid != id) {
        // continuing a copy: follow it (the history is the same up to here)
        session = J({...(session!.v as Map), 'id': sid, 'live': null});
      }
      switch (d['state'].s) {
        case 'running':
          if (_runId != d['run_id'].s) _startRun(d['run_id'].s, d['text'].s ?? '', d['view'].isNull ? null : d['view']);
        case 'done' || 'error' || 'stopped':
          _lastResult = d['result'].isNull ? null : d['result'];
          _runError = d['state'].s == 'error' ? d['error'].s : (d['state'].s == 'stopped' ? 'Stopped' : null);
          _runId = null;
          if (session != null) session = J({...(session!.v as Map), 'running': false});
          reload().then((_) {
            _runText = null;
            _runView = null;
            _parts.clear();
            renderThread(scroll: true);
          });
          onRunEnded?.call();
      }
      renderThread(scroll: true);
      renderComposer();
    } else if (e.type == 'claude.event') {
      if (d['run_id'].s != _runId) return;
      final kind = d['kind'].s;
      if (kind == 'tool_error') {
        for (final p in _parts) {
          if (p['id'] == d['id'].s) p['error'] = d['text'].s;
        }
      } else {
        _parts.add({'kind': kind, 'text': d['text'].s, 'name': d['name'].s, 'summary': d['summary'].s, 'id': d['id'].s});
      }
      renderThread(scroll: true);
    }
  }

  // ---------------------------------------------------------------- actions

  bool get _share => store.pref<bool>('claude.shareView');

  Future<void> send() async {
    final text = input.value.trim();
    if (text.isEmpty || busy || claudeBlocker() != null) return;
    final s = session;
    final v = quick && _share ? view?.call() : null;
    try {
      final J run;
      if (quick) {
        run = await request('/api/claude/agent/messages', method: 'POST', body: {'text': text, 'view': v});
      } else {
        if (s == null) return;
        final live = !s['live'].isNull;
        if (live &&
            !await confirmDialog(
                'Continue a copy?',
                'This session is open in another Claude Code window, so the app will not write to it. '
                    'Your message starts a copy with the same history; the original stays as it is.',
                confirm: 'Continue a copy')) {
          return;
        }
        run = await request('/api/claude/sessions/${encSession(s['id'].s!)}/messages', method: 'POST', body: {
          'text': text,
          'fork': live,
        });
      }
      input.value = '';
      if (_runId != run['run_id'].s) _startRun(run['run_id'].s, text, v == null ? null : J(v));
      renderThread(scroll: true);
      renderComposer();
    } on ApiError catch (e) {
      toast(e.message, kind: 'error', title: 'Claude');
    }
  }

  Future<void> stop() async {
    final sid = id;
    if (sid == null) return;
    try {
      await request('/api/claude/sessions/${encSession(sid)}/stop', method: 'POST');
    } on ApiError catch (e) {
      toast(e.message, kind: 'error', title: 'Claude');
    }
  }

  // ---------------------------------------------------------------- rendering

  Object _tool(Map<String, Object?> p) => h('div.cc-tool${p['error'] != null ? '.err' : ''}', [
        h('div.cc-tool-line', [h('b', p['name'] as String? ?? 'Tool'), h('span.mono.muted', p['summary'] as String? ?? '')]),
        if (p['error'] != null) h('div.cc-tool-err', p['error'] as String),
      ]);

  Object _bot(List<Map<String, Object?>> parts, {bool working = false}) {
    final children = <Object>[];
    var tools = <Map<String, Object?>>[];
    void flush() {
      if (tools.isEmpty) return;
      final group = tools;
      children.add(group.length <= 3
          ? h('div.cc-tools', [for (final t in group) _tool(t)])
          : h('details.cc-tools', [
              h('summary.muted.small', '${group.length} tool calls · ${group.map((t) => t['name']).toSet().join(', ')}'),
              for (final t in group) _tool(t),
            ], {
              if (group.any((t) => t['error'] != null) || working) 'open': '',
            }));
      tools = [];
    }

    for (final p in parts) {
      if (p['kind'] == 'tool') {
        tools.add(p);
      } else if (p['kind'] == 'text') {
        flush();
        children.add(markdown(p['text'] as String? ?? ''));
      }
    }
    flush();
    if (working) children.add(h('div.cc-working.muted.small', [h('span.cc-spin'), 'Claude is working…']));
    return h('div.cc-msg.bot', children);
  }

  Object _user(String text, J? v) => h('div.cc-user', [
        if (v != null && !v.isNull) h('div.cc-view-chip', [icon('eye', size: 12), viewLabel(v)], {'data-tip': 'Sent with your view'}),
        h('div.cc-msg.user', text),
      ]);

  List<Map<String, Object?>> _partsOf(J m) => [
        for (final p in m['parts'].list)
          {'kind': p['kind'].s, 'text': p['text'].s, 'name': p['name'].s, 'summary': p['summary'].s, 'error': p['error'].s},
      ];

  void renderThread({bool scroll = false}) {
    final atBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 60;
    if (session == null) {
      replace(thread, h('div.empty-state${quick ? '' : '.tall'}', [
        icon('bot', size: 30),
        h('div', quick ? 'No agent session chosen' : 'Chat with Claude Code'),
        h('div.muted.small', quick
            ? 'On the Claude page, pick a session and choose "Use as agent".'
            : 'Pick a session on the left, or start a new one. Claude runs on this Mac with that session\'s '
                'history, folder and tools.'),
      ]));
      return;
    }
    final result = _lastResult;
    replace(thread, [
      if (_truncated) h('div.cc-note.muted.small', 'Older messages are not shown.'),
      for (final m in _history)
        switch (m['role'].s) {
          'user' => _user(m['text'].s ?? '', m['view'].isNull ? null : m['view']),
          'note' => h('div.cc-note.muted.small', m['text'].s),
          _ => _bot(_partsOf(m)),
        },
      if (_runText != null) _user(_runText!, _runView),
      if (_runId != null || _parts.isNotEmpty) _bot(_parts, working: _runId != null),
      if (_runId == null && _runError != null) h('div.cc-note.err', _runError),
      if (_runId == null && result != null && result['turns'].i != null)
        h('div.cc-note.muted.tiny', [
          '${result['turns'].i} turns',
          if (result['duration_ms'].i != null) ' · ${(result['duration_ms'].i! / 1000).toStringAsFixed(1)} s',
        ]),
    ]);
    if (scroll || atBottom) thread.scrollTop = thread.scrollHeight.toDouble();
  }

  void renderComposer() {
    final blocker = claudeBlocker();
    if (blocker != null) {
      replace(composer, h('div.cc-note.warn.small', blocker));
      return;
    }
    final s = session;
    final live = s != null && !s['live'].isNull;
    final v = quick ? view?.call() : null;
    input.disabled = s == null;
    final hadFocus = document.activeElement == input;
    replace(composer, [
      input,
      h('div.cc-composer-bar', [
        if (v != null)
          h('label.cc-share.small', [
            h('input', null, {'type': 'checkbox', if (_share) 'checked': ''}, {
              'change': (e) {
                store.setPref('claude.shareView', (e.target as HTMLInputElement).checked);
                renderComposer();
              },
            }),
            ' Share my view: ',
            h('b', viewLabel(J(v))),
          ], {'data-tip': 'Sends what is on screen (page, symbol, filters, results…) with your message'})
        else
          h('span.muted.tiny', 'Runs with --dangerously-skip-permissions'),
        h('span.spacer'),
        if (busy)
          h('button.btn.danger', [icon('pause', size: 14), 'Stop'], {'type': 'button'}, {'click': (_) => stop()})
        else
          h('button.btn.primary', live && !quick ? 'Send to a copy' : 'Send', {'type': 'button', if (s == null) 'disabled': ''},
              {'click': (_) => send()}),
      ]),
    ]);
    if (hadFocus) input.focus(); // re-attaching the field drops focus
  }

  void dispose() => _scope.dispose();
}

