// News panel: headlines for the active symbol or the whole market.
import 'dart:async';

import 'package:web/web.dart';

import '../../core/json.dart';
import '../../core/time.dart';
import '../api.dart';
import '../dom.dart';
import '../kit/state.dart';
import '../store.dart';

class NewsPanel {
  final HTMLElement root = h('div.panel');
  final HTMLElement _body = h('div.panel-scroll');
  final Scope _scope = Scope();
  bool _market = false;
  List<J> _items = const [];
  String? _error;
  int _seq = 0;
  Timer? _poll;

  NewsPanel() {
    append(root, [
      h('div.panel-head', [h('span.panel-title.static', 'News'), h('span.spacer'), h('div.seg.inline', [
        h('button.seg-btn', 'Symbol', {'type': 'button', 'data-k': 'symbol'}, {'click': (_) => _set(false)}),
        h('button.seg-btn', 'Market', {'type': 'button', 'data-k': 'market'}, {'click': (_) => _set(true)}),
      ])]),
      _body,
    ]);
    void track() {
      for (final c in store.charts) {
        _scope.listen(c.symbol, (_) => _load());
      }
    }

    track();
    _scope.listen(store.active, (_) => _load());
    _poll = Timer.periodic(const Duration(minutes: 2), (_) => _load());
    _load();
  }

  void dispose() {
    _poll?.cancel();
    _scope.dispose();
  }

  void _set(bool market) {
    _market = market;
    _load();
  }

  Future<void> _load() async {
    if (!root.isConnected && _items.isNotEmpty) return;
    final seq = ++_seq;
    _render(loading: true);
    try {
      final r = await api.news(_market ? null : store.activeChart.symbol.value, limit: 30);
      if (seq != _seq) return;
      _items = r['items'].list;
      _error = null;
    } catch (e) {
      if (seq != _seq) return;
      _error = '$e';
    }
    _render();
  }

  void _render({bool loading = false}) {
    for (final el in qsa(root, '.seg-btn')) {
      el.classList.toggle('on', (el.getAttribute('data-k') == 'market') == _market);
    }
    replace(_body, [
      if (_error != null) h('div.error.small.pad', _error),
      if (loading && _items.isEmpty) h('div.muted.pad.small', 'Loading headlines…'),
      for (final it in _items)
        h('a.news-item', [
          h('div.news-head', it['headline'].s ?? ''),
          if ((it['summary'].s ?? '').isNotEmpty) h('div.news-sum', it['summary'].s),
          h('div.muted.tiny', [
            ago(it['published_at'].s ?? ''),
            ' · ${it['source'].s ?? ''}',
            for (final s in it['symbols'].list.take(4))
              h('span.news-sym', s.s ?? '', null, {
                'click': (e) {
                  e.preventDefault();
                  store.activeChart.symbol.value = s.s!;
                },
              }),
          ]),
        ], {'href': it['url'].s ?? '#', 'target': '_blank', 'rel': 'noreferrer'}),
      if (!loading && _items.isEmpty && _error == null) h('div.muted.pad.small', 'No recent headlines.'),
    ]);
  }
}
