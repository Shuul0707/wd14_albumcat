# 当前状态与已知限制

> 哪些做法已经真机验过、哪些只有代码或模拟器证据、哪些坑还没填。

> 本文是 [README](../README.md) 的延伸阅读，可以直接跳读。

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

---

[← 回到 README](../README.md)
