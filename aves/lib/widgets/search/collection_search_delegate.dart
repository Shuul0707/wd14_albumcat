import 'package:aves/locale/calendar/calendar_utils.dart';
import 'package:aves/model/app_ocrs.dart';
import 'package:aves/model/dynamic_albums.dart';
import 'package:aves/model/entry/entry.dart';
import 'package:aves/model/filters/aspect_ratio.dart';
import 'package:aves/model/filters/container/dynamic_album.dart';
import 'package:aves/model/filters/container/set_and.dart';
import 'package:aves/model/filters/covered/location.dart';
import 'package:aves/model/filters/covered/stored_album.dart';
import 'package:aves/model/filters/covered/tag.dart';
import 'package:aves/model/filters/date.dart';
import 'package:aves/model/filters/favourite.dart';
import 'package:aves/model/filters/filters.dart';
import 'package:aves/model/filters/mime.dart';
import 'package:aves/model/filters/missing.dart';
import 'package:aves/model/filters/query.dart';
import 'package:aves/model/filters/rating.dart';
import 'package:aves/model/filters/recent.dart';
import 'package:aves/model/filters/type.dart';
import 'package:aves/model/filters/weekday.dart';
import 'package:aves/model/grouping/common.dart';
import 'package:aves/model/settings/settings.dart';
import 'package:aves/model/source/album.dart';
import 'package:aves/model/source/collection_lens.dart';
import 'package:aves/model/source/collection_source.dart';
import 'package:aves/model/source/location/country.dart';
import 'package:aves/model/source/location/place.dart';
import 'package:aves/model/source/tag.dart';
import 'package:aves/services/ai_tagging_service.dart';
import 'package:aves/services/hybrid_ranking.dart';
import 'package:aves/services/semantic_search_service.dart';
import 'package:aves/theme/icons.dart';
import 'package:aves/widgets/collection/collection_page.dart';
import 'package:aves/widgets/common/action_mixins/feedback.dart';
import 'package:aves/widgets/common/action_mixins/vault_aware.dart';
import 'package:aves/widgets/common/basic/tv_edge_focus.dart';
import 'package:aves/widgets/common/expandable_filter_row.dart';
import 'package:aves/widgets/common/extensions/build_context.dart';
import 'package:aves/widgets/common/identity/aves_filter_chip.dart';
import 'package:aves/widgets/common/search/delegate.dart';
import 'package:aves/widgets/common/search/page.dart';
import 'package:aves/widgets/viewer/controls/notifications.dart';
import 'package:material_ui/material_ui.dart';
import 'package:provider/provider.dart';

class CollectionSearchDelegate extends AvesSearchDelegate with FeedbackMixin, VaultAwareMixin {
  final CollectionSource source;
  final CollectionLens? parentCollection;
  final ValueNotifier<String?> _expandedSectionNotifier = ValueNotifier(null);
  final FocusNode _suggestionsTopFocusNode = FocusNode();
  final ScrollController _suggestionsScrollController = ScrollController();
  late final List<MimeFilter> _mimeTypeFilters;

  @override
  FocusNode? get suggestionsFocusNode => _suggestionsTopFocusNode;

  @override
  ScrollController get suggestionsScrollController => _suggestionsScrollController;

  static const int searchHistoryCount = 10;
  static final typeFilters = [
    FavouriteFilter.instance,
    MimeFilter.image,
    MimeFilter.video,
    TypeFilter.animated,
    TypeFilter.motionPhoto,
    AspectRatioFilter.portrait,
    AspectRatioFilter.landscape,
    TypeFilter.panorama,
    TypeFilter.sphericalVideo,
    TypeFilter.slowMotion,
    TypeFilter.geotiff,
    TypeFilter.hdr,
    TypeFilter.raw,
  ];

  new({
    required super.searchFieldLabel,
    required super.searchFieldStyle,
    required this.source,
    this.parentCollection,
    super.canPop,
    String? initialQuery,
  }) : super(
         routeName: SearchPage.routeName,
       ) {
    query = initialQuery ?? '';
    _mimeTypeFilters = source.visibleEntries.map((entry) => entry.mimeType).toSet().map(MimeFilter.new).toList()..sort();
  }

  @override
  void dispose() {
    _expandedSectionNotifier.dispose();
    _suggestionsTopFocusNode.dispose();
    _suggestionsScrollController.dispose();
    super.dispose();
  }

