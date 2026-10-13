# albumcat

用 WD14 给照片库自动打**动漫角色标签**，然后在手机第三方相册里按角色浏览。

手机上的照片**不移动、不改名、不重编码，连元数据都不改一个字节**。手机只把 448px 缩略图交给电脑做推理，
标签回来只落到相册 App（Aves）自己的 SQLite 表里；原图的 SHA256 和**修改时间逐字节不变**，
所以相册的排序、时间线、任何别的 App 都看不出这张照片被碰过，而 Aves 里照样能按 `角色 / 作品 / 分级` 浏览。

```
手机相册（DCIM/…）
      │  ① 手机上自己解出 448 物理像素的 JPEG 缩略图（实测约 43 KB/张）发给电脑，原图既不上传也不解码
      ▼
电脑（只在打标时需要开着）
   albumcat 管线
     1. WD14 ONNX 推理  → 角色 / 通用标签 / 分级
     2. 插件（可选）     → 追加识别结果，比如特定 IP 角色的精准识别
     3. 规则引擎        → 决定打哪些标签；置信度落在复核带的交给人看
     4. 返回标签列表
      │  ② 层级标签  角色|初音未来（走 HTTP，不落盘）
      ▼
手机上的 Aves：标签存进它自己的 appTags 表，按 角色 → 初音未来 浏览
```

三条路线的核心引擎（扫描 → 推理 → 规则 → 插件）完全相同，只是最后一步把标签放到哪：

| | **Aves 自己的数据库**（当前做法） | **XMP 写进照片**（早期做法） | **Immich API** |
|---|---|---|---|
| 照片位置 | 不变 | 不变 | 不变（云端另存一份） |
| 照片字节 | **一个字节都不动** | 只改元数据段，像素不变 | 完全不动 |
| 照片修改时间 | **不变，相册排序不受影响** | 被改成重写那一刻 ← 这是 B 被停用的原因 | 不变 |
| 手机上谁读 | Aves（本仓库 fork 版） | Aves / Lightroom / digiKam | 官方 Immich App |
| 换个 App | 标签留在 Aves 库里，别的 App 看不到 | 任何读 XMP 的 App 都看得到 | 只属于 Immich |
| 命令 | `tools\albumcat.cmd serve` + App 里点一下 | `albumcat tag` | `albumcat run` |
| 要写安卓 App | 要，fork 一份 Aves | 不用，Aves 是现成 App | 不用 |

## 路线 A：让 Aves 自己存标签

### 为什么放弃了写 XMP

> 我反对这种直接改写照片数据的方法，本质是有些破坏性的，比如会重置修改日期导致相册中排序混乱

这个反对是对的。JPEG 的 XMP 在 APP1 段里，改它就得把整个文件重写一遍，重写就换修改时间、换 inode，
`MediaScannerConnection` 再扫一遍，相册按修改时间重排——照片会从原来的位置跳到最前面。
XMP 路线的代码（`albumcat tag` / `JpegXmpSink` / `albumcat undo`）还在仓库里，也验过字节级可逆
（5/5 文件 `undo` 后 SHA256 与原图完全一致），但它**不再是默认做法**，保留只是留个退路。
现在的做法是：**fork Aves，标签只进 Aves 自己的数据库。**

### 数据怎么分家

改的是 fork 的这些地方（源码在 `.tmp\aves-src`，upstream 是 Aves 1.15.5）：

| 文件 | 改动 |
|---|---|
| `lib/model/db/db_sqflite_schema.dart` | 新表 `appTags(id INTEGER PRIMARY KEY, tags TEXT)`，加进 `allTables` |
| `lib/model/db/db_sqflite_upgrade.dart` | schema 16 → 17，`_upgradeFrom16` 只建表，不动老数据 |
| `lib/model/db/db.dart` / `db_sqflite.dart` | `openDatabase(..., version: 17)`；新接口 `loadAppTags` / `saveAppTags` / `removeAppTags` / `clearAppTags`，写用 `_db.batch()` + `ConflictAlgorithm.replace`，读用 `queryCursor(bufferSize: 1000)` |
| `lib/model/app_tags.dart`（新） | 全局单例 `AppTags`（`ChangeNotifier`），内存里 `_byEntry`，标签分隔符 `';'`，带 `generation` 计数器给 UI 做缓存失效 |
| `lib/model/entry/entry.dart` | `fileTags` = 只来自文件的 `dc:subject`；`tags` = `fileTags` ∪ `appTags.tagsOf(id)` |
| `lib/model/entry/extensions/metadata_edition.dart` | ★ 闸门：`editTags()` 把「只存在于 appTags 的标签」从待写入集合里剔掉 |

那个闸门是整件事的关键，它保证**纯 App 标签永远摸不到照片文件**：

```dart
final appOnlyTags = appTags.tagsOf(id).where((tag) => !fileTags.contains(tag)).toSet();
final writableTags = appOnlyTags.isEmpty ? tags : tags.where((tag) => !appOnlyTags.contains(tag)).toSet();
// 只有 writableTags 会进 editIptcValues / editTagsXmp
```

配套改了两组调用点（这里最容易漏）：

- **所有会回写文件的 UI 改读 `fileTags`**：标签编辑器、分类里的批量改标签、信息页的动作。
  不这么改，用户随便一编辑，App 标签就会被当作「用户写的」落进照片。
- **显示和筛选继续读 `entry.tags`（并集）**：网格角标、查看器、标签页、筛选器、统计、命名模式。
- 两处**故意**仍读 `entry.tags`：标签编辑器的联想词、批量改里的 `obsoleteTags`。

### 手机上怎么用

