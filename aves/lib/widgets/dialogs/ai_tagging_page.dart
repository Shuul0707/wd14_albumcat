import 'dart:math';

import 'package:aves/model/app_tags.dart';
import 'package:aves/model/app_vector.dart';
import 'package:aves/model/app_vectors.dart';
import 'package:aves/model/entry/entry.dart';
import 'package:aves/model/entry/extensions/props.dart';
import 'package:aves/model/settings/settings.dart';
import 'package:aves/services/ai_tagging_service.dart';
import 'package:material_ui/material_ui.dart';

/// Runs the external tagger over [entries] and stores the answers in Aves' own
/// database. Nothing here touches the media files.
class AiTaggingPage extends StatefulWidget {
  final List<AvesEntry> entries;

  const new({super.key, required this.entries});

  @override
  State<AiTaggingPage> createState() => _AiTaggingPageState();
}

class _AiTaggingPageState extends State<AiTaggingPage> {
  late final TextEditingController _serverController;
  late final TextEditingController _tokenController;

  bool get _includeGeneral => settings.aiTaggingIncludeGeneral;
  bool get _charactersOnly => !_includeGeneral;

  /// the stored value is only trusted when the dropdown knows it
  int get _concurrency =>
      const [1, 2, 4, 8].contains(settings.aiTaggingConcurrency) ? settings.aiTaggingConcurrency : 4;

  bool _skipTagged = true;
  bool _running = false;
  bool _stopRequested = false;
  int _done = 0;
  int _total = 0;
  final List<String> _log = [];

  List<AvesEntry> get _images => widget.entries.where((e) => e.isImage).toList();

  @override
  void initState() {
    super.initState();
    _serverController = TextEditingController(text: settings.aiTaggingServer);
    _tokenController = TextEditingController(text: settings.aiTaggingToken);
  }

  @override
  void dispose() {
    _serverController.dispose();
    _tokenController.dispose();
    super.dispose();
  }

  void _say(String message) {
    if (!mounted) return;
    setState(() {
      _log.insert(0, message);
      if (_log.length > 200) _log.removeRange(200, _log.length);
    });
  }

  Future<void> _testConnection() async {
    settings.aiTaggingServer = _serverController.text.trim();
    settings.aiTaggingToken = _tokenController.text.trim();
    setState(_log.clear);
    try {
      final (base, viaUsb) = await AiTaggingService.resolveReachable(
        settings.aiTaggingServer,
        token: settings.aiTaggingToken,
      );
      _say(viaUsb ? '走 USB（数据线）：$base' : '走局域网：$base');
      final health = await AiTaggingService.health(base, token: settings.aiTaggingToken);
      _say('连上了：$health');
    } catch (error) {
      _say('连不上：${AiTaggingService.describeError(error)}');
    }
  }

