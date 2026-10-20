// Claude page: chat with the Claude Code sessions on this Mac. The session list and history come from
// Claude Code's own files; a message runs that session (claude -p --resume) on the server, and its reply
// streams back over the event socket (claude.run / claude.event), so every open tab follows along.
import 'dart:async';

import 'package:web/web.dart';

import '../../core/json.dart';
import '../../core/time.dart';
import '../api.dart';
import '../dom.dart';
import '../events.dart';
import '../kit/icons.dart';
import '../kit/overlay.dart';
import '../store.dart';

const _modes = [
  ('default', 'Ask (deny prompts)', 'Uses the session\'s own permission settings. Anything that would ask is refused, '
      'since prompts cannot be answered from here.'),
  ('acceptEdits', 'Accept edits', 'File edits are allowed without asking; other prompts are refused.'),
  ('plan', 'Plan only', 'Claude plans and reads but changes nothing.'),
];

String _enc(String s) => Uri.encodeComponent(s);

class ClaudePage {
  final HTMLElement root = h('div.page.claude-page');
  final HTMLElement _list = h('div.cc-list');
  final HTMLElement _head = h('div.cc-head');
  final HTMLElement _thread = h('div.cc-thread', null, {'aria-live': 'polite'});
  final HTMLElement _composer = h('div.cc-composer');
  late final HTMLInputElement _search;
  late final HTMLTextAreaElement _input;

  J _status = const J(null);
  List<J> _sessions = [];
  List<J> _history = [];
  bool _truncated = false;
  String? _loadError;
  bool _shown = false;
  int _seq = 0;

  /// The reply being streamed: the run, what was asked, and the parts received so far.
  String? _runId;
  String? _runText;
  final List<Map<String, Object?>> _parts = [];
  J? _lastResult;
  String? _runError;

  String get _sel => store.pref<String>('claude.session');
  set _sel(String v) => store.setPref('claude.session', v);
  String get _mode => store.pref<String>('claude.mode');
  set _mode(String v) => store.setPref('claude.mode', v);

  J? get _current => _sessions.where((s) => s['id'].s == _sel).firstOrNull;
  bool get _busy => _runId != null || (_current?['running'].b ?? false);

  ClaudePage() {
    _search = h('input.input.cc-search', null, {'type': 'search', 'placeholder': 'Search sessions', 'aria-label': 'Search sessions'},
        {'input': (_) => _renderList()}) as HTMLInputElement;
    _input = h('textarea.input.cc-input', null, {
      'rows': '3',
      'placeholder': 'Message Claude…  (Enter to send, Shift+Enter for a new line)',
      'aria-label': 'Message',
    }, {
      'keydown': (e) {
        final k = e as KeyboardEvent;
        if (k.key == 'Enter' && !k.shiftKey && !k.isComposing) {
          k.preventDefault();
          _send();
        }
      },
    }) as HTMLTextAreaElement;
    append(root, [
      h('aside.cc-side', [
        h('div.cc-side-head', [
          _search,
          h('button.icon-btn', icon('refresh', size: 15), {'type': 'button', 'data-tip': 'Refresh', 'aria-label': 'Refresh sessions'},
              {'click': (_) => _loadSessions()}),
          h('button.btn.small.primary', [icon('plus', size: 14), 'New'], {'type': 'button', 'data-tip': 'Start a new session in a folder'},
              {'click': (_) => _newSession()}),
        ]),
        _list,
      ]),
      h('section.cc-main', [_head, _thread, _composer]),
    ]);
  }

  void onShow() {
    if (!_shown) {
      _shown = true;
      _loadStatus();
    }
    _loadSessions();
  }

  Future<void> _loadStatus() async {
    try {
      _status = await request('/api/claude/status');
    } on ApiError catch (e) {
      _loadError = e.message;
    }
    _renderComposer();
  }

  Future<void> _loadSessions() async {
    try {
      _sessions = (await request('/api/claude/sessions?limit=300'))['sessions'].list;
      _loadError = null;
    } on ApiError catch (e) {
      _loadError = e.message;
    }
    if (_sel.isEmpty && _sessions.isNotEmpty) _sel = _sessions.first['id'].s!;
    _renderList();
    _renderHead();
    if (_history.isEmpty) _loadHistory();
    _renderComposer();
  }

