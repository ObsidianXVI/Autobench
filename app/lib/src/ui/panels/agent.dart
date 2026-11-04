// Agent panel (live activity, track record, saved analyses with graded outcomes), notes panel and
// the drawings object tree.
import 'dart:js_interop';

import 'package:web/web.dart';

import '../../core/format.dart';
import '../../core/json.dart';
import '../../core/models.dart';
import '../../core/time.dart';
import '../api.dart';
import '../dom.dart';
import '../kit/overlay.dart';
import '../kit/state.dart';
import '../store.dart';

const _actionLabel = {
  'strong_buy': 'Strong buy', 'buy': 'Buy', 'hold': 'Hold', 'watch': 'Watch', 'sell': 'Sell', 'strong_sell': 'Strong sell',
};

HTMLElement scoreBar(String label, double? value) {
  final v = value ?? 0;
  return h('div.score', [
    h('span.muted.small', label),
    h('div.score-bar', [
      h('div.score-mid'),
      h(v >= 0 ? 'div.score-fill.pos' : 'div.score-fill.neg', null,
          {'style': v >= 0 ? 'left:50%;width:${v / 2}%' : 'left:${50 + v / 2}%;width:${v.abs() / 2}%'}),
    ]),
    h('span.small', value == null ? '–' : value.toStringAsFixed(0)),
  ]);
}

/// Agent activity and recorded recommendations.
class AgentPanel {
  final HTMLElement root = h('div.panel');
  final HTMLElement _body = h('div.panel-scroll');
  final Scope _scope = Scope();
  bool _all = false;
  List<J> _items = const [];
  J? _record;
  final Set<String> _open = {};
  final Map<String, J> _full = {};
  int _seq = 0;

  AgentPanel() {
    append(root, [h('div.panel-head', [h('span.panel-title.static', ['✦ ', 'Agent'])]), _body]);
    _scope.listen(store.activity, (_) => _render());
    _scope.listen(store.analysesVersion, (_) => refresh());
    for (final c in store.charts) {
      _scope.listen(c.symbol, (_) => refresh());
    }
    _scope.listen(store.active, (_) => refresh());
    refresh();
  }

  void dispose() => _scope.dispose();

  String get symbol => store.activeChart.symbol.value;

  Future<void> refresh() async {
    final seq = ++_seq;
    try {
      final items = await api.analyses(symbol: _all ? null : symbol);
      final rec = await api.trackRecord();
      if (seq != _seq) return;
      _items = items;
      _record = rec['summary'];
    } catch (_) {}
    _render();
  }

  void _render() {
    final rec = _record;
    final checkbox = h('input', null, {'type': 'checkbox'}) as HTMLInputElement;
    checkbox.checked = _all;
    checkbox.addEventListener('change', ((Event _) {
      _all = checkbox.checked;
      refresh();
    }).toJS);
    replace(_body, [
      h('section', [
        h('h4', 'Live activity'),
        if (store.activity.value.isEmpty)
          h('div.muted.small', [
            'Agent actions (view changes, drawings, analyses, alerts) appear here live. Connect an MCP client to ',
            h('code', '/mcp'),
            ' and ask it to research a stock.',
          ])
        else
          h('ul.activity', [
            for (final a in store.activity.value.take(20))
              h('li.${a.kind}', [h('span.muted.tiny', clockTime(a.at)), ' ', a.text], null, {
                'click': (_) {
                  if (a.symbol != null) store.activeChart.symbol.value = a.symbol!;
                },
              }),
          ]),
      ]),
      if (rec != null && (rec['total'].i ?? 0) > 0)
        h('section', [
          h('h4', 'Track record'),
          h('div.kpis', [
            _kpi('Calls', '${rec['total'].i}', '${rec['resolved'].i ?? 0} resolved'),
            _kpi('Hit rate', rec['hit_rate_pct'].isNull ? '–' : '${rec['hit_rate_pct'].s}%', ''),
            _kpi('Avg R', rec['avg_r_multiple'].s ?? '–', ''),
            _kpi('α vs SPY', pct(rec['avg_alpha_vs_spy_pct'].d), ''),
          ]),
        ]),
      h('section', [
        h('div.title-row', [h('h4', 'Analyses'), h('label.small.muted', [checkbox, ' all symbols'])]),
        if (_items.isEmpty)
          h('div.muted.small', 'No saved analyses${_all ? '' : ' for $symbol'}. Ask the agent: “research $symbol for a swing trade”.'),
        for (final a in _items) _card(a),
      ]),
    ]);
  }

