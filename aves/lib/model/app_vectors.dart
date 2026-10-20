import 'dart:math' as math;
import 'dart:typed_data';

import 'package:aves/model/app_vector.dart';
import 'package:aves/services/common/services.dart';
import 'package:flutter/foundation.dart';

final AppVectors appVectors = AppVectors._private();

/// Semantic vectors for the whole library, kept in memory as one matrix.
///
/// Ten thousand photos at 512 dimensions is 20 MB of float32 — small enough to
/// hold, and holding it is what makes a query a single matrix-vector product
/// (a few milliseconds) instead of ten thousand database round trips.
///
/// The matrix is rebuilt lazily and invalidated on every write, so a tagging
/// run that finishes mid-search cannot leave the two out of sync.
class AppVectors with ChangeNotifier {
  Map<int, AppVector> _byEntry = {};

  int _generation = 0;
  int get generation => _generation;

  /// Model the cached matrix was built for; `null` = not built yet.
  String? _matrixModel;
  Float32List? _matrix;
  List<int> _matrixIds = const [];
  int _dim = 0;

  /// How many photos have a vector at all.
  int get entryCount => _byEntry.length;

  /// Distinct model ids present, so the UI can warn about a half-migrated store.
  Set<String> get models => _byEntry.values.map((v) => v.model).toSet();

  /// The model most rows belong to.
  ///
  /// After swapping the image tower the store is briefly a mix of old and new
  /// vectors. Searching must pick *one* space — comparing a query against both
  /// would rank half the library by noise — so we go with the majority and the
  /// stragglers simply do not appear until they are re-encoded.
  String? get dominantModel {
    if (_byEntry.isEmpty) return null;
    final counts = <String, int>{};
    for (final vector in _byEntry.values) {
      counts[vector.model] = (counts[vector.model] ?? 0) + 1;
    }
    var best = counts.entries.first;
    for (final entry in counts.entries) {
      if (entry.value > best.value) best = entry;
    }
    return best.key;
  }

  new _private() {
    if (kFlutterMemoryAllocationsEnabled) ChangeNotifier.maybeDispatchObjectCreation(this);
  }

  Future<void> init() async {
    _byEntry = await localMediaDb.loadAppVectors();
    _invalidate();
  }

  AppVector? of(int entryId) => _byEntry[entryId];

  Future<void> saveById(Map<int, AppVector> rows) async {
    if (rows.isEmpty) return;
    // A row from another model replaces the old one: keeping both would let
    // `nearest` compare incompatible spaces (and quietly rank by noise).
    _byEntry.addAll(rows);
    await localMediaDb.saveAppVectors(rows);
    _invalidate();
    notifyListeners();
  }

  Future<void> removeIds(Set<int> entryIds) async {
    final removed = entryIds.where(_byEntry.containsKey).toSet();
    if (removed.isEmpty) return;

    await localMediaDb.removeAppVectors(removed);
    removed.forEach(_byEntry.remove);
    _invalidate();
    notifyListeners();
  }

  Future<void> clear() async {
    await localMediaDb.clearAppVectors();
    _byEntry.clear();
    _invalidate();
    notifyListeners();
  }

  /// Cosine top-k across every vector belonging to [model].
  ///
  /// Vectors are unit-norm on both sides, so a dot product *is* the cosine;
  /// the norms are divided out anyway, because a half-precision round trip can
  /// leave one slightly off and a plain dot product would then favour long
  /// vectors.
  List<VectorHit> nearest(
    Float32List query, {
    required String model,
    int limit = 60,
    double minScore = 0.15,
  }) {
    _ensureMatrix(model);
    final matrix = _matrix;
    if (matrix == null || _matrixIds.isEmpty || _dim != query.length) {
      return const [];
    }

    var queryNorm = 0.0;
    for (var i = 0; i < _dim; i++) {
      queryNorm += query[i] * query[i];
    }
    queryNorm = queryNorm <= 0 ? 1.0 : math.sqrt(queryNorm);

    final hits = <VectorHit>[];
    for (var row = 0; row < _matrixIds.length; row++) {
      final base = row * _dim;
      var dot = 0.0;
      var norm = 0.0;
      for (var i = 0; i < _dim; i++) {
        final v = matrix[base + i];
        dot += v * query[i];
        norm += v * v;
      }
      final score = dot / (queryNorm * (norm <= 0 ? 1.0 : math.sqrt(norm)));
      if (score >= minScore) {
        hits.add(VectorHit(_matrixIds[row], score));
      }
    }

    hits.sort((a, b) => b.score.compareTo(a.score));
    return hits.length > limit ? hits.sublist(0, limit) : hits;
  }

  /// Rank the library by similarity to one entry's own vector.
  ///
  /// This is the cheap half of search: the query vector already exists, so it
  /// is a matrix-vector product with no encoding step, no network and no
  /// model — which is why it works even when the PC is off.
  List<VectorHit> similarTo(int entryId, {int limit = 60, double minScore = 0.15}) {
    final vector = _byEntry[entryId];
    if (vector == null) return const [];
    final query = vector.decode();
    final hits = nearest(query, model: vector.model, limit: limit + 1, minScore: minScore);
    return hits.where((hit) => hit.entryId != entryId).take(limit).toList();
  }

  // -- internals ------------------------------------------------------- #

  void _invalidate() {
    _matrixModel = null;
    _matrix = null;
    _matrixIds = const [];
    _dim = 0;
    _generation++;
  }

  void _ensureMatrix(String model) {
    if (_matrix != null && _matrixModel == model) return;

    final ids = <int>[];
    final encoded = <AppVector>[];
    _byEntry.forEach((id, vector) {
      if (vector.model == model) {
        ids.add(id);
        encoded.add(vector);
      }
    });

    if (ids.isEmpty) {
      _matrixModel = model;
      _matrix = Float32List(0);
      _matrixIds = const [];
      _dim = 0;
      return;
    }

    final dim = encoded.first.dim;
    final matrix = Float32List(ids.length * dim);
    for (var row = 0; row < encoded.length; row++) {
      final vector = encoded[row];
      if (vector.dim != dim) continue; // skip a stray row instead of crashing
      final values = vector.decode();
      matrix.setRange(row * dim, row * dim + dim, values);
    }

    _matrixModel = model;
    _matrix = matrix;
    _matrixIds = ids;
    _dim = dim;
  }
}
