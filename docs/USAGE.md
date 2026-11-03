# 安装与用法

> 装依赖、下模型、配置、自检和标签格式。

> 本文是 [README](../README.md) 的延伸阅读，可以直接跳读。

## 安装

### 1. 模型

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

### 2. Python 依赖

```bat
tools\install_deps.cmd
```

会创建 `.venv` 并从清华源装依赖。默认装 `onnxruntime-directml`
（Windows 上用 DirectML / DX12，不依赖 CUDA）。想强制 CPU 就 `tools\install_deps.cmd --cpu`。

### 3. 配置

编辑 `config.toml`：没写的键一律用默认值（默认值都在 `src/albumcat/config.py` 里），
写错键名会直接报错。手机那条路线上真正要看的是 `[publish]` 里的几个词根（`角色` / `作品` /
`分级`）和 `tree_in_subject = true`——App 把扁平标签名原样存下来，关掉它角色就只剩裸名、没法筛。

### 4. 自检

```bat
tools\albumcat.cmd doctor
```

会检查模型文件、Python 包、推理后端、插件加载，以及防火墙有没有放行手机要连的那个端口。

## 使用

手机上那条路线的用法见 [PHONE](PHONE.md)。

### 只看不动

```bat
REM 本地打标冒烟测试——不联网、不写任何东西
tools\albumcat.cmd plan samples\
```

### 用网页看分类对不对

```bat
tools\albumcat.cmd serve
REM 然后浏览器打开 http://127.0.0.1:8899/ui
```

选一个文件夹（或者把文件夹整个拖进页面），里面的图会先平铺出来——这一步一张都没上传。
点「开始分类」，进度条走完页面就自动切成四堆：`插画 / 梗图 / 照片 / 截图`，各多少张、占多少比例；
没把握的单独归到「未识别」。点开任意一张，能看到 `截图 / 插画 / 梗图 / 照片` 的**完整**分数、
阈值、原图尺寸和尺寸先验挪了多少，而不是手机端那一个冠军标签。
页面只读：不写缓存、不碰相册数据库，图片在浏览器里缩到长边 1024 才发出去。
细节见 [图片类型分类](CLASSIFIER.md#自己看一眼网页看板)。

## 标签长什么样

标签是**层级**的，层级分隔符在 Aves 里是 `|`（它把整串当一个扁平标签名）：

| 来源 | 标签 | 说明 |
|---|---|---|
| 角色 | `角色\|Hatsune Miku` | WD14 角色标签，`hatsune_miku_(vocaloid)` 会把括号里的作品名剥出来 |
| 作品 | `作品\|Vocaloid` | 从角色标签的括号内容推导 |
| 分级 | `分级\|R18`、`分级\|敏感`、`分级\|轻微` | 来自 WD14 的 rating head |
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

---

[← 回到 README](../README.md)