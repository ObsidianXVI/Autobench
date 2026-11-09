// Claude page: the Claude Code sessions on this Mac (from Claude Code's own files) and a chat with the
// selected one. A session can be made the agent: the one the quick chat (⌘K, on any page) talks to.
import 'dart:async';
import 'dart:js_interop';

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
  late final _PromptEditor _prompt;
  final HTMLElement _chatBox = h('div.cc-chat-box');

  List<J> _sessions = [];
  String? _loadError;

  String get _sel => store.pref<String>('claude.session');
  set _sel(String v) => store.setPref('claude.session', v);

  J? get _current => _sessions.where((s) => s['id'].s == _sel).firstOrNull;
  String? get _agent => claudeStatus.value['agent'].s;

  ClaudePage() {
    _chat = ChatView(onRunEnded: _loadSessions);
    _prompt = _PromptEditor(onClose: () => _showPrompt(false));
    append(_chatBox, [_chat.thread, _chat.composer]);
    _search = h('input.input.cc-search', null, {'type': 'search', 'placeholder': 'Search sessions', 'aria-label': 'Search sessions'},
        {'input': (_) => _renderList()}) as HTMLInputElement;
    append(root, [
      h('aside.cc-side', [
        h('div.cc-side-head', [
          _search,
          h('button.icon-btn', icon('refresh', size: 15), {'type': 'button', 'data-tip': 'Refresh', 'aria-label': 'Refresh sessions'},
              {'click': (_) => _loadSessions()}),
          h('button.icon-btn.cc-prompt-btn', icon('edit', size: 15), {
            'type': 'button',
            'data-tip': 'System prompt: standing instructions for every run started here',
            'aria-label': 'System prompt',
          }, {'click': (_) => _showPrompt(_prompt.root.isHidden)}),
          h('button.btn.small.primary', [icon('plus', size: 14), 'New'], {'type': 'button', 'data-tip': 'Start a new session in a folder'},
              {'click': (_) => _newSession()}),
        ]),
        _list,
      ]),
      h('section.cc-main', [_head, _chatBox, _prompt.root]),
    ]);
    claudeStatus.listen((_) {
      _renderList();
      _renderHead();
    });
  }

  void onShow() {
    loadClaudeStatus();
    _loadSessions();
    if (store.pref<String>('claude.prompt') == 'open') _showPrompt(true);
  }

  /// Opens the system prompt editor in place of the chat (from the toolbar or a toast).
  void _showPrompt(bool open) {
    store.setPref('claude.prompt', open ? 'open' : '');
    _prompt.root.isHidden = !open;
    _chatBox.isHidden = open;
    root.querySelector('.cc-prompt-btn')?.classList.toggle('on', open);
    if (open) _prompt.load();
  }

  void openPrompt() => _showPrompt(true);

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
    if (e.type == 'claude.system_prompt') {
      _prompt.onChanged(e.data);
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


/// The system prompt: appended to Claude Code's own prompt on every run the app starts. The agent can rewrite it
/// (update_system_prompt), so each version is kept and can be restored.
class _PromptEditor {
  final void Function() onClose;
  final HTMLElement root = h('section.cc-prompt');
  final HTMLElement _meta = h('div.muted.small.cc-prompt-meta');
  final HTMLElement _notice = h('div');
  final HTMLElement _count = h('span.muted.tiny');
  final HTMLElement _history = h('div.cc-prompt-history');
  late final HTMLTextAreaElement _text;
  J _data = const J(null);
  String _saved = '';
  bool _loading = false;

  _PromptEditor({required this.onClose}) {
    root.isHidden = true;
    _text = h('textarea.input.cc-prompt-text', null, {
      'aria-label': 'System prompt',
      'spellcheck': 'false',
      'placeholder': 'e.g. Always size trades to my risk tiers. Answer briefly; lead with the recommendation…',
    }, {'input': (_) => _renderCount()}) as HTMLTextAreaElement;
    _text.addEventListener('keydown', ((KeyboardEvent e) {
      if (e.key == 's' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        _save();
      }
    }).toJS);
    append(root, [
      h('div.cc-prompt-head', [
        h('div', [
          h('h2', 'System prompt'),
          h('div.muted.small', 'Added to Claude Code\'s own prompt on every message sent from Autobench (this page and ⌘K). '
              'The agent can read and update it; each change is kept below.'),
        ]),
        h('span.spacer'),
        h('button.icon-btn', icon('x', size: 15), {'type': 'button', 'aria-label': 'Close system prompt', 'data-tip': 'Back to the chat'},
            {'click': (_) => onClose()}),
      ]),
      _notice,
      _meta,
      _text,
      h('div.row.gap.cc-prompt-actions', [
        _count,
        h('span.spacer'),
        h('button.btn.small', 'Discard changes', {'type': 'button', 'aria-label': 'Discard prompt changes'}, {
          'click': (_) {
            _text.value = _saved;
            _renderCount();
          },
        }),
        h('button.btn.small.primary', 'Save', {'type': 'button', 'aria-label': 'Save system prompt', 'data-tip': '⌘S'},
            {'click': (_) => _save()}),
      ]),
      _history,
    ]);
  }

  bool get _dirty => _text.value.trim() != _saved.trim();
  int get _max => _data['max_length'].i ?? 20000;

  Future<void> load({bool keepEdits = false}) async {
    if (_loading) return;
    _loading = true;
    if (!keepEdits) _text.disabled = true; // a load finishing after the user typed would wipe the text
    try {
      _data = await request('/api/claude/system-prompt');
    } on ApiError catch (e) {
      replace(_notice, h('div.check-msg.error', e.message));
      return;
    } finally {
      _loading = false;
      _text.disabled = false;
    }
    _saved = _data['text'].s ?? '';
    if (!keepEdits) _text.value = _saved;
    replace(_notice, null);
    _render();
  }

  /// A change saved elsewhere (the agent, another tab): reload unless the user has unsaved edits.
  void onChanged(J d) {
    if (root.isHidden) return;
    if (!_dirty) {
      load();
      return;
    }
    replace(_notice, h('div.check-msg.warn', [
      '${d['updated_by'].s == 'agent' ? 'The agent' : 'Someone'} saved a new version while you were editing. ',
      h('button.link', 'Load it (discards your edits)', {'type': 'button'}, {'click': (_) => load()}),
    ]));
    load(keepEdits: true);
  }

  Future<void> _save() async {
    if (_text.value.length > _max) {
      toast('The prompt is longer than $_max characters', kind: 'error');
      return;
    }
    try {
      _data = await request('/api/claude/system-prompt', method: 'PUT', body: {'text': _text.value});
      _saved = _data['text'].s ?? '';
      _text.value = _saved;
      replace(_notice, null);
      _render();
      toast('Used from the next message', title: 'System prompt saved', kind: 'success', ms: 2200);
    } on ApiError catch (e) {
      toast(e.message, kind: 'error', title: 'System prompt');
    }
  }

  String _by(String? by) => by == 'agent' ? 'the agent' : 'you';

  void _render() {
    final at = _data['updated_at'].s;
    final note = _data['note'].s;
    replace(_meta, at == null
        ? 'Not set yet.'
        : ['Saved ${ago(at)} by ${_by(_data['updated_by'].s)}', if (note != null) ' — “$note”']);
    _renderCount();
    final hist = _data['history'].list;
    replace(_history, hist.isEmpty
        ? null
        : [
            h('div.cc-prompt-history-title', 'Earlier versions'),
            for (final v in hist)
              h('div.cc-prompt-ver', [
                h('div.cc-prompt-ver-head', [
                  h('span.small', '${ago(v['at'].s ?? '')} · ${_by(v['by'].s)}'),
                  if (v['note'].s != null) h('span.muted.small.cc-prompt-ver-note', v['note'].s),
                  h('span.spacer'),
                  h('button.btn.small', 'Restore', {'type': 'button', 'data-tip': 'Put this version in the editor (then Save)'}, {
                    'click': (_) {
                      _text.value = v['text'].s ?? '';
                      _renderCount();
                      _text.focus();
                    },
                  }),
                ]),
                h('div.muted.tiny.cc-prompt-ver-text', _preview(v['text'].s ?? '')),
              ]),
          ]);
  }

  String _preview(String t) {
    final one = t.replaceAll(RegExp(r'\s+'), ' ').trim();
    return one.isEmpty ? '(empty)' : (one.length > 220 ? '${one.substring(0, 220)}…' : one);
  }

  void _renderCount() {
    final n = _text.value.length;
    _count.textContent = '$n / $_max${_dirty ? ' · unsaved' : ''}';
    _count.classList.toggle('error', n > _max);
  }
}
