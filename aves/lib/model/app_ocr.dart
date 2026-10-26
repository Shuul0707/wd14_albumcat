import 'package:flutter/foundation.dart';

/// Text read off one photo by OCR.
///
/// Kept next to the tags and the vector, and for the same reason kept **out of
/// the file**: it is derived data, it is not tiny (a poster yields a few
/// hundred bytes), and no other app would read it. Removing the row loses an
/// index, never a photo.
@immutable
class AppOcr {
  /// Which engine produced this text. Stylised anime lettering makes engines
  /// disagree, so a row is only comparable with rows from the same engine --
  /// that is why the name travels with the text instead of being assumed.
  final String engine;

  /// Newline-joined lines, in reading order.
  ///
  /// One flat string rather than a list of boxes: the only question ever asked
  /// of it is "does this photo contain this word", which is a substring match.
  final String text;

  final int updated;

  const AppOcr({
    required this.engine,
    required this.text,
    this.updated = 0,
  });

  int get lineCount => text.isEmpty ? 0 : '\n'.allMatches(text).length + 1;

  static AppOcr? fromRow(Map<String, Object?> row, {required int id}) {
    final engine = row['engine'] as String?;
    final text = row['text'] as String?;
    if (text == null || text.isEmpty) return null;
    return AppOcr(
      engine: engine ?? '',
      text: text,
      updated: (row['updated'] as int?) ?? 0,
    );
  }

  Map<String, Object?> toRow(int id) => {
        'id': id,
        'engine': engine,
        'text': text,
        'updated': updated,
      };

  @override
  String toString() => 'AppOcr(lines=$lineCount, engine=$engine)';
}

/// One search hit: the photo id and the line that matched.
///
/// The matched line is worth carrying: on a poster the user typed a word that
/// appears in one of dozens of lines, and showing that line is the difference
/// between "why is this photo here" and an obvious answer.
@immutable
class AppOcrHit {
  final int id;
  final String line;

  const AppOcrHit({required this.id, required this.line});

  @override
  String toString() => 'AppOcrHit(id=$id)';
}
