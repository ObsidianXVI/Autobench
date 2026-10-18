import 'dart:js_interop';

import 'package:web/web.dart';

import '../core/format.dart';
import '../core/json.dart';
import '../core/models.dart';
import '../core/time.dart';
import 'api.dart';
import 'dom.dart';

class Activity {
  final int id;
  final DateTime at;
  final String text;
  final String? symbol;
  final String kind; // agent | user | system
  const Activity(this.id, this.at, this.text, this.symbol, this.kind);
}

const _actionLabel = {
  'strong_buy': 'Strong buy', 'buy': 'Buy', 'hold': 'Hold', 'watch': 'Watch', 'sell': 'Sell',
  'strong_sell': 'Strong sell',
};

HTMLElement _score(String label, double? value) {
  final v = value ?? 0;
  final fill = h(v >= 0 ? 'div.score-fill.pos' : 'div.score-fill.neg');
  fill.style
    ..left = v >= 0 ? '50%' : '${50 + v / 2}%'
    ..width = '${v.abs() / 2}%';
  return h('div.score', [
    h('span.muted.small', label),
    h('div.score-bar', [h('div.score-mid'), fill]),
    h('span.small', value == null ? '–' : value.toStringAsFixed(0)),
  ]);
}

/// A definition list from (term, value) pairs; null values are skipped.
HTMLElement _kv(List<(String, Object?)> rows) => h('dl.kv', [
      for (final (k, v) in rows)
        if (v != null) [h('dt', k), h('dd', v)],
    ]);

abstract class _Panel {
  final HTMLElement root = h('div.panel-body');
  void refresh();
}

// ------------------------------------------------------------------------------- overview

class _Overview extends _Panel {
  String symbol = '';
  Quote? quote;
  J? _tech, _fund;
  List<J> _news = const [];
  String? _err;
  int _seq = 0;

  @override
  void refresh() {
    final seq = ++_seq;
    _tech = _fund = null;
    _err = null;
    _news = const [];
    _render();
    api.summary(symbol).then((t) {
      if (seq == _seq) _tech = t;
    }, onError: (Object e) {
      if (seq == _seq) _err = '$e';
    }).whenComplete(() => seq == _seq ? _render() : null);
    api.fundamentals(symbol).then((f) {
      if (seq == _seq) _fund = f;
    }, onError: (Object _) {
      if (seq == _seq) _fund = const J({});
    }).whenComplete(() => seq == _seq ? _render() : null);
    api.news(symbol).then((r) {
      if (seq == _seq) _news = r['items'].list;
    }, onError: (Object _) {}).whenComplete(() => seq == _seq ? _render() : null);
  }

  void renderQuote() => _render();

  void _render() {
    final t = _tech, f = _fund;
    final q = quote;
    replace(root, [
      if (_err != null) h('div.error.small', _err),
      if (f != null && f['profile']['name'].truthy)
        h('section', h('div.title-row', [
          h('b', f['profile']['name'].s),
          h('span.muted.small', '${f['profile']['sector'].s ?? ''} · ${f['profile']['industry'].s ?? ''}'),
        ])),
      if (t != null) _technical(t) else if (_err == null) h('div.muted.small', 'Loading technicals…'),
      if (f != null && f['valuation'].truthy) _fundamentals(f),
      if (q != null) h('div.muted.small', 'Quote source: ${q.source}${q.feed != null ? ' (${q.feed})' : ''}'),
      if (_news.isNotEmpty)
        h('section', [
          h('h4', 'Headlines'),
          h('ul.news', [
            for (final it in _news.take(8))
              h('li', [
                h('a', it['headline'].s ?? '', {'href': it['url'].s ?? '#', 'target': '_blank', 'rel': 'noreferrer'}),
                h('div.muted.small', '${(it['published_at'].s ?? '').padRight(10).substring(0, 10).trim()} · ${it['source'].s ?? ''}'),
              ]),
          ]),
        ]),
    ]);
  }

