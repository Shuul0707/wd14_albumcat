/// Hybrid ranking: booru tag hits plus Chinese-CLIP vector similarity.
///
/// A search phrase is handled by two halves and this file is where they meet:
///
///     "黑暗的初音未来"
///         tags   -> hatsune_miku (a character), 初音未来 matched literally
///         vector -> "黑暗" (the words left over once the tag is removed)
///
/// The two halves are not treated as equals. When the phrase names a
/// character, photos tagged with that character come first *as a class* -- a
/// dark photo of someone else is not a better answer than a bright photo of
/// Hatsune Miku. Inside that class the vector still decides the order, so
/// "黑暗的初音未来" puts dark Miku photos above bright ones. When the phrase
/// names no character there is no class to privilege, and tags and vector
/// simply share the score evenly.
///
/// Deliberately pure Dart -- no Flutter, no IO, no platform channels -- so the
/// ordering rules can be unit-tested without a device or a model.
library;

import 'dart:math' as math;
import 'dart:typed_data';

/// What kind of thing a mention resolved to.
enum MentionKind { character, series, general, rating }

const Map<String, MentionKind> _kindsByName = <String, MentionKind>{
  'character': MentionKind.character,
  'series': MentionKind.series,
  // The PC calls a plain tag ``tag``; keep both spellings working.
  'general': MentionKind.general,
  'tag': MentionKind.general,
  'rating': MentionKind.rating,
};

/// Squeeze a tag or a name down to the form used for comparisons.
///
/// Lowercase, then drop everything that is not a letter or a digit. That makes
/// `Kasane Teto`, `kasane_teto` and `KasaneTeto` the same string, which is
/// exactly what is needed: the phone stores pretty labels (`Kasane Teto`) while
/// the PC side knows booru keys (`kasane_teto`). CJK is kept as is.
String squeezeTag(Object? value) {
  final String text = (value ?? '').toString().toLowerCase();
  if (text.isEmpty) return '';
  final StringBuffer buffer = StringBuffer();
  for (final int rune in text.runes) {
    final bool isAsciiLetter = (rune >= 0x61 && rune <= 0x7a) || (rune >= 0x30 && rune <= 0x39);
    // CJK ideographs, kana, and the CJK punctuation-free ranges; anything else
    // (spaces, `_`, `|`, `-`, quotes, full-width brackets) is a separator.
    final bool isCjk = (rune >= 0x3040 && rune <= 0x30ff) ||
        (rune >= 0x3400 && rune <= 0x4dbf) ||
        (rune >= 0x4e00 && rune <= 0x9fff) ||
        (rune >= 0xf900 && rune <= 0xfaff);
    if (isAsciiLetter || isCjk) buffer.writeCharCode(rune);
  }
  return buffer.toString();
}

/// The leaf of a hierarchical tag path: `角色|Kasane Teto` -> `Kasane Teto`.
String tagLeaf(Object? tag) {
  final String text = (tag ?? '').toString();
  final int cut = text.lastIndexOf('|');
  return cut < 0 ? text : text.substring(cut + 1);
}

/// One tag the query named, as resolved by the PC side (`POST /query`).
class QueryMention {
  const QueryMention({
    required this.key,
    this.label = '',
    this.zh = '',
    this.kind = MentionKind.general,
    this.matched = '',
    this.series = '',
    this.weight = 0.55,
    this.keys = const <String>[],
  });

  factory QueryMention.fromJson(Map<String, Object?> json) {
    // The PC sends ``category``; accept ``kind`` too so older/hand-built
    // payloads (and the unit tests) keep working. Getting this wrong is
    // silent and expensive: every mention becomes a plain tag, so the
    // character band never opens and "黑暗的初音未来" quietly degrades to a
    // vector-only search.
    final String kind = (json['category'] ?? json['kind'] ?? '').toString();
    final Object? keys = json['keys'];
    return QueryMention(
      key: (json['key'] ?? '').toString(),
      label: (json['label'] ?? '').toString(),
      zh: (json['zh'] ?? '').toString(),
      kind: _kindsByName[kind] ?? MentionKind.general,
      matched: (json['matched'] ?? '').toString(),
      series: (json['series'] ?? '').toString(),
      weight: _toDouble(json['weight'], 0.55),
      keys: keys is Iterable ? <String>[for (final Object? k in keys) k.toString()] : const <String>[],
    );
  }

