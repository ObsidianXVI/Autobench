// Minimal, safe markdown parser for agent rationales and notes: headings, bold/italic/code,
// lists, tables and paragraphs. Produces a small block tree that the UI renders as DOM nodes
// (never as HTML strings).

sealed class Inline {
  final String text;
  const Inline(this.text);
}

class Plain extends Inline {
  const Plain(super.text);
}

class Bold extends Inline {
  const Bold(super.text);
}

class Italic extends Inline {
  const Italic(super.text);
}

class Code extends Inline {
  const Code(super.text);
}

sealed class Block {
  const Block();
}

class Heading extends Block {
  final int level; // 1-4 as written
  final List<Inline> content;
  const Heading(this.level, this.content);
}

class Paragraph extends Block {
  final List<List<Inline>> lines;
  const Paragraph(this.lines);
}

class ListBlock extends Block {
  final bool ordered;
  final List<List<Inline>> items;
  const ListBlock(this.ordered, this.items);
}

class Table extends Block {
  final List<List<Inline>> head;
  final List<List<List<Inline>>> rows;
  const Table(this.head, this.rows);
}

final _inlineRe = RegExp(r'(\*\*[^*]+\*\*|`[^`]+`|\*[^*]+\*)');
final _headingRe = RegExp(r'^(#{1,4})\s+(.*)');
final _listRe = RegExp(r'^\s*([-*]|\d+\.)\s+');
final _orderedRe = RegExp(r'^\s*\d+\.');
final _separatorRe = RegExp(r'^:?-{2,}:?$');
final _blockStartRe = RegExp(r'^(#{1,4}\s|\s*\||\s*([-*]|\d+\.)\s)');

List<Inline> parseInline(String text) {
  final out = <Inline>[];
  var last = 0;
  for (final m in _inlineRe.allMatches(text)) {
    if (m.start > last) out.add(Plain(text.substring(last, m.start)));
    final tok = m.group(0)!;
    if (tok.startsWith('**')) {
      out.add(Bold(tok.substring(2, tok.length - 2)));
    } else if (tok.startsWith('`')) {
      out.add(Code(tok.substring(1, tok.length - 1)));
    } else {
      out.add(Italic(tok.substring(1, tok.length - 1)));
    }
    last = m.end;
  }
  if (last < text.length) out.add(Plain(text.substring(last)));
  return out;
}

List<String> _cells(String line) {
  var t = line.trim();
  if (t.startsWith('|')) t = t.substring(1);
  if (t.endsWith('|')) t = t.substring(0, t.length - 1);
  return [for (final c in t.split('|')) c.trim()];
}

List<Block> parseMarkdown(String text) {
  final lines = text.replaceAll('\r', '').split('\n');
  final blocks = <Block>[];
  var i = 0;
  while (i < lines.length) {
    final line = lines[i];
    if (line.trim().isEmpty) {
      i++;
      continue;
    }
    final h = _headingRe.firstMatch(line);
    if (h != null) {
      blocks.add(Heading(h.group(1)!.length, parseInline(h.group(2)!)));
      i++;
      continue;
    }
    if (line.trim().startsWith('|')) {
      final rows = <List<String>>[];
      while (i < lines.length && lines[i].trim().startsWith('|')) {
        final cells = _cells(lines[i]);
        if (!cells.every(_separatorRe.hasMatch)) rows.add(cells);
        i++;
      }
      if (rows.isNotEmpty) {
        blocks.add(Table([for (final c in rows.first) parseInline(c)], [
          for (final r in rows.skip(1)) [for (final c in r) parseInline(c)],
        ]));
      }
      continue;
    }
    if (_listRe.hasMatch(line)) {
      final ordered = _orderedRe.hasMatch(line);
      final items = <List<Inline>>[];
      while (i < lines.length && _listRe.hasMatch(lines[i])) {
        items.add(parseInline(lines[i].replaceFirst(_listRe, '')));
        i++;
      }
      blocks.add(ListBlock(ordered, items));
      continue;
    }
    final para = <List<Inline>>[];
    while (i < lines.length && lines[i].trim().isNotEmpty && !_blockStartRe.hasMatch(lines[i])) {
      para.add(parseInline(lines[i]));
      i++;
    }
    if (para.isEmpty) {
      // a line that looks like a block start but did not parse as one: keep it as text
      para.add(parseInline(lines[i]));
      i++;
    }
    blocks.add(Paragraph(para));
  }
  return blocks;
}
