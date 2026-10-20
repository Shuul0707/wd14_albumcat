import 'package:aves/services/hybrid_ranking.dart';
import 'package:aves/theme/icons.dart';
import 'package:material_ui/material_ui.dart';

/// The one line the app bar shows above a semantic result grid.
///
/// A search that went through the tag table and the vector is not obviously
/// different from a plain filter: the photos are the same photos, only ordered
/// by relevance. This line is the only place that says so, and it says *what*
/// was understood ("初音未来（角色）· 黑暗"), not just "semantic search".
class SemanticSearchBanner extends StatelessWidget {
  /// Fixed, because the collection app bar has to reserve the height up front
  /// (`_getAppBarContentHeight`) and a wrapping line would change it mid-layout.
  static const double preferredHeight = 46;

  final SemanticSearchInfo info;

  const SemanticSearchBanner({super.key, required this.info});

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final description = info.description;
    final parts = <String>[
      if (description.isNotEmpty) description,
      '${info.hitCount} 张',
      // Worth saying: on a cable the LAN address is not what answered, and that
      // is the difference between "it works" and "why is it slow".
      if (info.viaUsb) '数据线',
    ];
    return Container(
      height: preferredHeight,
      width: double.infinity,
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 4),
      color: theme.colorScheme.surfaceContainerHighest,
      child: Row(
        children: [
          Icon(AIcons.search, size: 16, color: theme.colorScheme.onSurfaceVariant),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              parts.join(' · '),
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              style: theme.textTheme.bodySmall?.copyWith(color: theme.colorScheme.onSurfaceVariant),
            ),
          ),
        ],
      ),
    );
  }
}
