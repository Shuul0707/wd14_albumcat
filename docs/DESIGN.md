# 设计说明

> 为什么标签不进照片文件、数据怎么分家、fork 改了哪些地方，以及整套管线的技术要点。

> 本文是 [README](../README.md) 的延伸阅读，可以直接跳读。

## 三条路线对照

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

---

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

---

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

---

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

---

[← 回到 README](../README.md)