  Future<void> _run() async {
    settings.aiTaggingServer = _serverController.text.trim();
    settings.aiTaggingToken = _tokenController.text.trim();
    final entries = _images.where((e) => !_skipTagged || appTags.tagsOf(e.id).isEmpty).toList();
    if (entries.isEmpty) {
      _say('没有需要处理的图片。');
      return;
    }
    setState(() {
      _running = true;
      _stopRequested = false;
      _done = 0;
      _total = entries.length;
      _log.clear();
    });
    _say('开始处理 ${entries.length} 张图片…');

    // exactly the 448 px WD14 eats: a bigger thumbnail is only decoded, encoded
    // and shipped so the server can throw the extra pixels away. The extent is
    // in DIP and the platform multiplies it back by the ratio, hence the division.
    final extentDip = max(1.0, 448 / MediaQuery.of(context).devicePixelRatio);
    var tagged = 0, empty = 0, failed = 0;
    final stats = AiTaggingStats();
    try {
      // What the user typed first, then the USB tunnel: with the cable plugged
      // in (`tools\phone_link.cmd`) the run works with no Wi-Fi at all, without
      // anyone having to retype the address.
      final (base, viaUsb) = await AiTaggingService.resolveReachable(
        settings.aiTaggingServer,
        token: settings.aiTaggingToken,
      );
      _say(viaUsb ? '电脑端在数据线上：$base' : '电脑端：$base');
      // Saved slice by slice instead of at the end: an interrupted run (phone
      // killed, cable pulled, computer gone) then keeps every tag the computer
      // had already answered, which is the difference between losing a batch and
      // losing the session.
      Future<void> save(List<AiTaggingOutcome> outcomes) async {
        final writes = <int, Set<String>>{};
        final vectors = <int, AppVector>{};
        for (final outcome in outcomes) {
          if (!outcome.ok) {
            failed++;
            continue;
          }
          final vector = outcome.vector;
          if (vector != null) vectors[outcome.entryId] = vector;
          final selection = outcome.selectTags(includeGeneral: _includeGeneral);
          if (selection.isEmpty) {
            empty++;
            continue;
          }
          writes[outcome.entryId] = selection;
          tagged++;
        }
        if (writes.isNotEmpty) await appTags.setTagsById(writes);
        // Vectors are stored even when no tag was published: a photo the tagger
        // had nothing to say about is still searchable by meaning.
        if (vectors.isNotEmpty) await appVectors.saveById(vectors);
      }

      await AiTaggingService.tag(
        entries,
        server: base,
        token: settings.aiTaggingToken,
        batchSize: settings.aiTaggingBatchSize,
        concurrency: settings.aiTaggingConcurrency,
        extentDip: extentDip,
        stats: stats,
        isCancelled: () => _stopRequested,
        onNotice: _say,
        onOutcomes: save,
        onProgress: (done, total) {
          if (mounted) setState(() => _done = done);
        },
      );
      _say('完成：$tagged 张写了标签，$empty 张没认出来，$failed 张失败。');
      _say(stats.describe());
      debugPrint('albumcat-stats: ${stats.describe()}');
      if (_stopRequested) _say('（中途停止，已经保存的标签都留着了）');
    } catch (error) {
      _say('出错：${AiTaggingService.describeError(error)}');
      if (tagged > 0 || failed > 0) {
        _say('已经存下来的：$tagged 张写了标签，$empty 张没认出来，$failed 张失败。');
      }
    } finally {
      if (mounted) setState(() => _running = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('AI 打标签')),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Text('电脑端地址', style: theme.textTheme.labelMedium),
          TextField(
            controller: _serverController,
            keyboardType: TextInputType.url,
            autocorrect: false,
            decoration: const InputDecoration(hintText: 'http://192.168.1.10:8899'),
            onChanged: (v) => settings.aiTaggingServer = v.trim(),
          ),
          const SizedBox(height: 12),
          Text('令牌（电脑端没设 --token 就留空）', style: theme.textTheme.labelMedium),
          TextField(
            controller: _tokenController,
            autocorrect: false,
            decoration: const InputDecoration(hintText: '可留空'),
            onChanged: (v) => settings.aiTaggingToken = v.trim(),
          ),
          const SizedBox(height: 8),
          Row(
            children: [
              OutlinedButton(
                onPressed: _running ? null : _testConnection,
                child: const Text('测试连接'),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  '把电脑上的 tools\\albumcat.cmd serve 跑起来',
                  style: theme.textTheme.bodySmall,
                ),
              ),
            ],
          ),
          const Divider(height: 32),
          CheckboxListTile(
            controlAffinity: ListTileControlAffinity.leading,
            contentPadding: EdgeInsets.zero,
            dense: true,
            value: _charactersOnly,
            onChanged: _running
                ? null
                : (v) => setState(() {
                      settings.aiTaggingIncludeGeneral = v != true;
                    }),
            title: const Text('只标角色（推荐）'),
            subtitle: const Text('关掉则连场景、服饰等普通标签一起标'),
          ),
          CheckboxListTile(
            controlAffinity: ListTileControlAffinity.leading,
            contentPadding: EdgeInsets.zero,
            dense: true,
            value: _skipTagged,
            onChanged: _running ? null : (v) => setState(() => _skipTagged = v == true),
            title: const Text('跳过已经有标签的图片'),
          ),
          Row(
            children: [
              Expanded(child: Text('同时取几张缩略图', style: theme.textTheme.bodyMedium)),
              DropdownButton<int>(
                value: _concurrency,
                onChanged: _running ? null : (v) => setState(() => settings.aiTaggingConcurrency = v ?? 4),
                items: const [1, 2, 4, 8].map((v) => DropdownMenuItem<int>(value: v, child: Text('$v'))).toList(),
              ),
            ],
          ),
          const SizedBox(height: 8),
          if (_running)
            Column(
              children: [
                LinearProgressIndicator(value: _total == 0 ? null : _done / _total),
                const SizedBox(height: 4),
                Text('$_done / $_total'),
              ],
            ),
          const SizedBox(height: 8),
          Row(
            children: [
              OutlinedButton(
                onPressed: _running ? null : _run,
                child: Text(_running ? '正在打标签…' : '开始打标签'),
              ),
              const SizedBox(width: 8),
              OutlinedButton(
                onPressed: _running ? () => setState(() => _stopRequested = true) : null,
                child: const Text('停止'),
              ),
            ],
          ),
          const Divider(height: 32),
          SizedBox(
            height: 260,
            child: Container(
              width: double.infinity,
              padding: const EdgeInsets.all(8),
              decoration: BoxDecoration(
                border: Border.all(color: theme.dividerColor),
                borderRadius: BorderRadius.circular(4),
              ),
              child: SingleChildScrollView(
                child: Text(
                  _log.isEmpty ? '（还没有输出）' : _log.join('\n'),
                  style: theme.textTheme.bodySmall?.copyWith(fontFamily: 'monospace'),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