  HTMLElement _technical(J t) {
    final tr = t['trend'], mo = t['momentum'], vo = t['volatility'], ret = t['returns_pct'];
    return h('section', [
      h('h4', ['Technical read ', h('span.muted.small', '(${t['as_of'].s ?? ''})')]),
      h('div.bias', t['mechanical_bias'].s ?? ''),
      _score('Trend', tr['score'].d),
      _score('Momentum', mo['score'].d),
      _kv([
        ('RSI 14', [n(mo['rsi14'].d, 1), ' ', h('span.muted', mo['rsi_state'].s ?? '')]),
        ('MA stack', tr['ma_stack'].s ?? '–'),
        ('Structure', tr['structure'].s ?? ''),
        ('ADX', [n(tr['adx'].d, 1), ' ', h('span.muted', tr['trend_strength'].s ?? '')]),
        ('ATR 14', '${n(vo['atr14'].d)} (${n(vo['atr_pct'].d)}%)'),
        ('Vol regime', '${vo['regime'].s ?? '–'}${vo['squeeze_on'].truthy ? ' · squeeze' : ''}'),
        ('Returns', '1m ${pct(ret['1m'].d)} · 3m ${pct(ret['3m'].d)} · 1y ${pct(ret['1y'].d)}'),
        if (t['relative_strength'].truthy) ('vs SPY 3m', pct(t['relative_strength']['outperformance_3m_pct'].d)),
        if (mo['divergence'].truthy) ('Divergence', mo['divergence'].s),
      ]),
      h('div.levels', [
        for (final l in t['levels']['resistances'].list.take(3).toList().reversed)
          h('div.level.res', [
            h('span', 'R ${n(l['price'].d)}'),
            h('span.muted.small', '${l['touches'].s ?? ''}× · ${pct(l['distance_pct'].d)}'),
          ]),
        h('div.level.cur', h('span', 'Last ${n(t['price'].d)}')),
        for (final l in t['levels']['supports'].list.take(3))
          h('div.level.sup', [
            h('span', 'S ${n(l['price'].d)}'),
            h('span.muted.small', '${l['touches'].s ?? ''}× · ${pct(l['distance_pct'].d)}'),
          ]),
      ]),
      if (t['recent_events'].list.isNotEmpty) ...[
        h('h4', 'Recent events'),
        h('ul.events', [
          for (final e in t['recent_events'].list.take(6))
            h('li', [
              h('span.muted.small', e['date'].s ?? ''),
              ' ',
              h(e['bias'].s == 'bullish' ? 'span.up' : e['bias'].s == 'bearish' ? 'span.down' : 'span', e['event'].s ?? ''),
            ]),
        ]),
      ],
    ]);
  }

  HTMLElement _fundamentals(J f) {
    final v = f['valuation'], p = f['profitability'], a = f['analysts'];
    return h('section', [
      h('h4', 'Fundamentals'),
      _kv([
        ('Market cap', compact(f['size']['market_cap'].d)),
        ('P/E · fwd', '${n(v['trailing_pe'].d, 1)} · ${n(v['forward_pe'].d, 1)}'),
        ('EV/EBITDA', n(v['ev_to_ebitda'].d, 1)),
        ('Gross / op margin', '${n(p['gross_margin_pct'].d, 1)}% / ${n(p['operating_margin_pct'].d, 1)}%'),
        ('Revenue growth', pct(f['growth']['revenue_growth_yoy_pct'].d)),
        ('Analysts', '${a['recommendation'].s ?? '–'} · target ${n(a['target_mean'].d)} (${pct(a['upside_to_mean_target_pct'].d)})'),
        ('Next earnings', f['other']['next_earnings'].s ?? '–'),
        ('Short % float', '${n(f['short_interest_and_ownership']['short_pct_of_float'].d, 1)}%'),
      ]),
    ]);
  }
}

// ------------------------------------------------------------------------------- agent

class _AgentPanel extends _Panel {
  final void Function(String tag) onShowTag;
  String symbol = '';
  List<Activity> activity = const [];
  bool _all = false;
  List<J> _items = const [];
  J? _record;
  final Set<String> _open = {};
  final Map<String, J> _full = {};
  int _seq = 0;

  _AgentPanel(this.onShowTag);

  @override
  void refresh() {
    final seq = ++_seq;
    api.analyses(symbol: _all ? null : symbol).then((r) {
      if (seq == _seq) _items = r;
    }, onError: (Object _) {
      if (seq == _seq) _items = const [];
    }).whenComplete(() => seq == _seq ? render() : null);
    api.trackRecord().then((r) {
      if (seq == _seq) _record = r['summary'];
    }, onError: (Object _) {}).whenComplete(() => seq == _seq ? render() : null);
    render();
  }

