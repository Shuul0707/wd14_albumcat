import 'dart:convert';
import 'dart:typed_data';

/// One photo's Chinese-CLIP embedding.
///
/// Kept beside [AppTags] and for the same reason: the vector is *derived*
/// data. It is never written into the media file (that would add ~1 KB to
/// every JPEG and Aves would not read it anyway), and it can always be rebuilt
/// by tagging the photo again.
///
/// [model] is stored per row on purpose. Swap the image tower and the old
/// vectors are silently meaningless — comparing a vector from model A with a
/// query from model B produces numbers that look like scores but rank noise.
/// Callers filter on [model] instead of trusting that the store is uniform.
class AppVector {
  final String model;
  final int dim;

  /// base64 of little-endian float32, exactly as `/plan` returns it when the
  /// phone asks for `vector_dtype: float32`.
  final String data;

  /// seconds since epoch, so a re-index can be incremental later
  final int updated;

  const AppVector({
    required this.model,
    required this.dim,
    required this.data,
    required this.updated,
  });

  factory AppVector.fromJson(Map<String, dynamic> json, {String? fallbackModel}) {
    final model = (json['model'] as String?) ?? fallbackModel ?? '';
    final dim = (json['dim'] as num?)?.toInt() ?? 0;
    final vector = json['vector'] as String? ?? '';
    if (model.isEmpty || dim <= 0 || vector.isEmpty) {
      throw const FormatException('semantic vector is incomplete');
    }
    return AppVector(
      model: model,
      dim: dim,
      data: vector,
      updated: DateTime.now().millisecondsSinceEpoch ~/ 1000,
    );
  }

  Float32List decode() {
    final bytes = base64Decode(data);
    if (bytes.length < dim * 4) {
      throw FormatException('vector payload is ${bytes.length} bytes, expected ${dim * 4}');
    }
    final view = ByteData.sublistView(bytes);
    final out = Float32List(dim);
    for (var i = 0; i < dim; i++) {
      out[i] = view.getFloat32(i * 4, Endian.little);
    }
    return out;
  }

  @override
  String toString() => 'AppVector($model, dim: $dim, ${data.length} chars)';
}

/// A hit from [AppVectors.nearest]: the entry, and how close it is (cosine).
class VectorHit {
  final int entryId;
  final double score;

  const VectorHit(this.entryId, this.score);

  @override
  String toString() => 'VectorHit($entryId, ${score.toStringAsFixed(3)})';
}
