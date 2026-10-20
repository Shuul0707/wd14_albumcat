import 'package:aves/services/hybrid_ranking.dart';
import 'package:test/test.dart';

QueryMention character({String key = 'hatsune_miku', List<String> keys = const []}) => QueryMention(
      key: key,
      label: 'Hatsune Miku',
      zh: '初音未来（VOCALOID）',
      kind: MentionKind.character,
      matched: '初音未来',
      series: 'VOCALOID',
      weight: 1.0,
      keys: keys,
    );

QueryMention dark() => const QueryMention(
      key: 'dark',
      label: 'Dark',
      zh: '黑暗',
      kind: MentionKind.general,
      matched: '黑暗',
      weight: 0.55,
    );

ParsedQuery phrase({List<QueryMention> mentions = const [], String text = '黑暗的初音未来'}) =>
    ParsedQuery(text: text, mentions: mentions, vectorText: '黑暗');

void main() {
  test('tag leaves and squeezing', () {
    expect(tagLeaf('角色|Hatsune Miku'), 'Hatsune Miku');
    expect(tagLeaf('Dark'), 'Dark');
    expect(squeezeTag('Hatsune Miku'), 'hatsunemiku');
    expect(squeezeTag('初音未来（VOCALOID）'), '初音未来vocaloid');
  });

  test('a named character goes above everything, however dark the others are', () {
    final query = phrase(mentions: [character(), dark()]);
    final ranked = rankCandidates([
      RankCandidate(entryId: 1, tags: ['角色|Hatsune Miku'], vectorScore: 0.20),
      RankCandidate(entryId: 2, tags: ['通用|Dark'], vectorScore: 0.90),
      RankCandidate(entryId: 3, tags: ['角色|Hatsune Miku'], vectorScore: 0.85),
    ], query);

    // Both Miku photos outrank the very dark non-Miku one.
    expect(ranked.map((r) => r.entryId).toList(), [3, 1, 2]);
    expect(ranked[0].band, 1);
    expect(ranked[1].band, 1);
    expect(ranked[2].band, 0);
    // Inside the band the vector still orders: the darker Miku wins.
    expect(ranked[0].score, greaterThan(ranked[1].score));
    // The matched character tag is reported so the UI can say why.
    expect(ranked[0].matched, contains('角色|Hatsune Miku'));
  });

  test('without a character, tag and vector carry the same weight', () {
    final query = phrase(mentions: [dark()], text: '黑暗');
    final ranked = rankCandidates([
      RankCandidate(entryId: 1, tags: ['通用|Dark'], vectorScore: 0.20),
      RankCandidate(entryId: 2, tags: ['通用|Bright'], vectorScore: 0.90),
    ], query);

    final withTag = ranked.firstWhere((r) => r.entryId == 1);
    final other = ranked.firstWhere((r) => r.entryId == 2);
    expect(withTag.band, 0);
    expect(withTag.tagScore, 1.0);
    expect(other.tagScore, 0.0);
    // 0.5 * 1.0 + 0.5 * 0.0 against 0.5 * 0.0 + 0.5 * 1.0: a dead heat, and the
    // tag hit is not allowed to bury the vector.
    expect(withTag.score, closeTo(other.score, 1e-9));
  });

  test('a series mention covers its whole cast', () {
    final query = phrase(mentions: [
      QueryMention(key: 'VOCALOID', kind: MentionKind.series, matched: 'v家', keys: ['hatsune_miku', 'kagamine_rin']),
    ], text: 'v家');
    final ranked = rankCandidates([
      RankCandidate(entryId: 1, tags: ['角色|Kagamine Rin']),
      RankCandidate(entryId: 2, tags: ['角色|Hatsune Miku']),
      RankCandidate(entryId: 3, tags: ['角色|Hakurei Reimu']),
    ], query);

    expect(ranked.firstWhere((r) => r.entryId == 1).band, 1);
    expect(ranked.firstWhere((r) => r.entryId == 2).band, 1);
    expect(ranked.firstWhere((r) => r.entryId == 3).band, 0);
  });

  test('a flat vector spread is no information, not a ranking', () {
    final query = phrase(mentions: [character(), dark()]);
    final ranked = rankCandidates([
      RankCandidate(entryId: 1, tags: ['角色|Hatsune Miku'], vectorScore: 0.30),
      RankCandidate(entryId: 2, tags: ['角色|Hatsune Miku'], vectorScore: 0.30),
    ], query);
    expect(ranked.every((r) => r.vectorScore == 0.5), isTrue);
    expect(ranked[0].score, ranked[1].score);
  });

  test('photos without vectors still rank on tags, weights renormalised', () {
    final query = ParsedQuery(text: '黑暗的初音未来', mentions: [character(), dark()], vectorText: '黑暗');
    final ranked = rankCandidates([
      RankCandidate(entryId: 1, tags: ['角色|Hatsune Miku']),
      RankCandidate(entryId: 2, tags: ['角色|Hatsune Miku', '通用|Dark']),
      RankCandidate(entryId: 3, tags: ['通用|Dark']),
    ], query);

    expect(ranked.first.entryId, 2, reason: 'character plus the dark tag');
    expect(ranked.first.band, 1);
    expect(ranked[1].band, 1);
    expect(ranked.last.band, 0);
  });

  test('an empty phrase ranks nothing', () {
    expect(rankCandidates([RankCandidate(entryId: 1)], ParsedQuery(text: '')), isEmpty);
    expect(rankCandidates(const [], phrase(mentions: [dark()])), isEmpty);
  });

  // The PC calls the field `category`; a mismatch here is invisible in a hand
  // built ParsedQuery but turns every character into a plain tag on a device.
  test('a real /query payload keeps the character band', () {
    final ParsedQuery parsed = ParsedQuery.fromJson(<String, Object?>{
      'text': '黑暗的初音未来',
      'vector_text': '黑暗',
      'residual': '黑暗',
      'mentions': <Object?>[
        <String, Object?>{
          'key': 'dark',
          'category': 'tag',
          'label': 'Dark',
          'zh': '昏暗',
          'matched': '黑暗',
          'weight': 0.55,
        },
        <String, Object?>{
          'key': 'hatsune_miku',
          'category': 'character',
          'label': 'Hatsune Miku',
          'zh': '初音未来（VOCALOID）',
          'matched': '初音未来',
          'series': 'VOCALOID',
          'weight': 1.0,
        },
      ],
    });

    expect(parsed.hasCharacter, isTrue);
    expect(parsed.characters.single.key, 'hatsune_miku');
    expect(parsed.sideMentions.single.key, 'dark');

    final List<RankedCandidate> ranked = rankCandidates([
      RankCandidate(entryId: 1, tags: ['角色|Shameimaru Aya'], vectorScore: 0.40),
      RankCandidate(entryId: 2, tags: ['角色|Hatsune Miku'], vectorScore: 0.10),
    ], parsed);

    expect(ranked.first.entryId, 2, reason: 'the character band wins over a darker photo');
    expect(ranked.first.band, 1);
    expect(ranked.last.band, 0);
  });

  test('a named character nobody has falls back to balanced weights', () {
    final List<RankedCandidate> ranked = rankCandidates([
      RankCandidate(entryId: 1, tags: ['角色|Nakano Nino'], vectorScore: 0.10),
      RankCandidate(entryId: 2, tags: ['角色|Shirakami Fubuki'], vectorScore: 0.40),
    ], phrase(mentions: [character(), dark()]));

    // Nobody carries Miku, so the band is empty: the two signals share the
    // score again instead of paying 0.55 for a band no photo can enter.
    expect(ranked.every((RankedCandidate r) => r.band == 0), isTrue);
    expect(ranked.first.score, closeTo(0.5, 1e-9));
    expect(ranked.first.entryId, 2);
  });
}