  void render() {
    final rec = _record;
    final checkbox = h('input', null, {'type': 'checkbox'}) as HTMLInputElement;
    checkbox.checked = _all;
    checkbox.addEventListener('change', ((Event _) {
      _all = checkbox.checked;
      refresh();
    }).toJS);
    replace(root, [
      h('section', [
        h('h4', 'Live activity'),
        if (activity.isEmpty)
          h('div.muted.small', [
            'Agent actions (drawings, view changes, saved analyses) appear here in real time. Connect an MCP client to ',
            h('code', '/mcp'),
            '.',
          ])
        else
          h('ul.activity', [
            for (final a in activity.take(12))
              h('li.${a.kind}', [h('span.muted.small', clockTime(a.at)), ' ${a.text}']),
          ]),
      ]),
      if (rec != null && (rec['total'].i ?? 0) > 0)
        h('section', [
          h('h4', 'Track record'),
          _kv([
            ('Calls', '${rec['total'].i} (${rec['resolved'].i ?? 0} resolved, ${rec['open'].i ?? 0} open)'),
            if (!rec['hit_rate_pct'].isNull) ('Hit rate', '${rec['hit_rate_pct'].s}%'),
            if (!rec['avg_r_multiple'].isNull) ('Avg R', rec['avg_r_multiple'].s),
            if (!rec['avg_alpha_vs_spy_pct'].isNull) ('Avg α vs SPY', pct(rec['avg_alpha_vs_spy_pct'].d)),
            if (!rec['brier_score'].isNull) ('Brier', rec['brier_score'].s),
          ]),
        ]),
      h('section', [
        h('div.title-row', [
          h('h4', 'Analyses'),
          h('label.small.muted', [checkbox, ' all symbols']),
        ]),
        if (_items.isEmpty)
          h('div.muted.small',
              'No saved analyses${_all ? '' : ' for $symbol'}. Ask the agent: “research $symbol for a swing trade”.'),
        for (final a in _items) _card(a),
      ]),
    ]);
  }

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
        h('span.muted.small', '${a['horizon'].s ?? ''} · conf ${((a['confidence'].d ?? 0) * 100).toStringAsFixed(0)}%'),
        h('span.spacer'),
        h('span.muted.small', ago(a['created_at'].s ?? '')),
      ], null, {
        'click': (_) {
          if (!_open.remove(id)) {
            _open.add(id);
            if (full == null) {
              api.analysis(id).then((r) {
                _full[id] = r;
                render();
              }, onError: (Object _) {});
            }
          }
          render();
        },
      }),
      h('p.small', a['summary'].s ?? ''),
      h('div.plan.small', [
        h('span', '@ ${n(a['price_at_creation'].d)}'),
        if (a['entry_low'].truthy || a['entry_high'].truthy)
          h('span', 'entry ${n(a['entry_low'].d)}–${n(a['entry_high'].d)}'),
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
              if (!ev['deadline'].isNull) h('span.muted', ' · horizon ends ${ev['deadline'].s}'),
            ]),
          if (a['scores'].map.isNotEmpty)
            h('div.scores', [for (final e in a['scores'].map.entries) _score(e.key, e.value.d)]),
          if (a['invalidation'].truthy) h('p.small', [h('b', 'Invalidation:'), ' ${a['invalidation'].s}']),
          if (a['risks'].list.isNotEmpty) h('p.small', [h('b', 'Risks:'), ' ${a['risks'].list.map((r) => r.s).join('; ')}']),
          if (a['catalysts'].list.isNotEmpty)
            h('p.small', [h('b', 'Catalysts:'), ' ${a['catalysts'].list.map((r) => r.s).join('; ')}']),
          if (a['annotation_tag'].truthy)
            h('button.btn.small', 'Show drawings (${a['annotation_tag'].s})', {'type': 'button'}, {
              'click': (_) => onShowTag(a['annotation_tag'].s!),
            }),
          if (full != null && full['rationale'].truthy)
            markdown(full['rationale'].s!)
          else
            h('div.muted.small', full == null ? 'Loading rationale…' : 'No rationale saved.'),
        ]),
    ]);
  }
}

// ------------------------------------------------------------------------------- drawings

class _DrawingsPanel extends _Panel {
  final void Function(String id) onSelect;
  final void Function(Annotation a) onToggle;
  final void Function(Annotation a) onDelete;
  final void Function(String? tag) onTagFilter;
  List<Annotation> annotations = const [];
  String? selectedId, tagFilter;

  _DrawingsPanel(this.onSelect, this.onToggle, this.onDelete, this.onTagFilter);