  @override
  Widget buildSuggestions(BuildContext context) {
    final upQuery = query.trim().toUpperCase();
    bool containQuery(CollectionFilter filter) => filter.matchLabel(context, upQuery);
    return SafeArea(
      child: NotificationListener(
        onNotification: (notification) {
          if (notification is SelectFilterNotification) {
            _select(context, {notification.filter});
            return true;
          } else if (notification is DecomposeFilterNotification) {
            final filter = notification.filter;
            if (filter is DynamicAlbumFilter) {
              final innerFilter = filter.filter;
              final newFilters = innerFilter is SetAndFilter ? innerFilter.innerFilters : {innerFilter};
              _select(context, newFilters);
              return true;
            }
          }
          return false;
        },
        child: ValueListenableBuilder<String?>(
          valueListenable: _expandedSectionNotifier,
          builder: (context, expandedSection, child) {
            final queryFilter = _buildQueryFilter(false);
            return Selector<Settings, Set<CollectionFilter>>(
              selector: (context, s) => s.hiddenFilters,
              builder: (context, hiddenFilters, child) {
                bool notHidden(CollectionFilter filter) => !hiddenFilters.contains(filter);
                bool isVisible(CollectionFilter filter) => containQuery(filter) && notHidden(filter);

                final visibleTypeFilters = typeFilters.where(notHidden).toList();
                if (hiddenFilters.contains(MimeFilter.video)) {
                  [MimeFilter.image, TypeFilter.sphericalVideo].forEach(visibleTypeFilters.remove);
                }

                final history = settings.searchHistory.where(isVisible).toList();

                return ListView(
                  controller: _suggestionsScrollController,
                  padding: const EdgeInsets.only(top: 8),
                  children: [
                    TvEdgeFocus(
                      focusNode: _suggestionsTopFocusNode,
                    ),
                    if (_semanticWorthy) _buildSemanticRow(context),
                    if (_ocrWorthy) _buildOcrRow(context),
                    _buildFilterRow(
                      context: context,
                      filters: [
                        queryFilter,
                        ...visibleTypeFilters,
                      ].nonNulls.where(isVisible).toList(),
                      // usually perform hero animation only on tapped chips,
                      // but we also need to animate the query chip when it is selected by submitting the search query
                      heroTypeBuilder: (filter) => filter == queryFilter ? HeroType.always : HeroType.onTap,
                    ),
                    if (upQuery.isEmpty && history.isNotEmpty)
                      _buildFilterRow(
                        context: context,
                        title: context.l10n.searchRecentSectionTitle,
                        filters: history,
                      ),
                    _buildDateFilters(context, isVisible),
                    _buildMimeTypeFilters(context, isVisible),
                    _buildAlbumFilters(isVisible),
                    _buildCountryFilters(isVisible),
                    _buildStateFilters(isVisible),
                    _buildPlaceFilters(isVisible),
                    _buildTagFilters(isVisible),
                    _buildRatingFilters(context, isVisible),
                    _buildMetadataFilters(context, isVisible),
                  ],
                );
              },
            );
          },
        ),
      ),
    );
  }

  Widget _buildFilterRow({
    required BuildContext context,
    String? title,
    required List<CollectionFilter> filters,
    HeroType Function(CollectionFilter filter)? heroTypeBuilder,
  }) {
    final calendar = settings.avesLocale.calendar;
    void onTap(filter) => _select(context, {filter is QueryFilter ? QueryFilter(filter.query, calendar) : filter});
    const onLongPress = AvesFilterChip.showDefaultLongPressMenu;
    return title != null
        ? TitledExpandableFilterRow(
            title: title,
            filters: filters,
            expandedNotifier: _expandedSectionNotifier,
            heroTypeBuilder: heroTypeBuilder,
            onTap: onTap,
            onLongPress: onLongPress,
          )
        : ExpandableFilterRow(
            filters: filters,
            isExpanded: false,
            heroTypeBuilder: heroTypeBuilder,
            onTap: onTap,
            onLongPress: onLongPress,
          );
  }

  Widget _buildDateFilters(BuildContext context, CollectionFilterPredicate containQuery) {
    final calendar = settings.avesLocale.calendar;
    final calOps = calendar.ops;

    final firstDayOfWeekIndex = MaterialLocalizations.of(context).firstDayOfWeekIndex;
    const daysPerWeek = DateTime.daysPerWeek;
    final _monthFilters = List.generate(calOps.monthsPerYear, (i) => DateFilter(calendar, DateLevel.m, DateTime(1, i + 1)));
    final _weekdayFilters = List.generate(daysPerWeek, (i) => WeekDayFilter((i + firstDayOfWeekIndex - 1) % daysPerWeek + 1));

    final filters = [
      DateFilter.onThisDay,
      RecentlyAddedFilter.instance,
      ..._monthFilters,
      ..._weekdayFilters,
    ].where(containQuery).toList();

    return _buildFilterRow(
      context: context,
      title: context.l10n.searchDateSectionTitle,
      filters: filters,
    );
  }

