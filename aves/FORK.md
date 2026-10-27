# aves/ —— 带 AI 打标签的 Aves fork

这是 [Aves](https://github.com/deckerst/aves)（Thibault Deckers 写的相册应用，BSD 3-Clause，
见本目录 `LICENSE`）的一份 fork，加进了本项目要用的「AI 打标签」功能：把照片缩略图发给电脑端的
WD14 服务，拿回角色标签，**存进 Aves 自己的数据库**（`appTags` 表），照片文件一个字节都不改。

- 上游基线：`deckerst/aves` 分支 `develop`，commit `f3cbe6e128bfbfc686b312f8abb3b7af77e8cd7`
  （2026-10-10，「perf: cache synced directory children lists」），应用版本 `1.15.5+179`。
- 本目录只保留源码：没有 `.git`，没有构建产物，也没有 `.flutter` 子模块。
- `README.md` / `CHANGELOG.md` / `LICENSE` 都是上游的原文，改的是代码。

## 用起来长什么样

集合页工具栏最右边多了一个「AI 打标签」按钮（`lib/widgets/collection/app_bar.dart`）。
点开是设置页：电脑端地址、令牌、同时取几张缩略图、「只标角色」、「跳过已经有标签的图片」，
下面是「测试连接」和「开始打标签」，输出框里逐批报告进度。打标结果落在 Aves 的
`appTags` 表里，Tags 页就会出现 `角色|Hakurei Reimu` 这种可以点开筛选的标签。

## 相对上游改了什么

新增（15 个文件）：

| 文件 | 作用 |
| --- | --- |
| `lib/model/app_tags.dart` | `appTags` 表的内存模型；`ChangeNotifier`，改了标签集合就通知界面刷新 |
| `lib/services/ai_tagging_service.dart` | 取缩略图、POST `/plan_batch`、把结果一批一落盘；单批失败重试一次，连着 3 批失败才停。请求里带 `include_vector`，一趟同时拿语义向量 |
| `lib/widgets/dialogs/ai_tagging_page.dart` | 上面那个设置页 / 进度框（语义搜索的入口不在这里，在原生搜索框里） |
| `lib/model/settings/modules/ai_tagging.dart` | 打标签相关设置项（地址、令牌、并发、只看角色…） |
| `android/gradle/gradle-daemon-jvm.properties.disabled` | 把上游钉死「用 Adoptium JDK 构建守护进程」的那份配置停用（改后不强制下载第二个 JDK） |
| `lib/model/app_vector.dart` | 单条语义向量（`model` / `dim` / base64 float32）；`decode()` 成 `Float32List` |
| `lib/model/app_vectors.dart` | `appVectors` 表的内存模型；把全库向量拼成一张 `N×dim` 矩阵，`nearest()` 做余弦 top-k（1 万张几毫秒） |
| `lib/services/semantic_search_service.dart` | `query()` 调 PC 的 `/query`（优先端侧文本塔编码）、`searchHybrid()` 取全库余弦再交给融合排序，结果映射回 `AvesEntry` |
| `lib/services/on_device_text_encoder.dart` | 端侧文本塔的 Dart 侧：私有目录检查、`.part` 下载后改名、加载与编码 |
| `lib/services/hybrid_ranking.dart` | 纯 Dart 的融合打分（角色 band + 标签 + 向量）与「听懂了什么」的文案，11 个单测钉住 |
| `lib/widgets/search/semantic_search_banner.dart` | 语义排序结果页 app bar 底部那一条说明横幅 |
| `lib/model/app_ocr.dart` | 一张图的文字（引擎名 / 换行连接的整段文字 / 时间）；空文字是「扫过但没字」，不是缺数据 |
| `lib/model/app_ocrs.dart` | `appOcr` 表的内存模型；只用来回答「库里有没有文字可搜」，真正的查询每次都下 SQLite |
| `lib/widgets/viewer/info/ocr_section.dart` | 信息页的「图上文字」段：可选中复制的正文 + `rapidocr · 2 行 · 时间` 落款 |
| `android/.../channel/calls/TextEncoderHandler.kt` | 端侧 Chinese-CLIP 文本塔：onnxruntime-android 加载、自实现 BERT WordPiece、原生侧 L2 归一化 |

数据库与标签模型：

- `lib/model/db/db_sqflite_schema.dart`、`db_sqflite.dart`、`db_sqflite_upgrade.dart`、`db.dart`
  —— schema 升到 **v19**，多三张表：`appTags`（条目 id → `;` 分隔的标签串）、
  `appVectors`（条目 id → 图像塔 id / 维度 / base64 float32 向量）、`appOcr`（条目 id → 引擎名 /
  整段文字 / 时间），各带增删查接口与迁移（16→17、17→18、18→19）。这些都只住在数据库里，照片文件一个字节都不改。
- `lib/model/entry/entry.dart` —— 条目显示的标签是「文件自带的标签 ∪ app 标签」的并集。
- `lib/model/entry/extensions/metadata_edition.dart` —— **闸门**：界面上改标签时，只有本来就在
  文件里的标签会被写回文件（IPTC/XMP），纯 app 标签永远不落到照片上。
- `lib/model/source/media_store_source.dart`、`collection_source.dart`、`tag.dart`
  —— 启动时载入 app 标签、监听变化；媒体库一次少了一大截时（看着像抖了一下）宁可留孤儿行，
  也不把 app 标签一起清掉（`appTags` 是不可再生的）。
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

搜索框（语义排序没有单独页面）：

- `lib/widgets/search/collection_search_delegate.dart` —— `buildResults` 里判 `_semanticWorthy`
  （有向量 + 设了电脑端地址 + 查询不含 Aves 自己的语法 `tag:` `#` `@` `rating>` 纯数字），成立就
  `resolveReachable` → `searchHybrid` → push 一个普通 `CollectionPage`（`fixedSort: true` +
  `fixedSelection` + `semantic`）；建议列表多一行「按意思搜「…」」；失败时提示并回落原生 `QueryFilter`。
- `lib/model/source/collection_lens.dart` —— `fixedSelection` 的列表在 `_applyFilters` 之后直接返回
  （不再按日期叠组）；`copyWith` 会把 `fixedSort` / `fixedSelection` / `semantic` 一起带走，
  所以 viewer（`CollectionGrid._goToViewer` 拿的是 lens 的拷贝）与网格同序。

图上文字（OCR 那一半）：

- `lib/widgets/search/collection_search_delegate.dart` 的 `_ocrWorthy` / `_buildOcrRow` ——
  建议列表再一行「搜图片上的文字「…」」，点了才走 SQLite `LIKE`；回车仍归原生查询。
- `lib/widgets/viewer/info/ocr_section.dart` 挂在 `lib/widgets/viewer/info/info_page.dart` 上 ——
  打开照片 → Info 就能看到这张图的文字，引擎名 / 行数 / 时间在下面一行。
- `lib/widgets/dialogs/ai_tagging_page.dart` —— 收尾统计多一句「N 张认出了文字」，
  空文字的行也会写库（否则下次打标又要为同一张图付一遍 OCR 的钱）。

## 怎么构建

需要：**JDK 21**、**Flutter 3.47.6**（Dart 3.13.5）、Android SDK（`compileSdk 37`，
build-tools 36.0.0）。自己在设备上装的话，一份 debug APK 就够了（debug 用本地 debug keystore 签名，
`adb install` 直接能装）：

```powershell
# 在本仓库根目录构建（-Platform 原样传给 flutter 的 --target-platform）
tools\aves_build.ps1 -Platform 'android-arm,android-arm64,android-x64'   # 通用包
tools\aves_build.ps1 -Platform android-x64                              # 只在 x86_64 上能跑

# 等价的手工命令（在 aves\ 目录里跑）
flutter build apk --debug --flavor libre --target-platform android-arm,android-arm64,android-x64 -t lib/main_libre.dart
# 产物：build\app\outputs\flutter-apk\app-libre-debug.apk
```

⚠️ **`--target-platform` 决定的是「打包哪个 ABI 的 Flutter 引擎」，必须三个都传，否则真机一打开就崩**：
插件库（onnxruntime、mpv、tiff、jpeg……）来自 AAR，三个 ABI 永远都在，所以 `-Platform android-x64`
构建出来的包里 `lib/arm64-v8a/` 也有一堆 `.so`——但**没有 `libflutter.so`**。这种包装到 arm64
手机上能装成功，一打开就退出（v0.3.0 / v0.4.0 就是这么坏的）。核对办法：

```powershell
python -c "import zipfile;print([i.filename for i in zipfile.ZipFile('build/app/outputs/flutter-apk/app-libre-debug.apk').infolist() if i.filename.endswith('libflutter.so')])"
```

三个 ABI 的 `libflutter.so` 都在，才是真机和模拟器都能装的通用包（约 380 MB）。

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

## 上游升级怎么跟

`aves/` 是整棵源码树的副本（没有 `.git`），所以升级是「拿上游新版本，把我们那 30 来个文件的改动搬过去」，
不是一条 `git rebase`。顺序：

1. **先记基线**。现在是 `deckerst/aves` 的 `develop` @ `f3cbe6e1`、应用版本 `1.15.5+179`（见本文件开头）。
   每升一次就把它改成新值，否则下次没人知道「差异是相对谁算的」。
2. **取上游新版**：`git clone https://github.com/deckerst/aves .tmp/aves-upstream`，
   `git -C .tmp/aves-upstream checkout <新 tag 或 commit>`。
3. **看差异落在哪**：`git diff --no-index --stat aves/ .tmp/aves-upstream/` 先给全貌。我们改过、上游没动的文件
   直接拷过去；两边都动过的手工合——数据库、`pubspec`、`android/` 三处几乎必然两边都动。
4. **最容易撞的三处**：
   - **数据库版本号**：`lib/model/db/db_sqflite_upgrade.dart` 里我们是 `case 16 -> _upgradeFrom16`，schema 到 v17。
     上游要是自己占了 17，就把我们的迁移改号，并确认老用户从任意旧版本到新版本的迁移链仍然连续。
   - **`pubspec.yaml` / `pubspec.lock`**：我们的 `dependency_overrides`（`image` 钉 4.10.1）是照着上游 libre 的
     lock 组合出来的，上游一升可能整段作废，别硬搬，重新解一遍冲突。
   - **`android/`**：Aliyun 镜像、`kotlin.incremental=false`、删掉的 `org.gradle.java.home`、停用的
     `gradle-daemon-jvm.properties` 都是本机/国内网络补丁，上游不会带，也不该指望它带。
5. **改完必须重验，一步都不能省**：`dart analyze` 干净 → `tools\aves_build.ps1 -Platform android-x64` 出包 →
   装到模拟器 → `tools\verify_aves_tags.py verify` 12/12。换版本、换设备，验的东西都要重验一遍。
6. **顺手更新两份文档**：本文件顶部的基线与「相对上游改了什么」的表，以及仓库根 `docs/STATUS.md` 里
   「fork 基于上游 Aves 1.15.5」那一行。

留个可选做法：如果你更想要「干净上游 + 补丁」的形态，可以把每次搬完的改动导出成 patch
（`git diff --no-index aves/ .tmp/aves-upstream/ > .tmp/fork.patch`，别提交进仓库），
下次升级先 `git apply --3way` 试一遍，比人肉比对省事。
