import 'package:aves/services/common/services.dart';
import 'package:flutter/foundation.dart';

final AppTags appTags = AppTags._private();

/// Tags the app derives by itself - typically from an AI tagger - kept
/// deliberately *out* of the media files.
///
/// Aves has no tag store of its own: [AvesEntry.fileTags] is parsed from the
/// file's `dc:subject`, and the tag editor writes any change straight back into
/// the file (see `metadata_edition.dart`). That is exactly what this class
/// avoids. App tags live in their own DB table, are unioned in for display and
/// filtering, and are never part of what gets written back - so tagging a photo
/// leaves its bytes, its mtime and its inode untouched.
class AppTags with ChangeNotifier {
  /// the separator Aves itself uses for `dc:subject`
  static const separator = ';';

  Map<int, Set<String>> _byEntry = {};

  /// bumped on every change, so an [AvesEntry] can tell whether the tags it
  /// cached are still valid without having to listen to this store
  int _generation = 0;

  int get generation => _generation;

  new _private() {
    if (kFlutterMemoryAllocationsEnabled) ChangeNotifier.maybeDispatchObjectCreation(this);
  }

  Future<void> init() async {
    _byEntry = (await localMediaDb.loadAppTags()).map((id, tags) => MapEntry(id, tags.split(separator).where((v) => v.isNotEmpty).toSet()));
  }

  int get entryCount => _byEntry.length;

  /// every tag in the store, for the tag filter grid
  Set<String> get allTags => Set.unmodifiable(_byEntry.values.expand((v) => v).toSet());

  Set<String> tagsOf(int entryId) => _byEntry[entryId] ?? const {};

  bool anyOf(int entryId) => _byEntry[entryId]?.isNotEmpty ?? false;

  Future<void> setTags(int entryId, Set<String> tags) => setTagsById({entryId: tags});

  Future<void> addTags(int entryId, Set<String> tags) => addTagsById({entryId: tags});

  /// Batch variant, used by the tagger so a whole run commits as few times as
  /// possible. An empty set removes the row.
  Future<void> setTagsById(Map<int, Set<String>> tagsById) async {
    final toSave = <int, String>{};
    final toRemove = <int>{};
    tagsById.forEach((id, tags) {
      final cleaned = tags.where((v) => v.isNotEmpty).toSet();
      if (cleaned.isEmpty) {
        if (_byEntry.remove(id) != null) {
          toRemove.add(id);
        }
      } else {
        _byEntry[id] = cleaned;
        toSave[id] = (cleaned.toList()..sort()).join(separator);
      }
    });
    if (toSave.isEmpty && toRemove.isEmpty) return;

    if (toSave.isNotEmpty) {
      await localMediaDb.saveAppTags(toSave);
    }
    if (toRemove.isNotEmpty) {
      await localMediaDb.removeAppTags(toRemove);
    }

    _generation++;
    notifyListeners();
  }

  Future<void> addTagsById(Map<int, Set<String>> tagsById) => setTagsById(tagsById.map((id, tags) => MapEntry(id, {...tagsOf(id), ...tags})));

  Future<void> removeEntries(Iterable<int> entryIds) => removeIds(entryIds.toSet());

  Future<void> removeIds(Set<int> entryIds) async {
    final removed = entryIds.where(_byEntry.containsKey).toSet();
    if (removed.isEmpty) return;

    await localMediaDb.removeAppTags(removed);
    removed.forEach(_byEntry.remove);

    _generation++;
    notifyListeners();
  }

  Future<void> clear() async {
    await localMediaDb.clearAppTags();
    _byEntry.clear();

    _generation++;
    notifyListeners();
  }
}