  Future<void> _loadHistory() async {
    final id = _sel;
    final seq = ++_seq;
    if (id.isEmpty) {
      _history = [];
      _renderThread();
      return;
    }
    try {
      final r = await request('/api/claude/sessions/${_enc(id)}/messages?limit=80');
      if (seq != _seq) return;
      _history = r['messages'].list;
      _truncated = r['truncated'].b ?? false;
    } on ApiError catch (e) {
      if (seq != _seq) return;
      _history = [];
      _loadError = e.message;
    }
    _renderThread(scroll: true);
  }

  void _select(String id) {
    if (id == _sel) return;
    _sel = id;
    _clearRun();
    _history = [];
    _renderList();
    _renderHead();
    _renderThread();
    _renderComposer();
    _loadHistory();
  }

  void _clearRun() {
    _runId = null;
    _runText = null;
    _parts.clear();
    _lastResult = null;
    _runError = null;
  }

  // ---------------------------------------------------------------- events

  void onEvent(ServerEvent e) {
    final d = e.data;
    final sid = d['session_id'].s;
    if (e.type == 'claude.run') {
      final forkedFrom = d['forked_from'].s;
      final mine = d['run_id'].s == _runId || sid == _sel || (forkedFrom != null && forkedFrom == _sel);
      if (!mine) {
        if (d['state'].s != 'running') _loadSessions();
        return;
      }
      if (forkedFrom == _sel && sid != null && sid != _sel) {
        // continuing a copy: follow the new session
        _sel = sid;
        toast('Continuing in a copy of the session (${sid.substring(0, 8)}). The original is untouched.', title: 'Claude');
      }
      switch (d['state'].s) {
        case 'running':
          if (_runId != d['run_id'].s) {
            _clearRun();
            _runId = d['run_id'].s;
            _runText = d['text'].s;
          }
        case 'done' || 'error' || 'stopped':
          _lastResult = d['result'].isNull ? null : d['result'];
          _runError = d['state'].s == 'error' ? d['error'].s : (d['state'].s == 'stopped' ? 'Stopped' : null);
          _runId = null;
          _loadSessions().then((_) => _loadHistory().then((_) {
                _runText = null;
                _parts.clear();
                _renderThread(scroll: true);
              }));
      }
      _renderList();
      _renderHead();
      _renderThread(scroll: true);
      _renderComposer();
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
      _renderThread(scroll: true);
    }
  }

  // ---------------------------------------------------------------- actions

  Future<void> _send() async {
    final text = _input.value.trim();
    final s = _current;
    if (text.isEmpty || s == null || _busy) return;
    final live = !s['live'].isNull;
    if (live &&
        !await confirmDialog(
            'Continue a copy?',
            'This session is open in another Claude Code window, so the app will not write to it. '
                'Your message starts a copy with the same history; the original stays as it is.',
            confirm: 'Continue a copy')) {
      return;
    }
    try {
      final run = await request('/api/claude/sessions/${_enc(s['id'].s!)}/messages', method: 'POST', body: {
        'text': text,
        'mode': _mode,
        'fork': live,
      });
      _input.value = '';
      if (_runId != run['run_id'].s) {
        _clearRun();
        _runId = run['run_id'].s;
        _runText = text;
      }
      _renderThread(scroll: true);
      _renderComposer();
    } on ApiError catch (e) {
      toast(e.message, kind: 'error', title: 'Claude');
    }
  }

  Future<void> _stop() async {
    try {
      await request('/api/claude/sessions/${_enc(_sel)}/stop', method: 'POST');
    } on ApiError catch (e) {
      toast(e.message, kind: 'error', title: 'Claude');
    }
  }