1. 电脑上起服务：`tools\albumcat.cmd serve`（默认 `:8899`；用 `--token` 时 App 里填同一串）。
2. 装 `dist\aves-fork-libre-debug-arm64.apk`（真机）或 `-x64`（模拟器）。
3. Aves 里授权照片访问 → 右上角 ✨（`AI 打标签`）→ 填电脑地址（如 `http://192.168.1.10:8899`）→
   **开始打标签**。App 只对**当前集合里可见的**照片取缩略图，按 8 张一批、批内 4 路并发取图发给电脑。
   （手机和电脑不在同一个局域网？插数据线跑一次 `tools\phone_link.cmd`，地址不用改，
   见下面「[没有 Wi-Fi 的时候](#没有-wi-fi-的时候插数据线备用通道)」。）
4. 标签随后出现在 Aves 的 `Tags` 页（层级形式 `角色|Shameimaru Aya`），点一下就筛出这个角色的照片。

设置项在 `lib/model/settings/modules/ai_tagging.dart`：地址、令牌、批大小 `8`、批内并发 `4`、最低分 `0.35`、
是否连通用标签一起打 `false`。取缩略图走 Aves 自己的 debug MethodChannel
（`deckers.thibault/aves/debug` 的 `getThumbnail`，release 里也无条件注册）。
**默认那条路是「自己解图」**：App 直接用 `BitmapFactory` 解原文件（`inJustDecodeBounds` 先读尺寸 →
power-of-two 采样 → `RGB_565` → 只缩到长边 448 **物理像素** → 按 EXIF 转正 → JPEG `quality = 88`），
`getByDecoding()` 解不出来才退回 glide → mediaStore → resolver 那三条系统路
（`ThumbnailMethod.decode` 排在 `android_debug_service.dart:175` 那个枚举的第一位）。
原图的像素不会被改，手机上也没有任何缩略图缓存可依赖——每轮都是真解码，所以计时稳定。
透明底的 PNG 会多一步**白底压平**（`ARGB_8888` 解 → 白底合成 → `RGB_565`），所以立绘不会变成黑底。
「448 物理像素」的坑在 `extentDip = 448 / devicePixelRatio`：`ThumbnailFetcher.kt:52` 拿到的 `widthDip`
还要乘一次屏幕密度，早先直接传 512 DIP，在 2.6 倍密度屏上就是 1344 px（面积 9 倍）。
`quality` 是本项目给 `BitmapUtils.getBytes()` / `DebugHandler.getThumbnail` 新加的旋钮（默认仍是 Aves 原本的 100）。
批内 4 路并发取图，取一张送一张（不等整批取完再上传）。

这条链路每一步都量过（安卓 15 模拟器，24 张，同一台电脑同一个服务端）：
**4.45 秒 → 3.25 秒 → 2.83 秒**（185 → 135 → 95 ms/张），传的字节 5162 KB → 2943 KB → **1034 KB**（-80%），
三段分别是取图 3.6 → 2.2 s、上传 0.5 → 0.2 s、电脑端缓存命中 1–2 ms。
代价也量过两轮：① 512 DIP/q100 → 448/q88，标签 **16/17 逐字相同**，
唯一的差异是一张**界面截图**（网格里的小缩略图被当成角色），从一个临界角色掉成一个；
② 系统取图（glide，q88/448，2943 KB）→ 自己解图（1034 KB），标签 **17/17 逐字相同**，
真实照片与 4 张探针图零差异（`tools\verify_aves_tags.py verify` 11/11 PASS）。

### 没有 Wi-Fi 的时候：插数据线（备用通道）

手机和电脑不在同一个局域网时不用折腾网络，一根数据线就够：

```
tools\albumcat.cmd serve       REM 电脑端，照旧
tools\phone_link.cmd           REM 另一个窗口：把手机的 127.0.0.1:8899 接到电脑的 8899
```

`tools\phone_link.cmd` 做的事只有一件：`adb reverse tcp:8899 tcp:8899`——它让**手机自己的回环地址**
落到电脑上，于是 App 连 `http://127.0.0.1:8899` 就等于连电脑。顺带一个好处：这条流量到电脑时是从
loopback 进来的，**完全不经过 Windows 防火墙**，所以局域网那次「包被丢掉」的坑在这里根本不存在。
`-r` 撤掉隧道，`-s SERIAL` 在多设备时挑一台，也可以传别的端口（`tools\phone_link.cmd 9000`）。

App 这边**不需要改地址**：`AiTaggingService.resolveReachable()` 先探你填的地址（3 秒超时），
探不通才换 `http://127.0.0.1:8899`（`lib/services/ai_tagging_service.dart` 的 `usbBase`）。
日志里会写明走了哪条：`电脑端在数据线上：http://127.0.0.1:8899` 还是 `电脑端：http://192.168.1.10:8899`。
前提是手机开了 **USB 调试**、线是数据线、电脑在手机上被授权过（`adb devices` 显示 `device`
而不是 `unauthorized`）。拔线或重开 adb，隧道就没了，插回去再跑一次脚本即可。

验过的方式：在模拟器上用 `iptables` 把手机发往电脑 `10.0.2.2:8899` 的出站流量 `REJECT` 掉
（等价于「这里没有局域网」），25 张照样跑完——**19 张写了标签、0 失败**，界面日志写着
`电脑端在数据线上：http://127.0.0.1:8899`；删掉规则再跑一轮，日志变回 `电脑端：http://10.0.2.2:8899`、
3.1 s，说明回退没有把原路拖慢。⚠️ 这条验证跑在模拟器上，**adb 的传输是 loopback 而不是真 USB**，
所以真机上还需要确认的只有一件事：手机端 adbd 接受 `reverse`（任何开了 USB 调试的现代安卓都接受）。

### 打标跑到一半断了会怎样

**已经打好的标签不会丢**——这是 2026-10-13 专门改过的一件事。原先整轮结果攒在内存里、**跑完才落盘**，
所以拔线、手机被杀、电脑端消失，代价是整轮标签全白打；现在**一批一落盘**（每批 8 张，电脑回一批就写一次
Aves 的 `appTags`），中断只会丢掉正在飞的那几批。

- 单批失败**自动重试一次**；连着 **3** 批都失败才停下，提示是
  「连着 3 批都没成功，先停下来。已经打好的标签都留在手机里了。」
- 其余情况都正常收尾并报数：`完成：6 张写了标签，2 张没认出来，14 张失败。`；
  真出错时日志会补一句 `已经存下来的：N 张写了标签…`，一眼能看出保住了多少。
- 剩下的图不用手动挑：勾着「跳过已经有标签的图片」（默认就勾着）再点一次开始，只补没打上的。

怎么验的：一个可复现的 A/B。手机连的不是真服务端，而是 `tools\slow_proxy.py`——一个转发的代理，
**第 1 批之后一律返回 503**（等价于「电脑端没了」，比掐网线更容易复现）。同样的中断点、
电脑端同样回完 8 张：改前 `appTags` **一行没多**（只剩那 4 行探针），改后**多出 6 行**
（回完的 8 张里有 2 张没认出来），而且整轮是以 `完成：` 收尾而不是 `出错：`。
配套脚本还有 `.tmp\clear_apptags.py`（清出待打标的子集）与
`.tmp\bench_aves_run.py --keep-skip-tagged`（驱动界面跑一轮、不碰那个勾选框）。

### 构建

```
tools\aves_build.ps1                                      # 模拟器 x86_64
tools\aves_build.ps1 -Platform android-arm64 -NoVerbose    # 真机 arm64
```

产物是 `build\app\outputs\flutter-apk\app-libre-debug.apk`，两个平台都约 252 MB
（debug 包之所以这么大，是 media-kit 的 AAR 把三套 ABI 的 libmpv 都带进来了）。

⚠️ **ABI 必须选对**：`-Platform android-x64` 的包里只有 `lib/x86_64/libflutter.so`，
`-Platform android-arm64` 的包里只有 `lib/arm64-v8a/libflutter.so`；两者的另一套 ABI 只有插件库、
**没有 Flutter 引擎**，装到不匹配的设备上起不来。所以真机用 arm64 那个包，模拟器用 x64 那个包。

国内网络下这条构建能跑通，靠的是三处已经落盘的镜像，不用再配：
`~\.gradle\init.d\zz-mirrors.gradle`（Gradle 全局 init 脚本，把 `dl.google.com`、`repo1.maven.org`、
`plugins.gradle.org` 重写到阿里云）、`PUB_HOSTED_URL=https://pub.flutter-io.cn` +
`FLUTTER_STORAGE_BASE_URL=https://storage.flutter-io.cn`、Android SDK 包从腾讯镜像装
（`.tmp\install_sdk_pkg.py`；这条线上 `dl.google.com` 只有 21 KiB/s，腾讯镜像 26 MB/s）。

### 验证

`tools\verify_aves_tags.py`（要模拟器 + `adb root`）：

```
python tools\verify_aves_tags.py snapshot   # 记录基线与探针文件
python tools\verify_aves_tags.py verify     # 打标后逐项断言
```

**All 11 checks passed**：4/4 探针文件的 SHA256 与 mtime 不变、数据库 `user_version=17`、
appTags 有 12 行，而且是**逐文件**对得上的——`aves-probe-aya.jpg → ['角色|Shameimaru Aya']`、
`aves-probe-reimu.jpg` / `reimu.png → ['角色|Hakurei Reimu']`、`aves-probe-teto.jpg → ['角色|Kasane Teto']`，
每个文件恰好一个层级标签（早先版本拿全表并集去断言，只要库里任何一行有该角色就 PASS，是假阳性，已改掉）。

UI 级证据（截图在 `docs\evidence\`）：

- `aves-tags-by-character.png`：Aves 的 Tags 页里 `角色|` 一族磁贴——`角色|Hakurei Reimu 4`、
  `角色|Shameimaru Aya 3`、`角色|Kasane Teto 3`、`角色|Kagiyama Hina 2`，另有 `作品|Touhou Project 3`。
- `aves-filter-by-character.png`：点 `角色|Hakurei Reimu` 后，集合里只剩属于这个角色的 4 张。
- `aves-ai-tagging-page.png`：App 里的 AI 打标签页。

### 踩过的坑

- **预置的 `FlutterSharedPreferences.xml` 读不进去**：属主 `chown` 对了，App 还是当没设置。
  根因是 SELinux MLS 类别不匹配（`ls -Z` 看到预置文件是 `s0:c211,…`，App 自己写的是 `c212`），
  预置文件光 `chown` 不够，还得 `restorecon`。
- **`--verbose` 看不到 Dart 编译错误**：`flutter_tools/.../build_system/targets/common.dart:315`
  抛的是没有消息的 `Exception()`，真正的诊断要靠 `flutter build bundle --debug -t lib/main_libre.dart`。
- **`image` 必须钉在 4.10.1**（写进 `pubspec.yaml` 的 `dependency_overrides`）：4.11.0 会调
  `ZLibDecoder.decodeLazy`，而 `archive` 升不到 4.1.0——`pdf 3.12.0` 卡 `<4.1.0`，
  `pdf ≥3.13` 又要求 `xml ^7.0.1`，和 `dlna_dart` 的 `xml ^6.5.0` 冲突。上游自己的 libre lock 就是这个组合。
- **`kotlin.incremental=false`**：强杀 Gradle 之后 Kotlin 增量缓存会写坏，再构建报
  `Could not close incremental caches`；这个 fork 的 Kotlin 几乎不动，稳定性比那点增量收益重要。
- **uiautomator 坐标在屏幕最下方会偏**：Welcome 页 `CONTINUE` 报的中心 y=2253 点不中，y=2290 才行；
  而且 `uiautomator dump` 常返回上一帧的旧树，点完立刻看会以为没生效。

## 为什么曾经是 XMP

> 这一节讲的是 **XMP 路线**（早期做法）的原理。它仍然有用：fork 里 Aves 把文件里的
> `dc:subject` 当作 `fileTags`，和 App 自己的标签取并集，所以「Aves 只读 `dc:subject`」
> 这条结论依旧是设计前提。但**当前做法不写任何 XMP**——原因见上一节。

安卓相册能"按分类浏览"的通用通路只有两条：**文件夹位置**和**照片元数据**。
前者必须移动文件，后者有国际标准，叫 **XMP**（Adobe 制定）。

XMP 里表达层级标签的字段有好几个，各家读的不一样，所以我们**四组都写**，代价为零、一个文件到处能读：

| 字段 | 内容 | 谁读 |
|---|---|---|
| `dc:subject` | 扁平关键词：所有标签的叶子名 **＋ 层级路径** | **Aves**、通用搜索 |
| `lr:HierarchicalSubject` | `角色\|初音未来` 层级路径 | Lightroom |
| `digiKam:TagsList` | 同上 | **Immich**、digiKam |
| `xmp:Rating` | 0–5 星 | 通用 |

> ⭐ **Aves 只认 `dc:subject`**——这是读它源码得出的结论，不是猜的。
> `MetadataFetchHandler.kt` 用 `XMP.DC_SUBJECT_PROP_NAME` 取值、按 `";"` 拼接，
> Dart 侧再 `xmpSubjects.split(';')`；整个 `android/` 与 `lib/` 里 `Hierarchical` **零命中**，
> 也就是它**根本不读** `lr:HierarchicalSubject` 和 `digiKam:TagsList`。
> 所以层级路径必须同时出现在 `dc:subject` 里，否则手机上根本看不到「角色|…」这个标签可点。
> 这就是 `publish.tree_in_subject`（默认开）的作用。
>
> 同时要知道：**Aves 的标签是平的**（`updateTags()` 就是 `Set<String>`，没有层级概念）。
> `角色|Kasane Teto` 在它眼里是一个名字里带竖线的普通标签，不会折叠成「角色 → Kasane Teto」。
> 这不影响使用——点它就筛出该角色的全部照片，而且所有 `角色|…` 排序时自然聚在一起；
> 想手动归组可以用 Aves 的 tag group，想找标签可以用它的搜索。
> 真正的层级树留给 digiKam / Lightroom（它们读 `lr:HierarchicalSubject`）。
>
> 顺带提醒：通用标签默认每张最多 25 个（`publish.max_general_tags_per_image`）。
> 图多了之后 Aves 的 Tags 页会很热闹，觉得吵就把这个数字调小，或用 `general_tag_denylist` 屏蔽。

标签**树**默认只装 `角色` / `作品` / `分级`——几十个 booru 通用标签会把角色标签淹掉。
通用标签照样写进 `dc:subject`，仍然搜得到。想让它们也进树就把 `publish.general_tags_in_tree` 打开。

> Immich 路线也曾在计划里当过主目标。它能用 HTTP API 打标签、完全不碰照片字节，
> 代价是要在电脑上多存一整个照片库。Aves fork 没有这个代价，所以现在走 fork 这条路，
> Immich 保留为「想在网页端浏览」或「验证分类效果」的可选方案。

## 安装

### 1. 前置：Immich 服务（只有走 Immich 路线才需要）

**走 Aves fork 这条路可以跳过这一节。**

在要当"相册服务器"的机器上按官方文档部署：

<https://immich.app/docs/install/docker-compose>

本机已经装好了 Docker Desktop 和 WSL2，compose 文件在 `immich\` 下：

```bat
cd immich
docker compose up -d
```

> 从命令行调 docker 时记得让 `E:\Docker\resources\bin` 在 PATH 上，否则会报
> `error getting credentials - err: exec: "docker-credential-desktop": executable file not found`。

部署完之后在 Immich 网页里：

1. **Account Settings → API Keys → Create**，权限给 `tag.read`、`tag.create`、`tag.asset`、
   `asset.read`、`asset.update`、`search.read`（或者测试阶段直接给全权限）。复制这个 key。
2. 如果你打算用 **External Library**（挂载已有照片目录，而不是让手机上传），
   记得挂载必须是**可写**的，否则 Immich 会静默丢弃元数据编辑。

### 2. 模型

```bat
python tools\download_model.py
```

下载 `SmilingWolf/wd-v1-4-vit-tagger-v2`（356 MB）到 `models\vit\`。
默认走 hf-mirror（实测比 huggingface 快），加 `--repo` 可以换别的 WD14 变体。

顺手把特征输出也加上（只想用 WD14 自带标签的话可以跳过；想做精准角色插件就必须跑）：

```bat
python tools\add_feature_output.py
```

会写出 `models\vit\model_features.onnx`（373 MB）。

### 3. Python 依赖

```bat
tools\install_deps.cmd
```

会创建 `.venv` 并从清华源装依赖。默认装 `onnxruntime-directml`
（Windows 上用 DirectML / DX12，不依赖 CUDA）。想强制 CPU 就 `tools\install_deps.cmd --cpu`。

### 4. 配置

编辑 `config.toml`，至少填上 API key：

```toml
[immich]
base_url = "http://127.0.0.1:2283"
api_key  = "你的 key"
```

也可以用环境变量，避免把 key 写进文件：`ALBUMCAT_IMMICH_API_KEY=...`。

### 5. 自检

```bat
tools\albumcat.cmd doctor
```

会检查模型文件、Python 包、推理后端、插件加载、Immich 连通性。

## 使用

### XMP 路线：把标签写进照片（早期做法，已不是默认）

```bat
REM 先看会打什么标签——不写任何东西
tools\albumcat.cmd tag "D:\照片\旅行" --top 5

REM 真的写进 XMP
tools\albumcat.cmd tag "D:\照片\旅行" --apply

REM 后悔了，精确回滚这一次运行
tools\albumcat.cmd undo --run 20261010-235729
tools\albumcat.cmd undo --list
```

`tag` 接受文件或目录（目录会递归）。默认 dry-run，必须显式 `--apply` 才动文件。

写入前会把**每个文件原本的 XMP 包**记进 `data/journal.jsonl`，所以 `undo` 是逐字节还原，
不是"尽量删干净"。实测：撤销后所有文件 SHA256 与原始文件完全一致。

写完把照片放回手机，用 [Aves](https://github.com/deckerst/aves) 打开：
**Tags** 页会出现 `角色` 分组，点进去就是该角色的全部图片；
也可以用 `...` → **Add dynamic album** 把它固化成会自己更新的相册。

#### 第一次用之前，先确认 Aves 真的读得到

字段层面已经查清（Aves 只读 `dc:subject`，我们也照这个写了），但"它扫到之后会不会真的
建出这个标签"只能在真机上试。生成几张带人造标签的探针照片：

```bat
.venv\Scripts\python.exe tools\make_aves_probe.py
```

它会往 `.tmp\aves-probe\` 写 **3 张 JPEG + 1 张 PNG**（手机上这两样都会出现——
相机出 JPEG、截屏出 PNG），每张的 `dc:subject` 里都有
`角色|AVES-PROBE-1234`（以及裸的 `AVES-PROBE-1234`）。
把它们拷进手机上一个会被相册扫描的目录（比如 `DCIM/Camera`），打开 Aves，
到 **Tags** 页找 `角色|AVES-PROBE-1234`。找到就说明这条路走得通。

**JPEG 和 PNG 要分开看**：Aves 内部它们是两条不同的分支（JPEG 走 Exif/`APP1`，
PNG 走它自己的 "PNG Textual Data" / `iTXt`）。如果只有 JPEG 显示了标签，
那说明**截屏仍然分不了类**——这正是最该知道的一条信息，别因为 JPEG 通了就收工。

### 手机上：让 App 自己跑（XMP 路线的完整形态）

电脑端 `tag` 适合处理已经在电脑上的照片。要处理**手机相册里的**照片，用手机 App：
它自己扫描相册、把缩略图发给电脑、把返回的标签写回照片。电脑在这条链路里只做它擅长的那一件事——推理。

```bat
REM 1) 电脑端开服务。启动时会打印局域网地址，例如 http://192.168.1.10:8899
tools\albumcat.cmd serve

