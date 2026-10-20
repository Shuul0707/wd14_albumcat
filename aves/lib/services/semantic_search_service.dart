import 'dart:convert';
import 'dart:typed_data';

import 'package:aves/model/app_vectors.dart';
import 'package:aves/model/entry/entry.dart';
import 'package:aves/services/ai_tagging_service.dart';
import 'package:aves/services/on_device_text_encoder.dart';
import 'package:http/http.dart' as http;

/// One search result: the photo and how close it is (cosine, 0..1).
class SemanticHit {
  final AvesEntry entry;
  final double score;

  const SemanticHit(this.entry, this.score);
}

/// Turns a Chinese phrase into a vector and ranks the library against it.
///
/// The image tower has already run (during tagging), so a query only needs the
/// *text* tower — one short forward pass, either on the PC via `/encode_text`
/// or, later, on the phone itself. Ranking is a matrix-vector product over
/// [AppVectors], which is why it stays responsive on ten thousand photos.
class SemanticSearchService {
  static const _timeout = Duration(seconds: 30);

  /// `true` when there is anything to search at all.
  static bool get hasVectors => appVectors.entryCount > 0;

  static String? get activeModel => appVectors.models.isEmpty ? null : appVectors.models.first;

  /// Encode one phrase.
  ///
  /// Prefers the on-device text tower when it is installed (no network, works
  /// with the PC off); otherwise asks the PC for a vector in float32 so it
  /// drops straight into a [Float32List] without a half-float decoder.
  static Future<Float32List> encodeQuery(
    String text, {
    required String server,
    String token = '',
    bool preferOnDevice = true,
  }) async {
    if (preferOnDevice && OnDeviceTextEncoder.isLoaded) {
      final local = await OnDeviceTextEncoder.encode(text);
      if (local != null) return local;
    }

    final base = AiTaggingService.normalizeServer(server);
    final http.Response response;
    try {
      response = await http
          .post(
            Uri.parse('$base/encode_text'),
            headers: {
              'Content-Type': 'application/json; charset=utf-8',
              if (token.isNotEmpty) 'X-Albumcat-Token': token,
            },
            body: utf8.encode(jsonEncode({'text': text, 'dtype': 'float32'})),
          )
          .timeout(_timeout);
    } catch (error) {
      throw AiTaggingException('连不上电脑端的语义编码服务：${AiTaggingService.describeError(error)}');
    }

    Map<String, dynamic> body;
    try {
      final decoded = jsonDecode(utf8.decode(response.bodyBytes));
      if (decoded is! Map) throw const FormatException('expected an object');
      body = Map<String, dynamic>.from(decoded);
    } catch (error) {
      throw AiTaggingException('电脑端返回的内容看不懂：$error');
    }

    if (response.statusCode == 503 || body['ok'] != true) {
      throw AiTaggingException('${body['error'] ?? '语义模型没有加载'}');
    }

    final dim = (body['dim'] as num?)?.toInt() ?? 0;
    final encoded = body['vector'] as String? ?? '';
    if (dim <= 0 || encoded.isEmpty) {
      throw AiTaggingException('电脑端没有返回向量（检查 [semantic] 配置）');
    }

    final bytes = base64Decode(encoded);
    if (bytes.length < dim * 4) {
      throw AiTaggingException('向量长度不对：$dim 维应有 ${dim * 4} 字节，实际 ${bytes.length}');
    }
    final view = ByteData.sublistView(bytes);
    final vector = Float32List(dim);
    for (var i = 0; i < dim; i++) {
      vector[i] = view.getFloat32(i * 4, Endian.little);
    }
    return vector;
  }

  /// Rank [entries] by similarity to [text].
  static Future<List<SemanticHit>> search(
    String text, {
    required String server,
    required String token,
    required List<AvesEntry> entries,
    int limit = 60,
    double minScore = 0.15,
  }) async {
    final model = activeModel;
    if (model == null) {
      throw AiTaggingException('还没有任何语义向量。先跑一次 AI 打标签（会顺带建向量）。');
    }
    final query = await encodeQuery(text, server: server, token: token);
    final hits = appVectors.nearest(query, model: model, limit: limit, minScore: minScore);

    final byId = {for (final entry in entries) entry.id: entry};
    return [
      for (final hit in hits)
        if (byId[hit.entryId] != null) SemanticHit(byId[hit.entryId]!, hit.score),
    ];
  }
}
