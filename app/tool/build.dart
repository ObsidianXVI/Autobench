// Builds the web UI into build/: copies static/ and compiles lib/main.dart with dart2js.
//
//   dart run tool/build.dart           release build (-O2, minified)
//   dart run tool/build.dart --dev     one unminified build (readable stack traces)
//   dart run tool/build.dart --watch   development: unminified builds, rebuilt on every change
//
// `autobench serve` serves build/ from disk, so reloading the page picks up a rebuild.
import 'dart:async';
import 'dart:io';

final root = File.fromUri(Platform.script).parent.parent;
final staticDir = Directory('${root.path}/static');
final libDir = Directory('${root.path}/lib');
final outDir = Directory('${root.path}/build');

Future<void> main(List<String> args) async {
  final watch = args.contains('--watch');
  if (!await build(release: !watch && !args.contains('--dev'))) {
    if (!watch) exit(1);
  }
  if (!watch) return;
  stdout.writeln('Watching lib/ and static/ for changes (Ctrl-C to stop)…');
  var last = _stamp();
  var busy = false;
  Timer.periodic(const Duration(milliseconds: 400), (_) async {
    if (busy) return;
    final now = _stamp();
    if (now == last) return;
    last = now;
    busy = true;
    await build(release: false);
    busy = false;
  });
}

/// A fingerprint of all source files (paths + modification times).
String _stamp() {
  final parts = <String>[];
  for (final dir in [libDir, staticDir]) {
    for (final f in dir.listSync(recursive: true).whereType<File>()) {
      parts.add('${f.path}:${f.lastModifiedSync().millisecondsSinceEpoch}');
    }
  }
  parts.sort();
  return parts.join('|');
}

Future<bool> build({required bool release}) async {
  final watch = Stopwatch()..start();
  outDir.createSync(recursive: true);
  for (final f in staticDir.listSync(recursive: true).whereType<File>()) {
    final rel = f.path.substring(staticDir.path.length + 1);
    final dest = File('${outDir.path}/$rel')..parent.createSync(recursive: true);
    f.copySync(dest.path);
  }
  final result = await Process.run(Platform.resolvedExecutable, [
    'compile',
    'js',
    release ? '-O2' : '-O1',
    if (!release) '--no-minify',
    '-o',
    '${outDir.path}/main.dart.js',
    '${libDir.path}/main.dart',
  ], workingDirectory: root.path);
  // dart2js leaves a .deps file next to its output; it is not needed at runtime
  final deps = File('${outDir.path}/main.dart.js.deps');
  if (deps.existsSync()) deps.deleteSync();
  if (result.exitCode != 0) {
    stderr.writeln('Build failed:\n${result.stdout}${result.stderr}');
    return false;
  }
  final size = File('${outDir.path}/main.dart.js').lengthSync();
  stdout.writeln('Built build/ in ${watch.elapsedMilliseconds} ms '
      '(main.dart.js ${(size / 1024).toStringAsFixed(0)} KB, ${release ? 'release' : 'dev'})');
  return true;
}