  void _newSession() {
    final folder = h('input.input', null, {'placeholder': '/Users/you/Projects/app', 'aria-label': 'Folder'}) as HTMLInputElement;
    folder.value = _current?['cwd'].s ?? '';
    final name = h('input.input', null, {'placeholder': 'Optional', 'aria-label': 'Name'}) as HTMLInputElement;
    final text = h('textarea.input', null, {'rows': '4', 'aria-label': 'First message'}) as HTMLTextAreaElement;
    Dialog.open('New Claude Code session', h('div.cc-new', [
      h('label.field-col', [h('span', 'Folder'), folder, h('span.muted.tiny', 'Claude works in this folder, as if started there.')]),
      h('label.field-col', [h('span', 'Name'), name]),
      h('label.field-col', [h('span', 'First message'), text]),
    ]), width: '520px', actions: [
      const DialogAction('Cancel'),
      DialogAction('Start', primary: true, onPressed: () async {
        try {
          final run = await request('/api/claude/sessions', method: 'POST', body: {
            'cwd': folder.value.trim(),
            'text': text.value,
            'name': name.value.trim(),
            'mode': _mode,
          });
          _sel = run['session_id'].s!;
          _clearRun();
          _runId = run['run_id'].s;
          _runText = text.value;
          _history = [];
          _renderHead();
          _renderThread();
          _renderComposer();
          Timer(const Duration(seconds: 2), _loadSessions);
          return true;
        } on ApiError catch (e) {
          toast(e.message, kind: 'error', title: 'Claude');
          return false;
        }
      }),
    ]);
  }

  // ---------------------------------------------------------------- rendering

  String _label(J s) => s['name'].s ?? s['title'].s ?? s['id'].s!.substring(0, 8);

  void _renderList() {
    final q = _search.value.trim().toLowerCase();
    final shown = [
      for (final s in _sessions)
        if (q.isEmpty || ['id', 'name', 'title', 'cwd'].any((k) => (s[k].s ?? '').toLowerCase().contains(q))) s,
    ];
    if (_loadError != null && _sessions.isEmpty) {
      replace(_list, h('div.empty-state', [icon('bot', size: 26), h('div', 'Could not load sessions'), h('div.muted.small', _loadError)]));
      return;
    }
    if (shown.isEmpty) {
      replace(_list, h('div.empty-state', [
        icon('bot', size: 26),
        h('div', q.isEmpty ? 'No Claude Code sessions' : 'No matches'),
        if (q.isEmpty) h('div.muted.small', 'Sessions you start with `claude` in a terminal appear here.'),
      ]));
      return;
    }
    replace(_list, [
      for (final s in shown)
        h('button.cc-row${s['id'].s == _sel ? '.on' : ''}', [
          h('div.cc-row-top', [
            h('b.cc-row-name', _label(s)),
            if (s['running'].b == true) h('span.cc-dot.run', null, {'data-tip': 'Claude is answering'}),
            if (s['running'].b != true && !s['live'].isNull) h('span.cc-dot.live', null, {'data-tip': 'Open in a terminal'}),
          ]),
          if (s['name'].s != null && s['title'].s != null) h('div.cc-row-title.muted.small', s['title'].s),
          h('div.muted.tiny', [s['project'].s ?? '', ' · ', ago(s['updated'].s ?? '')]),
        ], {'type': 'button', 'data-id': s['id'].s!, 'aria-current': s['id'].s == _sel ? 'true' : 'false'},
            {'click': (_) => _select(s['id'].s!)}),
    ]);
  }

  void _renderHead() {
    final s = _current;
    if (s == null) {
      replace(_head, null);
      return;
    }
    final id = s['id'].s!, cwd = s['cwd'].s ?? '';
    final live = s['live'];
    replace(_head, [
      h('div.cc-head-main', [
        h('h2', _label(s)),
        h('div.muted.small.cc-head-meta', [
          h('span.mono', cwd.length > 64 ? '…${cwd.substring(cwd.length - 63)}' : cwd, {'data-tip': cwd}),
          ' · ',
          h('button.link.mono', id.substring(0, 8), {'type': 'button', 'data-tip': 'Copy session id $id'},
              {'click': (_) => _copy(id, 'Session id copied')}),
          ' · ',
          h('button.link', 'Copy resume command', {'type': 'button', 'data-tip': 'cd … && claude --resume …'},
              {'click': (_) => _copy("cd '${cwd.replaceAll("'", r"'\''")}' && claude --resume $id", 'Command copied')}),
        ]),
      ]),
      if (!live.isNull)
        h('div.cc-banner', [
          icon('info', size: 14),
          h('span', 'Open in another Claude Code window (pid ${live['pid'].i}). Messages sent here continue a copy of it.'),
        ]),
    ]);
  }

