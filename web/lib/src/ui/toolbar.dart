import 'package:web/web.dart';

import 'dom.dart';

const _tools = [
  ('select', 'Select (click a drawing; Delete removes it)', 'M5 3l14 8-6 2-2 6z'),
  ('trendline', 'Trend line', 'M4 19L20 5'),
  ('ray', 'Ray', 'M4 18l16-9M18 7l3 1-1 3'),
  ('horizontal_line', 'Horizontal line', 'M3 12h18'),
  ('horizontal_ray', 'Horizontal ray', 'M6 12h15M6 9v6'),
  ('vertical_line', 'Vertical line', 'M12 3v18'),
  ('rectangle', 'Rectangle / zone', 'M4 6h16v12H4z'),
  ('fib_retracement', 'Fibonacci retracement', 'M3 5h18M3 9h18M3 13h18M3 19h18'),
  ('price_range', 'Measure (price range)', 'M12 3v18M8 7l4-4 4 4M8 17l4 4 4-4'),
  ('arrow', 'Arrow', 'M5 19L19 5M11 5h8v8'),
  ('long_position', 'Long position (click entry, then target; stop defaults to 2R)', 'M4 4h16v8H4zM4 12h16v6H4z'),
  ('short_position', 'Short position (click entry, then target)', 'M4 6h16v6H4zM4 12h16v8H4z'),
  ('text', 'Text label', 'M5 6h14M12 6v13'),
];

const _svgNs = 'http://www.w3.org/2000/svg';

Element _icon(String d) {
  final svg = document.createElementNS(_svgNs, 'svg');
  for (final (k, v) in [
    ('viewBox', '0 0 24 24'), ('width', '18'), ('height', '18'), ('fill', 'none'), ('stroke', 'currentColor'),
    ('stroke-width', '1.8'), ('stroke-linecap', 'round'), ('stroke-linejoin', 'round'),
  ]) {
    svg.setAttribute(k, v);
  }
  for (final part in d.split('|')) {
    final path = document.createElementNS(_svgNs, 'path');
    path.setAttribute('d', part);
    svg.appendChild(path);
  }
  return svg;
}

class Toolbar {
  final HTMLElement root = h('nav.toolbar', null, {'aria-label': 'Drawing tools'});
  final Map<String, HTMLElement> _buttons = {};

  Toolbar({
    required void Function(String tool) onTool,
    required void Function() onClearMine,
    required void Function() onClearAgent,
  }) {
    for (final (tool, title, icon) in _tools) {
      final b = h('button', _icon(icon), {'title': title, 'aria-label': title, 'type': 'button'}, {
        'click': (_) => onTool(tool),
      });
      _buttons[tool] = b;
      root.append(b);
    }
    append(root, [
      h('div.toolbar-sep'),
      h('button', _icon('M5 7h14M9 7V4h6v3M7 7l1 13h8l1-13'),
          {'title': 'Remove my drawings on this symbol', 'aria-label': 'Remove my drawings', 'type': 'button'},
          {'click': (_) => onClearMine()}),
      h('button.agent', _icon('M12 3l2 5 5 2-5 2-2 5-2-5-5-2 5-2z|M4 20l16-16'), {
        'title': "Remove the agent's drawings on this symbol",
        'aria-label': 'Remove agent drawings',
        'type': 'button',
      }, {
        'click': (_) => onClearAgent()
      }),
    ]);
  }

  set active(String tool) {
    _buttons.forEach((t, b) => b.classList.toggle('active', t == tool));
  }
}
