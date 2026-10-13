# 当前状态与已知限制

> 哪些做法已经真机验过、哪些只有代码或模拟器证据、哪些坑还没填。

> 本文是 [README](../README.md) 的延伸阅读，可以直接跳读。

## 当前状态

| 部分 | 状态 |
|---|---|
| **Aves fork：标签只进相册 App 自己的库** | ✅ **端到端跑通**（`tools\verify_aves_tags.py` 11/11：4/4 探针文件的 SHA256 与 mtime 都不变、数据库 `user_version=17`、appTags 逐文件对得上——每个文件恰好一个 `角色\|X` 标签；提速版 APK 重装后又跑过一遍，仍 11/11） |
| Aves fork：在相册 UI 里按角色浏览 | ✅ 已在模拟器上验过（Tags 页出现 `角色\|Hakurei Reimu 4`、`角色\|Shameimaru Aya 3`、`角色\|Kasane Teto 3`、`角色\|Kagiyama Hina 2` 等磁贴，点进去集合里只剩这个角色的照片；截图在 `docs\evidence\`） |
| Aves fork：纯 App 标签绝不会写进照片 | ✅ 代码闸门 + 调用点改造（`metadata_edition.dart` 的 `editTags()` 把只存在于 appTags 的标签从待写入集合里剔掉，所有回写 UI 改读 `fileTags`；显示/筛选仍读并集），`dart analyze` 干净 |
| Aves fork：构建（x64 / arm64） | ✅ 通过（`tools\aves_build.ps1`，源码就是仓库里的 `aves/`；x64 约 188 MB、arm64 约 204 MB，均已解包确认引擎 ABI 与包名匹配） |
| Aves fork：真机安装 | ⏳ arm64 包已构建好，等真机装上跑一轮 |
| 发布 | ✅ [Releases v0.1.0](https://github.com/Shuul0707/wd14_albumcat/releases/tag/v0.1.0) 里放着两个 APK。干净安装（先卸载、再装 release 包、走完首启授权）后跑一轮：**12 张全部打上标签、0 失败**，`tools\verify_aves_tags.py` 11/11 |
| Aves fork：没有局域网时走数据线 | ✅ 已验（`tools\phone_link.cmd` = `adb reverse tcp:8899 tcp:8899`；App 侧 `resolveReachable()` 先探填的地址再退回 `http://127.0.0.1:8899`。模拟器上把发往电脑 `10.0.2.2:8899` 的出站流量 `REJECT` 掉后，25 张仍 19 写 / 0 失败，日志写「电脑端在数据线上」；删规则后又变回局域网、3.1 s。真 USB 电缆本身没验，adb 在模拟器上走 loopback） |
| Aves fork：打标提速（自解码 448px + q88 + 批内并发） | ✅ 已量（24 张：端到端 **2.83 s**，**95 ms/张**，每张传输 43 KB。精度用同一批 24 张 A/B 量过：448/q88 vs 512 DIP/q100 是 **16/17 标签逐字相同**（唯一差异是一张界面截图里的临界角色），自解码 vs 系统取图是 **17/17 逐字相同**） |
| Aves fork：打标中途断了，已保存的标签不丢 | ✅ 已验（一批一落盘 + 单批重试一次 + 连着 3 批失败才停。A/B：`tools\slow_proxy.py` 在第 1 批之后返回 503，电脑端同样回完 8 张 → 改前 `appTags` 新增 **0** 行、改后新增 **6** 行，且都以「完成：」收尾。另有两处防御性收口：`media_store_source.dart` 只在过期条目不超过库容 **1/4** 时才清 `appTags`，`MediaStoreImageProvider.kt` 把查询失败当「未知」而不是「空」——这两处**没有**造出真故障复现） |
| 核心引擎（推理/规则/插件/发布/撤销） | ✅ 代码完成 |
| 模型下载、依赖安装、DirectML 后端 | ✅ 就绪 |
| 首次真实推理（预处理契约验证） | ✅ 通过（纯色/渐变图给出 `Monochrome`/`no Humans` 等应有标签） |
| 768 维特征输出 + `feature` 插件链路 | ✅ 通过（`tools\check_feature_plugin.py` 自检，分数与独立计算吻合到 1e-7） |
| `feature` 插件的**训练**闭环（由修正样本训头） | ✅ 通过（`albumcat train`，合成数据与真实图像上都验过端到端） |
| 真实动漫图上的角色识别效果 | ✅ 通过（4 张样图 4/4 与人工判定一致，含一次正确的"复核带"降级） |
| HEIC 支持 | ❌ 未做（见"已知限制"） |

## 已知限制

- **fork 的标签只属于 Aves**。标签在 Aves 自己的数据库（`appTags` 表）里，
  所以电脑上的 Lightroom、手机上的别的相册 App 都看不到；卸载 Aves 或清它的数据，标签就一起没了，
  换手机也不会跟着走。
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
  `.heic` / `.webp` 会被跳过并在报告里记一条错误，而不是让你看到一个说不出所以然的「找到 12 张」。
- **Aves 的字段选择和索引行为都已验过，但只在模拟器上验过**。字段是读源码定的：照片文件自带的标签
  它只从 `dc:subject` 读（`publish.tree_in_subject` 让层级路径也进手机端存的那份扁平列表）。
  索引行为是在安卓 15
  模拟器装 Aves 后拉它的 `metadata.db` 确认的（见 [PHONE](PHONE.md) 的「Aves 那一端，在设备上也是验过的」）。
  换成你自己的手机、自己的 Aves 版本时，值得第一件事就做这个：把 `tools\make_aves_probe.py`
  生成的 4 张探针图拷进手机、等它扫完、在「AI 打标签」里跑一轮，再拉 `metadata.db` 看
  `appTags` 里是不是每个文件恰好一行 `角色|X`（`tools\verify_aves_tags.py` 就是这段的自动版）。
  **换设备换版本，验的东西就得重新验一遍。**
- `feature` 插件的分类头质量取决于标注样本的数量和覆盖面；本项目提供的是训练工具
  和自检，样本还得自己攒。
- WD14 的模型 batch 维写死为 1，`model.batch_size` 实际不起作用；已实测批处理也没有收益
  （DirectML 单张 101.8 ms，batch 反而更慢）。

---

[← 回到 README](../README.md)