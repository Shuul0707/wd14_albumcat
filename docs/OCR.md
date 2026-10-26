# OCR：把图片里的字变成可搜索的索引

> 目标：搜「初音未来」时，除了靠 CLIP 猜测画面，还能**确定地**找到写着这四个字的图。

---

## 1. 为什么是 RapidOCR，不是 Umi-OCR

[Umi-OCR](https://github.com/hiroi-sora/Umi-OCR) 是一个 **Windows 桌面应用**：Qt 界面、自带 HTTP 服务、自带模型管理器。我们要的是它**底下那个引擎**，而且必须在**本进程内**、跑在**我们已经在发的 onnxruntime** 上。

那个引擎就是 **RapidOCR**（PaddleOCR 的检测 + 识别模型转成 ONNX），PyPI 包名 `rapidocr_onnxruntime`。

依赖 Umi-OCR 本体意味着：多一套 Python 环境、一个必须开窗口的 GUI、以及一个无法在无头服务器上跑的东西 —— 而 `/plan` 恰恰经常跑在没有屏幕的机器上。

```bash
pip install rapidocr-onnxruntime
```

模型**在包里**（约 15 MB），没有下载步骤、没有 `models/` 目录要管。这是它和 CLIP 最大的区别：CLIP 需要 `tools/export_chinese_clip_onnx.py` 先导出，OCR 装完就能用。

---

## 2. 怎么开

### 服务端配置

```toml
[ocr]
enabled = true     # 关掉则 /ocr 报 available = false
max_side = 1600    # 长边上限，检测器要走完整张图
min_score = 0.5    # 低于这个置信度的碎片直接丢
```

### 请求时按张开启

OCR **不是**打标的一部分，因为它比打标慢一个数量级：

| 步骤 | 耗时（本机 CPU） |
| --- | --- |
| WD14 打标 | 0.1–0.6 s |
| CLIP 图像塔 | 0.1–3 s |
| **OCR** | **2.5–7 s** |

所以它是独立开关：

```bash
curl -X POST http://127.0.0.1:8899/plan \
  -H 'Content-Type: application/json' \
  -d '{"image":"<base64>","id":"x","include_ocr":true}'
```

响应里多一个字段：

```json
"ocr": {
  "text": "初音未来 演唱会 2026\n上海站·3月21日",
  "lines": 2,
  "engine": "rapidocr",
  "ms": 6412
}
```

**没有文字时是 `null`** —— 大多数插画本来就没有字，这是正常答案而不是失败。

**不传 `include_ocr` 时，响应里没有 `ocr` 这个键**，和加这个功能之前逐字节一致。老客户端不会因为服务端升级而多解析一个字段。

### 检查引擎状态

```bash
curl http://127.0.0.1:8899/ocr
# {"enabled": true, "available": true, "reason": "", "engine": "rapidocr",
#  "max_side": 1600, "min_score": 0.5, "ok": true}
```

---

## 3. 实测

用一张程序生成的图（微软雅黑，46 px）：

```
初音未来 演唱会 2026
上海站·3月21日
```

识别结果（`min_score = 0.5`）：

```
初音未来 演唱会 2026
上海站·3月21日
```

两行全对，标点（`·`）也对。耗时 **6.4 s**（首次含模型加载，之后 2.5–5.5 s）。

同一张图第二次走缓存：**66 ms**。

---

## 4. 手机端

### 存哪

新表 `appOcr`（schema **18 → 19**），一行一张图：

```sql
CREATE TABLE appOcr(
  id INTEGER PRIMARY KEY,
  engine TEXT,     -- 哪个引擎产出的，换引擎后可识别
  text TEXT,       -- 换行连接的整段文字
  updated INTEGER
)
```

**不写进照片**，和 `appTags` / `appVectors` 同一条规矩：派生数据住数据库，删掉只丢索引，不丢照片。

`engine` 跟着走的原因和向量的 `model` 一样：换引擎后两批结果不可比，混在一起会让搜索"看起来能用、结果是乱的"。

### 怎么搜

搜索框里输入文字，建议行出现：

> 🔍 搜图片上的文字「初音未来」

点它 → 用 `LIKE '%…%'` 查库 → 按命中顺序打开普通 `CollectionPage`（`fixedSelection` + `fixedSort`，网格、多选、详情、排序菜单全部沿用原生）。

**回车仍然走 Aves 自己的查询**，只有点了那一行才会搜文字 —— 和语义搜索的交互一致，不让老用户觉得"回车行为变了"。

### 为什么不用 FTS

`LIKE '%…%'` 用不上索引，全表扫。这是**故意的**：

- 列的体量是几百字节，行数等于照片数，扫一万行是毫秒级；
- FTS 要另一张表、另一套迁移、还要把中文分词器和手机端自己的文字处理逻辑对齐 —— 为一个能跑通的查询引入三个长期维护点，不值。

用户输入里的 `%`、`_`、`\` 都会转义，否则搜「50%」会匹配到几乎所有图。

---

## 5. 局限（先知道，别踩）

**1. 慢。** 2.5–7 s/张 意味着：
- 一千张图约 **1–2 小时**；
- 一万张图约 **7–19 小时**。

建议：**只对需要搜索文字的图开**（海报、截图、带字幕的图），不要全库跑。手机端现在还在每次打标时都请求 `include_ocr: true`，如果库很大，这是下一步要改的地方 —— 应该按目录或按类型决定，而不是全量。

**2. 艺术字识别率低。** 二次元图上的装饰性标题、手写体、竖排日文，识别质量明显下降。`min_score = 0.5` 会挡掉一部分噪音，但挡不住"识别成一个看起来合理的错词"。

**3. 不能定位。** 只存文本，不存坐标。所以搜到之后无法在图上高亮"这几个字在哪"。

**4. 只做简体中文 + 拉丁。** 引擎对繁体、日文假名的支持取决于模型，没有单独验证。

---

## 6. 怎么验证

```bash
# 1. 引擎在不在
curl http://127.0.0.1:8899/ocr

# 2. 单元测试（不需要模型）
python -m pytest tests/test_ocr.py -v

# 3. 一张真图
python - <<'PY'
import base64, json, urllib.request
data = open("samples/with_text.png", "rb").read()
body = json.dumps({"image": base64.b64encode(data).decode(),
                   "id": "1", "include_ocr": True}).encode()
req = urllib.request.Request("http://127.0.0.1:8899/plan", data=body,
                             headers={"Content-Type": "application/json"})
print(json.loads(urllib.request.urlopen(req).read())["ocr"])
PY
```

手机侧改动（10 个 Dart 文件）**没有在本机编译过** —— 本机没有 Flutter / JDK / Android SDK。CI 会跑 `dart analyze`，清单在 `.github/workflows/flutter.yml`，新增的 `app_ocr.dart`、`app_ocrs.dart`、`collection_search_delegate.dart` 已经加进去了。
