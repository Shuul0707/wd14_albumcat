# 语义搜索（Chinese-CLIP）

> 按意思找图：输入「沙滩」，出来的是画面像沙滩的照片，按相关度排序 —— 而不是「标签里恰好有沙滩两个字」。

---

## 1. 为什么还要第二个模型

WD14 是一个**封闭词表**分类器：

> 「这张图里有没有我这 9083 个标签中的某一个？」

它能回答「有没有初音未来」，但问它「有没有沙滩」时，只能指望 `beach` 这个标签恰好命中。
而「雨天的街道」「夕阳下的教室」这类**没有对应标签**的描述，它无能为力。

Chinese-CLIP 把照片和任意中文短语映射到**同一个 512 维空间**：

> 「这张照片和这句话有多接近？」

于是排序就是一次余弦相似度。两者不冲突，是互补：

| | WD14 | Chinese-CLIP |
| --- | --- | --- |
| 输出 | 9083 个标签的概率 | 512 维向量 |
| 擅长 | 角色名、作品、分级 | 场景、氛围、构图、任意描述 |
| 词表 | 封闭 | 开放（自由输入） |
| 用途 | 精确筛选（Tags 页） | 模糊找图（语义搜索） |
| 端侧 | 不现实（ViT 大模型） | 文本塔可以（单句前向） |

---

## 2. 架构：重的一半留在电脑，轻的一半给手机

```
手机：448px 缩略图 ──┐
                    ├─→ PC：WD14 打标 ──→ 标签（appTags）
                    └─→ PC：CLIP 图像塔 ──→ 512 维向量（每张 ~1 KB）
                                              │
手机：存进 appVectors 表 ←────────────────────┘
      （entry_id, model, dim, vector）

用户输入「沙滩」
   ├─ 手机有文本模型 → 端侧 ONNX 编码（离线、~200 ms）
   └─ 没有 → POST /encode_text → PC 编码
                    ↓
        与 appVectors 里所有向量算余弦 → 按分数排序 → 结果网格
```

**为什么这样切**：图像塔（ViT-B/16）一张图要 1–3 秒，一万张就是 3–8 小时，只能在电脑上跑；
文本塔每次搜索只处理一句话，量化后手机可以扛。这也正好延续项目「手机轻、电脑重」的既有取舍。

### 为什么向量存 Aves 数据库，而不是写进照片

用户原话是「写入每张图片的语义向量」，这里做了一个有意的选择：

| 方案 | 问题 |
| --- | --- |
| 写进 XMP | 512 × 2 B ≈ 1 KB/张，写进 APP1 段会让每张图多 1 KB；**Aves 的搜索并不读 XMP**，写了也用不上；而且违背本项目「照片文件一个字节都不改」的核心约束 |
| 写 sidecar | Aves 不读，且手机会多出成千上万个 `.xmp` 文件 |
| **写 Aves 的 `appVectors` 表** | 与 `appTags` 同源：可查、可排序、可删除、可重建，照片仍然不动 |

所以「写入」落在 Aves 自己的数据库里 —— 和标签完全一致的哲学。跨设备想带走向量，用 `GET /export_vectors`（规划中）导出 JSON 即可。

---

## 3. 准备模型

```bash
# 开发机一次性操作，需要 torch + transformers（运行时不需要）
python -m pip install torch transformers onnx onnxruntime

# 导出两个塔 + vocab.txt 到 models/chinese-clip/
python tools/export_chinese_clip_onnx.py

# 可选：int8 量化（体积约 1/4，精度损失通常 < 1%）
python tools/export_chinese_clip_onnx.py --quantize

# 验证：跑一次真实前向，确认输出 512 维且已归一化
python tools/export_chinese_clip_onnx.py --self-test
```

产物：

```
models/chinese-clip/
├── image_encoder.onnx     # 图像塔（~90 MB int8 / ~330 MB fp32）
├── text_encoder.onnx      # 文本塔（~100 MB int8 / ~400 MB fp32）
└── vocab.txt              # BERT 词表（运行时光文本塔需要）
```

启动后自检：

```bash
tools\albumcat.cmd serve
# 日志里会出现 semantic 状态；或直接：
curl http://127.0.0.1:8899/semantic
```

没有模型时**不会报错**：`available: false`，打标功能完全不受影响。

---

## 4. API

### `GET /semantic`

```json
{
  "ok": true,
  "enabled": true,
  "available": true,
  "model": "chinese-clip-vit-base-patch16",
  "dim": 512,
  "dtype": "float16",
  "image_model": "image_encoder.onnx",
  "text_model": "text_encoder.onnx",
  "reason": ""
}
```

### `POST /encode_text`

```bash
curl -X POST http://127.0.0.1:8899/encode_text \
  -H "Content-Type: application/json" \
  -d '{"text": "沙滩", "dtype": "float32"}'
```

```json
{
  "ok": true,
  "model": "chinese-clip-vit-base-patch16",
  "dim": 512,
  "dtype": "float32",
  "vector": "…base64(float32)…",
  "ms": 38.2
}
```

