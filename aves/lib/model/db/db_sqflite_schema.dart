import 'package:sqflite/sqflite.dart';

class SqfliteLocalMediaDbSchema {
  static const entryTable = 'entry';
  static const dateTakenTable = 'dateTaken';
  static const metadataTable = 'metadata';
  static const addressTable = 'address';
  static const favouriteTable = 'favourites';
  static const coverTable = 'covers';
  static const dynamicAlbumTable = 'dynamicAlbums';
  static const vaultTable = 'vaults';
  static const trashTable = 'trash';
  static const videoPlaybackTable = 'videoPlayback';
  static const debugTable = 'debug';
  // tags the app derived by itself (AI tagging) and deliberately keeps out of
  // the media files: no XMP/IPTC is ever written for them, so a tagged photo
  // keeps its bytes, its mtime and its inode
  static const appTagTable = 'appTags';
  // Chinese-CLIP embeddings, derived the same way and kept out of the files
  // for the same reason: 512 float32 per photo is ~2 KB that no other app reads
  static const appVectorTable = 'appVectors';
  // text read off the photo by OCR, derived and kept out of the files for the
  // same reason as tags and vectors: no other app reads it, and a stray write
  // would touch the bytes of a photo we promised not to modify
  static const appOcrTable = 'appOcr';

  static const allTables = [
    entryTable,
    dateTakenTable,
    metadataTable,
    addressTable,
    favouriteTable,
    coverTable,
    dynamicAlbumTable,
    vaultTable,
    trashTable,
    videoPlaybackTable,
    debugTable,
    appTagTable,
    appVectorTable,
    appOcrTable,
  ];

  static Future<void> createLatestVersion(Database db) async {
    await Future.forEach(allTables, (table) => createTable(db, table));
  }

  static Future<void> createTable(Database db, String table) {
    switch (table) {
      case entryTable:
        return db.execute(
          'CREATE TABLE $entryTable('
          'id INTEGER PRIMARY KEY'
          ', contentId INTEGER'
          ', uri TEXT'
          ', path TEXT'
          ', sourceMimeType TEXT'
          ', width INTEGER'
          ', height INTEGER'
          ', sourceRotationDegrees INTEGER'
          ', sizeBytes INTEGER'
          ', title TEXT'
          ', dateAddedSecs INTEGER DEFAULT (strftime(\'%s\',\'now\'))'
          ', dateModifiedMillis INTEGER'
          ', sourceDateTakenMillis INTEGER'
          ', durationMillis INTEGER'
          ', trashed INTEGER DEFAULT 0'
          ', origin INTEGER DEFAULT 0'
          ')',
        );
      case dateTakenTable:
        return db.execute(
          'CREATE TABLE $dateTakenTable('
          'id INTEGER PRIMARY KEY'
          ', dateMillis INTEGER'
          ')',
        );
      case metadataTable:
        return db.execute(
          'CREATE TABLE $metadataTable('
          'id INTEGER PRIMARY KEY'
          ', mimeType TEXT'
          ', dateMillis INTEGER'
          ', flags INTEGER'
          ', rotationDegrees INTEGER'
          ', xmpSubjects TEXT'
          ', xmpTitle TEXT'
          ', latitude REAL'
          ', longitude REAL'
          ', rating INTEGER'
          ')',
        );
      case addressTable:
        return db.execute(
          'CREATE TABLE $addressTable('
          'id INTEGER PRIMARY KEY'
          ', addressLine TEXT'
          ', countryCode TEXT'
          ', countryName TEXT'
          ', adminArea TEXT'
          ', locality TEXT'
          ')',
        );
      case favouriteTable:
        return db.execute(
          'CREATE TABLE $favouriteTable('
          'id INTEGER PRIMARY KEY'
          ')',
        );
      case coverTable:
        return db.execute(
          'CREATE TABLE $coverTable('
          'filter TEXT PRIMARY KEY'
          ', entryId INTEGER'
          ', packageName TEXT'
          ', color TEXT'
          ')',
        );
      case dynamicAlbumTable:
        return db.execute(
          'CREATE TABLE $dynamicAlbumTable('
          'name TEXT PRIMARY KEY'
          ', filter TEXT'
          ')',
        );
      case vaultTable:
        return db.execute(
          'CREATE TABLE $vaultTable('
          'name TEXT PRIMARY KEY'
          ', autoLock INTEGER'
          ', useBin INTEGER'
          ', lockType TEXT'
          ')',
        );
      case trashTable:
        return db.execute(
          'CREATE TABLE $trashTable('
          'id INTEGER PRIMARY KEY'
          ', path TEXT'
          ', dateMillis INTEGER'
          ')',
        );
      case videoPlaybackTable:
        return db.execute(
          'CREATE TABLE $videoPlaybackTable('
          'id INTEGER PRIMARY KEY'
          ', resumeTimeMillis INTEGER'
          ')',
        );
      case debugTable:
        return db.execute(
          'CREATE TABLE $debugTable('
          'id INTEGER PRIMARY KEY AUTOINCREMENT'
          ', message TEXT'
          ')',
        );
      case appTagTable:
        return db.execute(
          'CREATE TABLE $appTagTable('
          'id INTEGER PRIMARY KEY'
          ', tags TEXT'
          ')',
        );
      case appVectorTable:
        return db.execute(
          'CREATE TABLE $appVectorTable('
          'id INTEGER PRIMARY KEY'
          // which image tower produced it: vectors from different models live
          // in different spaces and must never be compared with each other
          ', model TEXT'
          ', dim INTEGER'
          // base64 of little-endian float32
          ', vector TEXT'
          ', updated INTEGER'
          ')',
        );
      case appOcrTable:
        return db.execute(
          'CREATE TABLE $appOcrTable('
          'id INTEGER PRIMARY KEY'
          // which engine produced it: two engines disagree on stylised
          // lettering, so rows from a different engine are not comparable
          ', engine TEXT'
          // newline-joined lines in reading order; substring-searched
          ', text TEXT'
          ', updated INTEGER'
          ')',
        );
      default:
        throw Exception('unknown table=$table');
    }
  }
}
