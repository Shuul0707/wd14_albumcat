import 'dart:convert';
import 'dart:typed_data';

import 'package:aves/model/app_vectors.dart';
import 'package:aves/model/entry/entry.dart';
import 'package:aves/services/ai_tagging_service.dart';
import 'package:aves/services/hybrid_ranking.dart';
import 'package:aves/services/on_device_text_encoder.dart';
import 'package:http/http.dart' as http;

/// One search result: the photo and how close it is (cosine, 0..1).
class SemanticHit {
  final AvesEntry entry;
  final double score;

  const SemanticHit(this.entry, this.score);
}

/// One hybrid result: the photo, its final score, and why it scored.
class HybridHit {
  final AvesEntry entry;
  final double score;

  /// 2 = the exact character, 1 = same series, 0 = no character band.
  final int band;
  final double tagScore;
  final double vectorScore;

  /// Tag leaves this photo shares with the query (for the UI).
  final List<String> matched;

  const HybridHit(
    this.entry,
    this.score, {
    this.band = 0,
    this.tagScore = 0,
    this.vectorScore = 0,
    this.matched = const [],
  });
}

/// What one search produced: the phrase as the PC understood it, plus hits.
class HybridResult {
  final ParsedQuery query;
  final List<HybridHit> hits;

  const HybridResult(this.query, this.hits);

  /// Whether any hit landed in the character band.
  bool get hasCharacterBand => hits.any((HybridHit hit) => hit.band > 0);
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

  static String? get activeModel => appVectors.dominantModel;

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

  /// Decode the `vector` block of a `/query` or `/encode_text` reply.
  ///
  /// The wire form is base64 little-endian floats. Half precision is refused
  /// rather than guessed: we always ask for `float32`, so a `float16` reply
  /// means the PC ignored the request and the bytes would decode to noise.
  static Float32List? _decodeVectorBlock(Object? block) {
    if (block == null) return null;
    if (block is Float32List) return block;
    if (block is! Map) return null;
    final dtype = (block['dtype'] ?? 'float32').toString();
    if (dtype != 'float32') return null;
    final dim = (block['dim'] as num?)?.toInt() ?? 0;
    final encoded = block['vector'];
    if (dim <= 0 || encoded is! String || encoded.isEmpty) return null;
    final bytes = base64Decode(encoded);
    if (bytes.length < dim * 4) return null;
    final view = ByteData.sublistView(bytes);
    final vector = Float32List(dim);
    for (var i = 0; i < dim; i++) {
      vector[i] = view.getFloat32(i * 4, Endian.little);
    }
    return vector;
  }

  /// Ask the PC what tags a phrase names, and get a vector for the rest.
  ///
  /// The Chinese name table (nine thousand tags, `1girl` = 女孩/一个女孩) lives
  /// on the PC, so parsing happens there and arrives in one round trip. The
  /// vector is best-effort: a PC without a semantic model still answers with
  /// the mentions, and a PC that is not reachable at all falls back to the
  /// on-device text tower with no mentions — tag matching is lost, but search
  /// is not.
  static Future<ParsedQuery> query(
    String text, {
    required String server,
    String token = '',
    bool withVector = true,
  }) async {
    final trimmed = text.trim();
    if (trimmed.isEmpty) return const ParsedQuery(text: '');

    final base = AiTaggingService.normalizeServer(server);
    Object? failure;
    try {
      final response = await http
          .post(
            Uri.parse('$base/query'),
            headers: {
              'Content-Type': 'application/json; charset=utf-8',
              if (token.isNotEmpty) 'X-Albumcat-Token': token,
            },
            body: utf8.encode(jsonEncode({'text': trimmed, 'vector': withVector, 'dtype': 'float32'})),
          )
          .timeout(_timeout);
      final decoded = jsonDecode(utf8.decode(response.bodyBytes));
      if (decoded is Map) {
        final body = Map<String, dynamic>.from(decoded);
        if (body['ok'] == true) {
          return ParsedQuery.fromJson({
            ...body,
            'vector': _decodeVectorBlock(body['vector']),
          });
        }
        failure = body['error'];
      }
    } catch (error) {
      failure = error;
    }

    // No PC: the phone can still encode the whole phrase itself.
    if (withVector && OnDeviceTextEncoder.isLoaded) {
      final local = await OnDeviceTextEncoder.encode(trimmed);
      if (local != null) {
        return ParsedQuery(text: trimmed, vector: local, vectorText: trimmed);
      }
    }
    throw AiTaggingException(
      '连不上电脑端的查询解析（标签对照表在电脑上）：${failure ?? '未知原因'}',
    );
  }

  /// Rank [entries] against a phrase, using both its tags and its meaning.
  ///
  /// This is the call the search dialog makes. It replaces "vector only":
  /// `黑暗的初音未来` finds the Hatsune Miku photos first (a character band,
  /// decided by tag matching) and orders them by how dark they look (the
  /// vector). A phrase with no character in it weighs tags and vector evenly.
  static Future<HybridResult> searchHybrid(
    String text, {
    required String server,
    required String token,
    required List<AvesEntry> entries,
    int limit = 60,
    double minScore = 0.15,
    RankWeights weights = RankWeights.standard,
  }) async {
    final parsed = await query(text, server: server, token: token);

    // Raw cosines for the whole library: normalising them against the spread of
    // *this* result set is what makes the vector term actually discriminate.
    final raw = <int, double>{};
    final model = activeModel;
    final vector = parsed.vector;
    if (model != null && vector != null) {
      for (final hit in appVectors.nearest(vector, model: model, limit: 1 << 30, minScore: -2)) {
        raw[hit.entryId] = hit.score;
      }
    }

    final candidates = [
      for (final entry in entries) RankCandidate(entryId: entry.id, tags: entry.tags, vectorScore: raw[entry.id]),
    ];
    final ranked = rankCandidates(candidates, parsed, weights: weights);

    final byId = {for (final entry in entries) entry.id: entry};
    final hits = <HybridHit>[];
    for (final r in ranked) {
      final entry = byId[r.entryId];
      if (entry == null) continue;
      final cosine = raw[r.entryId];
      // A character hit is always worth showing, however irrelevant the pixels
      // look; otherwise a photo needs either a tag hit or a real vector hit.
      final relevant = r.band > 0 || r.tagScore > 0 || (cosine != null && cosine >= minScore);
      if (!relevant) continue;
      hits.add(
        HybridHit(
          entry,
          r.score,
          band: r.band,
          tagScore: r.tagScore,
          vectorScore: cosine ?? 0,
          matched: r.matched,
        ),
      );
      if (hits.length >= limit) break;
    }
    return HybridResult(parsed, hits);
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