REM 2) 构建手机 App
tools\android_build.cmd assembleDebug
REM    产物：android\app\build\outputs\apk\debug\app-debug.apk
REM    连上手机（开 USB 调试）直接装：
E:\Android\sdk\platform-tools\adb.exe install -r android\app\build\outputs\apk\debug\app-debug.apk
```

App 里的顺序：填电脑地址 → `测试连接` → `选择相册文件夹`（系统弹一次授权，之后一直有效）
→ 保持「只预览，不写入」勾着跑一遍 → 满意了取消勾选再跑。

#### 地址填什么，填错了会怎样

填 `serve` 启动时打印的那行，形如 `http://192.168.83.124:8899`。`albumcat doctor` 也会
把这一行直接打出来（`phone should use: …`），不必自己猜哪张网卡。

地址栏是有意做宽容的，下面这些都会被自动修正成同一个地址，改动了什么会写进下方日志：

| 你填的 | 实际用的 |
|---|---|
| `Http://192.168.83.124:8899` | `http://192.168.83.124:8899` |
| `192.168.83.124` | `http://192.168.83.124:8899` |
| `192.168.83.124:8899` | `http://192.168.83.124:8899` |
| `http:/192.168.83.124:8899` | `http://192.168.83.124:8899` |
| `http://192.168.83.124：8899`（全角冒号） | `http://192.168.83.124:8899` |
| `http://192.168.83.124:8899/health`（从浏览器粘的） | `http://192.168.83.124:8899` |

真的读不懂时会指名道姓——告诉你第几个字符是什么、码点多少，而不是笼统一句「请填写地址」。

这份宽容是踩坑换来的。早先的检查是 `startsWith("http")`，它**大小写敏感**：输入法把首字母
大写成 `Http://` 就会被判为"没填地址"，而框里明明有地址——看到「请填写电脑端地址」的人
只会把同一串再打一遍，然后得到同一个错误。还有两处更隐蔽的：中文输入法的全角 `：`／`／`
能过前缀检查、再死在 `URL()` 深处且不说明原因；而地址以前**只在点「开始」或选完文件夹时
才保存**，中途进程被系统回收（或只是转屏）就会让 `onCreate` 用旧值把输入框刷回去，空框里
显示着灰色示例 `http://192.168.1.10:8899`，看着就像是填好的。现在每敲一个字就存一次。
逻辑都在 `ServerAddress.kt`，它不 import 任何安卓类，所以那张表能在电脑上逐行断言
（`tools\check_android_client.py`）。

界面上两个输入框现在各有一行**常驻标签**（「电脑端地址」/「令牌（电脑端没设 --token 就留空）」）。
这不是装饰：早先只有占位符，而占位符一有字就消失——两个都填了字的框长得一模一样，
再加上最上面那个框曾经被系统栏整个压住（见「技术要点」），填错栏位是迟早的事。
真填错了也不要紧：**地址框为空、而令牌框里那段文字看着像个端点**（带点的主机名、`localhost`、
或带端口）时，`ServerAddress.looksLikeAddress()` 会把它当地址用、清空令牌栏，并在日志里
说明「地址是从「令牌」那一栏读到的」。反过来，真正的令牌不会被误当成地址——那会静默丢掉令牌、
随后认证失败，比原来的 bug 更糟，所以这一条有 15 个用例专门守着。

#### 手机连不上电脑？先看防火墙

**回环流量不受 Windows 防火墙管辖**，所以在电脑上 `http://127.0.0.1:8899/health` 秒回，
完全不能说明手机连得上。而防火墙的入站规则是**按可执行文件路径**匹配的：这台机器上早就
有好几条 python 的放行规则，但它们指向的都是别的解释器，项目虚拟环境里的那个一份都没有。

查：

```bat
tools\albumcat.cmd doctor
REM   phone (route A)
REM     phone should use: http://192.168.1.23:8899
REM     [WARN] no inbound rule covers python.exe on TCP 8899 ...
```

`tools\albumcat.cmd serve` 启动时也会打同样的 WARNING（`--host 127.0.0.1` 时不打，因为那种绑定本来就不给手机用）。

修：

```bat
tools\allow_firewall.cmd
REM 端口不是 8899 时：tools\allow_firewall.cmd 9000
```

它会弹 UAC，然后加一条**只针对本虚拟环境解释器 + 只针对这一个端口**的入站放行。
不想用脚本就照抄 `doctor` 打印出来的那条 `netsh` 命令。
撤销：`netsh advfirewall firewall delete rule name="albumcat serve"`。

如果懒得先查，也可以**直接在手机上点「测试连接」**：App 会把失败原因分好类再说出来，
防火墙丢包会明确提示你去跑 `tools\allow_firewall.cmd`，而不是甩一坨 Java 异常
（见 `NetDiagnosis.kt`，以及「技术要点」里那两条反直觉的教训）。

三点容易踩的：规则用 `profile=any`，因为这台机器同时挂了 VPN（Private）和 Wi-Fi（Public）
两个配置文件，Windows 按包从哪个网卡进来挑配置文件，写死一个就是赌运气；
规则里的解释器路径必须是**展开后的**绝对路径（`tools\..\.venv\...` 这种带 `..` 的写法
未必能和进程映像路径匹配上，而不匹配的表现就是"规则加了但没用"）；另外
`netsh` 的输出语言**会随有没有控制台而变**（同一条查询在 shell 里回英文、在代码里回中文），
所以 `src\albumcat\firewall.py` 是按中英双语解析的。

**照片不出手机。** 传过去的是手机上就地缩放的 448px JPEG 缩略图（约 30–60 KB），
电脑不落盘、用完即弃。原始照片位置不变、像素不重编码——只在 JPEG 的 APP1 段
（或 PNG 的 `iTXt` 块）里插一段 XMP，其余字节（JPEG 是 `SOS` 之后的熵编码数据，
PNG 是 IDAT 块）逐字节原样搬运。

App 里也有「撤销上一次写入」，和电脑端 `undo` 是同一套思路：拿运行前记下的旧 XMP 包还原。
写回前会先比对文件的当前内容是否还是我们上次写进去的那份，被别人改过就不动它。

#### 手机上跑之前，先在电脑上彩排一遍

真机上出了问题很难看出是哪一环。`tools\simulate_phone.py` 把 App 除 SAF 之外**整条链路**
在电脑上跑一遍：用 Python 复刻 `Thumbnailer` 的缩放和白边、调真正的 `TaggingService`（就是
`serve` 跑的那份代码）、再用 **App 自己的 `Xmp.kt`**（经 JVM）做 merge/build/splice 写进真实照片。

```bat
.venv\Scripts\python.exe tools\simulate_phone.py
```

每张照片验六件事：缩略图认出的角色对不对、标签能被**别人的** XMP 解析器（Pillow）读到、
像素逐字节没变、重复跑不堆积标签、写入了 App 用来跳过的标记、撤销后 SHA-256 与原件相同。
它跑的是 `samples\` 里所有的 JPEG **和 PNG**——包括那张 1.53 MB 的真实安卓截屏，
因为截屏走的是完全不同的代码路径（iTXt 块而不是 APP1 段）。
在真机上再出错，就只剩 SAF 文件读写这一段了。

上面那个脚本是从 Python 一侧发请求的，App 自己的 HTTP 客户端仍然没被执行过——而它的典型
失败方式是「字段名写错 → 收到空数组 → 跑完了但什么都没发布」，在手机上极难定位。
`tools\check_android_client.py` 就是补这一段：它把 **App 真实的 `TaggerClient.kt` 和
`Journal.kt`** 拿到桌面 JVM 上编译运行（`android.util.Base64` / `android.content.Context`
用两个小垫片顶上），自己起一个 `albumcat serve`，然后：

```bat
.venv\Scripts\python.exe tools\check_android_client.py
```

- 用真实服务端验 `health()` / `plan()`：每一项都和「Python 解析同一个响应」逐字段比对，
  确认标签是**真的读到了**而不是静默变成空数组；
- 验 `Journal` 的撤销语义：同一张图重试后只留**最早**那行（那才是运行前的状态）、
  `prev` 逐字节可还原、原本没有 XMP 的记成 `null` 而不是空数组、半行垃圾不会让整个日志报废。

（这几个脚本的 JVM 管道是共用的，见 `tools\kotlin_jvm.py`。）

嫌一个一个跑麻烦的话，有一条命令跑全部自检（现在 10 项，从便宜到贵排序——
明显的坏掉几秒内就暴露，而不是等 ONNX 模型加载两次）：

```bat
tools\check_all.cmd
tools\check_all.cmd --list          REM 看看有哪些
tools\check_all.cmd -k runner       REM 只跑名字里带 runner 的
```

而**决定你照片命运的那份代码是 `TagRunner.kt`**：什么时候写、什么时候跳过、写失败怎么办、
半路断线了怎么办、写完通知谁——全在它里面。它此前没有被任何东西执行过，而它的每一种失败
都是「照片被写坏」或「几千张在无声地失败」的形状。`tools\check_android_runner.py` 把它
连同 `Xmp.kt` / `Journal.kt` / `Rescan.kt` / `DocumentPaths.kt` / `TaggerClient.kt` 一起
拉到桌面 JVM 上，用一套内存里的假相册（假的 SAF 树、假的字节存储、假的媒体扫描器）真跑：

```bat
.venv\Scripts\python.exe tools\check_android_runner.py
```

六个场景，56 项断言：

| 场景 | 验的是什么 |
|---|---|
| `dry` | 一个乱七八糟的文件夹（嵌套子目录、`.nomedia`、`.txt`、HEIC）里认出 4 张能打的图，另外 1 张 HEIC 被计数而不是被无视，且一个字节都没写 |
| `write` | 写 4 张（**3 张 JPEG + 1 张 PNG**）→ 逐字节变大且带上 `albumcat` 标记和 `角色\|` 层级路径 → **重跑全部跳过且字节完全不变** → 撤销后 SHA-256 与原件相同、标记消失、日志清空 |
| `batch` | 70 张一次跑完，`Rescan` 只调 **2** 次 `scanFile`（64 + 6），不是 70 次跨进程往返 |
| `abort` | 服务端不可达时，连续失败 8 张就**停下并说明原因**（而不是把几千张挨个超时一遍），且一张照片都没被动过 |
| `failwrite` | 中间那张每次写入都失败（磁盘满的形状）时，**它的日志记录已经先写下去了**——所以哪怕 App 正好死在写入中途，原始字节也还有救 |
| `stop` | 5 张跑到第 3 张时按「停止」：正好处理 3 张、算作**用户按停而不是出错**、没轮到的 2 张**一个字节都没动**、写盘次数正好 3 次、已写好的 3 张照样通知媒体库、撤销把这 5 张全部逐字节还原 |

`failwrite` 那条不是凑数的：把 `TagRunner.kt` 里的顺序换回「先写照片、再记日志」，这一条立刻
变成 `JOURNAL == 2` 而失败（其余 37 条照过）。**知道一个测试真的会失败，才算验过。**

`stop` 这条也是：把 `TagRunner.kt` 里那个循环内的停止检查**删掉**，8 条断言立刻变红
（5 张全被写完，而界面却准备报告「已停止」）。但**把检查从循环体开头挪到循环体末尾却测不出来**——
两种写法都会在下一张开始前退出。这条测试锁住的是「检查在循环**内**，
而不是循环之后」：停在哪一张飞到一半时按下，那一张仍然会写完并进日志，而不是被丢下半途而废。

PNG 那张也不是凑数的。App 写到 PNG 时走的是一整条**不同的**代码路径
（iTXt 块、CRC32、不是 APP1 段），而「路径走错了」这种东西很难靠文字断言发现——
所以驱动还会把每张被改过的文件的**前 8 字节**打出来（PNG 必须始终以 `89504e47 0d0a1a0a` 开头，
一旦有人把 JPEG 的 APP1 段塞进 PNG，这里立刻露馅），并把原字节全部 dump 到磁盘，
再用 Pillow 打开、逐像素比对。

它还会先直接问服务端「这几张图分别是什么」，再拿回答去核对照片里实际写进去的东西——
所以「Aves 要读的 `dc:subject` 里有层级路径」这条是按**真实识别结果**验的，
而不是靠假设某张样图里一定有角色。

#### 但假相册顶掉的那一层，恰好藏过一个真 bug

`check_android_runner.py` 为了能在桌面跑，**替换**了 App 里唯二两处真正碰安卓系统的地方：
`Thumbnailer`（解码照片并缩到 448px）和 `ContentResolver`（按 SAF URI 读写字节日志）。
凡是只活在这一层里的 bug，它一个都看不见。真有一个：

```kotlin
// Thumbnailer.kt -- 看起来是「流没打开就报错」，其实是「解码没返回就报错」
resolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, bounds) }
    ?: throw DecodeFailed("cannot open $uri")