  @override
  void refresh() {
    final tag = tagFilter;
    final shown = tag == null ? annotations : annotations.where((a) => a.tag == tag).toList();
    replace(root, [
      if (tag != null)
        h('div.chip', [
          'tag: $tag ',
          h('button', '×', {'aria-label': 'clear filter', 'type': 'button'}, {'click': (_) => onTagFilter(null)}),
        ]),
      if (shown.isEmpty) h('div.muted.small', 'No drawings. Use the toolbar on the left, or let the agent draw.'),
      h('ul.drawings', [
        for (final a in shown)
          h(a.id == selectedId ? 'li.selected' : 'li', [
            h('span.author.${a.author}', a.author == 'agent' ? '✦ AI' : 'You'),
            h('span.ellipsis', '${a.type.replaceAll('_', ' ')}${a.text != null ? ' — ${a.text}' : ''}'),
            h('span.spacer'),
            h('button.icon', a.visible ? '◉' : '○', {'title': a.visible ? 'Hide' : 'Show', 'type': 'button'}, {
              'click': (e) {
                e.stopPropagation();
                onToggle(a);
              },
            }),
            h('button.icon', '×', {'title': 'Delete', 'type': 'button'}, {
              'click': (e) {
                e.stopPropagation();
                onDelete(a);
              },
            }),
          ], null, {
            'click': (_) => onSelect(a.id)
          }),
      ]),
    ]);
  }
}

// ------------------------------------------------------------------------------- notes

class _NotesPanel extends _Panel {
  String symbol = '';
  List<Note> _notes = const [];
  String? _openId;
  final HTMLElement _list = h('div.panel-list');
  late final HTMLInputElement _title;
  late final HTMLTextAreaElement _content;
  late final HTMLElement _save;
  int _seq = 0;

  _NotesPanel() {
    _title = h('input', null, {'aria-label': 'Note title'}) as HTMLInputElement;
    _content = h('textarea', null, {'placeholder': 'Markdown…', 'rows': '4', 'aria-label': 'Note content'})
        as HTMLTextAreaElement;
    _save = h('button.btn.primary.small', 'Save', {'type': 'submit'});
    final form = h('form.note-form', [_title, _content, _save], null, {
      'submit': (e) async {
        e.preventDefault();
        final title = _title.value.trim();
        if (title.isEmpty) return;
        await api.createNote(title, _content.value, symbol);
        _title.value = '';
        _content.value = '';
        _syncForm();
        refresh();
      },
    });
    _title.addEventListener('input', ((Event _) => _syncForm()).toJS);
    _syncForm();
    append(root, [form, _list]);
  }

  void _syncForm() {
    final show = _title.value.isNotEmpty;
    _content.isHidden = !show;
    _save.isHidden = !show;
  }

  @override
  void refresh() {
    _title.placeholder = 'New note on $symbol…';
    final seq = ++_seq;
    api.notes(symbol).then((r) {
      if (seq == _seq) _notes = r;
    }, onError: (Object _) {
      if (seq == _seq) _notes = const [];
    }).whenComplete(() => seq == _seq ? _render() : null);
  }

  void _render() {
    replace(_list, [
      if (_notes.isEmpty) h('div.muted.small', 'No notes for $symbol. The agent stores theses, key dates and lessons here.'),
      for (final nt in _notes)
        h('article.card', [
          h('header', [
            h('span.author.${nt.author}', nt.author == 'agent' ? '✦ AI' : 'You'),
            h('b.ellipsis', nt.title),
            h('span.spacer'),
            h('span.muted.small', ago(nt.updatedAt)),
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
                await api.deleteNote(nt.id);
                refresh();
              },
            }),
          ],
        ]),
    ]);
  }
}

// ------------------------------------------------------------------------------- watchlist

class _WatchlistPanel extends _Panel {
  final void Function(String symbol) onPick;
  String symbol = '';
  List<Watchlist> _lists = const [];
  String _name = 'default';
  final Map<String, Quote> _quotes = {};
  final HTMLSelectElement _select = h('select', null, {'aria-label': 'Watchlist'}) as HTMLSelectElement;
  final HTMLElement _addCurrent = h('button.btn.small', null, {'type': 'button'});
  late final HTMLInputElement _add;
  final HTMLElement _list = h('div.panel-list');

  _WatchlistPanel(this.onPick) {
    _select.addEventListener('change', ((Event _) {
      _name = _select.value;
      _render();
      _loadQuotes();
    }).toJS);
    _addCurrent.addEventListener('click', ((Event _) {
      _update(add: [symbol]);
    }).toJS);
    _add = h('input', null, {'placeholder': 'Add tickers (comma separated)', 'aria-label': 'Add tickers'})
        as HTMLInputElement;
    final form = h('form.row.gap', _add, null, {
      'submit': (e) {
        e.preventDefault();
        final syms = _add.value.split(RegExp(r'[\s,]+')).where((s) => s.isNotEmpty).toList();
        if (syms.isNotEmpty) _update(add: syms);
        _add.value = '';
      },
    });
    append(root, [h('div.row.gap', [_select, _addCurrent]), form, _list]);
  }

