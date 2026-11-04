// Claude page: the Claude Code sessions on this Mac (from Claude Code's own files) and a chat with the
// selected one. A session can be made the agent: the one the quick chat (⌘K, on any page) talks to.
import 'dart:async';

import 'package:web/web.dart';

import '../../core/json.dart';
import '../../core/time.dart';
import '../api.dart';
import '../claude_chat.dart';
import '../dom.dart';
import '../events.dart';
import '../kit/icons.dart';
import '../kit/overlay.dart';
import '../store.dart';

class ClaudePage {
  final HTMLElement root = h('div.page.claude-page');
  final HTMLElement _list = h('div.cc-list');
  final HTMLElement _head = h('div.cc-head');
  late final HTMLInputElement _search;
  late final ChatView _chat;

  List<J> _sessions = [];
  String? _loadError;

  String get _sel => store.pref<String>('claude.session');
  set _sel(String v) => store.setPref('claude.session', v);

  J? get _current => _sessions.where((s) => s['id'].s == _sel).firstOrNull;
  String? get _agent => claudeStatus.value['agent'].s;

  ClaudePage() {
    _chat = ChatView(onRunEnded: _loadSessions);
    _search = h('input.input.cc-search', null, {'type': 'search', 'placeholder': 'Search sessions', 'aria-label': 'Search sessions'},
        {'input': (_) => _renderList()}) as HTMLInputElement;
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
      h('section.cc-main', [_head, _chat.thread, _chat.composer]),
    ]);
    claudeStatus.listen((_) {
      _renderList();
      _renderHead();
    });
  }

  void onShow() {
    loadClaudeStatus();
    _loadSessions();
  }

  Future<void> _loadSessions() async {
    try {
      _sessions = (await request('/api/claude/sessions?limit=300'))['sessions'].list;
      _loadError = null;
    } on ApiError catch (e) {
      _loadError = e.message;
    }
    if (_sel.isEmpty && _sessions.isNotEmpty) _sel = _agent ?? _sessions.first['id'].s!;
    _renderList();
    _renderHead();
    _chat.setSession(_current);
  }

  void _select(String id) {
    if (id == _sel) return;
    _sel = id;
    _renderList();
    _renderHead();
    _chat.setSession(_current);
  }

  void onEvent(ServerEvent e) {
    if (e.type == 'claude.agent') {
      loadClaudeStatus();
      return;
    }
    _chat.onEvent(e);
    // the chat followed a copy of the session: select it in the list too
    final followed = _chat.id;
    if (followed != null && followed != _sel) {
      _sel = followed;
      _loadSessions();
    } else if (e.type == 'claude.run') {
      final running = e.data['state'].s == 'running';
      final sid = e.data['session_id'].s;
      if (sid != null && _sessions.any((s) => s['id'].s == sid && (s['running'].b ?? false) != running)) _loadSessions();
    }
  }

  Future<void> _setAgent(String? id) async {
    try {
      await request('/api/claude/agent', method: 'PUT', body: {'session': id});
      await loadClaudeStatus();
      if (id != null) toast('Press ⌘K (Ctrl+K) on any page to chat with it.', title: 'Agent session set', ms: 4000);
    } on ApiError catch (e) {
      toast(e.message, kind: 'error', title: 'Claude');
    }
  }

  void _newSession() {
    final folder = h('input.input', null, {'placeholder': '/Users/you/Projects/app', 'aria-label': 'Folder'}) as HTMLInputElement;
    folder.value = _current?['cwd'].s ?? '';
    final name = h('input.input', null, {'placeholder': 'Optional', 'aria-label': 'Name'}) as HTMLInputElement;
    final text = h('textarea.input', null, {'rows': '4', 'aria-label': 'First message'}) as HTMLTextAreaElement;
    final agent = h('input', null, {'type': 'checkbox'}) as HTMLInputElement;
    agent.checked = _agent == null;
    Dialog.open('New Claude Code session', h('div.cc-new', [
      h('label.field-col', [h('span', 'Folder'), folder, h('span.muted.tiny', 'Claude works in this folder, as if started there.')]),
      h('label.field-col', [h('span', 'Name'), name]),
      h('label.field-col', [h('span', 'First message'), text]),
      h('label.check-row', [agent, ' Use as the agent (quick chat, ⌘K)']),
    ]), width: '520px', actions: [
      const DialogAction('Cancel'),
      DialogAction('Start', primary: true, onPressed: () async {
        try {
          final run = await request('/api/claude/sessions', method: 'POST', body: {
            'cwd': folder.value.trim(),
            'text': text.value,
            'name': name.value.trim(),
            'agent': agent.checked,
          });
          final sid = run['session_id'].s!;
          _sel = sid;
          _chat.setSession(J({'id': sid, 'name': name.value.trim(), 'cwd': folder.value.trim(), 'running': true, 'live': null}));
          _chat.onEvent(ServerEvent('claude.run', J({...(run.v as Map), 'text': text.value})));
          if (agent.checked) loadClaudeStatus();
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
            if (s['id'].s == _agent) h('span.badge-agent', 'AGENT', {'data-tip': 'The quick chat (⌘K) talks to this session'}),
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
    final isAgent = id == _agent;
    replace(_head, [
      h('div.cc-head-row', [
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
        if (isAgent)
          h('button.btn.small.cc-agent-on', [icon('check', size: 14), 'Agent'], {
            'type': 'button',
            'data-tip': 'The quick chat (⌘K) talks to this session. Click to stop using it.',
          }, {'click': (_) => _setAgent(null)})
        else
          h('button.btn.small', [icon('bot', size: 14), 'Use as agent'], {
            'type': 'button',
            'data-tip': 'Make this the session the quick chat (⌘K, any page) talks to',
          }, {'click': (_) => _setAgent(id)}),
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
}
