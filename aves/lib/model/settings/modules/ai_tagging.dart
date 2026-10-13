import 'package:aves_model/aves_model.dart';

mixin AiTaggingSettings on SettingsAccess {
  /// base address of the tagger service, e.g. `http://192.168.1.10:8899`
  String get aiTaggingServer => getString(SettingKeys.aiTaggingServerKey) ?? '';

  set aiTaggingServer(String newValue) => set(SettingKeys.aiTaggingServerKey, newValue);

  /// optional shared token, if the service was started with one
  String get aiTaggingToken => getString(SettingKeys.aiTaggingTokenKey) ?? '';

  set aiTaggingToken(String newValue) => set(SettingKeys.aiTaggingTokenKey, newValue);

  /// how many images to send in one request
  int get aiTaggingBatchSize => getInt(SettingKeys.aiTaggingBatchSizeKey) ?? 8;

  set aiTaggingBatchSize(int newValue) => set(SettingKeys.aiTaggingBatchSizeKey, newValue);

  /// how many thumbnails to decode at once
  ///
  /// Getting a thumbnail out of the phone costs far more than the tagging
  /// itself, and the decodes are independent, so overlapping a few of them is
  /// the cheapest speedup there is. Beyond ~4 the phone's decoder saturates.
  int get aiTaggingConcurrency => getInt(SettingKeys.aiTaggingConcurrencyKey) ?? 4;

  set aiTaggingConcurrency(int newValue) => set(SettingKeys.aiTaggingConcurrencyKey, newValue);

  /// tags scoring below this are dropped, so a wrong guess does not pollute the library
  double get aiTaggingMinScore => getDouble(SettingKeys.aiTaggingMinScoreKey) ?? 0.35;

  set aiTaggingMinScore(double newValue) => set(SettingKeys.aiTaggingMinScoreKey, newValue);

  /// keep general tags (wardrobe, scenery, style...) and not only the characters
  bool get aiTaggingIncludeGeneral => getBool(SettingKeys.aiTaggingIncludeGeneralKey) ?? false;

  set aiTaggingIncludeGeneral(bool newValue) => set(SettingKeys.aiTaggingIncludeGeneralKey, newValue);
}
