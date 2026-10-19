// Undo/redo history of reversible operations (drawing create/update/delete).

/// One reversible step: [undo] restores the previous state, [redo] re-applies it.
class Command {
  final String label;
  final Future<void> Function() undo;
  final Future<void> Function() redo;
  Command(this.label, {required this.undo, required this.redo});
}

class UndoStack {
  final int limit;
  final List<Command> _done = [];
  final List<Command> _undone = [];
  bool _busy = false;

  UndoStack({this.limit = 100});

  bool get canUndo => _done.isNotEmpty && !_busy;
  bool get canRedo => _undone.isNotEmpty && !_busy;
  String? get undoLabel => _done.isEmpty ? null : _done.last.label;
  String? get redoLabel => _undone.isEmpty ? null : _undone.last.label;

  /// Records an operation that has already been applied.
  void push(Command c) {
    _done.add(c);
    if (_done.length > limit) _done.removeAt(0);
    _undone.clear();
  }

  Future<String?> undo() async {
    if (!canUndo) return null;
    final c = _done.removeLast();
    _busy = true;
    try {
      await c.undo();
      _undone.add(c);
      return c.label;
    } finally {
      _busy = false;
    }
  }

  Future<String?> redo() async {
    if (!canRedo) return null;
    final c = _undone.removeLast();
    _busy = true;
    try {
      await c.redo();
      _done.add(c);
      return c.label;
    } finally {
      _busy = false;
    }
  }

  void clear() {
    _done.clear();
    _undone.clear();
  }
}