  /// Booru key, e.g. `hatsune_miku` (empty for a series mention).
  final String key;

  /// Pretty English label, e.g. `Hatsune Miku`.
  final String label;

  /// Chinese display name, e.g. `初音未来（VOCALOID）`.
  final String zh;

  final MentionKind kind;

  /// The exact substring the user typed, e.g. `初音未来`.
  final String matched;

  /// Series the character belongs to, if the table knows one.
  final String series;

  /// How much a hit on this mention is worth, relative to other mentions.
  final double weight;

  /// A series mention stands for a whole cast; this is that cast.
  final List<String> keys;

  bool get isCharacter => kind == MentionKind.character || kind == MentionKind.series;

  /// The Chinese name without its trailing series hint.
  String get zhName {
    final int cut = zh.indexOf(RegExp('[（(]'));
    return cut < 0 ? zh : zh.substring(0, cut);
  }

  String get display => zhName.isNotEmpty ? zhName : (label.isNotEmpty ? label : key);

  /// Every spelling that a stored tag leaf might use for this mention.
  Set<String> get needles {
    final Set<String> out = <String>{};
    void add(Object? value) {
      final String squeezed = squeezeTag(value);
      // One syllable of CJK or two Latin letters are too easy to hit by
      // accident; the PC side already applied the same floor when parsing.
      if (squeezed.length >= 2) out.add(squeezed);
    }

    add(key);
    add(label);
    add(zhName);
    add(matched);
    return out;
  }
}

/// A phrase plus everything the PC side could tell us about it.
class ParsedQuery {
  const ParsedQuery({
    required this.text,
    this.mentions = const <QueryMention>[],
    this.residual = '',
    this.vectorText = '',
    this.vector,
  });

  /// Tolerant constructor: a `/query` payload, or the older `/encode_text`
  /// style when the feature is used on its own.
  factory ParsedQuery.fromJson(Map<String, Object?> json) {
    final Object? rawMentions = json['mentions'];
    final Object? rawVector = json['vector'];
    Float32List? vector;
    if (rawVector is Float32List) {
      vector = rawVector;
    }
    return ParsedQuery(
      text: (json['text'] ?? '').toString(),
      mentions: rawMentions is Iterable
          ? <QueryMention>[
              for (final Object? item in rawMentions)
                if (item is Map) QueryMention.fromJson(item.cast<String, Object?>()),
            ]
          : const <QueryMention>[],
      residual: (json['residual'] ?? '').toString(),
      vectorText: (json['vector_text'] ?? '').toString(),
      vector: vector,
    );
  }

  final String text;
  final List<QueryMention> mentions;

  /// What is left of the phrase once the tags are taken out.
  final String residual;

  /// What the PC actually encoded (it prefers [residual]).
  final String vectorText;

  /// The query embedding, when one could be made.
  final Float32List? vector;

  bool get isEmpty => text.trim().isEmpty && mentions.isEmpty && vector == null;

  bool get hasCharacter => mentions.any((QueryMention m) => m.isCharacter);

  Iterable<QueryMention> get characters => mentions.where((QueryMention m) => m.isCharacter);

  /// Tags that are not characters: these feed [RankedCandidate.tagScore].
  Iterable<QueryMention> get sideMentions =>
      mentions.where((QueryMention m) => !m.isCharacter);
}

/// One photo taking part in the ranking.
class RankCandidate {
  const RankCandidate({required this.entryId, this.tags = const <String>[], this.vectorScore});

  final int entryId;

  /// The tags the phone stores for this photo (map keys of `entry.tags`).
  final Iterable<String> tags;

  /// Cosine similarity against the query vector, when the photo has a vector.
  final double? vectorScore;
}

/// A scored photo. [band] is the character class: 1 carries the named
/// character (or a member of a named series), 0 does not.
class RankedCandidate {
  const RankedCandidate({
    required this.entryId,
    required this.score,
    required this.band,
    required this.characterScore,
    required this.tagScore,
    required this.vectorScore,
    this.matched = const <String>[],
  });

