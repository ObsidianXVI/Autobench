// A small DOM builder: h('div.card.small', [children], on: {'click': ...}).
import 'dart:js_interop';
import 'dart:js_interop_unsafe';

import 'package:web/web.dart';

import '../core/markdown.dart' as md;

/// Creates an element. [spec] is a tag with optional classes ("li.card.selected"); [children]
/// may contain nodes, strings, numbers, nested iterables and nulls (skipped).
HTMLElement h(
  String spec, [
  Object? children,
  Map<String, String>? attrs,
  Map<String, void Function(Event)>? on,
]) {
  final parts = spec.split('.');
  final el = document.createElement(parts.first) as HTMLElement;
  final classes = parts.skip(1).where((c) => c.isNotEmpty).join(' ');
  if (classes.isNotEmpty) el.className = classes;
  attrs?.forEach((k, v) => el.setAttribute(k, v));
  on?.forEach((type, fn) => el.addEventListener(type, fn.toJS));
  append(el, children);
  return el;
}

/// Appends [children] (see [h]) to [parent].
void append(Node parent, Object? children) {
  if (children == null || children == false) return;
  // Order matters: compiled to JavaScript, Dart lists are JS arrays and pass an `is Node` check
  // (Node is an extension type over JSObject), so iterables and primitives are tested first.
  if (children is Iterable) {
    for (final c in children) {
      append(parent, c);
    }
  } else if (children is String || children is num || children is bool) {
    parent.appendChild(document.createTextNode('$children'));
  } else {
    parent.appendChild(children as Node);
  }
}

/// Replaces all children of [parent].
void replace(Element parent, Object? children) {
  parent.textContent = '';
  append(parent, children);
}

/// The class string "a b" for the given flags.
String cx(Map<String, bool> flags) => flags.entries.where((e) => e.value).map((e) => e.key).join(' ');

/// Renders parsed markdown as DOM nodes (text is never interpreted as HTML).
HTMLElement markdown(String text) {
  Object inline(List<md.Inline> parts) => [
        for (final p in parts)
          switch (p) {
            md.Bold() => h('b', p.text),
            md.Italic() => h('i', p.text),
            md.Code() => h('code', p.text),
            md.Plain() => p.text,
          },
      ];
  return h('div.md', [
    for (final b in md.parseMarkdown(text))
      switch (b) {
        md.Heading() => h('h${(b.level + 2).clamp(3, 6)}', inline(b.content)),
        md.Paragraph() => h('p', [
            for (var i = 0; i < b.lines.length; i++) [if (i > 0) h('br'), inline(b.lines[i])],
          ]),
        md.ListBlock() => h(b.ordered ? 'ol' : 'ul', [for (final it in b.items) h('li', inline(it))]),
        md.CodeBlock() => h('pre.md-code', h('code', b.text)),
        md.Table() => h('table.md-table', [
            h('thead', h('tr', [for (final c in b.head) h('th', inline(c))])),
            h('tbody', [
              for (final r in b.rows) h('tr', [for (final c in r) h('td', inline(c))]),
            ]),
          ]),
      },
  ]);
}

/// The value of an input/textarea/select element.
String valueOf(EventTarget? t) {
  final x = t as JSObject?;
  if (x == null) return '';
  if (x.isA<HTMLInputElement>()) return (x as HTMLInputElement).value;
  if (x.isA<HTMLTextAreaElement>()) return (x as HTMLTextAreaElement).value;
  if (x.isA<HTMLSelectElement>()) return (x as HTMLSelectElement).value;
  return '';
}

/// Whether keyboard focus is in a text field (so global shortcuts should not fire).
bool typingIn(EventTarget? t) {
  final x = t as JSObject?;
  if (x == null || !x.isA<HTMLElement>()) return false;
  final el = x as HTMLElement;
  return el.tagName == 'INPUT' || el.tagName == 'TEXTAREA' || el.tagName == 'SELECT' || el.isContentEditable;
}

extension Hidden on HTMLElement {
  /// The `hidden` attribute as a bool (package:web types the property as JSAny).
  bool get isHidden => hasAttribute('hidden');
  set isHidden(bool v) => v ? setAttribute('hidden', '') : removeAttribute('hidden');
}

/// querySelectorAll as a Dart list.
List<HTMLElement> qsa(Element root, String selector) {
  final nodes = root.querySelectorAll(selector);
  return [for (var i = 0; i < nodes.length; i++) nodes.item(i)! as HTMLElement];
}

/// The pointer's viewport position. Read as doubles: package:web types clientX/Y as int, but browsers
/// report fractional pixels (zoom, high-DPI, pointer events), and the int cast would throw.
(double, double) clientXY(Event e) {
  final o = e as JSObject;
  return ((o['clientX'] as JSNumber).toDartDouble, (o['clientY'] as JSNumber).toDartDouble);
}
