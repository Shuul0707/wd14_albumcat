# 安装与用法

> 装依赖、下模型、配置、自检，以及另外两条投放路线（写 XMP / 写 Immich）和标签格式。

> 本文是 [README](../README.md) 的延伸阅读，可以直接跳读。

## 安装

### 1. 前置：Immich 服务（只有走 Immich 路线才需要）

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

### 2. 模型

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

### 3. Python 依赖

```bat
tools\install_deps.cmd
```

会创建 `.venv` 并从清华源装依赖。默认装 `onnxruntime-directml`
（Windows 上用 DirectML / DX12，不依赖 CUDA）。想强制 CPU 就 `tools\install_deps.cmd --cpu`。

### 4. 配置

编辑 `config.toml`，至少填上 API key：

```toml
[immich]
base_url = "http://127.0.0.1:2283"
api_key  = "你的 key"
```

也可以用环境变量，避免把 key 写进文件：`ALBUMCAT_IMMICH_API_KEY=...`。

### 5. 自检

```bat
tools\albumcat.cmd doctor
```

会检查模型文件、Python 包、推理后端、插件加载、Immich 连通性。

## 使用

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

---

### Immich 路线：写进 Immich

```bat
REM 看端到端会做什么（默认 dry-run，不改动任何数据）
tools\albumcat.cmd run --limit 50

REM 真的写标签
tools\albumcat.cmd run --limit 50 --apply
```

两条路线共用同一个 journal，`undo` 会自动识别该回滚哪种记录。
`run` 会把"哪次运行给哪个 asset 挂了哪个标签"记下来，所以回滚不会误删你手动加的标签。

### 只看不动

```bat
REM 本地打标冒烟测试——不联网、不写任何东西
tools\albumcat.cmd plan samples\
```

## 标签长什么样

标签是**层级**的，Immich 侧边栏会渲染成树：

| 来源 | 标签 | 说明 |
|---|---|---|
| 角色 | `角色/Hatsune Miku` | WD14 角色标签，`hatsune_miku_(vocaloid)` 会把括号里的作品名剥出来 |
| 作品 | `作品/Vocaloid` | 从角色标签的括号内容推导 |
| 分级 | `分级/R18`、`分级/敏感`、`分级/轻微` | 来自 WD14 的 rating head |
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