  Watchlist? get _current {
    for (final l in _lists) {
      if (l.name == _name) return l;
    }
    return null;
  }

  @override
  void refresh() {
    _addCurrent.textContent = '+ $symbol';
    api.watchlists().then((l) {
      _lists = l;
      if (_current == null && l.isNotEmpty) _name = l.first.name;
      replace(_select, [for (final w in l) h('option', w.name, {'value': w.name})]);
      _select.value = _name;
      _render();
      _loadQuotes();
    }, onError: (Object _) {});
  }

  void _loadQuotes() {
    for (final s in _current?.symbols ?? const <String>[]) {
      api.quote(s).then((q) {
        _quotes[s] = q;
        _render();
      }, onError: (Object _) {});
    }
  }

  Future<void> _update({List<String>? add, List<String>? remove}) async {
    await api.updateWatchlist(_name, add: add, remove: remove);
    refresh();
  }

  void _render() {
    final cur = _current;
    replace(_list, [
      h('ul.watch', [
        for (final s in cur?.symbols ?? const <String>[])
          h(s == symbol ? 'li.selected' : 'li', [
            h('b', s),
            h('span.spacer'),
            h('span', _quotes[s]?.price.toStringAsFixed(2) ?? '…'),
            h((_quotes[s]?.changePct ?? 0) >= 0 ? 'span.chg.up' : 'span.chg.down',
                _quotes[s]?.changePct != null ? pct(_quotes[s]!.changePct) : ''),
            h('button.icon', '×', {'title': 'Remove', 'type': 'button'}, {
              'click': (e) {
                e.stopPropagation();
                _update(remove: [s]);
              },
            }),
          ], null, {
            'click': (_) => onPick(s)
          }),
      ]),
      if (cur != null && cur.symbols.isEmpty)
        h('div.muted.small', 'Empty. Add tickers above, or ask the agent to build a watchlist.'),
    ]);
  }
}

// ------------------------------------------------------------------------------- shell

const tabs = [('overview', 'Overview'), ('agent', 'Agent'), ('drawings', 'Drawings'), ('notes', 'Notes'), ('watchlist', 'Watchlist')];

class Sidebar {
  final HTMLElement root = h('aside.sidebar');
  final HTMLElement _tabs = h('div.tabs', null, {'role': 'tablist'});
  final HTMLElement _body = h('div.sidebar-body');
  final void Function(String tab) onTab;
  late final _Overview overview = _Overview();
  late final _AgentPanel agent;
  late final _DrawingsPanel drawings;
  final _NotesPanel notes = _NotesPanel();
  late final _WatchlistPanel watchlist;
  String tab = '';
  int unseen = 0;

  Sidebar({
    required this.onTab,
    required void Function(String tag) onShowTag,
    required void Function(String id) onSelect,
    required void Function(Annotation a) onToggle,
    required void Function(Annotation a) onDelete,
    required void Function(String? tag) onTagFilter,
    required void Function(String symbol) onPick,
  }) {
    agent = _AgentPanel(onShowTag);
    drawings = _DrawingsPanel(onSelect, onToggle, onDelete, onTagFilter);
    watchlist = _WatchlistPanel(onPick);
    append(root, [_tabs, _body]);
  }

  _Panel _panel(String t) => switch (t) {
        'agent' => agent,
        'drawings' => drawings,
        'notes' => notes,
        'watchlist' => watchlist,
        _ => overview,
      };

  /// Shows [t]; the panel reloads its data when it becomes visible.
  void show(String t) {
    if (t != tab) {
      tab = t;
      replace(_body, _panel(t).root);
      _panel(t).refresh();
    }
    renderTabs();
  }

  /// Reloads [t] if it is the visible panel (others reload when shown).
  void refreshIfShown(String t) {
    if (t == tab) _panel(t).refresh();
  }

  void renderTabs() {
    replace(_tabs, [
      for (final (t, label) in tabs)
        h(t == tab ? 'button.active' : 'button', [
          label,
          if (t == 'agent' && unseen > 0) h('span.pill', '$unseen'),
          if (t == 'drawings' && drawings.annotations.isNotEmpty) h('span.muted.small', ' ${drawings.annotations.length}'),
        ], {
          'role': 'tab',
          'aria-selected': '${t == tab}',
          'type': 'button'
        }, {
          'click': (_) => onTab(t)
        }),
    ]);
  }
}
