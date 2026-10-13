# 开发与验证

> 从源码构建 Aves fork、跑验证脚本、目录结构，以及踩过的坑。

> 本文是 [README](../README.md) 的延伸阅读，可以直接跳读。

## 构建

```
tools\aves_build.ps1                                      # 模拟器 x86_64
tools\aves_build.ps1 -Platform android-arm64 -NoVerbose    # 真机 arm64
```

⚠️ fork 的源码**不在这个仓库里**：它是 [`deckerst/aves`](https://github.com/deckerst/aves) 的一份副本，
放在本机的 `.tmp\aves-src\`（改动清单见 [设计说明](DESIGN.md)）。所以上面两条命令要在那台已经打好改动的
工作机上跑，没打过改动的 upstream 是构建不出 AI 打标签页的。

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

## 验证

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

## 踩过的坑

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

---

## 项目结构

目录里还有两条已舍弃路线留下的文件（写 XMP、写 Immich，以及那个旧的 Kotlin SAF App）：它们仍在仓库里，但不再维护；目录树里标了「（已舍弃）」的就是它们，始末见 [LEGACY](LEGACY.md)。

```
albumcat/
├─ config.toml                  配置（见文件内注释）
├─ requirements.txt
├─ src/albumcat/
│  ├─ tagger.py                 WD14 ONNX 推理（照抄上游预处理契约）
│  ├─ rules.py                  规则引擎：分数 → 发布决策
│  ├─ plugins.py                插件注册表与五种 Recognizer
│  ├─ xmp.py                    JPEG/PNG 内部 XMP 的读写（XMP 路线的核心）（已舍弃）
│  ├─ publishers.py             JpegXmpSink + ImmichPublisher + 操作日志（可撤销）（已舍弃）
│  ├─ server.py                 给手机用的局域网打标服务（纯标准库 HTTP）
│  ├─ firewall.py               启动/自检时看 Windows 会不会把手机的包丢掉
│  ├─ immich.py                 Immich REST 客户端（已舍弃）
│  ├─ pipeline.py               串起 扫描→推理→插件→规则
│  ├─ types.py                  Prediction / TagSet / TagPlan
│  ├─ config.py                 配置加载
│  └─ cli.py                    命令行
├─ android/                     旧的 Kotlin SAF App（已舍弃）；当前做法是下面的 Aves fork
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
│        ├─ Xmp.kt               与 xmp.py 同构的 XMP 读写（已舍弃）
│        ├─ DocumentPaths.kt     SAF 文档 id → 文件路径（只依赖 JDK，可离线测）
│        ├─ Rescan.kt            写完告诉系统媒体库「这张图变了」，相册才知道要重读
│        └─ Journal.kt           手机上记录「原包 → 新包」，用于撤销
├─ tools/
│  ├─ download_model.py         模型下载（纯标准库，可续传）
│  ├─ add_feature_output.py     给模型加特征输出（替代上游 1.2GB 拆分模型）
│  ├─ check_feature_plugin.py   feature 插件链路自检
│  ├─ setup_android.py          下载 Android SDK（绕开坏掉的 Windows TLS）
│  ├─ make_aves_probe.py        生成「哨兵标签」探针图（3 张 JPEG + 1 张 PNG），验证 Aves 读不读 XMP
│  ├─ check_kotlin_xmp.py       把 App 的 Xmp.kt 拿到桌面 JVM 上跑，和 xmp.py 逐字节对拍（JPEG + PNG）（已舍弃）
│  ├─ simulate_phone.py         在电脑上彩排 App 整条链路（只差 SAF）（已舍弃）
│  ├─ check_android_client.py   把 App 真实的 TaggerClient.kt / Journal.kt 拿到桌面 JVM 上跑（已舍弃）
│  ├─ check_android_runner.py   把 App 真实的 TagRunner.kt 拿到桌面 JVM 上，用假相册跑完整流程（已舍弃）
│  ├─ smoke_device.py           在真机/模拟器上无人值守跑完整轮（需要设备，故不在 check_all 里）（已舍弃）
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
├─ dist/                        构建好的 Aves fork 安装包（arm64 给真机 / x64 给模拟器；不进仓库）
├─ docs/evidence/               验证截图（按角色浏览、筛选、打标页）
└─ .tmp/aves-src/               Aves fork 的工作副本（改动见 DESIGN.md；不进主仓库）
```

---

[← 回到 README](../README.md)