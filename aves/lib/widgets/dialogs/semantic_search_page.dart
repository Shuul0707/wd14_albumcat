import 'package:aves/model/app_vectors.dart';
import 'package:aves/model/entry/entry.dart';
import 'package:aves/services/ai_tagging_service.dart';
import 'package:aves/services/hybrid_ranking.dart';
import 'package:aves/services/on_device_text_encoder.dart';
import 'package:aves/services/semantic_search_service.dart';
import 'package:material_ui/material_ui.dart';

/// Free-form search: type "黑暗的初音未来" and get the dark Miku photos first.
///
/// Two signals, one ranking. The PC parses the phrase into WD14 tags (the
/// Chinese name table lives there), then [SemanticSearchService.searchHybrid]
/// combines those tags with the query's meaning: a named character puts its
/// photos in a band above everything else, and the leftover words ("黑暗") order
/// the photos inside that band. With no character in the phrase the tags and
/// the vector weigh the same.
///
/// Deliberately not wired into Aves' own search bar: that one is a *filter*
/// (tags, dates, albums) with instant per-keystroke semantics, while this is a
/// ranking that needs a network round trip for the text tower. Mixing the two
/// would make the normal search feel slow.
class SemanticSearchPage extends StatefulWidget {
  final List<AvesEntry> entries;
  final String server;
  final String token;

  const SemanticSearchPage({
    super.key,
    required this.entries,
    required this.server,
    required this.token,
  });

  @override
  State<SemanticSearchPage> createState() => _SemanticSearchPageState();
}

class _SemanticSearchPageState extends State<SemanticSearchPage> {
  final _controller = TextEditingController();
  List<HybridHit> _hits = const [];
  ParsedQuery? _parsed;
  bool _running = false;
  String? _error;
  bool _searched = false;

  /// true once the on-device text tower is loaded (search then works offline)
  bool _local = false;
  bool _installing = false;
  double _progress = 0;

  @override
  void initState() {
    super.initState();
    _initLocalEncoder();
  }

  Future<void> _initLocalEncoder() async {
    if (!await OnDeviceTextEncoder.isInstalled()) return;
    final ok = await OnDeviceTextEncoder.load();
    if (!mounted) return;
    setState(() => _local = ok);
  }