  final int entryId;
  final double score;
  final int band;
  final double characterScore;
  final double tagScore;
  final double vectorScore;

  /// Tag leaves the photo has in common with the query, for the UI.
  final List<String> matched;

  @override
  String toString() =>
      'RankedCandidate($entryId score=${score.toStringAsFixed(3)} band=$band '
      'char=${characterScore.toStringAsFixed(2)} tag=${tagScore.toStringAsFixed(2)} '
      'vec=${vectorScore.toStringAsFixed(3)})';
}

/// Weights for the two situations the user described.
class RankWeights {
  const RankWeights({
    this.character = 0.55,
    this.tag = 0.20,
    this.vector = 0.25,
    this.balancedTag = 0.50,
    this.balancedVector = 0.50,
  });

  /// Character present: the band already does most of the work, then this mix
  /// decides the order *inside* the band.
  final double character;
  final double tag;
  final double vector;

  /// No character named: "tags and vector matter about equally".
  final double balancedTag;
  final double balancedVector;

  static const RankWeights standard = RankWeights();
}

/// Rank [candidates] for [query]. Sorted best first; empty when [query] is.
///
/// Two shapes of answer, matching what the PC side reports:
///
///  * a character (or series) is named -> band-first. Band 1 = the photo carries
///    the named character, band 0 = everything else. Inside a band the score is
///    `character*char + tag*tag + vector*vec`, so "黑暗" only orders the Miku
///    photos among themselves. A series mention ("间谍过家家") expands to its
///    cast, so every character in that series lands in band 1 too.
///  * no character -> a single list sorted by `balancedTag*tag +
///    balancedVector*vec`: the two signals weigh the same.
///
/// Vector similarity is min-max normalised over *this* candidate set, because
/// raw cosine on Chinese-CLIP sits in a narrow band (0.15-0.35 for most
/// photos); comparing raw cosines would barely move the ordering while looking
/// meaningful in the UI.
List<RankedCandidate> rankCandidates(
  List<RankCandidate> candidates,
  ParsedQuery query, {
  RankWeights weights = RankWeights.standard,
}) {
  if (candidates.isEmpty) return const <RankedCandidate>[];
  // Nothing to rank on: returning the photos with score 0 would only invite the
  // caller to show an arbitrary list.
  if (query.isEmpty) return const <RankedCandidate>[];

  // Everything that means "this photo has the named character": the character's
  // own spellings, plus the whole cast when the phrase named a series instead.
  final Set<String> characterNeedles = <String>{};
  for (final QueryMention m in query.mentions) {
    if (!m.isCharacter) continue;
    characterNeedles.addAll(m.needles);
    for (final String key in m.keys) {
      final String squeezed = squeezeTag(key);
      if (squeezed.isNotEmpty) characterNeedles.add(squeezed);
    }
  }

  // The tag half only counts non-character mentions: a character hit is
  // already worth a whole band, counting it twice would drown the vector.
  double sideTotal = 0;
  for (final QueryMention m in query.sideMentions) {
    sideTotal += m.weight;
  }

  final List<double?> rawVectors = <double?>[for (final RankCandidate c in candidates) c.vectorScore];
  final double vMin = rawVectors.whereType<double>().fold<double>(double.infinity, math.min);
  final double vMax = rawVectors.whereType<double>().fold<double>(-double.infinity, math.max);
  final bool hasVectorScores = vMin.isFinite && vMax.isFinite;
  final double vSpan = hasVectorScores ? vMax - vMin : 0;
  // A flat spread means every photo scored the same; 0.5 is "no information",
  // which keeps those photos in the middle instead of pretending they differ.
  final bool flatSpread = !hasVectorScores || vSpan < 1e-6;
  double normalise(double? raw) {
    if (!hasVectorScores || raw == null) return 0;
    if (flatSpread) return 0.5;
    final double t = (raw - vMin) / vSpan;
    return t.isNaN ? 0.5 : t.clamp(0.0, 1.0);
  }

  final bool banded = query.hasCharacter && characterNeedles.isNotEmpty;
  final bool hasSideTags = sideTotal > 0;
  final bool hasVector = query.vector != null || hasVectorScores;

  double wCharacter = banded ? weights.character : 0;
  double wTag = weights.tag;
  double wVector = weights.vector;
  double wBalancedTag = weights.balancedTag;
  double wBalancedVector = weights.balancedVector;
  if (!hasSideTags) {
    wTag = 0;
    wBalancedTag = 0;
  }
  if (!hasVector) {
    wVector = 0;
    wBalancedVector = 0;
  }
  final double baseSum = wCharacter + wTag + wVector;
  if (baseSum > 0 && baseSum != 1) {
    wCharacter /= baseSum;
    wTag /= baseSum;
    wVector /= baseSum;
  }
  final double balancedSum = wBalancedTag + wBalancedVector;
  if (balancedSum > 0 && balancedSum != 1) {
    wBalancedTag /= balancedSum;
    wBalancedVector /= balancedSum;
  }

  // Pass 1: who carries the named character, and how much of the rest they hit.
  final List<_Scored> rows = <_Scored>[];
  for (final RankCandidate candidate in candidates) {
    final List<String> matched = <String>[];
    // Aves often stores both `Hakurei Reimu` and `角色|Hakurei Reimu` for the
    // same photo; the subtitle should say it once.
    final Set<String> seenMatched = <String>{};
    int band = 0;
    double characterScore = 0;
    double sideScore = 0;
    final Set<String> seenSide = <String>{};

    for (final Object? tag in candidate.tags) {
      final String squeezed = squeezeTag(tagLeaf(tag));
      if (squeezed.isEmpty) continue;
      if (banded && characterNeedles.contains(squeezed)) {
        band = 1;
        characterScore = 1.0;
        if (seenMatched.add(squeezed)) matched.add(tag.toString());
      }
      if (hasSideTags) {
        for (final QueryMention m in query.sideMentions) {
          if (!m.needles.contains(squeezed)) continue;
          if (seenSide.add(m.key)) sideScore += m.weight;
          if (seenMatched.add(squeezed)) matched.add(tag.toString());
          break;
        }
      }
    }

    rows.add(_Scored(
      entryId: candidate.entryId,
      band: band,
      characterScore: characterScore,
      tagScore: hasSideTags ? (sideScore / sideTotal).clamp(0.0, 1.0) : 0,
      vectorScore: normalise(candidate.vectorScore),
      matched: matched,
    ));
  }

  // A named character nobody in the library has is not a ranking signal: paying
  // 0.55 of the score to an empty band would only make every result look bad
  // (top hit 0.25 instead of ~0.5). Fall back to the balanced weights, which is
  // also what the user implicitly asked for by naming something absent.
  final bool effectiveBanded = banded && rows.any((_Scored r) => r.band > 0);

  final double charWeight = effectiveBanded ? wCharacter : 0;
  final double tagWeight = effectiveBanded ? wTag : wBalancedTag;
  final double vectorWeight = effectiveBanded ? wVector : wBalancedVector;

  final List<RankedCandidate> scored = <RankedCandidate>[
    for (final _Scored row in rows)
      RankedCandidate(
        entryId: row.entryId,
        score: charWeight * row.characterScore + tagWeight * row.tagScore + vectorWeight * row.vectorScore,
        band: effectiveBanded ? row.band : 0,
        characterScore: row.characterScore,
        tagScore: row.tagScore,
        vectorScore: row.vectorScore,
        matched: row.matched,
      ),
  ];

  scored.sort((RankedCandidate a, RankedCandidate b) {
    if (a.band != b.band) return b.band.compareTo(a.band);
    if (a.score != b.score) return b.score.compareTo(a.score);
    return b.vectorScore.compareTo(a.vectorScore);
  });
  return scored;
}

class _Scored {
  const _Scored({
    required this.entryId,
    required this.band,
    required this.characterScore,
    required this.tagScore,
    required this.vectorScore,
    required this.matched,
  });

  final int entryId;
  final int band;
  final double characterScore;
  final double tagScore;
  final double vectorScore;
  final List<String> matched;
}

double _toDouble(Object? value, double fallback) {
  if (value is num) return value.toDouble();
  if (value is String) return double.tryParse(value) ?? fallback;
  return fallback;
}
