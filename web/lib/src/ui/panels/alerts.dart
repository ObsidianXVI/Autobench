// Alerts panel: active / triggered alerts, create, pause/resume, delete, jump to chart.
import 'package:web/web.dart';

import '../../core/format.dart';
import '../../core/time.dart';
import '../api.dart';
import '../dom.dart';
import '../kit/icons.dart';
import '../kit/overlay.dart';
import '../kit/state.dart';
import '../store.dart';

class AlertsPanel {
  final HTMLElement root = h('div.panel');
  final HTMLElement _body = h('div.panel-scroll');
  final Scope _scope = Scope();
  String _filter = 'all';
  final void Function(String symbol) onCreate;

  AlertsPanel({required this.onCreate}) {
    append(root, [h('div.panel-head', [
      h('span.panel-title.static', 'Alerts'),
      h('span.spacer'),
      h('button.icon-btn', icon('plus', size: 16), {'type': 'button', 'data-tip': 'Create alert (Alt+A)', 'aria-label': 'Create alert'},
          {'click': (_) => onCreate(store.activeChart.symbol.value)}),
    ]), h('div.seg', [
      for (final (k, label) in [('active', 'Active'), ('triggered', 'Triggered'), ('all', 'All')])
        h('button.seg-btn', label, {'type': 'button', 'data-k': k}, {
          'click': (_) {
            _filter = k;
            _render();
          },
        }),
    ]), _body]);
    _scope.listen(store.alerts, (_) => _render());
    store.loadAlerts();
    _render();
  }

  void dispose() => _scope.dispose();

  void _render() {
    for (final el in qsa(root, '.seg-btn')) {
      el.classList.toggle('on', el.getAttribute('data-k') == _filter);
    }
    final items = [for (final a in store.alerts.value) if (_filter == 'all' || a.status == _filter) a];
    replace(_body, [
      if (items.isEmpty)
        h('div.empty-state', [
          icon('alarm', size: 28),
          h('div', _filter == 'triggered' ? 'Nothing has triggered yet' : 'No alerts'),
          h('div.muted.small', 'Right-click the chart at a price, use the bell in the toolbar, or put an alert on a horizontal line. The agent can create alerts too.'),
        ]),
      for (final a in items)
        h('div.alert-row.${a.status}', [
          h('div.alert-main', [
            h('div', [h('b', a.symbol), ' ', h('span', a.description.replaceFirst('${a.symbol} ', '').replaceFirst('${a.symbol}: ', ''))]),
            if (a.message != null && a.message!.isNotEmpty) h('div.muted.small', a.message),
            h('div.muted.tiny', [
              a.status == 'triggered' && a.triggeredAt != null
                  ? 'Triggered ${ago(a.triggeredAt!)}${a.triggerPrice != null ? ' at ${fmtPrice(a.triggerPrice!)}' : ''}'
                  : a.status == 'paused' ? 'Paused' : 'Active${a.repeat ? ' · repeats' : ''}',
              if (a.author == 'agent') ' · ✦ by agent',
              if (a.annotationId != null) ' · on drawing',
            ]),
          ], null, {
            'click': (_) {
              store.activeChart.symbol.value = a.symbol;
              store.page.value = 'chart';
            },
          }),
          h('div.alert-actions', [
            if (a.status != 'triggered')
              h('button.icon-btn', icon(a.status == 'paused' ? 'play' : 'pause', size: 14),
                  {'type': 'button', 'data-tip': a.status == 'paused' ? 'Resume' : 'Pause'}, {
                'click': (_) => _patch(a.id, {'status': a.status == 'paused' ? 'active' : 'paused'}),
              })
            else
              h('button.icon-btn', icon('refresh', size: 14), {'type': 'button', 'data-tip': 'Re-arm'},
                  {'click': (_) => _patch(a.id, {'status': 'active'})}),
            h('button.icon-btn', icon('trash', size: 14), {'type': 'button', 'data-tip': 'Delete'}, {
              'click': (_) async {
                await request('/api/alerts/${a.id}', method: 'DELETE');
                store.loadAlerts();
              },
            }),
          ]),
        ]),
    ]);
  }

  Future<void> _patch(String id, Map<String, Object?> body) async {
    try {
      await request('/api/alerts/$id', method: 'PATCH', body: body);
      await store.loadAlerts();
    } catch (e) {
      toast('$e', kind: 'error');
    }
  }
}
