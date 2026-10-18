// Backend event stream (/ws) with automatic reconnect and exponential backoff.
import 'dart:async';
import 'dart:convert';
import 'dart:js_interop';
import 'dart:math' as math;

import 'package:web/web.dart';

import '../core/json.dart';

class ServerEvent {
  final String type;
  final J data;
  const ServerEvent(this.type, this.data);
}

class EventStream {
  final void Function(ServerEvent) onEvent;
  final void Function(String status) onStatus; // connecting | open | closed
  WebSocket? _socket;
  int _retry = 0;
  Timer? _timer;

  EventStream({required this.onEvent, required this.onStatus}) {
    _connect();
  }

  void _connect() {
    onStatus('connecting');
    final proto = window.location.protocol == 'https:' ? 'wss' : 'ws';
    final socket = WebSocket('$proto://${window.location.host}/ws');
    _socket = socket;
    socket.addEventListener('open', ((Event _) {
      _retry = 0;
      onStatus('open');
    }).toJS);
    socket.addEventListener('message', ((MessageEvent m) {
      final raw = m.data;
      if (raw == null || !raw.isA<JSString>()) return;
      try {
        final j = J(jsonDecode((raw as JSString).toDart));
        onEvent(ServerEvent(j['type'].s ?? '', j['data']));
      } on FormatException {
        // ignore malformed frames
      }
    }).toJS);
    socket.addEventListener('close', ((Event _) {
      onStatus('closed');
      final delay = math.min(10000, 500 * math.pow(2, _retry++).toInt());
      _timer = Timer(Duration(milliseconds: delay), _connect);
    }).toJS);
  }

  void close() {
    _timer?.cancel();
    _socket?.close();
  }
}