  Widget _buildMimeTypeFilters(BuildContext context, CollectionFilterPredicate containQuery) {
    final filters = _mimeTypeFilters.where(containQuery).toList();
    return _buildFilterRow(
      context: context,
      title: context.l10n.searchFormatSectionTitle,
      filters: filters,
    );
  }

  Widget _buildAlbumFilters(CollectionFilterPredicate containQuery) {
    return ListenableBuilder(
      listenable: dynamicAlbums,
      builder: (context, child) => StreamBuilder<AlbumsChangedEvent>(
        stream: source.eventBus.on<AlbumsChangedEvent>(),
        builder: (context, _) {
          final filters = [
            ...albumGrouping.getGroups().map(albumGrouping.uriToFilter),
            ...source.rawAlbums.map(
              (album) => StoredAlbumFilter(
                album,
                source.getStoredAlbumDisplayName(context, album),
              ),
            ),
            ...dynamicAlbums.all,
          ].nonNulls.where(containQuery).toList()..sort();
          return _buildFilterRow(
            context: context,
            title: context.l10n.searchAlbumsSectionTitle,
            filters: filters,
          );
        },
      ),
    );
  }

  Widget _buildCountryFilters(CollectionFilterPredicate containQuery) {
    return StreamBuilder<CountriesChangedEvent>(
      stream: source.eventBus.on<CountriesChangedEvent>(),
      builder: (context, _) {
        return _buildFilterRow(
          context: context,
          title: context.l10n.searchCountriesSectionTitle,
          filters: source.sortedCountries.map((s) => LocationFilter(LocationLevel.country, s)).where(containQuery).toList(),
        );
      },
    );
  }

  Widget _buildStateFilters(CollectionFilterPredicate containQuery) {
    return StreamBuilder<PlacesChangedEvent>(
      stream: source.eventBus.on<PlacesChangedEvent>(),
      builder: (context, _) {
        return _buildFilterRow(
          context: context,
          title: context.l10n.searchStatesSectionTitle,
          filters: source.sortedStates.map((s) => LocationFilter(LocationLevel.state, s)).where(containQuery).toList(),
        );
      },
    );
  }

  Widget _buildPlaceFilters(CollectionFilterPredicate containQuery) {
    return StreamBuilder<PlacesChangedEvent>(
      stream: source.eventBus.on<PlacesChangedEvent>(),
      builder: (context, _) {
        return _buildFilterRow(
          context: context,
          title: context.l10n.searchPlacesSectionTitle,
          filters: source.sortedPlaces.map((s) => LocationFilter(LocationLevel.place, s)).where(containQuery).toList(),
        );
      },
    );
  }

  Widget _buildTagFilters(CollectionFilterPredicate containQuery) {
    return StreamBuilder<TagsChangedEvent>(
      stream: source.eventBus.on<TagsChangedEvent>(),
      builder: (context, _) {
        final filters = [
          ...tagGrouping.getGroups().map(tagGrouping.uriToFilter),
          ...source.sortedTags.map(TagFilter.new),
        ].nonNulls.where(containQuery).toList()..sort();
        return _buildFilterRow(
          context: context,
          title: context.l10n.searchTagsSectionTitle,
          filters: filters,
        );
      },
    );
  }

  Widget _buildRatingFilters(BuildContext context, CollectionFilterPredicate containQuery) {
    return _buildFilterRow(
      context: context,
      title: context.l10n.searchRatingSectionTitle,
      filters: [5, 4, 3, 2, 1, -1].map(RatingFilter.new).where(containQuery).toList(),
    );
  }

  Widget _buildMetadataFilters(BuildContext context, CollectionFilterPredicate containQuery) {
    return _buildFilterRow(
      context: context,
      title: context.l10n.searchMetadataSectionTitle,
      filters: [
        MissingFilter.date,
        LocationFilter.unlocated,
        MissingFilter.fineAddress,
        TagFilter(''),
        RatingFilter(0),
        MissingFilter.title,
      ].where(containQuery).toList(),
    );
  }

  var _selectingFromQuery = false;
  bool _semanticRunning = false;