```

`?.use { ... }` 返回的是 **lambda 的结果**，不是流。而测尺寸那一趟
（`inJustDecodeBounds = true`）按设计**就是返回 null**——所以 `?:` 每次都触发，
每张照片都以「打不开」告终，而**它其实刚刚成功打开了**。桌面套件 56/56 全绿，
真机上 4/4 失败。修法是先把流取出来（`private fun open(...)`），
再 `open(...).use { ... }` 且块后什么都不接。

教训不是「Kotlin 的 `?.use` 有坑」，而是：**一个把平台层换成假实现的测试，
测不到那一层里的错**。所以还需要一个不替换任何东西的测试。

#### 真机烟测：`tools\smoke_device.py`

```bat
REM 电脑端服务必须开着，App 要问它每张图是什么
tools\albumcat.cmd serve --port 8899
REM 另开一个窗口
.venv\Scripts\python.exe tools\smoke_device.py --serial emulator-5554 --port 8899
```

它在一个真的安卓系统上把 App 完整跑一轮，全程无人值守（约 60 秒，18 项断言）：

0. **先看版式**：用 `uiautomator` 取每个带文字控件的矩形，断言**两两不重叠**、地址框与令牌框各有
   一行常驻标签、屏幕上没有第二条「相册分类」标题栏、最上面的元素不在屏幕顶边上——这几条就是下面
   那个 ActionBar 遮挡 bug 的探针（见「技术要点」）。**"有没有文字"和"看不看得见"是两回事**：
   出问题的那版里每个按钮的文字都还搜得到，变的只有矩形坐标；
1. 推 4 张干净原图（3 JPEG + 1 PNG）进 `/sdcard/DCIM/AlbumcatSmoke/`，装 APK；
2. 用 root 直接写 App 的 prefs 指向电脑（省掉手打地址），再**用 uiautomator 走一遍真的
   SAF 拾取器**并授权——是按屏幕上的文字找控件，不是写死的坐标；
3. 点「开始」，断言日志说「写入 4 张、失败 0 张」，且 4 个文件的 SHA-256 **全部变了**；
4. 再点一次「开始」，断言「跳过 4 张」且**每个字节都没动**；
5. 点「撤销上一次写入」，断言 4 个文件的 SHA-256 与**最开始那组逐个相同**。

第三步是关键：它断言的是**文件真的被我改了**，不是「日志说改了」。
`--server` 还能把一份**故意写歪**的地址塞进去（比如用户报错时的 `Http://10.0.2.2:8899`），
此时多一条断言：日志里必须出现「地址已按 …使用」——即 App 是**修好了再用**，
而不是碰巧接受。这台机器上的实测输出：

```
-- setup --
  [PASS] the app installs
-- layout: the form must not be covered --
  [PASS] no two labelled elements overlap (nothing is drawn over the form)
  [PASS] both input boxes carry a visible label, not just a placeholder
  [PASS] no framework ActionBar above the form
  [PASS] the form starts below the top edge, clear of the status bar   最上面的元素 top=170
-- run: tag the photos --
  [PASS] wrote 4 photos   完成：写入 4 张，跳过 0 张，失败 0 张，用时 4.7 秒（运行号 20261012-171850）
  [PASS] the odd address was repaired, not silently ignored   完成：写入 4 张，跳过 0 张，失败 0 张，用时 4.7 秒（运行号 20261012-171850）
  [PASS] every photo was actually rewritten   4/4 changed
-- run again: must skip and change nothing --
  [PASS] re-run left every byte alone
-- undo: must restore the originals --
  [PASS] undo restored every photo byte-for-byte   4/4 identical
----------------------------------------------------------------------
All 18 checks passed -- the app works on a real Android system.
```

它**不在 `check_all.cmd` 里**，因为它需要一台设备；电脑侧的 10 项自检保持无依赖。

关于设备：实在的手机只要开了 USB 调试就行（预置 prefs 那步需要 root，非 root 设备上
就自己在界面上填一次地址）。本机用的是安卓模拟器——`E:\Android\sdk\emulator` 加一份
`system-images;android-35;google_apis;x86_64`，配一个 `--no-window` 的 AVD，WHPX
硬件加速下约 40 秒开机，`adb root` 可用。

#### Aves 那一端，在设备上也是验过的

模拟器上装了 Aves（F-Droid 的 libre 构建），把带哨兵标签的照片推进相册，
让它索引完，然后**直接把它的数据库拉出来看**——比截图可靠得多：

```
adb root
adb pull /data/data/deckers.thibault.aves.libre/databases/metadata.db
```

`metadata` 表里那两行（列名就叫 `xmpSubjects`，分隔符是 `;`）：

```
AVES-PROBE-1234;Hakurei Reimu;Touhou Project;1girl;角色|AVES-PROBE-1234;角色|Hakurei Reimu;作品|Touhou Project   rating=4
AVES-PROBE-1234;Shameimaru Aya;Touhou Project;1girl;角色|AVES-PROBE-1234;角色|Shameimaru Aya;作品|Touhou Project   rating=4
```

一行是 PNG、一行是 JPEG，内容一样。这同时证明了三件事：Aves 读的确实是 `dc:subject`；
层级路径**原样到达**（它不会拆成树，`角色|X` 就是它眼里一个带竖线的普通标签，但点它能筛出
所有 X 的照片）；`xmp:Rating` 也读到了。真实标注的 4 张照片跑完后重开 Aves，它的库里同样
出现了我们写进去的那几串标签。

注意 libre 构建的**包名不是 `deckers.thibault.aves`，而是 `deckers.thibault.aves.libre`**。

### Immich 路线：写进 Immich

```bat
REM 看端到端会做什么（默认 dry-run，不改动任何数据）
tools\albumcat.cmd run --limit 50

REM 真的写标签
tools\albumcat.cmd run --limit 50 --apply
```

两条路线共用同一个 journal，`undo` 会自动识别该回滚哪种记录。
`run` 会把"哪次运行给哪个 asset 挂了哪个标签"记下来，所以回滚不会误删你手动加的标签。

### 只看不动

```bat
REM 本地打标冒烟测试——不联网、不写任何东西
tools\albumcat.cmd plan samples\
```

## 标签长什么样

标签是**层级**的，Immich 侧边栏会渲染成树：

| 来源 | 标签 | 说明 |
|---|---|---|
| 角色 | `角色/Hatsune Miku` | WD14 角色标签，`hatsune_miku_(vocaloid)` 会把括号里的作品名剥出来 |
| 作品 | `作品/Vocaloid` | 从角色标签的括号内容推导 |
| 分级 | `分级/R18`、`分级/敏感`、`分级/轻微` | 来自 WD14 的 rating head |
| 通用 | `1girl`、`twintails` ... | 有数量上限，可用 denylist 过滤 |
| 插件 | 由插件自己决定 | 例如 `低质量图片` |

### 阈值与复核带

WD14 的角色标签有几千个，误报远比漏报烦人（上游 README 也建议至少用 0.5）。所以：

```
score < review_low (0.5)              → 丢弃
review_low <= score < review_high(0.7) → 标记 needs_review，默认不发布
score >= review_high                   → 发布
```

想连复核带里的一起发布，把 `publish_review_band` 设成 `true`。
想把某个角色不管多少分都打上，写一条规则就行。

## 扩展：插入特定 IP 的精准识别

这是设计时预留的核心能力。插件放在 `plugins/<id>/`，五种类型：

- **`feature`** —— 在 WD14 的图像特征（`wd-v1-4-vit-tagger-v2` 是 **768 维**）上跑一个小分类头。
  用你自己的修正样本训几百张，在这个 IP 上就能大幅超过 WD14 自带的角色标签，
  而且几乎不增加单图耗时（特征 WD14 已经算过了）。**这就是"精准识别特定角色"的正路。**
- **`python` / `image`** —— 任意 Python：OCR、感知哈希、人脸、读 sidecar 文件。
- **`remote`** —— 把预览图转发到局域网里另一个 HTTP 服务。
- **`rule`** —— 不带模型也不带代码，纯规则。

完整字段表和写法见 [`plugins/README.md`](plugins/README.md)。
仓库里带了一个能直接跑的示例插件 `plugins/example-flat-image/`。

### 训练一个角色插件

`tools\train_feature_plugin.py` 把上面说的闭环做完了：你只要把修正好的图按角色分目录放好，
它就会用 WD14 的特征训一个多标签分类头，导出成可以直接丢进 `plugins/` 的 `feature` 插件。

```bat
:: data\ 下面每个子目录是一个角色；_ 开头的目录是 background（把误报往下压）
::   data\初音未来\*.jpg
::   data\巡音ルカ\*.jpg
::   data\_background\*.jpg
tools\albumcat.cmd train --data data --out plugins\my-ip --id my-ip --name "我的角色头"
```

等价的直接调用（参数更多）：

```bat
.venv\Scripts\python.exe tools\train_feature_plugin.py ^
  --data data --out plugins\my-ip --id my-ip ^
  --target character --priority 10 --threshold 0.5 ^
  --epochs 400 --val-frac 0.2
```

- 纯 numpy 训练（Adam + BCE + L2），**不需要 torch / sklearn**。
- 导出时会跑一遍 ONNX 与 numpy 的往返比对，误差超过 `1e-4` 就拒绝落盘。
- 训练集和验证集的 precision / recall / exact-match 分开打印，**看验证集那组**。
- 产出的 `head.onnx` 只有几 KB（就是一个矩阵），所以插件又小又快。

没有真实样本时可以先跑一遍自检，确认整条链路是通的：

```bat
.venv\Scripts\python.exe tools\make_synth_labels.py
.venv\Scripts\python.exe tools\train_feature_plugin.py --data .tmp\synth-labels --out .tmp\test-plugin --id synth-test --epochs 400 --force
```

（生成 32 张纯合成色块图，正常应当 loss 收敛到 0.00x、验证集准确率 100%。）

查看当前加载了哪些：

```bat
tools\albumcat.cmd plugins
```

> **不需要上游的拆分模型。** 上游 README 让你用 `split_model.py` 切出
> 1.2 GB 的 `feature_extractor.onnx` + 43 MB 的 `classifier.onnx`；本项目改用
> `tools\add_feature_output.py` 把特征张量声明成原模型的**额外输出**，
> 一个 373 MB 文件、一个 session 同时拿到标签和特征。该脚本会用分类头自己的权重
> 反算一遍输出做校验，对不上就不落盘。
>
> 跑一次即可，`albumcat` 会自动发现 `model_features.onnx`。用
> `tools\albumcat.cmd model` 查看真实的特征维度。
>
> （顺带一提：上游 README 说的「1024 维」只对 ViT-L/14 成立，本项目用的
> `wd-v1-4-vit-tagger-v2` 是 ViT-B/16，实际是 768 维。）

## 项目结构

