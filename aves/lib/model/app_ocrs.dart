import 'package:aves/model/app_ocr.dart';
import 'package:aves/services/common/services.dart';
import 'package:flutter/foundation.dart';

final AppOcrs appOcrs = AppOcrs._private();

/// OCR text for the whole library.
///
/// Deliberately much simpler than `AppVectors`: there is no matrix here. Text
/// search is a substring match, and a substring match cannot be precomputed --
/// so every query goes to SQLite and this in-memory copy exists only to answer
/// "does any photo have text at all" without touching the database on a screen
/// that has nothing to show anyway.
class AppOcrs with ChangeNotifier {
  Map<int, AppOcr> _byEntry = {};

  /// How many photos have OCR text.
  int get entryCount => _byEntry.length;

  bool get hasText => _byEntry.isNotEmpty;

  new _private() {
    if (kFlutterMemoryAllocationsEnabled) ChangeNotifier.maybeDispatchObjectCreation(this);
  }

  Future<void> init() async {
    _byEntry = await localMediaDb.loadAppOcr();
    notifyListeners();
  }

  AppOcr? of(int entryId) => _byEntry[entryId];

  Future<void> saveById(Map<int, AppOcr> rows) async {
    if (rows.isEmpty) return;
    _byEntry.addAll(rows);
    await localMediaDb.saveAppOcr(rows);
    notifyListeners();
  }

  Future<void> removeById(Set<int> ids) async {
    if (ids.isEmpty) return;
    ids.forEach(_byEntry.remove);
    await localMediaDb.removeAppOcr(ids);
    notifyListeners();
  }

  /// Photos whose text contains [query].
  ///
  /// Hits carry the matching line, so the result grid can explain itself: on a
  /// poster with forty lines, "why is this photo here" has an answer that fits
  /// on one row.
  Future<List<AppOcrHit>> search(String query, {int limit = 200}) =>
      localMediaDb.searchAppOcr(query, limit: limit);
}
