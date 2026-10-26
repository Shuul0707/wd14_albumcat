import 'package:aves/model/app_ocr.dart';
import 'package:aves/model/app_vector.dart';
import 'package:aves/model/covers.dart';
import 'package:aves/model/dynamic_albums.dart';
import 'package:aves/model/entry/entry.dart';
import 'package:aves/model/favourites.dart';
import 'package:aves/model/filters/filters.dart';
import 'package:aves/model/metadata/address.dart';
import 'package:aves/model/metadata/catalog.dart';
import 'package:aves/model/metadata/trash.dart';
import 'package:aves/model/vaults/details.dart';
import 'package:aves/model/viewer/video_playback.dart';

abstract class LocalMediaDb {
  int get nextId;

  Future<String> get path;

  Future<void> init();

  Future<int> dbFileSize();

  Future<void> reset();

  Future<void> removeIds(Set<int> ids, {Set<EntryDataType>? dataTypes});

  // entries

  Future<void> clearEntries();

  Future<Set<AvesEntry>> loadEntries({int? origin, String? directory});

  Future<Set<AvesEntry>> loadEntriesById(Set<int> ids);

  Future<void> insertEntries(Set<AvesEntry> entries);

  Future<void> updateEntry(int id, AvesEntry entry);

  Future<Set<AvesEntry>> searchLiveEntries(String query, {int? limit});

  Future<Set<AvesEntry>> searchLiveDuplicates(int origin, Set<AvesEntry>? entries);

  // date taken

  Future<void> clearDates();

  Future<Map<int?, int?>> loadDates();

  // catalog metadata

  Future<void> clearCatalogMetadata();

  Future<Set<CatalogMetadata>> loadCatalogMetadata();

  Future<Set<CatalogMetadata>> loadCatalogMetadataById(Set<int> ids);

  Future<void> saveCatalogMetadata(Set<CatalogMetadata> metadataEntries);

  Future<void> updateCatalogMetadata(int id, CatalogMetadata? metadata);

  // address

  Future<void> clearAddresses();

  Future<Set<AddressDetails>> loadAddresses();

  Future<Set<AddressDetails>> loadAddressesById(Set<int> ids);

  Future<void> saveAddresses(Set<AddressDetails> addresses);

  Future<void> updateAddress(int id, AddressDetails? address);

  // vaults

  Future<void> clearVaults();

  Future<Set<VaultDetails>> loadAllVaults();

  Future<void> addVaults(Set<VaultDetails> rows);

  Future<void> updateVault(String oldName, VaultDetails row);

  Future<void> removeVaults(Set<VaultDetails> rows);

  // trash

  Future<void> clearTrashDetails();

  Future<Set<TrashDetails>> loadAllTrashDetails();

  Future<void> updateTrash(int id, TrashDetails? details);

  // favourites

  Future<void> clearFavourites();

  Future<Set<FavouriteRow>> loadAllFavourites();

  Future<void> addFavourites(Set<FavouriteRow> rows);

  Future<void> updateFavouriteId(int id, FavouriteRow row);

  Future<void> removeFavourites(Set<FavouriteRow> rows);

  // app tags
  //
  // tags the app derived by itself (AI tagging), deliberately kept out of the
  // media files. Rows are keyed by entry id and store a `;`-separated list.

  Future<void> clearAppTags();

  Future<Map<int, String>> loadAppTags();

  Future<void> saveAppTags(Map<int, String> rows);

  Future<void> removeAppTags(Set<int> ids);

  // app vectors
  //
  // Chinese-CLIP embeddings, derived like app tags and kept out of the media
  // files. One row per entry; `model` says which image tower produced it.

  Future<void> clearAppVectors();

  Future<Map<int, AppVector>> loadAppVectors();

  Future<void> saveAppVectors(Map<int, AppVector> rows);

  Future<void> removeAppVectors(Set<int> ids);

  //
  // text read off the photo by OCR, derived like the tags and kept out of the
  // media files. One row per entry; `engine` says which engine produced it.

  Future<void> clearAppOcr();

  Future<Map<int, AppOcr>> loadAppOcr();

  Future<void> saveAppOcr(Map<int, AppOcr> rows);

  Future<void> removeAppOcr(Set<int> ids);

  /// Photos whose OCR text contains [query], best match first.
  ///
  /// A plain `LIKE '%…%'`, which cannot use an index -- deliberately: the
  /// column is a few hundred bytes, the row count equals the number of photos,
  /// and an FTS table would be another schema to migrate and another tokenizer
  /// to keep in sync with the phone's own language handling.
  Future<List<AppOcrHit>> searchAppOcr(String query, {int limit = 200});

  // covers

  Future<void> clearCovers();

  Future<Set<CoverRow>> loadAllCovers();

  Future<void> addCovers(Set<CoverRow> rows);

  Future<void> updateCoverEntryId(int id, CoverRow row);

  Future<void> removeCovers(Set<CollectionFilter> filters);

  // dynamic albums

  Future<int> clearDynamicAlbums();

  Future<Set<DynamicAlbumRow>> loadAllDynamicAlbums();

  Future<void> addDynamicAlbums(Set<DynamicAlbumRow> rows);

  Future<void> removeDynamicAlbums(Set<String> names);

  // video playback

  Future<void> clearVideoPlayback();

  Future<Set<VideoPlaybackRow>> loadAllVideoPlayback();

  Future<VideoPlaybackRow?> loadVideoPlayback(int id);

  Future<void> addVideoPlayback(Set<VideoPlaybackRow> rows);

  Future<void> removeVideoPlayback(Set<int> ids);

  // debug

  Future<void> clearDebugLog();

  Future<List<String>> loadAllDebugLog();

  Future<void> addDebugLog(String message);
}