```
albumcat/
├─ config.toml                  配置（见文件内注释）
├─ requirements.txt
├─ src/albumcat/
│  ├─ tagger.py                 WD14 ONNX 推理（照抄上游预处理契约）
│  ├─ rules.py                  规则引擎：分数 → 发布决策
│  ├─ plugins.py                插件注册表与五种 Recognizer
│  ├─ xmp.py                    JPEG/PNG 内部 XMP 的读写（XMP 路线的核心）
│  ├─ publishers.py             JpegXmpSink + ImmichPublisher + 操作日志（可撤销）
│  ├─ server.py                 给手机用的局域网打标服务（纯标准库 HTTP）
│  ├─ firewall.py               启动/自检时看 Windows 会不会把手机的包丢掉
│  ├─ immich.py                 Immich REST 客户端
│  ├─ pipeline.py               串起 扫描→推理→插件→规则
│  ├─ types.py                  Prediction / TagSet / TagPlan
│  ├─ config.py                 配置加载
│  └─ cli.py                    命令行
├─ android/                     手机 App（Kotlin，XMP 路线用；当前做法是下面的 Aves fork）
│  └─ app/src/main/
│     ├─ res/values/themes.xml   无 ActionBar 的主题（框架 ActionBar 会画在表单之上，见「技术要点」）
│     ├─ res/values-v35/themes.xml  同上 + 退出 edge-to-edge（保险，API 36 起被忽略）
│     └─ java/com/albumcat/
│        ├─ MainActivity.kt      单屏 UI（含系统栏内边距与构建戳）
│        ├─ TagRunner.kt         扫相册 → 打标 → 写回 → 撤销
│        ├─ TaggerClient.kt      调电脑端 /plan
│        ├─ NetDiagnosis.kt      把连接失败翻译成「接下来该干什么」（不带任何 Android 依赖，可在桌面 JVM 上测）
│        ├─ ServerAddress.kt     地址归一化 + looksLikeAddress()（把误填进令牌栏的地址救回来）（同样不带 Android 依赖）
│        ├─ Thumbnailer.kt       缩到 448px、烘焙 EXIF 方向
│        ├─ Xmp.kt               与 xmp.py 同构的 XMP 读写
│        ├─ DocumentPaths.kt     SAF 文档 id → 文件路径（只依赖 JDK，可离线测）
│        ├─ Rescan.kt            写完告诉系统媒体库「这张图变了」，相册才知道要重读
│        └─ Journal.kt           手机上记录「原包 → 新包」，用于撤销
├─ tools/
│  ├─ download_model.py         模型下载（纯标准库，可续传）
│  ├─ add_feature_output.py     给模型加特征输出（替代上游 1.2GB 拆分模型）
│  ├─ check_feature_plugin.py   feature 插件链路自检
│  ├─ setup_android.py          下载 Android SDK（绕开坏掉的 Windows TLS）
│  ├─ make_aves_probe.py        生成「哨兵标签」探针图（3 张 JPEG + 1 张 PNG），验证 Aves 读不读 XMP
│  ├─ check_kotlin_xmp.py       把 App 的 Xmp.kt 拿到桌面 JVM 上跑，和 xmp.py 逐字节对拍（JPEG + PNG）
│  ├─ simulate_phone.py         在电脑上彩排 App 整条链路（只差 SAF）
│  ├─ check_android_client.py   把 App 真实的 TaggerClient.kt / Journal.kt 拿到桌面 JVM 上跑
│  ├─ check_android_runner.py   把 App 真实的 TagRunner.kt 拿到桌面 JVM 上，用假相册跑完整流程
│  ├─ smoke_device.py           在真机/模拟器上无人值守跑完整轮（需要设备，故不在 check_all 里）
│  ├─ aves_build.ps1            构建 Aves fork 的 APK（-Platform android-x64 / android-arm64）
│  ├─ verify_aves_tags.py       Aves fork 端到端验证：探针文件哈希/时间戳不变 + appTags 逐文件断言
│  ├─ phone_link.cmd            没有局域网时用数据线：adb reverse 把手机的 127.0.0.1:8899 接到电脑
│  ├─ kotlin_jvm.py             编译并驱动 Kotlin 源码的公共管道（上述 check_* 四个脚本共用）
│  ├─ check_all.cmd             一条命令跑全部自检（纯 ASCII 薄壳）
│  ├─ check_all.py              自检清单本体，从便宜到贵排序
│  ├─ install_deps.cmd          依赖安装（建 venv + 清华源）
│  ├─ allow_firewall.cmd        一键允许 8899 入站（会弹 UAC）
│  ├─ albumcat.cmd              PC 端命令入口
│  ├─ android_build.cmd         用缓存里的 Gradle 构建 App
│  └─ probe_*.py                环境/接口探测脚本
├─ plugins/                     插件目录（见 plugins/README.md）
├─ models/vit/                  model.onnx + model_features.onnx + selected_tags.csv
├─ third_party/wd14-API-onnx/   上游项目（MIT），保留作参考实现
├─ dist/                        构建好的 Aves fork 安装包（arm64 给真机 / x64 给模拟器）
├─ docs/evidence/               验证截图（按角色浏览、筛选、打标页）
└─ .tmp/aves-src/               Aves fork 的工作副本（改动见「路线 A」；不进主仓库）
```

## 技术要点

- **预处理必须和上游逐位一致**，否则分数会整体漂移：透明底合成白底 → RGB→**BGR** →
  白边补成正方形 → 一次性 resize 448×448（缩小 `INTER_AREA` / 放大 `INTER_CUBIC`）→
  **float32 原始 0–255，不做 /255、不做 mean/std 归一化** → 形状 `(1, 448, 448, 3)`（NHWC）。