  HTMLElement _kpi(String label, String value, String sub) =>
      h('div.kpi', [h('span.muted.tiny', label), h('b', value), if (sub.isNotEmpty) h('span.muted.tiny', sub)]);

  HTMLElement _card(J a) {
    final id = a['id'].s ?? '';
    final open = _open.contains(id);
    final action = a['action'].s ?? '';
    final dir = action.contains('buy') ? 'up' : action.contains('sell') ? 'down' : 'neutral';
    final full = _full[id];
    final ev = full?['evaluation'];
    final targets = a['targets'].list;
    return h('article.card', [
      h('header', [
        h('span.badge.$dir', _actionLabel[action] ?? action),
        h('b', a['symbol'].s),
        h('span.muted.small', '${a['horizon'].s ?? ''} · ${((a['confidence'].d ?? 0) * 100).toStringAsFixed(0)}%'),
        h('span.spacer'),
        h('span.muted.tiny', ago(a['created_at'].s ?? '')),
      ], null, {
        'click': (_) {
          if (!_open.remove(id)) {
            _open.add(id);
            if (full == null) {
              api.analysis(id).then((r) {
                _full[id] = r;
                _render();
              }, onError: (Object _) {});
            }
          }
          _render();
        },
      }),
      h('p.small', a['summary'].s ?? ''),
      h('div.plan.small', [
        h('span', '@ ${n(a['price_at_creation'].d)}'),
        if (a['entry_low'].truthy || a['entry_high'].truthy) h('span', 'entry ${n(a['entry_low'].d)}–${n(a['entry_high'].d)}'),
        if (a['stop'].truthy) h('span.down', 'stop ${n(a['stop'].d)}'),
        if (targets.isNotEmpty) h('span.up', 'targets ${targets.map((t) => n(t.d)).join(' / ')}'),
      ]),
      if (open)
        h('div.card-detail', [
          if (ev != null && !ev.isNull)
            h(ev['success'].b == true ? 'div.eval.up' : ev['success'].b == false ? 'div.eval.down' : 'div.eval', [
              h('b', (ev['status'].s ?? '').replaceAll('_', ' ')),
              if (!ev['return_pct'].isNull) h('span', ' · ${pct(ev['return_pct'].d)} since'),
              if (!ev['r_multiple'].isNull) h('span', ' · ${ev['r_multiple'].s}R'),
              if (!ev['alpha_vs_spy_pct'].isNull) h('span', ' · α ${pct(ev['alpha_vs_spy_pct'].d)}'),
            ]),
          if (a['scores'].map.isNotEmpty) h('div.scores', [for (final e in a['scores'].map.entries) scoreBar(e.key, e.value.d)]),
          if (a['invalidation'].truthy) h('p.small', [h('b', 'Invalidation: '), a['invalidation'].s]),
          if (a['risks'].list.isNotEmpty) h('p.small', [h('b', 'Risks: '), a['risks'].list.map((r) => r.s).join('; ')]),
          if (a['catalysts'].list.isNotEmpty) h('p.small', [h('b', 'Catalysts: '), a['catalysts'].list.map((r) => r.s).join('; ')]),
          h('div.row.gap', [
            h('button.btn.small', 'Open chart', {'type': 'button'}, {
              'click': (_) {
                store.activeChart.symbol.value = a['symbol'].s!;
                store.page.value = 'chart';
              },
            }),
            if (a['annotation_tag'].truthy)
              h('button.btn.small', 'Drawings: ${a['annotation_tag'].s}', {'type': 'button'}, {
                'click': (_) {
                  store.activeChart.symbol.value = a['symbol'].s!;
                  store.panel.value = 'layers';
                },
              }),
          ]),
          if (full != null && full['rationale'].truthy)
            markdown(full['rationale'].s!)
          else
            h('div.muted.small', full == null ? 'Loading rationale…' : 'No rationale saved.'),
        ]),
    ]);
  }
}

