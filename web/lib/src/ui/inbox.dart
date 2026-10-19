// Alert inbox: every firing of an alert, newest first (kept by the backend, so alerts that fired
// while no tab was open are still there). Opened from the inbox button in the top navigation.
import 'package:web/web.dart';

import '../core/format.dart';
import '../core/time.dart';
import 'dom.dart';
import 'kit/icons.dart';
import 'kit/overlay.dart';
import 'kit/state.dart';
import 'store.dart';

Popover? _open;

void _openChart(String symbol) {
  store.activeChart.symbol.value = symbol;
  store.page.value = 'chart';
}

/// Toast for one alert firing; opening the chart or dismissing it marks the inbox entry read.
void alertToast(String description, {required String symbol, String? message, double? price, String? eventId, int ms = 0}) {
  void seen() {
    if (eventId != null) store.markInboxRead([eventId]);
  }

  toast(message != null && message.isNotEmpty ? message : 'Triggered${price != null ? ' at ${fmtPrice(price)}' : ''}',
      title: '⏰ $description', kind: 'alert', ms: ms, actionLabel: 'Open chart', onAction: () {
    seen();
    _openChart(symbol);
  }, onDismiss: seen);
}

/// Toggles the inbox popover under [anchor].
void toggleInbox(Element anchor) {
  if (_open?.isOpen == true) {
    _open!.close();
    return;
  }
  final scope = Scope();
  final body = h('div.inbox');
  void render() => replace(body, _content());
  scope.listen(store.inbox, (_) => render());
  render();
  store.loadInbox();
  _open = Popover.open(body, anchor: anchor, align: 'end', cls: 'inbox-pop', onClose: scope.dispose);
}

List<Object?> _content() {
  final items = store.inbox.value;
  final unread = items.where((e) => !e.read).length;
  return [
    h('div.inbox-head', [
      h('b', 'Inbox'),
      if (unread > 0) h('span.muted.small', '$unread unread'),
      h('span.spacer'),
      if (unread > 0) h('button.btn.small', 'Mark all read', {'type': 'button'}, {'click': (_) => store.markInboxRead()}),
      if (items.isNotEmpty)
        h('button.icon-btn.small', icon('trash', size: 14), {'type': 'button', 'data-tip': 'Clear inbox', 'aria-label': 'Clear inbox'}, {
          'click': (_) async {
            if (await confirmDialog('Clear inbox', 'Remove all ${items.length} entries? Your alerts are not changed.',
                confirm: 'Clear', danger: true)) {
              store.clearInbox();
            }
          },
        }),
    ]),
    if (items.isEmpty)
      h('div.empty-state', [
        icon('inbox', size: 28),
        h('div', 'No alerts yet'),
        h('div.muted.small', 'Every time an alert fires it is listed here, also when no tab was open.'),
      ])
    else
      h('div.inbox-list', [
        for (final e in items)
          h('div.inbox-row${e.read ? '' : '.unread'}', [
            h('span.inbox-dot'),
            h('div.inbox-main', [
              h('div', [h('b', e.symbol), ' ', h('span', e.description.replaceFirst('${e.symbol} ', '').replaceFirst('${e.symbol}: ', ''))]),
              if (e.message != null && e.message!.isNotEmpty) h('div.muted.small', e.message),
              h('div.muted.tiny', [
                ago(e.at),
                if (e.price != null) ' · at ${fmtPrice(e.price!)}',
                if (e.author == 'agent') ' · ✦ by agent',
              ], {'data-tip': e.at}),
            ]),
            h('button.icon-btn.small', icon(e.read ? 'eye-off' : 'check', size: 14), {
              'type': 'button',
              'data-tip': e.read ? 'Mark unread' : 'Mark read',
              'aria-label': e.read ? 'Mark unread' : 'Mark read',
            }, {
              'click': (ev) {
                ev.stopPropagation();
                store.markInboxRead([e.id], !e.read);
              },
            }),
          ], {'data-id': e.id}, {
            'click': (_) {
              store.markInboxRead([e.id]);
              _open?.close();
              _openChart(e.symbol);
            },
          }),
      ]),
  ];
}
