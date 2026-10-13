# aves/ —— 带 AI 打标签的 Aves fork

这是 [Aves](https://github.com/deckerst/aves)（Thibault Deckers 写的相册应用，BSD 3-Clause，
见本目录 `LICENSE`）的一份 fork，加进了本项目要用的「AI 打标签」功能：把照片缩略图发给电脑端的
WD14 服务，拿回角色标签，**存进 Aves 自己的数据库**（`app_tags` 表），照片文件一个字节都不改。

- 上游基线：`deckerst/aves` 分支 `develop`，commit `f3cbe6e128bfbfc686b312f8abb3b7af77e8cd7`
  （2026-10-10，「perf: cache synced directory children lists」），应用版本 `1.15.5+179`。
- 本目录只保留源码：没有 `.git`，没有构建产物，也没有 `.flutter` 子模块。
- `README.md` / `CHANGELOG.md` / `LICENSE` 都是上游的原文，改的是代码。

## 用起来长什么样

集合页工具栏最右边多了一个「AI 打标签」按钮（`lib/widgets/collection/app_bar.dart`）。
点开是设置页：电脑端地址、令牌、同时取几张缩略图、「只标角色」、「跳过已经有标签的图片」，
下面是「测试连接」和「开始打标签」，输出框里逐批报告进度。打标结果落在 Aves 的
`app_tags` 表里，Tags 页就会出现 `角色|Hakurei Reimu` 这种可以点开筛选的标签。

## 相对上游改了什么

新增（5 个文件）：

| 文件 | 作用 |
| --- | --- |
| `lib/model/app_tags.dart` | `app_tags` 表的内存模型；`ChangeNotifier`，改了标签集合就通知界面刷新 |
| `lib/services/ai_tagging_service.dart` | 取缩略图、POST `/plan_batch`、把结果一批一落盘；单批失败重试一次，连着 3 批失败才停 |
| `lib/widgets/dialogs/ai_tagging_page.dart` | 上面那个设置页 / 进度框 |
| `lib/model/settings/modules/ai_tagging.dart` | 打标签相关设置项（地址、令牌、并发、只看角色…） |
| `android/gradle/gradle-daemon-jvm.properties.disabled` | 把上游钉死「用 Adoptium JDK 构建守护进程」的那份配置停用（改后不强制下载第二个 JDK） |

数据库与标签模型：

- `lib/model/db/db_sqflite_schema.dart`、`db_sqflite.dart`、`db_sqflite_upgrade.dart`、`db.dart`
  —— schema 升到 **v17**，多一张 `app_tags` 表（条目 id → `;` 分隔的标签串）以及它的增删查接口与迁移。
- `lib/model/entry/entry.dart` —— 条目显示的标签是「文件自带的标签 ∪ app 标签」的并集。
- `lib/model/entry/extensions/metadata_edition.dart` —— **闸门**：界面上改标签时，只有本来就在
  文件里的标签会被写回文件（IPTC/XMP），纯 app 标签永远不落到照片上。
- `lib/model/source/media_store_source.dart`、`collection_source.dart`、`tag.dart`
  —— 启动时载入 app 标签、监听变化；媒体库一次少了一大截时（看着像抖了一下）宁可留孤儿行，
  也不把 app 标签一起清掉（`app_tags` 是不可再生的）。
- `lib/widgets/collection/app_bar.dart`、`entry_set_action_delegate.dart`、
  `widgets/common/action_mixins/entry_editor.dart`、`widgets/dialogs/entry_editors/tag_editor_page.dart`、
  `widgets/viewer/action/entry_info_action_delegate.dart`、`lib/model/settings/settings.dart`、
  `plugins/aves_model/lib/src/settings/keys.dart` —— 入口按钮、设置项、标签编辑器与信息面板的接线。

取图（Android 侧，决定打标速度和正确性）：

- `channel/calls/DebugHandler.kt` —— 调试通道在 release 里也注册，多一种取图方式 `decode`，
  并透传 `jpegQuality`。
- `decoding/ThumbnailFetcher.kt` —— 新增 `getByDecoding()`：按目标边长做 2 的幂次降采样解码，
  按需 `RGB_565`，带 alpha 的图先合成到白底（否则透明背景会变黑），再按 EXIF 摆正。
- `utils/BitmapUtils.kt` —— 编码质量可传参（默认 100，打标用 88）。
- `model/provider/MediaStoreImageProvider.kt` —— 内容查询失败时不再把整库判成「都过期」，
  免得数据库被一次抖动清空。
- `lib/services/android_debug_service.dart` —— 上面那条通道的 Dart 侧封装。

## 怎么构建

需要：**JDK 21**、**Flutter 3.47.6**（Dart 3.13.5）、Android SDK（`compileSdk 37`，
build-tools 36.0.0）。自己在设备上装的话，一份 debug APK 就够了（debug 用本地 debug keystore 签名，
`adb install` 直接能装）：

```powershell
# 在本仓库根目录，构建 arm64（真机）或 x64（模拟器）
tools\aves_build.ps1 -Platform android-arm64
tools\aves_build.ps1 -Platform android-x64

# 等价的手工命令（在 aves\ 目录里跑）
flutter build apk --debug --flavor libre --target-platform android-arm64 -t lib/main_libre.dart
# 产物：build\app\outputs\flutter-apk\app-libre-debug.apk
```

几点注意：

- `pubspec.yaml` 已经换成 **libre 变体**（对应 `flavors/pubspec_libre.yaml`），只构建
  `--flavor libre`；包名是 `deckers.thibault.aves.libre`（debug 版会再加 `.debug`）。
- 本目录**不带 `.flutter` 子模块**，所以上游那个 `flutterw` 包装脚本在这里用不了，用你自己装的 Flutter。
- `android/gradle.properties` 里删掉了 `org.gradle.java.home`，Gradle 会走 `JAVA_HOME`。
- `android/gradle/wrapper/gradle-wrapper.properties` 指向 Gradle 官方下载地址；国内网络可以换成
  镜像（文件里注释了一行腾讯镜像）。
- `android/settings.gradle.kts` 与 `android/app/build.gradle.kts` 里把 Aliyun 镜像排在前面，
  原因只是国内拉 `plugins.gradle.org` / `dl.google.com` 很慢；不在国内就把那几行 `maven { }` 删掉。
- 出 **release** APK 需要自己的签名：把 `android/key_template.properties` 复制成
  `android/key.properties` 并填好，否则 release 产物是未签名的、装不上。本项目的 Release 里放的是
  debug APK，就是为了让任何人都能直接装。
