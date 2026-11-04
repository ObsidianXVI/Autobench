// Drawing edits that go through the API and the undo history. A drawing that is deleted and
// re-created gets a new id; `_Ref` keeps undo/redo pointing at the live one.
import '../core/models.dart';
import '../core/undo.dart';
import '../ui/api.dart';
import '../ui/kit/overlay.dart';
import '../ui/store.dart';

class _Ref {
  String id;
  _Ref(this.id);
}

Map<String, Object?> _styleMap(Annotation a) => {
      if (a.style.v is Map) ...Map<String, Object?>.from(a.style.v as Map),
    };

Future<Annotation> _recreate(Annotation a) => api.createAnnotation(a.symbol, a.type, a.points,
    text: a.text, style: _styleMap(a), interval: a.interval, tag: a.tag, layer: a.layer);

void _pushed() => store.undoVersion.value++;

Future<Annotation?> createDrawing(String symbol, String type, List<Point> points,
    {String? text, Map<String, Object?>? style, String? interval, String? layer, bool record = true}) async {
  try {
    final a = await api.createAnnotation(symbol, type, points, text: text, style: style, interval: interval, layer: layer);
    store.upsertDrawing(a);
    if (record) {
      final ref = _Ref(a.id);
      store.undo.push(Command('Create ${type.replaceAll('_', ' ')}', undo: () async {
        await api.deleteAnnotation(ref.id);
        store.removeDrawing(ref.id);
      }, redo: () async {
        final again = await _recreate(a);
        ref.id = again.id;
        store.upsertDrawing(again);
      }));
      _pushed();
    }
    return a;
  } catch (e) {
    toast('$e', kind: 'error');
    return null;
  }
}

/// Applies a patch (points/style/text/visible/interval) and records it for undo.
Future<Annotation?> updateDrawing(Annotation before, Map<String, Object?> patch, {String label = 'Edit drawing'}) async {
  // the inverse patch restores every field we change
  final inverse = <String, Object?>{
    if (patch.containsKey('points')) 'points': [for (final p in before.points) p.toJson()],
    if (patch.containsKey('style')) 'style': _styleMap(before),
    if (patch.containsKey('text')) 'text': before.text ?? '',
    if (patch.containsKey('visible')) 'visible': before.visible,
    if (patch.containsKey('interval')) 'interval': before.interval ?? '',
  };
  try {
    final after = await api.updateAnnotation(before.id, patch);
    store.upsertDrawing(after);
    final ref = _Ref(before.id);
    store.undo.push(Command(label, undo: () async {
      store.upsertDrawing(await api.updateAnnotation(ref.id, inverse));
    }, redo: () async {
      store.upsertDrawing(await api.updateAnnotation(ref.id, patch));
    }));
    _pushed();
    return after;
  } catch (e) {
    toast('$e', kind: 'error');
    return null;
  }
}

Future<void> deleteDrawing(Annotation a) async {
  try {
    await api.deleteAnnotation(a.id);
    store.removeDrawing(a.id);
    final ref = _Ref(a.id);
    store.undo.push(Command('Delete ${a.type.replaceAll('_', ' ')}', undo: () async {
      final again = await _recreate(a);
      ref.id = again.id;
      store.upsertDrawing(again);
    }, redo: () async {
      await api.deleteAnnotation(ref.id);
      store.removeDrawing(ref.id);
    }));
    _pushed();
  } catch (e) {
    toast('$e', kind: 'error');
  }
}

Future<Annotation?> cloneDrawing(Annotation a) {
  // offset the copy slightly so it is visible next to the original
  final shifted = [
    for (final p in a.points) Point(p.time, p.price == null ? null : double.parse((p.price! * 1.01).toStringAsFixed(4))),
  ];
  return createDrawing(a.symbol, a.type, shifted, text: a.text, style: _styleMap(a), interval: a.interval, layer: a.layer);
}

Future<void> undoLast() async {
  final label = await store.undo.undo();
  _pushed();
  if (label != null) toast('Undo: $label', ms: 1800);
}

Future<void> redoLast() async {
  final label = await store.undo.redo();
  _pushed();
  if (label != null) toast('Redo: $label', ms: 1800);
}
