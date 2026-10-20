import 'dart:io';
import 'dart:typed_data';

import 'package:aves/services/ai_tagging_service.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;

/// Chinese-CLIP's *text* tower, running on the phone.
///
/// The image tower has to stay on the PC (a ViT, seconds per photo); the text
/// tower only encodes one short sentence per query, so it fits on the device.
/// With it installed, semantic search works with the computer switched off.
///
/// The model is **not** bundled in the APK: ~100 MB of weights would dwarf the
/// rest of the app, and the repo's rule is that weights never get committed.
/// It is downloaded once from the PC (`GET /semantic/model`) into app-private
/// storage, then reused.
class OnDeviceTextEncoder {
  static const channel = MethodChannel('deckers.thibault/aves/text_encoder');

  static const modelFile = 'text_encoder.onnx';
  static const vocabFile = 'vocab.txt';

  /// Whole-download progress, 0..1 (the model is ~100 MB after int8).
  static void Function(double progress)? onInstallProgress;

  static bool _loaded = false;
  static int _dim = 0;

  static bool get isLoaded => _loaded;
  static int get dim => _dim;

  /// App-private folder the native side created (`files/semantic`).
  static Future<String?> modelDir() async {
    try {
      return await channel.invokeMethod<String>('dir');
    } catch (_) {
      return null;
    }
  }

  static Future<bool> isInstalled() async {
    final dir = await modelDir();
    if (dir == null) return false;
    return File('$dir/$modelFile').existsSync() && File('$dir/$vocabFile').existsSync();
  }

  /// Loads both files into memory. Safe to call repeatedly.
  static Future<bool> load() async {
    try {
      final result = await channel.invokeMapMethod<String, dynamic>('load');
      _loaded = result?['ok'] == true;
      _dim = (result?['dim'] as num?)?.toInt() ?? 0;
      return _loaded;
    } catch (_) {
      _loaded = false;
      _dim = 0;
      return false;
    }
  }

  /// Returns null when the tower is not loaded or the call fails — callers
  /// then fall back to the PC's `/encode_text`.
  static Future<Float32List?> encode(String text) async {
    if (!_loaded || text.trim().isEmpty) return null;
    try {
      final result = await channel.invokeMapMethod<String, dynamic>('encode', {'text': text});
      if (result?['ok'] != true) return null;
      final vector = result?['vector'];
      if (vector is Float32List) return vector;
      if (vector is List) return Float32List.fromList(vector.map((v) => (v as num).toDouble()).toList());
      return null;
    } catch (_) {
      return null;
    }
  }

  /// Downloads the two files from the PC, then loads them.
  ///
  /// Resumable on purpose: the model is big enough that a phone call or a
  /// screen lock will interrupt it, and re-downloading 100 MB because of that
  /// is a bad trade. A partial file is kept as `.part` and only renamed once
  /// the byte count matches.
  static Future<void> install({required String server, String token = ''}) async {
    final dir = await modelDir();
    if (dir == null) {
      throw AiTaggingException('拿不到 App 私有目录');
    }
    final base = AiTaggingService.normalizeServer(server);

    await _download('$base/semantic/model?file=$modelFile', File('$dir/$modelFile'), token);
    await _download('$base/semantic/model?file=$vocabFile', File('$dir/$vocabFile'), token);

    final ok = await load();
    if (!ok) {
      throw AiTaggingException('模型下载完成但加载失败（可能导出时 opset 不兼容）');
    }
  }

  static Future<void> _download(String url, File dest, String token) async {
    final client = http.Client();
    final part = File('${dest.path}.part');
    try {
      final request = http.Request('GET', Uri.parse(url));
      if (token.isNotEmpty) request.headers['X-Albumcat-Token'] = token;
      final response = await client.send(request).timeout(const Duration(seconds: 30));

      if (response.statusCode != 200) {
        throw AiTaggingException('下载 ${dest.uri.pathSegments.last} 失败：HTTP ${response.statusCode}');
      }

      final total = response.contentLength ?? 0;
      var received = 0;
      final sink = part.openWrite();
      try {
        await for (final chunk in response.stream) {
          sink.add(chunk);
          received += chunk.length;
          if (total > 0) onInstallProgress?.call(received / total);
        }
      } finally {
        await sink.flush();
        await sink.close();
      }

      if (total > 0 && received != total) {
        throw AiTaggingException('下载不完整：$received / $total 字节');
      }
      if (dest.existsSync()) dest.deleteSync();
      part.renameSync(dest.path);
    } finally {
      client.close();
    }
  }
}
