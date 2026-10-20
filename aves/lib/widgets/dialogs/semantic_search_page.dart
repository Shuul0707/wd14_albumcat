import 'dart:ui' show FontFeature;

import 'package:aves/model/app_vectors.dart';
import 'package:aves/model/entry/entry.dart';
import 'package:aves/services/ai_tagging_service.dart';
import 'package:aves/services/semantic_search_service.dart';
import 'package:material_ui/material_ui.dart';

/// Free-form search: type "沙滩" and get the photos that look like it.
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
  List<SemanticHit> _hits = const [];
  bool _running = false;
  String? _error;
  bool _searched = false;

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  Future<void> _search() async {
    final text = _controller.text.trim();
    if (text.isEmpty || _running) return;

    setState(() {
      _running = true;
      _error = null;
    });
    try {
      final hits = await SemanticSearchService.search(
        text,
        server: widget.server,
        token: widget.token,
        entries: widget.entries,
        limit: 60,
      );
      if (!mounted) return;
      setState(() {
        _hits = hits;
        _searched = true;
      });
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _error = AiTaggingService.describeError(error);
        _hits = const [];
        _searched = true;
      });
    } finally {
      if (mounted) setState(() => _running = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final total = appVectors.entryCount;

    return AlertDialog(
      title: const Text('语义搜索'),
      content: SizedBox(
        width: 520,
        height: 460,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              total == 0
                  ? '还没有语义向量：先跑一次 AI 打标签，向量会在同一趟里建好。'
                  : '已为 $total 张照片建立向量。试试「沙滩」「雨天的街道」「教室」。',
              style: theme.textTheme.bodySmall,
            ),
            const SizedBox(height: 12),
            Row(
              children: [
                Expanded(
                  child: TextField(
                    controller: _controller,
                    autofocus: true,
                    enabled: !_running,
                    decoration: const InputDecoration(
                      hintText: '描述你想找的画面',
                      isDense: true,
                      border: OutlineInputBorder(),
                    ),
                    onSubmitted: (_) => _search(),
                  ),
                ),
                const SizedBox(width: 8),
                FilledButton(
                  onPressed: _running || total == 0 ? null : _search,
                  child: Text(_running ? '搜索中…' : '搜索'),
                ),
              ],
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
                      separatorBuilder: (_, __) => const Divider(height: 1),
                      itemBuilder: (context, index) {
                        final hit = _hits[index];
                        return ListTile(
                          dense: true,
                          title: Text(
                            hit.entry.bestTitle,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                          ),
                          trailing: Text(
                            hit.score.toStringAsFixed(3),
                            style: theme.textTheme.bodySmall?.copyWith(
                              fontFeatures: const [FontFeature.tabularFigures()],
                            ),
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
