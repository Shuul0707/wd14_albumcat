# 设计说明

> 数据怎么分家、fork 改了哪些地方，以及整套管线的技术要点。

> 本文是 [README](../README.md) 的延伸阅读，可以直接跳读。

## 让 Aves 自己存标签

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

## 技术要点

- **预处理必须和上游逐位一致**，否则分数会整体漂移：透明底合成白底 → RGB→**BGR** →
  白边补成正方形 → 一次性 resize 448×448（缩小 `INTER_AREA` / 放大 `INTER_CUBIC`）→
  **float32 原始 0–255，不做 /255、不做 mean/std 归一化** → 形状 `(1, 448, 448, 3)`（NHWC）。
- **后端选择**：`auto` 依次尝试 DirectML → CUDA → CPU。这台机器的 GPU 是 RTX 5060
  （Blackwell，sm_120），官方 `onnxruntime-gpu` 轮子对 sm_120 支持有问题
  （[onnxruntime#27621](https://github.com/microsoft/onnxruntime/issues/27621) 就是同款卡的静默死锁），
  且本机是 CUDA 13.0 而官方轮子要 CUDA 12.x。所以走 DirectML。
- **手机上打标签的缩略图，尺寸、质量和「谁来解」都得自己开旋钮**：Aves 的 debug `getThumbnail` 把
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
- **特征向量不额外花钱**：特征提取是整个 ViT 塔，分类头只是最后一层 `Gemm`。
  所以把特征张量声明成额外输出后，一次 `session.run` 同时拿到两者，实测单图耗时不变
  （99.5 ms vs 之前的 101.8 ms）。
- **中间张量必须"声明"才能拿到**：ONNX Runtime 不允许按名字索取图内部张量
  （会报 `Invalid output name:...predictions_globalavgpooling/Mean:0`），因为优化阶段把它融合掉了。
  必须改图，把该张量加成图的输出。另外图的输出名**不能起别名**——输出名就是张量名，
  直接声明 `features` 会得到 `Graph output (features) does not exist in the graph`，
  所以要用一个 `Identity` 节点把它引出到干净的名字。
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
- **`.cmd` 只写纯 ASCII，中文一律交给 Python**。cmd.exe 是按 **OEM 代码页**（这里 cp936）
  读 `.cmd` 文件本身的，UTF-8 的中文会被当成两个 GBK 字符，整个字节流从此错位：
  `setlocal enabledelayedexpansion` 变成 `'local' is not recognized`，
  `set "ROOT=..."` 变成 `'t' is not recognized`，输出是 `椤归€氳繃` 这样的乱码。
  这种坏法**不报错，只是脚本被静默撕碎**。所以 `tools\check_all.cmd` 只有十几行 ASCII，
  真正的清单和中文输出都在 `tools\check_all.py` 里。

---

[← 回到 README](../README.md)