  void _copy(String text, String done) {
    window.navigator.clipboard.writeText(text);
    toast(done, ms: 1800);
  }

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

  List<Map<String, Object?>> _partsOf(J m) => [
        for (final p in m['parts'].list)
          {'kind': p['kind'].s, 'text': p['text'].s, 'name': p['name'].s, 'summary': p['summary'].s, 'error': p['error'].s},
      ];

  void _renderThread({bool scroll = false}) {
    final atBottom = _thread.scrollHeight - _thread.scrollTop - _thread.clientHeight < 60;
    final s = _current;
    if (s == null) {
      replace(_thread, h('div.empty-state.tall', [
        icon('bot', size: 30),
        h('div', 'Chat with Claude Code'),
        h('div.muted.small', 'Pick a session on the left, or start a new one. Claude runs on this Mac with that session\'s '
            'history, folder and tools.'),
      ]));
      return;
    }
    final result = _lastResult;
    final denied = result == null ? const <String>[] : [for (final d in result['denied'].list) d.s ?? ''];
    replace(_thread, [
      if (_truncated) h('div.cc-note.muted.small', 'Older messages are not shown.'),
      for (final m in _history)
        switch (m['role'].s) {
          'user' => h('div.cc-msg.user', m['text'].s),
          'note' => h('div.cc-note.muted.small', m['text'].s),
          _ => _bot(_partsOf(m)),
        },
      if (_runText != null) h('div.cc-msg.user', _runText),
      if (_runId != null || _parts.isNotEmpty) _bot(_parts, working: _runId != null),
      if (_runId == null && _runError != null) h('div.cc-note.err', _runError),
      if (_runId == null && denied.isNotEmpty)
        h('div.cc-note.warn.small', [
          'Not allowed without asking: ${denied.toSet().join(', ')}. ',
          'Prompts cannot be answered from here; choose "Accept edits", allow the tool in your Claude settings, '
              'or continue in the terminal.',
        ]),
      if (_runId == null && result != null)
        h('div.cc-note.muted.tiny', [
          if (result['turns'].i != null) '${result['turns'].i} turns',
          if (result['duration_ms'].i != null) ' · ${(result['duration_ms'].i! / 1000).toStringAsFixed(1)} s',
        ]),
    ]);
    if (scroll || atBottom) _thread.scrollTop = _thread.scrollHeight.toDouble();
  }

  void _renderComposer() {
    final s = _current;
    final st = _status;
    if (!st.isNull && st['enabled'].b == false) {
      replace(_composer, h('div.cc-note.muted.small', 'Claude chat is turned off (AUTOBENCH_CLAUDE_CHAT=0 in .env).'));
      return;
    }
    if (!st.isNull && st['available'].b == false) {
      replace(_composer, h('div.cc-note.warn.small',
          'The `claude` command was not found on this Mac. Install Claude Code, or set AUTOBENCH_CLAUDE_BIN in .env and restart.'));
      return;
    }
    final busy = _busy;
    final live = s != null && !s['live'].isNull;
    final mode = _modes.firstWhere((m) => m.$1 == _mode, orElse: () => _modes.first);
    final select = h('select.input.cc-mode', [
      for (final (k, label, _) in _modes) h('option', label, {'value': k, if (k == mode.$1) 'selected': ''}),
    ], {'aria-label': 'Permissions', 'data-tip': mode.$3}, {
      'change': (e) {
        _mode = valueOf(e.target);
        _renderComposer();
      },
    });
    _input.disabled = s == null;
    replace(_composer, [
      _input,
      h('div.cc-composer-bar', [
        select,
        h('span.spacer'),
        if (busy)
          h('button.btn.danger', [icon('pause', size: 14), 'Stop'], {'type': 'button'}, {'click': (_) => _stop()})
        else
          h('button.btn.primary', live ? 'Send to a copy' : 'Send', {'type': 'button', if (s == null) 'disabled': ''},
              {'click': (_) => _send()}),
      ]),
    ]);
  }
}