/// Research notes for the active symbol (markdown).
class NotesPanel {
  final HTMLElement root = h('div.panel');
  final HTMLElement _list = h('div.panel-scroll');
  final Scope _scope = Scope();
  List<Note> _notes = const [];
  String? _openId;
  late final HTMLInputElement _title;
  late final HTMLTextAreaElement _content;
  late final HTMLElement _form;
  int _seq = 0;

  NotesPanel() {
    _title = h('input.input', null, {'aria-label': 'Note title'}) as HTMLInputElement;
    _content = h('textarea.input', null, {'placeholder': 'Markdown…', 'rows': '5', 'aria-label': 'Note content'})
        as HTMLTextAreaElement;
    _form = h('form.note-form', [
      _title,
      _content,
      h('button.btn.primary.small', 'Save note', {'type': 'submit'}),
    ], null, {
      'submit': (e) async {
        e.preventDefault();
        final title = _title.value.trim();
        if (title.isEmpty) return;
        await api.createNote(title, _content.value, store.activeChart.symbol.value);
        _title.value = '';
        _content.value = '';
        _sync();
        refresh();
      },
    });
    _title.addEventListener('input', ((Event _) => _sync()).toJS);
    append(root, [h('div.panel-head', [h('span.panel-title.static', 'Notes')]), h('div.pad', _form), _list]);
    _sync();
    _scope.listen(store.notesVersion, (_) => refresh());
    for (final c in store.charts) {
      _scope.listen(c.symbol, (_) => refresh());
    }
    _scope.listen(store.active, (_) => refresh());
    refresh();
  }

  void dispose() => _scope.dispose();

  void _sync() {
    final show = _title.value.isNotEmpty;
    for (final el in qsa(_form, 'textarea, button')) {
      el.isHidden = !show;
    }
  }

  Future<void> refresh() async {
    final sym = store.activeChart.symbol.value;
    _title.placeholder = 'New note on $sym…';
    final seq = ++_seq;
    try {
      final r = await api.notes(sym);
      if (seq != _seq) return;
      _notes = r;
    } catch (_) {}
    _render();
  }

  void _render() {
    replace(_list, [
      if (_notes.isEmpty) h('div.muted.small.pad', 'No notes for ${store.activeChart.symbol.value}. The agent stores theses, key dates and lessons here.'),
      for (final nt in _notes)
        h('article.card', [
          h('header', [
            h('span.author.${nt.author}', nt.author == 'agent' ? '✦ AI' : 'You'),
            h('b.ellipsis', nt.title),
            h('span.spacer'),
            h('span.muted.tiny', ago(nt.updatedAt)),
          ], null, {
            'click': (_) {
              _openId = _openId == nt.id ? null : nt.id;
              _render();
            },
          }),
          if (nt.tags.isNotEmpty) h('div.chips', [for (final t in nt.tags) h('span.chip.small', t)]),
          if (_openId == nt.id) ...[
            markdown(nt.content),
            h('button.btn.small', 'Delete', {'type': 'button'}, {
              'click': (_) async {
                if (!await confirmDialog('Delete note', 'Delete "${nt.title}"?', confirm: 'Delete', danger: true)) return;
                await api.deleteNote(nt.id);
                refresh();
              },
            }),
          ],
        ]),
    ]);
  }
}