  @override
  Widget buildResults(BuildContext context) {
    if (_semanticWorthy) {
      if (!_semanticRunning) {
        _semanticRunning = true;
        WidgetsBinding.instance.addPostFrameCallback((_) => _selectSemantic(context));
      }
      return const Center(
        child: Padding(
          padding: EdgeInsets.all(24),
          child: CircularProgressIndicator(),
        ),
      );
    }
    // guard against multiple build calls
    if (!_selectingFromQuery) {
      _selectingFromQuery = true;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        // `buildResults` is called in the build phase,
        // so we post the call that will filter the collection
        // and possibly trigger a rebuild here
        _select(context, {_buildQueryFilter(true)});
      });
    }
    return const SizedBox();
  }

  /// Whether this search should go through the tag table and the vector,
  /// instead of Aves' own query filter.
  ///
  /// Only when the phone holds vectors, a PC is configured, and the text is not
  /// Aves' own query syntax: `tag:x`, `#hash`, `rating>3` and a bare number mean
  /// something precise, and reinterpreting them as a picture description would
  /// silently drop what the user typed.
  bool get _semanticWorthy {
    final text = query.trim();
    if (text.isEmpty) return false;
    if (!SemanticSearchService.hasVectors) return false;
    if (settings.aiTaggingServer.trim().isEmpty) return false;
    return !_looksLikeAvesQuery(text);
  }

  static final RegExp _avesQuerySyntax = RegExp(r'[#:@<>="]|^\d+$');

  static bool _looksLikeAvesQuery(String text) => _avesQuerySyntax.hasMatch(text);

  /// Advertises the semantic route in the suggestions row, so pressing Enter is
  /// an informed choice rather than a surprise reordering of the album.
  Widget _buildSemanticRow(BuildContext context) {
    final theme = Theme.of(context);
    final color = theme.colorScheme.primary;
    return InkWell(
      onTap: () => showResults(context),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        child: Row(
          children: [
            Icon(AIcons.search, size: 20, color: color),
            const SizedBox(width: 12),
            Expanded(
              child: Text(
                '按意思搜「${query.trim()}」',
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: theme.textTheme.bodyLarge?.copyWith(color: color),
              ),
            ),
          ],
        ),
      ),
    );
  }

  /// Whether this search could match text printed on the photos.
  ///
  /// Deliberately separate from [_semanticWorthy]: text is a substring match in
  /// the phone's own database, so it works with the PC switched off -- and it is
  /// the one route that answers "which photo has this written on it" exactly
  /// rather than approximately.
  bool get _ocrWorthy {
    final text = query.trim();
    // A single character matches a large share of any library, which is noise
    // rather than a search. Two is the shortest useful query in Chinese.
    if (text.length < 2) return false;
    if (!appOcrs.hasText) return false;
    return !_looksLikeAvesQuery(text);
  }

  /// Advertises the text route the same way the semantic one is advertised:
  /// pressing Enter still runs Aves' own query, so the user opts in by tapping.
  Widget _buildOcrRow(BuildContext context) {
    final theme = Theme.of(context);
    final color = theme.colorScheme.secondary;
    return InkWell(
      onTap: () => _selectOcr(context),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        child: Row(
          children: [
            Icon(AIcons.search, size: 20, color: color),
            const SizedBox(width: 12),
            Expanded(
              child: Text(
                '搜图片上的文字「${query.trim()}」',
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: theme.textTheme.bodyLarge?.copyWith(color: color),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Future<void> _selectOcr(BuildContext context) async {
    final text = query.trim();
    final hits = await appOcrs.search(text);
    if (!context.mounted) return;
    if (hits.isEmpty) {
      showFeedback(context, FeedbackType.warn, '没有照片写着「$text」');
      return;
    }
    // Keep the order the database returned: re-sorting by date here would make
    // "best match" a lie, and the grid is the only place the ranking shows.
    final byId = {
      for (final entry in (parentCollection?.sortedEntries ?? source.visibleEntries)) entry.id: entry,
    };
    final entries = [
      for (final hit in hits)
        if (byId[hit.id] != null) byId[hit.id]!,
    ];
    if (entries.isEmpty) {
      showFeedback(context, FeedbackType.warn, '找到了文字，但那些照片不在当前视图里');
      return;
    }
    _jumpToFixedPage(context, entries);
  }

  void _jumpToFixedPage(BuildContext context, List<AvesEntry> entries) {
    clean();
    Navigator.maybeOf(context)?.pushAndRemoveUntil(
      MaterialPageRoute(
        settings: const RouteSettings(name: CollectionPage.routeName),
        builder: (context) => CollectionPage(
          source: source,
          filters: const {},
          fixedSelection: entries,
          fixedSort: true,
        ),
      ),
      (route) => false,
    );
  }

  Future<void> _selectSemantic(BuildContext context) async {
    final text = query.trim();
    try {
      final (base, viaUsb) = await AiTaggingService.resolveReachable(settings.aiTaggingServer, token: settings.aiTaggingToken);
      final entries = (parentCollection?.sortedEntries ?? source.visibleEntries).toList();
      final result = await SemanticSearchService.searchHybrid(
        text,
        server: base,
        token: settings.aiTaggingToken,
        entries: entries,
        // More than a screenful: the grid scrolls, and the tail is exactly where
        // the description ("dark") and the vector do the sorting.
        limit: 120,
      );
      if (!context.mounted) return;
      if (result.hits.isEmpty) {
        showFeedback(context, FeedbackType.warn, '没找到与「$text」相似的照片');
        await _select(context, {_buildQueryFilter(true)});
        return;
      }
      _jumpToSemanticPage(
        context,
        result.hits.map((hit) => hit.entry).toList(),
        SemanticSearchInfo(
          text: text,
          parsed: result.query,
          hitCount: result.hits.length,
          characterMatched: result.hasCharacterBand,
          viaUsb: viaUsb,
        ),
      );
    } catch (error) {
      if (!context.mounted) return;
      // The tag table and the text tower live on the PC. Without it a semantic
      // search cannot even be attempted, so say why and still show the photos
      // Aves would have shown for this text.
      showFeedback(context, FeedbackType.warn, error is AiTaggingException ? error.toString() : '语义搜索失败：$error');
      await _select(context, {_buildQueryFilter(true)});
    }
  }

  void _jumpToSemanticPage(BuildContext context, List<AvesEntry> entries, SemanticSearchInfo semantic) {
    clean();
    Navigator.maybeOf(context)?.pushAndRemoveUntil(
      MaterialPageRoute(
        settings: const RouteSettings(name: CollectionPage.routeName),
        builder: (context) => CollectionPage(
          source: source,
          // no filters: the selection *is* the result, and filters would only
          // re-sort or re-filter it against the order we just computed
          filters: const {},
          fixedSelection: entries,
          fixedSort: true,
          semantic: semantic,
        ),
      ),
      (route) => false,
    );
  }

  QueryFilter? _buildQueryFilter(bool colorful) {
    final calendar = settings.avesLocale.calendar;
    final cleanQuery = query.trim();
    return cleanQuery.isNotEmpty ? QueryFilter(cleanQuery, calendar, colorful: colorful) : null;
  }

  Future<void> _select(BuildContext context, Set<CollectionFilter?> filters) async {
    final newFilters = filters.nonNulls.toSet();
    if (newFilters.isEmpty) {
      goBack(context);
      return;
    }

    for (final filter in newFilters) {
      if (!await unlockFilter(context, filter)) return;

      if (settings.saveSearchHistory) {
        final history = settings.searchHistory
          ..remove(filter)
          ..insert(0, filter);
        settings.searchHistory = history.take(searchHistoryCount).toList();
      }
    }

    if (parentCollection != null) {
      _applyToParentCollectionPage(context, newFilters);
    } else {
      _jumpToCollectionPage(context, newFilters);
    }
  }

  void _applyToParentCollectionPage(BuildContext context, Set<CollectionFilter> filters) {
    parentCollection!.addFilters(filters);
    if (Navigator.canPop(context)) {
      // We delay closing the current page after applying the filter selection
      // so that hero animation target is ready in the `FilterBar`,
      // even when the target is a child of an `AnimatedList`.
      // Do not use `WidgetsBinding.instance.addPostFrameCallback`,
      // as it may not trigger if there is no subsequent build.
      Future.delayed(const Duration(milliseconds: 100), () => goBack(context));
    } else {
      _jumpToCollectionPage(context, parentCollection!.filters);
    }
  }

  void _jumpToCollectionPage(BuildContext context, Set<CollectionFilter> filters) {
    clean();
    Navigator.maybeOf(context)?.pushAndRemoveUntil(
      MaterialPageRoute(
        settings: const RouteSettings(name: CollectionPage.routeName),
        builder: (context) => CollectionPage(
          source: source,
          filters: filters,
        ),
      ),
      (route) => false,
    );
  }
}
