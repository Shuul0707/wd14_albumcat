# 开发与验证

> 从源码构建 Aves fork、跑验证脚本、目录结构，以及踩过的坑。

> 本文是 [README](../README.md) 的延伸阅读，可以直接跳读。

## 构建

```
tools\aves_build.ps1 -Platform 'android-arm,android-arm64,android-x64'   # 通用包（发布用）
tools\aves_build.ps1 -Platform android-x64                              # 只给模拟器，不要发布
```

⚠️ fork 的源码就在仓库的 `aves/` 下（[`deckerst/aves`](https://github.com/deckerst/aves) 的一份副本，
改了什么见 [设计说明](DESIGN.md)，构建细节见 [aves/FORK.md](../aves/FORK.md)）。第一次构建要装
JDK 21 + Flutter 3.47.6，首次 `flutter build` 会自己生成 `android/gradlew` 与
wrapper，这两个文件用不着入库。

产物是 `aves/build/app/outputs/flutter-apk/app-libre-debug.apk`（通用包 **380,983,207 B**；
debug 包之所以这么大，是 media-kit 的 AAR 把三套 ABI 的 libmpv 都带进来了，加上
`com.microsoft.onnxruntime:onnxruntime-android` 又把 `libonnxruntime.so` 塞进了三个 ABI ——
合并语义搜索前是 198 MB，多出来的约 75 MB 就是 onnxruntime；三套 Flutter 引擎各 33–40 MB
是通用包的另一个大头）。

⚠️ **必须把三个 target-platform 一起传，否则真机一打开就崩**：`--target-platform` 决定的是
**把哪个 ABI 的 Flutter 引擎（`libflutter.so`、`libicu4x.so`）打进包里**。插件库（onnxruntime、
mpv、tiff、jpeg……）来自 AAR，三个 ABI 永远都在 —— 所以只看目录名会以为包是通用的，其实
`-Platform android-x64` 构建出来的 APK 里 `lib/arm64-v8a/` 只有 9 个插件库、**没有 `libflutter.so`**，
装到 arm64 手机上能装成功、也能起进程，然后在加载 flutter 引擎时直接死掉（v0.3.0 / v0.4.0
就是这样，用户报「打开之后就崩溃」）。核对办法：

```powershell
python -c "import zipfile;z=zipfile.ZipFile('aves/build/app/outputs/flutter-apk/app-libre-debug.apk');print([i.filename for i in z.infolist() if i.filename.endswith('libflutter.so')])"
```

三个 ABI 的 `libflutter.so` 都要出现，缺一个就说明这个包只能在指定的 ABI 上跑。

国内网络下这条构建能跑通，靠的是三处已经落盘的镜像，不用再配：
`~\.gradle\init.d\zz-mirrors.gradle`（Gradle 全局 init 脚本，把 `dl.google.com`、`repo1.maven.org`、
`plugins.gradle.org` 重写到阿里云）、`PUB_HOSTED_URL=https://pub.flutter-io.cn` +
`FLUTTER_STORAGE_BASE_URL=https://storage.flutter-io.cn`、Android SDK 包从腾讯镜像装
（`tools\setup_android.py` + `tools\sdk_install.py`；这条线上 `dl.google.com` 只有 21 KiB/s，腾讯镜像 26 MB/s）。

⚠️ `tools\` 里的脚本以 Windows 为主：`*.cmd` 是 cmd.exe 的入口、`aves_build.ps1` 是 PowerShell、
`firewall.py` 调 `netsh`。Linux / macOS 上 `python -m albumcat …` 与 pytest 都能跑，
构建与设备脚本要自己翻译。

## 自动检查（不用设备，也不用模型）

```bat
python -m pytest              REM 规则 / 标签字段 / 配置 / 类型 / 查询解析的单元测试，不到两秒
python tools\check_repo.py    REM 仓库卫生：*.cmd 纯 ASCII、疑似密钥、文档链接、.gitignore
tools\check_all.cmd           REM 上面两条 + 四条端到端的全跑（要模型；最贵那条真的起一个 serve）
```

`tests\test_tagdict.py` 钉住 `/query` 的解析规则：角色名与情绪词要分开、作品名要展开成角色表、
中英日三种写法落到同一个 tag、长匹配优先于短匹配。

`check_all` 支持 `-k` 只跑名字匹配的检查（`tools\check_all.cmd -k firewall`）和 `--list` 看清单。
CI 在 `.github/workflows/ci.yml`：push / PR 时跑 pytest、`check_repo.py`、`albumcat --help`（CPU 后端，
runner 上没有 DX12）；Flutter 侧的分析 `.github/workflows/flutter.yml` 是手动触发的。

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

```
albumcat/
├─ config.toml                  配置（见文件内注释）
├─ requirements.txt             依赖清单（等价于 pyproject 的 base + 一个 onnxruntime 变体）
├─ pyproject.toml               可 `pip install -e ".[dml,dev]"`；pytest 配置也在这
├─ LICENSE                      MIT（本仓库自己的代码；aves/ 沿用上游 BSD-3）
├─ CONTRIBUTING.md              环境、验法、代码风格、上游升级
├─ tests/                       pytest：规则 / 标签字段 / 配置 / 类型
├─ .github/workflows/           ci.yml（pytest + 仓库卫生）、flutter.yml（手动）
├─ src/albumcat/
│  ├─ tagger.py                 WD14 ONNX 推理（照抄上游预处理契约）
│  ├─ rules.py                  规则引擎：分数 → 发布决策
│  ├─ plugins.py                插件注册表与五种 Recognizer
│  ├─ server.py                 给手机用的局域网打标服务（纯标准库 HTTP）
│  ├─ firewall.py               启动/自检时看 Windows 会不会把手机的包丢掉
│  ├─ pipeline.py               串起 扫描→推理→插件→规则
│  ├─ types.py                  Prediction / TagSet / TagPlan
│  ├─ config.py                 配置加载
│  ├─ cli.py                    命令行
│  ├─ tagfields.py              flat / hierarchical 标签字段（手机端存的就是 flat 那份）
│  ├─ tagdict.py                查询解析：中文句子 → WD14 标签（含作品名展开）
│  └─ data/tag_zh.json          中文对照表（9083 标签 / 18058 匹配键 / 343 系列，约 1 MB）
├─ tools/
│  ├─ download_model.py         模型下载（纯标准库，可续传）
│  ├─ add_feature_output.py     给模型加特征输出（替代上游 1.2GB 拆分模型）
│  ├─ build_tag_zh.py           用 ffdkj 的 Danbooru 中文对照表重建 data/tag_zh.json
│  ├─ export_chinese_clip_onnx.py  导出 Chinese-CLIP 两个 ONNX 塔（语义搜索/混合检索用）
│  ├─ check_feature_plugin.py   feature 插件链路自检
│  ├─ check_firewall.py         防火墙自检
│  ├─ check_semantic.py         /semantic、/encode_text、/query 的 HTTP 契约自检
│  ├─ check_plan_batch.py       /plan_batch 批量接口自检（自带一个临时服务端）
│  ├─ make_aves_probe.py        把样例图拷成 4 张探针图（端到端验证用）
│  ├─ make_alpha_probe.py       造一张带透明通道的 PNG，验证取图时透明像素被压成白底
│  ├─ aves_build.ps1            构建 aves/ 的 APK（-Platform 可传多个 ABI，发布要三个都传）
│  ├─ verify_aves_tags.py       Aves fork 端到端验证：探针文件哈希/时间戳不变 + appTags 逐文件断言
│  ├─ phone_link.cmd            没有局域网时用数据线：adb reverse 把手机的 127.0.0.1:8899 接到电脑
│  ├─ slow_proxy.py             假服务端：第 N 批之后开始返 503，用来量「中途断了会不会丢标签」
│  ├─ check_all.cmd             一条命令跑全部自检（纯 ASCII 薄壳）
│  ├─ check_all.py              自检清单本体，从便宜到贵排序
│  ├─ check_repo.py             仓库卫生：*.cmd 纯 ASCII / 疑似密钥 / 文档链接 / .gitignore
│  ├─ run_tests.py              给 check_all 用的 pytest 壳
│  ├─ install_deps.cmd          依赖安装（建 venv + 清华源）
│  ├─ allow_firewall.cmd        一键允许 8899 入站（会弹 UAC）
│  ├─ albumcat.cmd              PC 端命令入口
│  ├─ setup_android.py          下载 Android SDK（绕开坏掉的 Windows TLS）
│  └─ sdk_install.py            把 SDK 包解到指定目录（给 setup_android.py 和手工安装共用）
├─ plugins/                     插件目录（见 plugins/README.md）
├─ models/vit/                  model.onnx + model_features.onnx + selected_tags.csv
├─ third_party/wd14-API-onnx/   上游项目（MIT），保留作参考实现
├─ aves/                        手机端 Aves 的 fork 源码（改了什么、怎么构建见 aves/FORK.md）
├─ dist/                        构建好的 fork 安装包（通用包，三个 ABI 都带引擎；不进仓库）
├─ docs/evidence/               验证截图（按角色浏览、筛选、打标页）
└─ .tmp/                        scratch：日志、拉下来的库、一次性脚本（不进仓库）
```

---

[← 回到 README](../README.md)

---

## 新增 Dart 文件时，记得加进 CI 清单

`.github/workflows/flutter.yml` 里的 `dart analyze` 只分析**明确列出的文件**，
不是整个 `lib/`。这么设计是为了不跑上游那几百个文件的告警。

代价是：**新文件不进清单，就永远不会被检查**。PR #1 正是这样漏掉三处编译错误的
—— `dart analyze` 一分钟就能抓到，但那些文件不在清单里。

所以新增 Dart 文件时，同步改这个清单。目前需要补的是：

```
lib/model/app_ocr.dart
lib/model/app_ocrs.dart
lib/widgets/search/collection_search_delegate.dart
```

另外 `lib/widgets/dialogs/semantic_search_page.dart` 已经在 5c85490 里被删掉
（语义搜索并入原生搜索框），但清单还留着它，`dart analyze` 会直接报"文件不存在"。
这一条顺手改一下。

> 推送账号没有 GitHub 的 `workflow` scope，改不了 `.github/workflows/*`，
> 所以上面这几行需要维护者手动同步，或者换一个有该 scope 的 token 推送。