  Future<void> _installLocalEncoder() async {
    setState(() {
      _installing = true;
      _progress = 0;
      _error = null;
    });
    OnDeviceTextEncoder.onInstallProgress = (value) {
      if (mounted) setState(() => _progress = value);
    };
    try {
      await OnDeviceTextEncoder.install(server: widget.server, token: widget.token);
      if (!mounted) return;
      setState(() => _local = OnDeviceTextEncoder.isLoaded);
    } catch (error) {
      if (!mounted) return;
      setState(() => _error = AiTaggingService.describeError(error));
    } finally {
      OnDeviceTextEncoder.onInstallProgress = null;
      if (mounted) setState(() => _installing = false);
    }
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  /// Re-rank by one result's own vector: "more like this".
  ///
  /// No text encoding involved, so it stays responsive and works with the
  /// computer off — the only cost is the first matrix build.
  void _similarTo(HybridHit hit) {
    final byId = {for (final entry in widget.entries) entry.id: entry};
    final hits = appVectors.similarTo(hit.entry.id, limit: 60);
    setState(() {
      _hits = [
        for (final neighbour in hits)
          if (byId[neighbour.entryId] != null)
            HybridHit(byId[neighbour.entryId]!, neighbour.score, vectorScore: neighbour.score),
      ];
      _parsed = null;
      _searched = true;
      _error = null;
    });
  }

  Future<void> _search() async {
    final text = _controller.text.trim();
    if (text.isEmpty || _running) return;

    setState(() {
      _running = true;
      _error = null;
    });
    try {
      final result = await SemanticSearchService.searchHybrid(
        text,
        server: widget.server,
        token: widget.token,
        entries: widget.entries,
        limit: 60,
      );
      if (!mounted) return;
      setState(() {
        _hits = result.hits;
        _parsed = result.query;
        _searched = true;
      });
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _error = AiTaggingService.describeError(error);
        _hits = const [];
        _parsed = null;
        _searched = true;
      });
    } finally {
      if (mounted) setState(() => _running = false);
    }
  }

  /// "初音未来（角色）· 黑暗" — what the ranking actually went on.
  String? _understood() {
    final parsed = _parsed;
    if (parsed == null || parsed.mentions.isEmpty) return null;
    final parts = <String>[
      for (final mention in parsed.mentions) '${mention.display}${_kindSuffix(mention.kind)}',
    ];
    final meaning = parsed.vectorText.trim();
    // A character nobody has is not a ranking signal: say so instead of
    // implying the (empty) character band did something.
    final bandEmpty = parsed.hasCharacter && !_hits.any((hit) => hit.band > 0);
    final String tail;
    if (bandEmpty) {
      tail = meaning.isNotEmpty
          ? '，相册里没有这些角色的标签，改按「$meaning」和画面相似度排序'
          : '，相册里没有这些角色的标签，按画面相似度排序';
    } else if (parsed.hasCharacter && meaning.isNotEmpty) {
      // With a character the leftover words only order inside the band.
      tail = '，再按「$meaning」排序';
    } else {
      tail = '';
    }
    return '识别到 ${parts.join(' · ')}$tail';
  }

  static String _kindSuffix(MentionKind kind) => switch (kind) {
        MentionKind.character => '（角色）',
        MentionKind.series => '（系列）',
        _ => '',
      };

  /// "角色 ｜ 角色|Hatsune Miku" — why this photo is here.
  String? _why(HybridHit hit) {
    final parts = <String>[];
    // Only one band exists below the rest: photos carrying the named
    // character. Same-series credit was dropped when the ranking was
    // simplified, so there is no "同系列" line to print.
    if (hit.band >= 1) {
      parts.add('角色');
    }
    if (hit.matched.isNotEmpty) {
      parts.add(hit.matched.join('·'));
    }
    if (parts.isEmpty) return null;
    return parts.join(' ｜ ');
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final total = appVectors.entryCount;
    final understood = _understood();

    return AlertDialog(
      title: const Text('语义搜索'),
      content: SizedBox(
        width: 520,
        height: 480,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              total == 0
                  ? '还没有语义向量：只能按标签匹配（跑一次 AI 打标签，向量会顺带建好）。'
                  : '已为 $total 张照片建立向量。试试「沙滩」「黑暗的初音未来」「雨天的教室」。',
              style: theme.textTheme.bodySmall,
            ),
            const SizedBox(height: 6),
            Row(
              children: [
                Icon(
                  _local ? Icons.offline_bolt : Icons.cloud_queue,
                  size: 14,
                  color: _local ? theme.colorScheme.primary : theme.textTheme.bodySmall?.color,
                ),
                const SizedBox(width: 4),
                Expanded(
                  child: Text(
                    _local ? '本机编码：断网也能搜（标签解析仍需电脑）' : '电脑编码：需要电脑在线',
                    style: theme.textTheme.bodySmall,
                  ),
                ),
                if (!_local)
                  TextButton(
                    onPressed: _installing ? null : _installLocalEncoder,
                    child: Text(
                      _installing ? '下载中 ${(_progress * 100).round()}%' : '下载到本机',
                    ),
                  ),
              ],
            ),
            const SizedBox(height: 6),
            Row(
              children: [
                Expanded(
                  child: TextField(
                    controller: _controller,
                    autofocus: true,
                    enabled: !_running,
                    decoration: const InputDecoration(
                      hintText: '描述你想找的画面，也可以带角色名',
                      isDense: true,
                      border: OutlineInputBorder(),
                    ),
                    onSubmitted: (_) => _search(),
                  ),
                ),
                const SizedBox(width: 8),
                FilledButton(
                  onPressed: _running ? null : _search,
                  child: Text(_running ? '搜索中…' : '搜索'),
                ),
              ],
            ),
            if (understood != null)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Text(
                  understood,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.primary),
                ),
              ),
            const SizedBox(height: 12),
            if (_error != null)
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: Text(_error!, style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.error)),
              ),
            Expanded(
              child: _hits.isEmpty
                  ? Center(
                      child: Text(
                        _searched ? '没有足够相似的照片' : '输入一句话开始',
                        style: theme.textTheme.bodySmall,
                      ),
                    )
                  : ListView.separated(
                      itemCount: _hits.length,
                      separatorBuilder: (_, _) => const Divider(height: 1),
                      itemBuilder: (context, index) {
                        final hit = _hits[index];
                        final why = _why(hit);
                        return ListTile(
                          dense: true,
                          title: Text(
                            hit.entry.bestTitle ?? '',
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                          ),
                          subtitle: why == null
                              ? null
                              : Text(why, maxLines: 1, overflow: TextOverflow.ellipsis, style: theme.textTheme.bodySmall),
                          trailing: Row(
                            mainAxisSize: MainAxisSize.min,
                            children: [
                              Text(
                                hit.score.toStringAsFixed(2),
                                style: theme.textTheme.bodySmall?.copyWith(
                                  fontFeatures: const [FontFeature.tabularFigures()],
                                ),
                              ),
                              IconButton(
                                icon: const Icon(Icons.center_focus_strong, size: 18),
                                tooltip: '找相似的',
                                visualDensity: VisualDensity.compact,
                                onPressed: () => _similarTo(hit),
                              ),
                            ],
                          ),
                        );
                      },
                    ),
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('关闭'),
        ),
      ],
    );
  }
}
