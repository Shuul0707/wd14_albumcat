# 已舍弃的路线（XMP / Immich）

> 两条已经舍弃的投放路线的存档：把标签写进照片的 XMP，和写进 Immich。

[← 回到 README](../README.md)

这两条路线已经舍弃、不再维护，其它文档里只讲 Aves 那条路。代码仍然留在仓库里，命令也仍然能跑（`albumcat tag`、`albumcat undo`、`albumcat run`），这一页只作存档，不会再跟着改。

为什么舍弃，一句话：**写 XMP 会重写照片文件**，改完文件的修改时间就变了，相册会按新时间重新排序；**Immich 是自建服务**，不算手机上的第三方相册。

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

## 为什么放弃了写 XMP

> 我反对这种直接改写照片数据的方法，本质是有些破坏性的，比如会重置修改日期导致相册中排序混乱

这个反对是对的。JPEG 的 XMP 在 APP1 段里，改它就得把整个文件重写一遍，重写就换修改时间、换 inode，
`MediaScannerConnection` 再扫一遍，相册按修改时间重排——照片会从原来的位置跳到最前面。
XMP 路线的代码（`albumcat tag` / `JpegXmpSink` / `albumcat undo`）还在仓库里，也验过字节级可逆
（5/5 文件 `undo` 后 SHA256 与原图完全一致），但它**不再是默认做法**，保留只是留个退路。
现在的做法是：**fork Aves，标签只进 Aves 自己的数据库。**

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

## XMP 实现的技术要点（原「技术要点」里搬出的条目）

### Immich 相关

- **只拉缩略图**：WD14 只看 448×448，从 Immich 拉 3–8 MB 原图纯属浪费，所以走
  `/assets/{id}/thumbnail?size=preview`。

- **批量写标签**：Immich 的 `PUT /tags/assets` 是"列出的每个标签 × 列出的每个 asset"的笛卡尔积，
  所以发布器把问题倒过来——**按标签汇总 asset 集合，每个标签发一次请求**，
  复杂度是 O(标签数) 而不是 O(图片数)。

### XMP 写文件与旧 Kotlin App 相关

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

## 手机上：让 App 自己跑（XMP 路线的完整形态）

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

### 地址填什么，填错了会怎样

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

### 照片不出手机，App 里也能撤销这次写入

**照片不出手机。** 传过去的是手机上就地缩放的 448px JPEG 缩略图（约 30–60 KB），
电脑不落盘、用完即弃。原始照片位置不变、像素不重编码——只在 JPEG 的 APP1 段
（或 PNG 的 `iTXt` 块）里插一段 XMP，其余字节（JPEG 是 `SOS` 之后的熵编码数据，
PNG 是 IDAT 块）逐字节原样搬运。

App 里也有「撤销上一次写入」，和电脑端 `undo` 是同一套思路：拿运行前记下的旧 XMP 包还原。
写回前会先比对文件的当前内容是否还是我们上次写进去的那份，被别人改过就不动它。

### 手机上跑之前，先在电脑上彩排一遍

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

### 但假相册顶掉的那一层，恰好藏过一个真 bug

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

### 真机烟测：`tools\smoke_device.py`

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

### 连不上时，旧 App 会先说清是哪种失败

如果懒得先查，也可以**直接在手机上点「测试连接」**：App 会把失败原因分好类再说出来，
防火墙丢包会明确提示你去跑 `tools\allow_firewall.cmd`，而不是甩一坨 Java 异常
（见 `NetDiagnosis.kt`，以及「技术要点」里那两条反直觉的教训）。

---

## 安装前置：Immich 服务（只有走 Immich 路线才需要）

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

---

## 已经舍弃的用法（原 USAGE.md）

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

### Immich 路线：写进 Immich

```bat
REM 看端到端会做什么（默认 dry-run，不改动任何数据）
tools\albumcat.cmd run --limit 50

REM 真的写标签
tools\albumcat.cmd run --limit 50 --apply
```

两条路线共用同一个 journal，`undo` 会自动识别该回滚哪种记录。
`run` 会把"哪次运行给哪个 asset 挂了哪个标签"记下来，所以回滚不会误删你手动加的标签。

---

## 验证结论（原 STATUS.md 的 XMP / Immich 行）

| 部分 | 状态 |
|---|---|
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

---

## 已知限制（原 STATUS.md 的 XMP / Immich 条目）

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

- Immich 是自建服务，不是严格意义上的"第三方手机相册"。手机上装的是官方 Immich App。

---

[← 回到 README](../README.md)