文本塔未安装时返回 **503**，`error` 说明原因。`dtype` 可选（默认取配置里的 float16），手机要 `float32` 是为了能直接内存映射。

### `GET /semantic/model?file=text_encoder.onnx`

把文本塔 / 词表以 `application/octet-stream` 流式发给手机（只允许 `text_encoder.onnx` 与 `vocab.txt`，图像塔不提供）。需要 `X-Albumcat-Token`。手机端「下载到本机」用的就是它。

### `POST /plan` / `/plan_batch` 增加 `include_vector`

```json
{"id": "123", "image": "<base64 jpeg>", "include_vector": true}
```

响应多一个字段：

```json
"semantic": {
  "model": "chinese-clip-vit-base-patch16",
  "dim": 512,
  "dtype": "float16",
  "vector": "…base64…"
}
```

默认 `false` —— 老客户端不传这个字段时，响应和以前**逐字节一致**，不会因为升级服务端而变慢。

---

## 5. 手机端（Aves fork）

新增一张表，schema 17 → 18：

```sql
CREATE TABLE appVectors(
  id      INTEGER PRIMARY KEY,   -- entry id，与 appTags 对齐
  model   TEXT,                  -- 'chinese-clip-vit-base-patch16'
  dim     INTEGER,               -- 512
  vector  BLOB,                  -- float16，1024 字节
  updated INTEGER                -- 秒级时间戳，便于增量重建
)
```

`model` 写进表里是有意为之：换了图像塔之后，旧向量必须能被识别出来并重建，
而不是被静默地拿去和新文本向量比（两者不在同一空间，算出来的分数毫无意义）。

搜索流程：

1. 读 `appVectors` 全部向量到内存（1 万张 × 1 KB ≈ 10 MB，可接受）；
2. 查询词编码成向量（端侧或 `/encode_text`）；
3. 余弦点积 → 排序 → 取 top-N → 拿 entry id 去查 Aves 的条目；
4. 结果页显示相似度（0–1），低于阈值（默认 0.2）的过滤掉。

性能：1 万条 512 维点积在手机上约 5–15 ms，搜索本身不是瓶颈，**编码才是**。

### 端侧文本塔（已实现）

`TextEncoderHandler.kt` 在设备上跑 `text_encoder.onnx`：

- **模型从哪来**：不打包进 APK（约 100 MB 权重会让安装包膨胀一倍，也违背「仓库不存权重」）。首次点「下载到本机」时从电脑 `GET /semantic/model?file=` 拉到 App 私有目录（`files/semantic/`），之后复用。下载先写 `.part`、校验字节数再改名，避免半截文件被当成完整模型。
- **分词**：Kotlin 里的 BERT WordPiece 与 `semantic.py::BertTokenizer` 逐条对齐——中文按字切分、全角折叠、`##` 续接、截断时保留 `[SEP]`。两份实现若不同步，向量看着正常但排序是噪声，所以两处都写了指向对方的注释。
- **输入输出**：`input_ids` / `attention_mask` / `token_type_ids`，int64 `[1, 52]`；输出 `[1, 512]`，**L2 归一化在原生侧做**（与 PC 的 `_l2_normalize` 一致）。
- **失败回落**：端侧未加载或调用异常 → 自动走 `/encode_text`，搜索不会因此不可用。UI 上会标出当前是「本机编码」还是「电脑编码」。
- **代价**：APK 增加 `onnxruntime-android` AAR（约 15 MB）；首次下载约 100 MB；每次查询约 200 ms（CPU + int8）。
- **好处**：装上之后搜索不需要电脑在线，也不需要局域网。

> 图像塔仍然只能在电脑上跑：ViT-B/16 一张 1–3 秒，一万张 6–14 小时。所谓「手机部署」，实际是**把文本塔放到手机、图像塔留在电脑**——这也是打标和建索引本来就要开机的那段时间。

---

## 6. 与标签搜索的关系

两者是并列入口，不是替代：

- **精确找**「某个角色的所有图」→ Tags 页（WD14 标签，零延迟，可离线）；
- **模糊找**「下雨的街道」「有海的照片」→ 语义搜索（CLIP，需要向量已建好）。

打标那一趟顺带就把向量建好了（`include_vector: true`），不需要再跑一遍全库。

---

## 7. 已知限制

- **不是 OCR**：图里的文字（比如截图上的台词）不在 CLIP 的语义里，别指望搜「写了什么」。
- **不是精确定位**：CLIP 给的是整图相似度，不会告诉你「沙滩在左下角」。
- **中文短查询效果最好**：3–8 个字的场景词（沙滩、雨天、夜景、教室）远好于长句。
- **模型换代必须重建向量**：`appVectors.model` 不匹配时应整表清空重算，不要混用。
- **排序不等于准确率**：相似度 0.3 和 0.35 的差别往往没有意义，只适合做粗排。
