// Stroke icons (24×24 viewBox). Paths separated by '|' become separate <path> elements;
// a leading '!' fills the path instead of stroking it.
import 'package:web/web.dart';

const icons = <String, String>{
  'wallet': 'M3 7h15a3 3 0 0 1 3 3v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM3 7l12-4 2 4|M16 13.5h2',
  'trade': 'M7 20V4M3 8l4-4 4 4|M17 4v16M13 16l4 4 4-4',
  'shield': 'M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z',
  'octagon': 'M8 3h8l5 5v8l-5 5H8l-5-5V8z|M9 9l6 6M15 9l-6 6',
  'cursor': 'M5 3l14 8-6 2-2 6z',
  'cross': 'M12 3v18M3 12h18',
  'trendline': 'M4 19L20 5|!M4 19m-2 0a2 2 0 1 0 4 0a2 2 0 1 0-4 0|!M20 5m-2 0a2 2 0 1 0 4 0a2 2 0 1 0-4 0',
  'ray': 'M4 18L21 8|!M4 18m-2 0a2 2 0 1 0 4 0a2 2 0 1 0-4 0',
  'extended': 'M2 19L22 5',
  'hline': 'M3 12h18',
  'hray': 'M6 12h15|!M6 12m-2 0a2 2 0 1 0 4 0a2 2 0 1 0-4 0',
  'vline': 'M12 3v18',
  'channel': 'M3 15L17 5M7 19l14-10',
  'fib': 'M3 5h18M3 9.5h18M3 13h18M3 19h18M5 5v14',
  'rect': 'M4 6h16v12H4z',
  'arrow': 'M5 19L19 5M10 5h9v9',
  'polyline': 'M3 18l5-7 5 4 8-10',
  'text': 'M5 6h14M12 6v13M9 19h6',
  'callout': 'M4 5h16v10H10l-4 4v-4H4z',
  'marker': 'M12 3l3 6 6 .9-4.5 4.3 1 6.3L12 17.5 6.5 20.5l1-6.3L3 9.9 9 9z',
  'measure': 'M12 3v18M8 7l4-4 4 4M8 17l4 4 4-4',
  'long': 'M4 4h16v8H4z|M4 12h16v6H4z',
  'short': 'M4 6h16v6H4z|M4 12h16v8H4z',
  'magnet': 'M6 3v8a6 6 0 0 0 12 0V3h-4v8a2 2 0 0 1-4 0V3z|M6 7h4M14 7h4',
  'lock': 'M6 11h12v9H6z|M8 11V8a4 4 0 0 1 8 0v3',
  'unlock': 'M6 11h12v9H6z|M8 11V8a4 4 0 0 1 7.5-2',
  'eye': 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z|M12 12m-3 0a3 3 0 1 0 6 0a3 3 0 1 0-6 0',
  'eye-off': 'M3 3l18 18|M10.6 6.1A10 10 0 0 1 12 6c6 0 10 6 10 6a17 17 0 0 1-3.3 3.8M6.6 7.6C3.8 9.4 2 12 2 12s4 7 10 7a9.6 9.6 0 0 0 4.2-1',
  'trash': 'M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13',
  'undo': 'M9 14L4 9l5-5|M4 9h11a5 5 0 0 1 0 10h-3',
  'redo': 'M15 14l5-5-5-5|M20 9H9a5 5 0 0 0 0 10h3',
  'camera': 'M3 8h4l2-3h6l2 3h4v11H3z|M12 13m-3.5 0a3.5 3.5 0 1 0 7 0a3.5 3.5 0 1 0-7 0',
  'fullscreen': 'M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5',
  'settings': 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z|M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 0 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z',
  'plus': 'M12 5v14M5 12h14',
  'minus': 'M5 12h14',
  'x': 'M6 6l12 12M18 6L6 18',
  'check': 'M5 12l5 5L20 7',
  'chev-down': 'M6 9l6 6 6-6',
  'chev-right': 'M9 6l6 6-6 6',
  'chev-left': 'M15 6l-6 6 6 6',
  'chev-up': 'M6 15l6-6 6 6',
  'search': 'M11 11m-7 0a7 7 0 1 0 14 0a7 7 0 1 0-14 0|M20 20l-4-4',
  'bell': 'M6 16V11a6 6 0 0 1 12 0v5l2 2H4z|M10 20a2 2 0 0 0 4 0',
  'alarm': 'M12 13m-7 0a7 7 0 1 0 14 0a7 7 0 1 0-14 0|M12 9v4l2 2|M5 3L2 6M19 3l3 3',
  'star': 'M12 3l2.8 5.7 6.2.9-4.5 4.4 1.1 6.2L12 17.3 6.4 20.2l1.1-6.2L3 9.6l6.2-.9z',
  'grid-1': 'M4 4h16v16H4z',
  'grid-2v': 'M4 4h16v16H4z|M12 4v16',
  'grid-2h': 'M4 4h16v16H4z|M4 12h16',
  'grid-3': 'M4 4h16v16H4z|M12 4v16|M12 12h8',
  'grid-4': 'M4 4h16v16H4z|M12 4v16|M4 12h16',
  'candles': 'M7 4v16M17 4v16|!M5 8h4v8H5z|M15 6h4v9h-4z',
  'hollow': 'M7 4v16M17 4v16|M5 8h4v8H5z|!M15 6h4v9h-4z',
  'bars': 'M7 4v16M4 8h3M7 16h3|M17 4v16M14 6h3M17 15h3',
  'line': 'M3 17l6-6 4 4 8-9',
  'area': 'M3 17l6-6 4 4 8-9v14H3z',
  'heikin': 'M7 4v16M17 4v16|!M5 7h4v9H5z|!M15 5h4v8h-4z',
  'compare': 'M3 17l5-5 4 3 9-9|M3 21l6-8 4 2 8-7',
  'fx': 'M10 4c-3 0-3 2-3 4v12M4 10h7|M14 12l6 7M20 12l-6 7',
  'list': 'M8 6h13M8 12h13M8 18h13|!M4 6m-1 0a1 1 0 1 0 2 0a1 1 0 1 0-2 0|!M4 12m-1 0a1 1 0 1 0 2 0a1 1 0 1 0-2 0|!M4 18m-1 0a1 1 0 1 0 2 0a1 1 0 1 0-2 0',
  'info': 'M12 12m-9 0a9 9 0 1 0 18 0a9 9 0 1 0-18 0|M12 11v5|!M12 8m-1 0a1 1 0 1 0 2 0a1 1 0 1 0-2 0',
  'news': 'M4 5h13v14H6a2 2 0 0 1-2-2z|M17 9h3v8a2 2 0 0 1-2 2|M7 9h7M7 12h7M7 15h4',
  'bot': 'M5 9h14v10H5z|M12 5v4|!M12 4m-1.5 0a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0|!M9 13m-1.3 0a1.3 1.3 0 1 0 2.6 0a1.3 1.3 0 1 0-2.6 0|!M15 13m-1.3 0a1.3 1.3 0 1 0 2.6 0a1.3 1.3 0 1 0-2.6 0|M9.5 16.5h5',
  'note': 'M6 3h9l4 4v14H6z|M14 3v5h5|M9 12h7M9 16h5',
  'layers': 'M12 3l9 5-9 5-9-5z|M3 13l9 5 9-5',
  'calendar': 'M4 6h16v14H4z|M4 10h16M9 3v4M15 3v4',
  'globe': 'M12 12m-9 0a9 9 0 1 0 18 0a9 9 0 1 0-18 0|M3 12h18|M12 3a14 14 0 0 1 0 18a14 14 0 0 1 0-18',
  'map': 'M3 4h7v9H3z|M10 4h11v6H10z|M10 10h5v10h-5z|M15 10h6v10h-6z|M3 13h7v7H3z',
  'filter': 'M3 5h18l-7 8v6l-4 2v-8z',
  'chart': 'M4 20V10M10 20V4M16 20v-7M22 20H2',
  'sun': 'M12 12m-4 0a4 4 0 1 0 8 0a4 4 0 1 0-8 0|M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4',
  'moon': 'M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z',
  'download': 'M12 4v12M7 11l5 5 5-5|M4 20h16',
  'refresh': 'M20 11a8 8 0 0 0-14.9-3M4 13a8 8 0 0 0 14.9 3|M4 4v4h4M20 20v-4h-4',
  'drag': '!M9 6m-1.5 0a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0|!M15 6m-1.5 0a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0|!M9 12m-1.5 0a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0|!M15 12m-1.5 0a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0|!M9 18m-1.5 0a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0|!M15 18m-1.5 0a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0',
  'more': '!M5 12m-1.6 0a1.6 1.6 0 1 0 3.2 0a1.6 1.6 0 1 0-3.2 0|!M12 12m-1.6 0a1.6 1.6 0 1 0 3.2 0a1.6 1.6 0 1 0-3.2 0|!M19 12m-1.6 0a1.6 1.6 0 1 0 3.2 0a1.6 1.6 0 1 0-3.2 0',
  'copy': 'M8 8h12v12H8z|M16 8V4H4v12h4',
  'edit': 'M4 20h4L19 9l-4-4L4 16z|M14 6l4 4',
  'external': 'M14 4h6v6|M20 4l-9 9|M18 14v6H4V6h6',
  'save': 'M5 3h12l4 4v14H3V3z|M7 3v6h9|M7 21v-7h10v7',
  'folder': 'M3 6h6l2 2h10v11H3z',
  'keyboard': 'M2 6h20v12H2z|M6 10h1M10 10h1M14 10h1M18 10h1M7 14h10',
  'pause': 'M8 5v14M16 5v14',
  'play': '!M7 4l13 8-13 8z',
  'clock': 'M12 12m-9 0a9 9 0 1 0 18 0a9 9 0 1 0-18 0|M12 7v5l3 2',
  'percent': 'M5 19L19 5|M7 7m-2 0a2 2 0 1 0 4 0a2 2 0 1 0-4 0|M17 17m-2 0a2 2 0 1 0 4 0a2 2 0 1 0-4 0',
  'log': 'M4 20h16|M4 20C9 19 12 14 13 4',
  'link': 'M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1|M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1',
  'table': 'M3 5h18v14H3z|M3 10h18M3 15h18M9 5v14',
  'pie': 'M12 3v9h9a9 9 0 1 1-9-9z|M15 3.5A9 9 0 0 1 20.5 9H15z',
};

const _svgNs = 'http://www.w3.org/2000/svg';

/// An inline SVG icon element.
Element icon(String name, {double size = 18, double stroke = 1.8, String? cls}) {
  final svg = document.createElementNS(_svgNs, 'svg');
  for (final (k, v) in [
    ('viewBox', '0 0 24 24'), ('width', '$size'), ('height', '$size'), ('fill', 'none'), ('stroke', 'currentColor'),
    ('stroke-width', '$stroke'), ('stroke-linecap', 'round'), ('stroke-linejoin', 'round'), ('aria-hidden', 'true'),
  ]) {
    svg.setAttribute(k, v);
  }
  svg.setAttribute('class', 'icon-svg${cls != null ? ' $cls' : ''}');
  for (final part in (icons[name] ?? icons['x']!).split('|')) {
    final path = document.createElementNS(_svgNs, 'path');
    if (part.startsWith('!')) {
      path.setAttribute('d', part.substring(1));
      path.setAttribute('fill', 'currentColor');
      path.setAttribute('stroke', 'none');
    } else {
      path.setAttribute('d', part);
    }
    svg.appendChild(path);
  }
  return svg;
}