- **后端选择**：`auto` 依次尝试 DirectML → CUDA → CPU。这台机器的 GPU 是 RTX 5060
  （Blackwell，sm_120），官方 `onnxruntime-gpu` 轮子对 sm_120 支持有问题
  （[onnxruntime#27621](https://github.com/microsoft/onnxruntime/issues/27621) 就是同款卡的静默死锁），
  且本机是 CUDA 13.0 而官方轮子要 CUDA 12.x。所以走 DirectML。
- **只拉缩略图**：WD14 只看 448×448，从 Immich 拉 3–8 MB 原图纯属浪费，所以走
  `/assets/{id}/thumbnail?size=preview`。
- **手机上那条路线的缩略图，尺寸、质量和「谁来解」都得自己开旋钮**：Aves 的 debug `getThumbnail` 把
  JPEG 写死 `quality = 100`（`BitmapUtils.kt:146`），要用质量换体积必须先加参数（已加 `jpegQuality`，
  默认仍是 100，只有 AI 打标签这条路径传 88）。另外 `extentDip` 的**单位是 DIP**，
  平台侧会乘屏幕密度（`ThumbnailFetcher.kt:52`），想要"448 物理像素"就得自己除以 `devicePixelRatio`
  ——早先直接传 512 DIP，在 2.6 倍密度的屏幕上是 1344 px，面积是模型要的 **9 倍**。
- **要缩略图不必求系统**：`ThumbnailFetcher.getByDecoding()` 自己用 `BitmapFactory` 解原文件
  （`inJustDecodeBounds` 读尺寸 → power-of-two 采样 → `RGB_565` → 只缩到长边 448 物理像素 →
  按 EXIF 转正 → JPEG q88）。对比走系统那条 glide 路：取图 152 → 95 ms/张、字节 2943 → 1034 KB、
  整轮 3.25 → 2.83 s，而 17 条标签**逐字相同**。省下来的是 Glide 的资源池/磁盘缓存开销、
  ARGB_8888（4 字节/像素）以及「PNG 图片按 PNG 编码」——`BitmapUtils` 只在 `mimeType` 说可能有
  alpha 时选 PNG，`RGB_565` 解码后一律走 JPEG（截屏缩略图因此小了一个量级）。
  `decode` 失败仍会退回 glide → mediaStore → resolver，格式覆盖没有变窄。
- **透明底 PNG 不能直接当 `RGB_565` 解**：`RGB_565` 没有 alpha 通道，透明像素会露出预乘后的**黑色**，
  而角色立绘大量是透明底。所以 `getByDecoding()` 对「可能带 alpha」的格式（`MimeTypes.canHaveAlpha`）
  改解 `ARGB_8888`，先用 `Canvas` + `drawColor(Color.WHITE)` **白底压平**再缩，压完就是 `RGB_565`
  （于是又回到 JPEG 编码）——看到的就是「白底上的角色」，也正是 WD14 训练分布里的那种图。
  这条专门验过（`tools\make_alpha_probe.py`）：把探针图里所有近白像素抠成透明
  （`zz-alpha-reimu.png`，876×1280，抠掉 30074 px），手机仍打出 `角色|Hakurei Reimu`，
  与不透明的同一张图一字不差。同一次跑的另一组数据也回来了：25 张墙钟 **2.8 s**、取图 1793 ms
  （71 ms/张）、753 KB——白底压平没有让速度退回去。
- **"手机打标签慢"慢在取图，不在推理**（安卓 15 模拟器 4 vCPU + DirectML 服务端，24 张）：
  取图 3.6 s、上传约 0.5 s、电脑端推理 1–2 ms（缓存命中）。尺寸收到 448 物理像素 + 质量 100→88 +
  批内 4 路并发是 **4.45 → 3.25 s**；再把取图换成自解码是 **→ 2.83 s**（185 → 135 → 95 ms/张，
  字节 5162 → 2943 → 1034 KB，-80%）。模拟器上取图耗时之和≈墙钟，说明设备侧的解码+编码本身就是
  瓶颈——4 个 guest vCPU 上 4 路并发几乎被串行化，只吃掉一小截。
- **实测吞吐**（RTX 5060 Laptop，`wd-v1-4-vit-tagger-v2`，448×448）：
  DirectML **102 ms/张**，CPU **627 ms/张**。1 万张分别是约 17 分钟和 1 小时 45 分。
- **batch 维是静态的**：这个 vit 导出的输入形状写死 `[1, 448, 448, 3]`，一次只能喂一张，
  传两张会报 `Got invalid dimensions for input: input_1:0 ... Got: 2 Expected: 1`。
  `tagger.interrogate_batch` 会自动降级到逐张推理；`config.model.batch_size` 只是"请求值"。
  实测批处理对这个模型没有收益（单张已经 102 ms），所以没有做动态 batch 重导出。
- **批量写标签**：Immich 的 `PUT /tags/assets` 是"列出的每个标签 × 列出的每个 asset"的笛卡尔积，
  所以发布器把问题倒过来——**按标签汇总 asset 集合，每个标签发一次请求**，
  复杂度是 O(标签数) 而不是 O(图片数)。
- **特征向量不额外花钱**：特征提取是整个 ViT 塔，分类头只是最后一层 `Gemm`。
  所以把特征张量声明成额外输出后，一次 `session.run` 同时拿到两者，实测单图耗时不变
  （99.5 ms vs 之前的 101.8 ms）。
- **中间张量必须"声明"才能拿到**：ONNX Runtime 不允许按名字索取图内部张量
  （会报 `Invalid output name:...predictions_globalavgpooling/Mean:0`），因为优化阶段把它融合掉了。
  必须改图，把该张量加成图的输出。另外图的输出名**不能起别名**——输出名就是张量名，
  直接声明 `features` 会得到 `Graph output (features) does not exist in the graph`，
  所以要用一个 `Identity` 节点把它引出到干净的名字。
- **XMP 写进照片是纯字节拼接，不解码图像**。JPEG 由一段段 `FFxx` 标记组成，
  XMP 就是其中一条 `APP1`（`FFE1`，载荷以 `http://ns.adobe.com/xap/1.0/\0` 开头）。
  写入过程是：保留开头的 APP0/Exif-APP1 连续段 → 紧跟插入我们的 APP1 → 跳过旧的 XMP APP1 →
  其余段和 `SOS` 之后的**熵编码数据原样搬运**。所以像素逐字节不变，实测 5 张照片全部
  `same`，体积只增加 3.4–5.4 KB。写盘用同目录临时文件 + `os.replace`，中断不会留下半张图。
- **PNG 完全是另一套手术，不是"换个头的 JPEG"**。XMP 在这里是 `iTXt` 块，块体是
  `keyword\0 压缩标志 压缩方法 语言标签\0 翻译关键字\0 正文`——**斜体那几个字节是规范的一部分**，
  而不是什么可有可无的填充。它们数错了以后，我们自己的 reader 照样读得回来
  （正文前面只是多一个 NUL），但 Pillow 会报
  `ExpatError: not well-formed (invalid token): line 1, column 1`。
  这个 bug 真的发生过：`_ITXT_HEAD` 当时写成 5 个字节。**能读回自己写的东西，证明不了格式对。**
  所以两件事是必须的：① 每种格式都要有一个**独立实现**（Pillow）来读我们写的文件；
  ② 验"像素没动"要验到 **IDAT 块字节完全一致**，而不是"文件还能打开"。
  新块插在 `IHDR` 之后、其余块（含 IDAT）逐字节搬运；`tEXt`/`zTXt`/别人的 `iTXt` 原样穿过。
  实测那张 1.53 MB 的安卓截屏：`1,530,707B → 1,534,501B`，IDAT 一致，撤销后 SHA-256 相同。
- **XMP 包要留白**：按 Adobe 惯例预留 2 KB padding，这样以后原地改写标签不会让文件反复增长。
  同一输入连写两次得到**字节完全相同**的结果（幂等）。
- **`<?xpacket?>` 不是 XML**：它是处理指令，直接喂给 `ElementTree` 会解析失败，
  必须先正则切出 `<x:xmpmeta>…</x:xmpmeta>` 再解析。
- **同一份 XMP 实现要有两份，而且必须逐字节相同**：电脑端是 `xmp.py`，手机端是 `Xmp.kt`
  （AndroidX 的 `ExifInterface` 读写不了 XMP，所以手机端只能自己拼 APP1 段 / iTXt 块）。
  两边"差不多"是最坏的情况——手机写、电脑读，字节能差出一点点，而问题只会在真机上暴露。
  所以 `Xmp.kt` 刻意只依赖 JDK（`java.io` / `javax.xml` / `org.w3c.dom` / `java.util.zip`），
  `tools\check_kotlin_xmp.py` 就能把它拉到桌面 JVM 上编译运行，和 Python 版本对拍：
  同样的输入比字节、同样的 JPEG 比整个文件、同样的 PNG 比整个文件、写完互相读，
  最后再用 Pillow 读一遍手机代码写出的文件。
  `TaggerClient.kt` / `Journal.kt` 同理能拉上来跑——桌面 JVM 只缺 `android.util.Base64`
  和 `android.content.Context` 两个类，各写一个几行的垫片即可（见 `tools\check_android_client.py`）。
  这比"在手机上打日志猜"快得多：它一上来就抓到 `Journal.record` 的 `wrote` 参数收得太松——
  Android 的 `encodeToString` 是平台类型，传 null 会编译通过、运行时才炸，已收紧为非空。
- **安卓工程要用阿里云镜像**：这台机器上 `plugins.gradle.org` 的 TLS 证书是自签的
  （Gradle 直接报 certificate error），`repo.maven.apache.org` 连不上（45 秒超时），
  而 `maven.aliyun.com` 各镜像 0.1 秒返回。所以 `android\settings.gradle.kts` 里
  三个仓库全是阿里云，`google()` 只做兜底。同理 AGP 8.7.2 默认要 build-tools 34.0.0，
  本地装的是 35.0.0，所以 `app\build.gradle.kts` 里显式钉了 `buildToolsVersion = "35.0.0"`；
  SDK 许可也不走交互式 `sdkmanager --licenses`，直接把哈希写进 `E:\Android\sdk\licenses\`。
- **只懂四个属性，其余的要原样搬过去**：我们读写 `dc:subject`、`lr:HierarchicalSubject`、
  `digiKam:TagsList`、`xmp:Rating`。可手机相机、Lightroom、截图工具都会往**同一个 XMP 包**里写
  自己的东西（字幕、GPS、厂商署名……）。重新拼包时如果只回填我们认识的那几个，别人的属性就被
  悄无声息地删掉了——对真实手机相册这是数据丢失，而且要过很久才会有人发现。
  所以 `xmp.py` 和 `Xmp.kt` 都有 `foreign_parts()` / `foreignParts()`：语义用 DOM 判定
  （哪些子元素不是我们的），**字节用一个故意"笨"的扫描器逐字取出原样源码**——不用 XML 序列化器
  重排，因为那会改掉引号风格、属性顺序和命名空间前缀。两边子元素数量对不上就整体放弃保留
  （宁可不保，也不乱改）。`tools\simulate_phone.py` 里有一张"相机已经写过元数据"的照片专门验这条。
- **撤销靠记录旧包，不是靠"删干净"**：写入前把每个文件**原本的 XMP 包**（base64）记进 journal，
  `undo` 时原样塞回去。因为大部分照片原本就没有 XMP，所以"原本没有"也要记为一种状态
  （`prev_packet: null`，撤销时整段移除）。
- **XMP 要用"别人的解析器"验一遍**：用自己的解析器读自己的输出是循环论证。
  用 Pillow 的 `Image.getxmp()`（独立实现，需要 `defusedxml`）读回来，
  四个字段全部正确解析；写出的包也逐字节核对过，命名空间是
  `dc` / `lr` / `digiKam` / `xmp` 四个标准 URI，开头结尾是标准的 `<?xpacket?>` 处理指令。
- **真机验证用"哨兵标签"**：`tools\make_aves_probe.py` 会生成几张带
  `角色|AVES-PROBE-1234` 这样一个人造标签的照片（3 张 JPEG + 1 张 PNG）。看到它就说明
  Aves 读的是**我们写的 XMP**，而不是它自己的识别结果或别处的旧数据——
  比拿一张本来就有标签的图去试要可信得多。**两种格式要分开看**：Aves 里它们是两条分支，
  只有 JPEG 通了不代表截屏能分类。
- **"Aves 会读 XMP"这句话必须问清楚是哪个字段**。最初按规范把层级路径只写进
  `lr:HierarchicalSubject`（这是 Lightroom 系的写法），`digiKam:TagsList` 给 digiKam。
  后来把 Aves 源码浅克隆下来（`git -c http.sslBackend=openssl clone --depth 1`，
  因为 GitHub API 被限流、镜像站被拦）grep 出真相：`MetadataFetchHandler.kt` 只用
  `XMP.DC_SUBJECT_PROP_NAME`，`entry.dart` 再 `split(';')`，而 `Hierarchical` 在
  `android/` 与 `lib/` 里**一次都没出现**。也就是说按规范写的那份，Aves 一个字都读不到，
  手机上会看不到任何「角色|…」标签——一个能通过全部电脑侧测试、却在第一次真机使用时
  才暴露的缺陷。**结论：想清"谁会读"时，去读那个"谁"的源码，别只信规范。**
- **写进文件还不够，得有人通知相册**。SAF 只换文件字节，媒体库（MediaStore）里那行记录
  原样不动，于是没有任何东西告诉相册"这张图变了"。Aves 收到媒体库变更才重读：
  `MediaStoreChangeStreamHandler`（对 `Images/Video` 注册 `ContentObserver`）
  → `onStoreChanged` → `refreshUris` → `refreshEntries(..., force: true)`
  → `entry.catalog(force: true)` → 打开**文件**重新解析 XMP（JPEG 走
  `StorageUtils.openInputStream`，不会被缓存里的旧 size 截断，于是旧尺寸也不影响）。
  所以 App 每写完一张（以及每撤销一张）就调
  `MediaScannerConnection.scanFile` 补这一环；路径由 SAF 文档 id 推出来
  （`primary:DCIM/...` → `/storage/emulated/0/DCIM/...`），推不出来就跳过——
  写本身早已成功并校验过。
  ⚠ 这一段最初是**唯一没法在电脑上验证**的环节（`DocumentPaths` 的映射能测，
  媒体库到相册的反应不能）。现在它在模拟器上验过了：写完重开 Aves，媒体库拿到的是新
  size，Aves 读到的也是新标签——所以真正的风险不是"通知丢了"，而是通知**比索引晚**，
  这种情况下手动下拉刷新一次 Aves 即可。
- **连接失败要看"是哪一种失败"，不能只把异常打印出来**。手机上第一次连电脑，
  四种完全不同的错误长得几乎一样：
  `ECONNREFUSED`（包到了，端口上没人听 → 服务没开）、
  `ETIMEDOUT`（包发出去没人回 → **几乎总是电脑的防火墙**）、
  `EHOSTUNREACH`/`ENETUNREACH`（根本没有到那儿的路 → 地址不对或不在同一网络）、
  `UnknownHostException`（地址本身没看懂）。四种的修法完全不同，而在手机屏幕上它们
  都是同一坨 Java 堆栈。所以有了 `NetDiagnosis.kt`：分类，然后直接说该执行哪条命令。
  两个反直觉的地方，都是测试逼出来的：① **光看消息文本不够**——`SocketTimeoutException`
  的消息常常压根没有 "timeout" 这个词（只有 `failed to connect to /10.0.0.5:8899 after
  8000ms`），纯文本匹配会把它归到"未知错误"；② **光看异常类型也不够**——被丢包和端口没人听
  都可能抛同一个 `ConnectException`。所以两者都用，且**先判最具体的**：一条同时带有
  `EHOSTUNREACH` 和 `Connection timed out` 字样的消息必须先被认成"没有路由"，
  否则用户会被指去修防火墙，而真正的问题在地址。
  顺带一提，Android 的连接错误里会嵌**手机自己的 IP**（`... from /192.168.1.5 ...`），
  把它抠出来和电脑的 IP 一比，"不在同一个网段"就是一句话能说清的事。
- **"本机连得上"证明不了"手机连得上"**。回环流量不受 Windows 防火墙管辖，
  所以 `http://127.0.0.1:8899/health` 秒回、`/plan` 也正常，而手机的包可能全部被丢掉——
  两种情况的电脑端表现一模一样，第一反应必然是去怀疑手机或 WiFi。
  而且防火墙的入站规则是**按可执行文件路径**匹配的：这台机器上早就有好几条
  `python.exe` 的放行规则（Python313、uv 装的 3.13、几个 GPT-SoVITS 运行时），
  但没有一条指向 `E:\AlbumCategories\.venv\Scripts\python.exe`，端口 8899 更是一条规则都没有。
  所以 `serve` 启动时和 `albumcat doctor` 都会读一遍 `netsh` 规则给出判断，
  并打印那条能修好它的命令。配置文件（Profile）也不能写死：这台机器同时挂着
  VPN（Private，Radmin VPN 网卡）和 Wi-Fi（Public，i-HDU），Windows 按包从哪个网卡进来挑，
  所以规则用 `profile=any`。**两个坑**：`netsh` 的输出语言会随有没有控制台而变
  （同一条查询，在 shell 里回英文、从 Python 里调回中文），字段名和取值都得双语解析；
  它是**在每条 `Rule Name:` 下面又画一条横线**、而不是只在记录之间画，
  按横线切分会把每条规则劈成两半、让相邻两条的端口和程序互相对调——
  这种错不报错，只会给出一个看着很合理的错误结论。
- **先记日志，再动照片**。这是 `TagRunner.kt` 里唯一一处顺序即安全性的地方。写入走的是
  SAF 的 `openOutputStream("rwt")`——**先截断，再把新字节流进去**。如果 App 被杀、电池没电、
  或手机正好在这一瞬间写满，照片就停在半写状态；此时若日志还没记下它的原始字节，
  那张照片的旧 XMP 就**永久找不回来了**（`undo` 需要 `prev` 才能还原）。所以现在是
  `journal.record(...)` 在前、`writeVerified(...)` 在后。一条"记了日志但写入失败"的行是无害的
  （`undo` 只会把还躺在那里的字节原样写回），而 `undoPlan` 对同一张图只保留**最早**那行，
  重试也不会覆盖运行前的原件。`tools\check_android_runner.py` 的 `failwrite` 场景专门守这条：
  三张图里中间那张每次写入都抛异常，日志里**必须仍是 3 行**。把顺序换回旧的"先写后记"，
  这一条立刻变成 2 行而失败——测试的价值就在于它真的会失败。
- **把平台层换成假实现的测试，测不到那一层里的错**。这是这个项目里最贵的一课。
  `tools\check_android_runner.py` 为了能在桌面上跑 `TagRunner.kt`，不得不**替换**掉
  `Thumbnailer`（解码 + 缩放）和 `ContentResolver`（SAF 读写）——因为安卓的
  `BitmapFactory` / `ContentResolver` 在桌面 JVM 上不存在。结果它 56/56 全绿，
  真机上 4/4 失败，原因就藏在那层假实现盖住的 3 行里：
  `resolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(...) } ?: throw ...`——
  `?.use { }` 返回的是 **lambda 的返回值**而不是流，而测尺寸那一趟
  （`inJustDecodeBounds = true`）按设计就是返回 null，于是 `?:` 每次都触发，
  每张照片都报「打不开」，**尽管它刚刚成功打开了**。
  这类 bug 有一个共同的形状：**替换掉的那一层是唯一没有测试执行过的一层**，
  而它恰好也是唯一没法在桌面上跑的一层。所以补的是 `tools\smoke_device.py`——
  一个**不替换任何东西**的测试，在真实安卓系统上跑完整轮。
  判断标准很简单：**每个"假的"都对应一条没有测试覆盖的真实代码路径**，数一数有几个假的，
  就知道还欠几个真机测试。
- **`.cmd` 只写纯 ASCII，中文一律交给 Python**。cmd.exe 是按 **OEM 代码页**（这里 cp936）
  读 `.cmd` 文件本身的，UTF-8 的中文会被当成两个 GBK 字符，整个字节流从此错位：
  `setlocal enabledelayedexpansion` 变成 `'local' is not recognized`，
  `set "ROOT=..."` 变成 `'t' is not recognized`，输出是 `椤归€氳繃` 这样的乱码。
  这种坏法**不报错，只是脚本被静默撕碎**。所以 `tools\check_all.cmd` 只有十几行 ASCII，
  真正的清单和中文输出都在 `tools\check_all.py` 里。
- **"填了地址，你还说我没填"这类报错比崩溃更难发现，因为它自称知道问题在哪**。
  「请填写电脑端地址」出自 `if (!base.startsWith("http"))`：大小写敏感，于是 `Http://…`
  被拒，而输入框里明明有地址；看到这句话的人只会把同一串再打一遍然后是同一个错误。
  更坏的是它**在说谎**——真实状态是输入框被 `restore()` 刷成了空（地址以前只在点「开始」
  时才保存，进程被系统回收或仅仅转个屏就丢），而空框里躺着一行灰色的示例地址，
  看上去是填好的。教训：**报错必须回显它看到的东西**。「第 15 个字符是全角的 `：`」是可
  执行的；「请填写地址」不可执行，而且会把所有人——包括写代码的自己——引向错误的假设
  （人会去检查网络、防火墙、`serve` 有没有起，唯独不会怀疑那句提示本身）。
  所以这段逻辑现在在 `ServerAddress.kt`：**纯 JDK，不 import 任何安卓类**，
  为的就是它能在电脑上被 27 条用例逐行断言。和 `NetDiagnosis.kt` 是同一个理由。
- **改对了，却在下一行把自己的说明擦掉了**。地址修好之后 `client()` 会打一行
  `地址已按 http://… 使用（你填的是 Http://…）`——这是整个修复里唯一让人知道
  发生了什么的东西。但 `start()` 是在 `client()` **之后**才清空日志面板的
  （每次运行只看本轮输出，免得几千行糊在一起），于是那行提示刚打出来就被抹掉，
  面板上只剩一次正常跑完的结果。功能上没有任何异常，用户永远不知道自己填错了、
  更不会知道下次可以少打几个字。修法是把提示存进 `addressNotice`，由调用方在
  各自清空**之后**自己发。
  值得记的是**它是被哪一层抓住的**：桌面上那 79 项用例全绿，因为它们只断言
  `ServerAddress.normalize` 的返回值，而返回的确实是对的；`TagRunner` 那 56 条也全绿，
  因为它根本不碰 `MainActivity`。抓住它的是 `tools\smoke_device.py` 里那句
  「日志里必须出现 `地址已按`」——一个站在整条链路最末端、只读屏幕上文字的断言。
  **"输出被清空"这类 bug 只存在于顺序里，任何按函数取值的测试都看不见它**；
  要么断言最终可见的那份输出，要么就别在清空附近打日志。
- **桌面套件全绿，手机上表单第一行却是看不见的**。这次是真的"看不见"：`AndroidManifest.xml`
  用的 `@android:style/Theme.Material.Light` 自带 ActionBar，而 `targetSdk 35` 在安卓 15 上强制
  edge-to-edge——**框架 ActionBar 在这种模式下不再为内容让位，而是直接画在内容之上**。它压住了标题行、
  提示行和地址框的大半：`uiautomator` 导出的矩形里，标题栏和构建戳 `b10 · 2026-10-13` 是肉眼可见地
  互相重叠的。真机（vivo）的系统栏更高，把地址框整个盖没，**于是屏幕上唯一看得见的输入框是「令牌」**，
  用户很自然地把地址填进了那一栏；地址框仍然是空的，App 就报「还没填电脑端地址」。
  两张截图、两个构建、两句不同的报错文本，到这里全部自洽——**不是用户操作失误，也不是装错了 APK**。
  为什么一直没抓住：`tools\check_android_client.py` 把 `TaggerClient` / `Journal` / `ServerAddress` /
  `NetDiagnosis` 都拉到桌面 JVM 上跑，**唯独不编译 `MainActivity.kt`**——它没有布局、没有主题、没有渲染层。
  修法分三层，缺一层都不够：①主题换成 `Theme.Material.Light.NoActionBar`（没有栏就没有遮挡）；
  ②`buildUi()` 装 `setOnApplyWindowInsetsListener`，把状态栏高度补进内边距（`values-v35` 里的
  `windowOptOutEdgeToEdgeEnforcement` 只是保险——它**从 API 36 起就被忽略**，真正兜底的是这个监听器）；
  ③地址框和令牌框各自加一行常驻标签——占位符一有字就消失，两个都填了字的框长得一模一样。
  补的测试也分两层：`check_android_client.py` 里加**静态源码断言**，`smoke_device.py` 里加**几何断言**。
  教训：**"有没有文字"和"看不看得见"是两回事**。那个 bug 存活期间，每个按钮的文字都还能被搜到，
  变的只有矩形坐标——所以任何按文字查找的断言一条都不会红。另外顺手被静态断言挡下的还有 aapt2 的
  一条：**XML 注释里出现连续两个 `-` 会让它拒绝整个文件**，而报错经 GBK 控制台是乱码
  （`注释中不能出现连续两个字符 "--"` 显示成 `ע���в���������ַ��� "--"`）。
- **自家构建把自家自检搞坏了**。`tools\kotlin_jvm.py` 的类路径是「Gradle 缓存里所有 Kotlin 相关 jar」，
  这个取法在缓存只服务过一个项目时没问题（36 个 jar）。构建 Aves fork 之后，同一个缓存里多了
  Kotlin 2.4 的一整套（这台机器上有 10 个版本的 `kotlin-stdlib`、3 个版本的编译器），于是：
  ① 类路径字符串涨到 **40,918 字符**，超过 Windows 命令行 32,767 的限制，
  四个桌面 JVM 用例一起报 `FileNotFoundError: [WinError 206] 文件名或扩展名太长`；
  ② 就算塞得下也是错的——`kotlin-scripting-compiler-embeddable` 停在 2.4.10，会通过 ServiceLoader
  注册进 2.4.20 的编译器里，直接 `ClassCastException: FirExtensionRegistrarAdapter$Companion
  cannot be cast to ProjectExtensionDescriptor`。
  修法是两条：**按编译器版本钉住** `org.jetbrains.kotlin`（`kotlinx-*`、`annotations`、`trove4j`
  这类与 Kotlin 版本无关的组各取最新一份），以及**把命令行参数写进 argfile（`@file`）**，
  绕过命令行长度上限（路径统一换成正斜杠——argfile 解析器会把反斜杠当转义）。
  教训：**"自检依赖的东西"和"项目构建依赖的东西"共用一份缓存时，前者会被后者悄悄改变行为**；
  另外这种失败长得像环境问题（WinError 206 看着像文件丢了），实际是自己昨天构建的副产品。

## 当前状态

| 部分 | 状态 |
|---|---|
| **Aves fork：标签只进相册 App 自己的库** | ✅ **端到端跑通**（`tools\verify_aves_tags.py` 11/11：4/4 探针文件的 SHA256 与 mtime 都不变、数据库 `user_version=17`、appTags 逐文件对得上——每个文件恰好一个 `角色\|X` 标签；提速版 APK 重装后又跑过一遍，仍 11/11） |
| Aves fork：在相册 UI 里按角色浏览 | ✅ 已在模拟器上验过（Tags 页出现 `角色\|Hakurei Reimu 4`、`角色\|Shameimaru Aya 3`、`角色\|Kasane Teto 3`、`角色\|Kagiyama Hina 2` 等磁贴，点进去集合里只剩这个角色的照片；截图在 `docs\evidence\`） |
| Aves fork：纯 App 标签绝不会写进照片 | ✅ 代码闸门 + 调用点改造（`metadata_edition.dart` 的 `editTags()` 把只存在于 appTags 的标签从待写入集合里剔掉，所有回写 UI 改读 `fileTags`；显示/筛选仍读并集），`dart analyze` 干净 |
| Aves fork：构建（x64 / arm64） | ✅ 通过（`tools\aves_build.ps1`，产物 `dist\aves-fork-libre-debug-{x64,arm64}.apk`，各约 252 MB，均已解包确认引擎 ABI 与包名匹配） |
| Aves fork：真机安装 | ⏳ arm64 包已构建好，等真机装上跑一轮 |
| Aves fork：没有局域网时走数据线 | ✅ 已验（`tools\phone_link.cmd` = `adb reverse tcp:8899 tcp:8899`；App 侧 `resolveReachable()` 先探填的地址再退回 `http://127.0.0.1:8899`。模拟器上把发往电脑 `10.0.2.2:8899` 的出站流量 `REJECT` 掉后，25 张仍 19 写 / 0 失败，日志写「电脑端在数据线上」；删规则后又变回局域网、3.1 s。真 USB 电缆本身没验，adb 在模拟器上走 loopback） |
| Aves fork：打标提速（自解码 448px + q88 + 批内并发） | ✅ 已量（24 张：**4.45 s → 3.25 s → 2.83 s**，185 → 135 → 95 ms/张，传输字节 5162 → 2943 → 1034 KB；精度用同一批 24 张 A/B 量过两轮：448/q88 vs 512 DIP/q100 是 **16/17 标签逐字相同**（唯一差异是一张界面截图里的临界角色），自解码 vs 系统取图是 **17/17 逐字相同**） |
| Aves fork：打标中途断了，已保存的标签不丢 | ✅ 已验（一批一落盘 + 单批重试一次 + 连着 3 批失败才停。A/B：`tools\slow_proxy.py` 在第 1 批之后返回 503，电脑端同样回完 8 张 → 改前 `appTags` 新增 **0** 行、改后新增 **6** 行，且都以「完成：」收尾。另有两处防御性收口：`media_store_source.dart` 只在过期条目不超过库容 **1/4** 时才清 `appTags`，`MediaStoreImageProvider.kt` 把查询失败当「未知」而不是「空」——这两处**没有**造出真故障复现） |
| 核心引擎（推理/规则/插件/发布/撤销） | ✅ 代码完成 |
| 模型下载、依赖安装、DirectML 后端 | ✅ 就绪 |
| 首次真实推理（预处理契约验证） | ✅ 通过（纯色/渐变图给出 `Monochrome`/`no Humans` 等应有标签） |
| 768 维特征输出 + `feature` 插件链路 | ✅ 通过（`tools\check_feature_plugin.py` 自检，分数与独立计算吻合到 1e-7） |
| `feature` 插件的**训练**闭环（由修正样本训头） | ✅ 通过（`albumcat train`，合成数据与真实图像上都验过端到端） |
| 真实动漫图上的角色识别效果 | ✅ 通过（4 张样图 4/4 与人工判定一致，含一次正确的"复核带"降级） |
| **XMP 路线：写进照片 + 撤销** | ✅ 通过（像素逐字节不变、标签树正确、撤销后 SHA256 与原图一致；写出的包经 Pillow 独立解析器核对） |
| XMP 路线：局域网打标服务 `albumcat serve` | ✅ 通过（HTTP 端到端：6 张样图角色识别与本地一致，缩略图 30–42 KB，约 90–190 ms/张，缓存命中可用；任意格式都能**打标**——服务端只收 448px 缩略图，能不能打标和能不能写回 XMP 是两件事） |
| XMP 路线：手机 App（Kotlin） | ✅ 编译通过且已装到设备上跑过（`app-debug.apk` 940,951 字节 / versionCode 3 / versionName 0.1.2，minSdk 26 / targetSdk 35）；`Xmp.kt` 与 `xmp.py` 已用 `tools\check_kotlin_xmp.py` 逐字节对拍通过（JPEG 与 PNG 两种格式都各自比过，含 Pillow 独立读回） |
| XMP 路线：别人写的 XMP 不被抹掉 | ✅ 通过（`xmp.py` 与 `Xmp.kt` 都保留非我方属性；两份实现对「什么是别人的」判定一致、写出的字节相同；`.tmp\test_foreign_xmp.py` + `.tmp\test_png_xmp.py` + `tools\check_kotlin_xmp.py` 第 4 项 + `tools\simulate_phone.py` 的相机元数据场景） |
| XMP 路线：手机端整条链路彩排 | ✅ 通过（`tools\simulate_phone.py` 用 App 自己的 `Xmp.kt` 跑 5 张真实照片 + **1 张 1.53 MB 的安卓截屏 PNG** + 1 张带相机元数据的照片，62/62：缩略图识别结果正确、Pillow 独立解析器读得到层级标签、**Aves 要读的 `dc:subject` 里确实有层级路径**、像素逐字节未变、重复跑不堆标签、撤销后 SHA-256 与原件相同） |
| XMP 路线：PNG 支持（安卓截屏） | ✅ 通过（`xmp.py` 与 `Xmp.kt` 各一套 iTXt 实现，`tools\check_kotlin_xmp.py` 第 5 项逐字节对拍 + 用 Pillow 读手机代码写出的 PNG；这个格式曾经因为块头多写一个字节而**两边自己都读得通、只有 Pillow 报错**，所以独立实现这一环是硬要求） |
| XMP 路线：确认 Aves 读哪个字段 | ✅ 已读源码定论（Aves 只从 `dc:subject` 建标签，不读 `lr:HierarchicalSubject` / `digiKam:TagsList`；据此刻意把层级路径也写进 `dc:subject`，见 `publish.tree_in_subject`） |
| XMP 路线：写完通知系统媒体库 | ✅ 通过（`Rescan.kt` 写完/撤销后调 `MediaScannerConnection.scanFile`，路径由 `DocumentPaths.kt` 从 SAF 文档 id 推出；映射逻辑用 `tools\check_android_client.py` 离线测过 10 种文档 id，`tools\check_android_runner.py` 验过 70 张只调 2 次扫描；真机侧已确认媒体库拿到了新的大小，Aves 重开后读到的就是新标签） |
| XMP 路线：App 的 HTTP 客户端与撤销日志 | ✅ 通过（`tools\check_android_client.py` 把 App 真实的 `TaggerClient.kt` / `Journal.kt` 拉到桌面 JVM 上跑真实服务端，82/82：每次 plan 的每个字段都与 Python 解析同一响应一致，确认标签是真读到的而非静默空数组；Journal 的重试保留最早一行、`prev` 逐字节可还原、半行垃圾不报废整份日志；`NetDiagnosis.kt` 对 12 种真实连接失败各归各类；`ServerAddress.kt` 对 27 种地址写法——含用户实际报错的那条 `Http://…`——该修的修对、该拒的拒，且拒绝时说出看到的是哪个字符） |
| XMP 路线：App 的写入主循环 `TagRunner.kt` | ✅ 通过（`tools\check_android_runner.py` 用内存假相册在桌面 JVM 上真跑 6 个场景 56/56：HEIC 被计数而非静默丢弃、重跑全部跳过且字节不变、撤销逐字节还原、**PNG 走的是另一条代码路径且像素逐字节未变**、70 张只触发 2 次媒体扫描、断线连续失败 8 张即停且一张未改、写入失败的那张也已先进日志、**半路按停止正好停在第 3 张且没轮到的 2 张一个字节没动**） |
| XMP 路线：长任务中途能停 | ✅ 通过（`TagRunner.requestStop()` 在写入循环、文件夹遍历、撤销三处都生效，界面上的「停止」按钮即时可点；`stop` 场景把检查删掉会让 8 条断言变红。日志也加了 400 行上限，否则几千张跑下来 `TextView` 会越append越慢且吃光内存） |
| XMP 路线：局域网可达性（防火墙） | ⚠️ 已查实**本机当前是封的**并给出修法（`src\albumcat\firewall.py` 读 netsh 规则判断有没有覆盖本虚拟环境解释器的入站放行；`albumcat doctor` 与 `albumcat serve` 启动都会提示；`tools\allow_firewall.cmd` 一键补规则。本机上已有的 python 放行规则全都指向别的解释器，端口 8899 无规则） |
| XMP 路线：连不上时 App 能说清是哪种错 | ✅ 通过（`NetDiagnosis.kt` 把「防火墙丢了包 / 端口没人听 / 网段不对 / 地址写错」四种长得几乎一样的异常分开，并指出该执行哪条命令；`tools\check_android_client.py` 用 12 条真实的 Android 异常文本断言分类，其中 2 条只有靠**异常类型**才分得出来） |
| XMP 路线：地址填对了却被说「没填地址」 | ✅ 已修（报错来自 `MainActivity` 里一句大小写敏感的 `startsWith("http")`，外加地址只在点「开始」时才存、进程一被回收就被 `restore()` 刷回空白、而空框显示灰色示例看着像填好了。改成独立的 `ServerAddress.kt`（纯 JDK，不 import 安卓类）统一归一化 + 输入即保存；`tools\check_android_client.py` 27 条地址用例逐行断言，含用户实际输入的 `Http://192.168.83.124:8899`） |
| XMP 路线：地址框被系统栏盖住，于是「填了地址还说没填」 | ✅ 已修（`AndroidManifest.xml` 原先用自带 ActionBar 的 `Theme.Material.Light`，而 `targetSdk 35` 在安卓 15 上强制 edge-to-edge，**框架 ActionBar 改为画在内容之上**，压住了标题行、提示行和地址框的大半；真机上系统栏更高，屏幕上唯一看得见的输入框只剩「令牌」，用户把地址填进了那一栏，地址框仍然是空的。改成 `Theme.Material.Light.NoActionBar` + `buildUi()` 里 `setOnApplyWindowInsetsListener` 补状态栏内边距 + 两个输入框各加一行常驻标签；模拟器截图前后对比已确认，并新增两条防线：`check_android_client.py` 的**静态源码断言**（主题必须 NoActionBar、内容必须避开系统栏、两个框必须有可见标签）与 `smoke_device.py` 的**几何断言**（带文字控件两两不重叠）。注意这一版 `MainActivity.kt` 不被任何桌面用例编译，见「技术要点」） |
| XMP 路线：手机 App 在真机上跑通 | ✅ **已跑通**（`tools\smoke_device.py` 在安卓 15 模拟器上无人值守跑完整轮 18/18：先断言版式没被遮挡 → 装 APK → 走真 SAF 拾取器授权 → 点「开始」→ 断言 4 张全部被改（SHA-256 真变了，不是「日志说改了」）→ 再点一次断言「跳过 4 张」且字节未动 → 点撤销断言逐字节还原。约 40 秒。**这一步抓到了一个桌面套件测不到的真 bug**，见「技术要点」） |
| XMP 路线：手机上 Aves 能否读到标签 | ✅ **已在设备上验证**（模拟器装 Aves 的 libre 构建，索引后直接 `adb pull` 它的 `metadata.db`：`xmpSubjects` 列里就是我们写的 `角色\|Shameimaru Aya` 那一串，PNG 与 JPEG 各一行、内容一致，`rating` 也读到了。注意 libre 构建包名是 `deckers.thibault.aves.libre`） |
| XMP 路线：整条链路在真机上的可重复性 | ✅ 通过（`tools\smoke_device.py` 把此前手工敲的那套步骤——推图 / 装包 / root 预置 prefs / 驱动 SAF / 点按钮 / 校验哈希——变成一条命令，含「重跑必须跳过」「撤销必须逐字节还原」两组不变式；它需要设备所以不在 `check_all.cmd` 里） |
| Immich 路线：端到端 | ⏳ Docker/WSL 已装好，待建库并填 API key |
| HEIC 支持 | ❌ 未做（见"已知限制"） |

## 已知限制

- **fork 的标签只属于 Aves**。标签在 Aves 自己的数据库（`appTags` 表）里，
  所以电脑上的 Lightroom、手机上的别的相册 App 都看不到；卸载 Aves 或清它的数据，标签就一起没了，
  换手机也不会跟着走。想「标签随照片走」只能回到 XMP 路线——那条路是通的，代价是照片的修改时间会变。
- **fork 基于上游 Aves 1.15.5**，改动落在 **27 个已有文件 + 5 个新文件**上（数据模型见上表，
  其余是 5 个回写调用点、设置、服务、打标页，以及 Kotlin 侧为「质量换体积」和「自己解图」新开的
  `jpegQuality` 参数与 `getByDecoding()`——`BitmapUtils.kt` + `DebugHandler.kt` + `ThumbnailFetcher.kt`；
  另有构建配置若干，`pubspec.lock` 占了大头）；
  上游更新后要 rebase。验证只做到「模拟器 + Aves 1.15.5 libre」，换设备换版本要重量一遍。
- **抗中断只覆盖了「电脑端消失 / 连接断」这一类**。`media_store_source.dart` 里「大批量判定为过期」
  的护栏（超过库容 **1/4** 就不清 `appTags`）与 `MediaStoreImageProvider.kt` 把「查询失败」当
  「未知」而不是「空」，都是**防御性**修法，没能在设备上造出对应故障来复现
  （权限一撤安卓就直接杀进程、重进只弹授权框，进不到扫描；新推的文件又写不进模拟器的 MediaStore）。
  它们的验证只到「编译通过 + 代码路径读通」。副作用是双向的：真有大批照片被删（超过 1/4）时，
  `appTags` 里会留下几行孤儿标签——最多几行字节，比丢掉一整轮标签便宜。
- **只处理 JPEG 和 PNG，不处理 HEIC**。这两种是刻意选的：安卓**截屏就是 PNG**，
  而截屏恰恰是最值得按角色分类的那批图；JPEG 是相机默认。
  `tag` 遇到 `.heic` / `.webp` 会跳过并在报告里记一条错误；App 端会**把这些张数报给你**
  （「另有 N 张 HEIC/WebP 等格式的图片被跳过」），而不是让你看到一个说不出所以然的「找到 12 张」。
  HEIC 的 XMP 在 MP4 风格的 box 里，写进去要重建 `iinf`/`iloc`/`iref` 三张表，
  风险和工作量都明显更高，所以没做。**HEIC 是 iPhone 默认格式**，
  真要走 iPhone 这条路必须补上（安卓相机可以设成 JPEG，所以现在不挡路）。
- **PNG 那条路和 JPEG 不是一回事，两套实现都得单独验**。PNG 的 XMP 在 `iTXt` 块里，
  块头除了 XML 还有压缩标志/方法/两个空文本字段——**这几个字节数错了，我们自己的 reader
  照样读得通**（正文前面只是多个 NUL），只有真解析器会拒绝。所以 `xmp.py` 和 `Xmp.kt`
  都写了一段，且两家都被独立实现（Pillow）验过：`tools\check_kotlin_xmp.py` 第 5 项
  和 `tools\check_android_runner.py` 的 PNG 断言。`undo` 的日志里存的是**格式相关的
  blob**（JPEG 存 packet，PNG 存整段 iTXt payload），所以只有 `xmp.find_block` /
  `restore_photo` 能解释它，别自己去 decode。
- **Aves 的字段选择和索引行为都已验过，但只在模拟器上验过**。字段是读源码定的：它只从
  `dc:subject` 建标签，我们也照这个写了（`publish.tree_in_subject`）。索引行为是在安卓 15
  模拟器装 Aves 后拉它的 `metadata.db` 确认的（见上面「Aves 那一端，在设备上也是验过的」）。
  换成你自己的手机、自己的 Aves 版本时，值得第一件事就做这个：把
  `tools\make_aves_probe.py` 生成的照片拷进手机、等它扫完、看 Tags 页有没有
  `角色|AVES-PROBE-1234`。**换设备换版本，验的东西就得重新验一遍。**
- Immich 是自建服务，不是严格意义上的"第三方手机相册"。手机上装的是官方 Immich App。
- `feature` 插件的分类头质量取决于标注样本的数量和覆盖面；本项目提供的是训练工具
  和自检，样本还得自己攒。
- WD14 的模型 batch 维写死为 1，`model.batch_size` 实际不起作用；已实测批处理也没有收益
  （DirectML 单张 101.8 ms，batch 反而更慢）。

## 许可

上游 `third_party/wd14-API-onnx` 为 MIT。模型 `wd-v1-4-vit-tagger-v2` 的使用请遵守
其 Hugging Face 页面上的条款。
