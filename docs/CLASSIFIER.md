# 图片类型分类：插画 / 梗图 / 照片 / 截图

给相册里的人一个「这张是插画还是别的什么」的答案。它不是又一个独立模型——输入就是 WD14
（`wd-v1-4-vit-tagger-v2`）在 448×448 上抽出的 **768 维特征**，接一个 4 类的 softmax，
12 KB，跑一张图不到 1 ms，跟着 WD14 一起在 `/plan` 里返回。

| 类别 | 标签 | 典型内容 |
| --- | --- | --- |
| `illustration` | `插画` | 二次元插画、AI 画、动画截图 |
| `meme` | `梗图` | 带字的梗图、表情包、聊天截图里的图 |
| `photo` | `照片` | 相机拍的真实照片（含 cosplay、手办、油画照片这类难例） |
| `screenshot` | `截图` | 手机/网页 UI 截图、文档截图 |

四类**互斥**：一张图只会拿到一个标签。冠军置信度低于 `threshold`（默认 0.5）时**一个标签都不发**——
宁可留空，也不把真实照片塞进「插画」。

## 两类人怎么用它

| 想干的事 | 用哪个入口 |
| --- | --- |
| 找某个角色的插画、梗图、表情包 | `角色\|…` 里的角色名，或者直接搜「黑暗的初音未来」 |
| 只整理收藏的插画，不想混进梗图 / 表情包 / 截图 | Tags 页点 `插画` |

两套标签是叠加的，不是二选一：一张初音未来的插画同时有 `角色|初音未来` 和 `插画`，
按角色筛、按类型筛、把两者一起搜都行。

⚠️ 手机端「只标角色（推荐）」勾着的时候，客户端只保存角色标签，`插画` 这类普通标签会被丢掉。
想拿到类型标签，把那个勾去掉（`设置 → AI 打标签 → 只标角色`），或只对插画目录单独跑一轮。

## 数据和训练

数据不进仓库（`/data/` 已在 `.gitignore` 里），仓库里只有训练出来的头。要复现，
先按下面的来源拉一遍，按类名建子目录，再跑一条命令。

### 目录结构

`--data` 指向的目录下，**每个子目录名就是一个类别**，目录里放 jpg/png：

```
data/datasets/kindset/
├── 插画/   1002 张
├── 梗图/    450 张
├── 照片/    650 张
└── 截图/    120 张
```

类名直接用中文，因为 `src/albumcat/types.py` 的 `prettify()` 对 CJK 原样返回：训练出的
`labels.json` 就是 `["截图", "插画", "梗图", "照片"]`，手机端 Tags 页直接显示中文磁贴，不需要任何映射。
（目录名以 `_` 开头会被当成「背景类」，那是给多标签训练用的，四类互斥时不要用。）

### 数据来源（共 2222 张）

| 类别 | 来源 | 张数 |
| --- | --- | --- |
| 插画 | `safebooru.org` XML API，轮换 `1girl solo` / `1boy solo` / `2girls` / `scenery` / `no humans` / `multiple boys` / `original` 等查询，取 `sample_url`（约 850 px 的 JPEG） | 1002 |
| 梗图 | HuggingFace `neuralcatcher/hateful_memes` 的 `img/*.png`（真实带字梗图，只当负样本，不用它自己的标注） | 450 |
| 照片 | `picsum.photos`（Unsplash 图）600 张 + Wikimedia Commons 搜 cosplay / figurine / oil painting 等 50 张难例 | 650 |
| 截图 | HuggingFace `silatus/1k_Website_Screenshots_and_Metadata` 的 `screenshot.png` | 120 |

下载器就是 `tools/fetch_illustration_dataset.py`（没有第三方依赖，`urllib` + PIL）：

```bat
python tools\fetch_illustration_dataset.py --out data\datasets\kind --illustrations 1000 --photos 600 --memes 450 --screenshots 120
REM 插画不够就追补，--pages 是每个查询翻几页
python tools\fetch_illustration_dataset.py --out data\datasets\kind --top-up --pages 3 --illustrations 600 --photos 0 --memes 0 --screenshots 0
```

它存 `<label>-NNNNN.jpg`（长边缩到 640，`quality=88`），并写 `manifest.jsonl` 记来源 URL。
要变成子目录，把文件按前缀 hardlink 过去（同一磁盘不占空间）：

```powershell
$map = @{ illustration = '插画'; meme = '梗图'; photo = '照片'; screenshot = '截图' }
foreach ($kv in $map.GetEnumerator()) {
  $dir = "data\datasets\kindset\$($kv.Value)"; New-Item -ItemType Directory -Force $dir | Out-Null
  Get-ChildItem "data\datasets\kind\$($kv.Key)-*.jpg" | ForEach-Object {
    $dst = Join-Path $dir $_.Name
    try { New-Item -ItemType HardLink -Path $dst -Target $_.FullName | Out-Null } catch { Copy-Item $_.FullName $dst }
  }
}
```

### 训练

先要特征模型（`models/vit/model_features.onnx`，多一个 `features` 输出）：

```bat
python tools\add_feature_output.py
```

然后：

```bat
python tools\train_feature_plugin.py --data data\datasets\kindset --loss ce --class-weight ^
  --cache data\datasets\kindset\embeddings.npz --out plugins\illustration ^
  --id illustration --name 插画分类 --target tag --threshold 0.5 ^
  --epochs 400 --lr 0.05 --l2 3e-3 --val-frac 0.2 --binary-label 插画 --priority 10
```

几个参数值得解释：

- `--loss ce` —— 走 softmax 交叉熵而不是多标签 sigmoid。它要求每张图**恰好一个**标签，
  导出的 ONNX 尾巴是 `Softmax`，`plugin.json` 里写 `"exclusive": true`、`"activation": "softmax"`，
  加载时 `plugins.py` 只取冠军类。用 `bce` 训出来的是「四个独立的是/否」，四类会同时亮。
- `--class-weight` —— 逆频率加权再归一化到均值 1。截图只有 120 张，不加权会被压着打。
- `--cache` —— 768 维特征的 npz 缓存，指纹是「路径 + 标签 + 大小 + mtime + 头尾各 8 KB 的哈希」。
  调参重跑时命中缓存，几秒进入训练；动了图片就自动重算（同一 mtime tick 内改等长文件也能发现）。
- `--binary-label 插画` —— 额外打一行「插画 vs 其余」的 accuracy / precision / recall / f1 / AUC，
  这才是用户真正关心的那个数。
- `--threshold 0.5` —— 冠军低于它就不发标签（弃权）。

整轮在 RTX 5060 上约 4 分钟，其中 2222 张抽特征约 3.5 分钟（DirectML，~10 张/秒）。

## 指标

验证集 444 张（`--val-frac 0.2`，随机切分）：

```text
exact-match (every label right): 99.8%
label        n+   TP   FP   FN  precision  recall   auc
截图         22   22    0    0      1.000   1.000  1.000
插画        209  209    0    0      1.000   1.000  1.000
梗图         88   88    0    0      1.000   1.000  1.000
照片        125  124    0    1      1.000   0.992  1.000
binary: '插画' vs everything else  (softmax winner)
  accuracy 100.0%  precision 1.000  recall 1.000  f1 1.000  auc 1.000
  confusion  tp 209  fp 0  fn 0  tn 235
```

域内（四个来源同分布）几乎满分，所以真正看的是域外。

### 域外表现

同一批头，喂它训练集里没有的东西（`.tmp/hard/`，不在仓库里）：

| 挑战 | 结果 |
| --- | --- |
| cosplay 照片 12 张 | 12/12 → `照片`（0.53–0.98） |
| 动画截图 8 张 | 8/8 → `插画`（0.77–1.00） |
| AI 生成图 8 张 | 8/8 → `插画`（0.95–1.00） |
| 手办照片 10 张 | 5 `照片` / 3 `截图` / 1 `插画`(0.67) / 1 弃权 |
| 油画照片 13 张 | 4 `照片` / 4 `插画` / 3 `梗图` / 1 `截图` / 1 弃权 |

读法：**「是不是插画」这件事，域外也基本靠得住**；「不是插画的那三类别是什么」会乱——
手办照片被叫成截图、油画照片被叫成梗图，都属于出了域之后在四个桶里乱指。
对使用者没有实质伤害（它们都不会进 `插画` 筛选，除了 4/13 的油画照片），但不要拿 `截图` 这个标签
去当真做「找截图」的工具；它只在手机 UI 截图上可信。

加难例前后对比：第一版（只有 safebooru + picsum，没有 cosplay/手办/油画难例）cosplay 照片
**只有 1/12 判对**、手办照片 4/10 误判成插画。把 50 张 Commons 难例塞进 `照片` 之后才变成 12/12。
难例比数量重要。

## 接入

```bat
tools\albumcat.cmd plugins
REM illustration  feature  tag  10  0.50  True
tools\albumcat.cmd plan 你的图.jpg
```

`plugins/illustration/` 里有三个文件：

| 文件 | 说明 |
| --- | --- |
| `head.onnx` | 12,489 B。`Gemm(transB=1)` → `Softmax`，输入 768 维 |
| `labels.json` | 四个类名，顺序 = softmax 列顺序 |
| `plugin.json` | 368 B：`kind: feature`、`target: tag`、`priority: 10`、`threshold: 0.5`、`input_dim: 768`、`exclusive: true`、`activation: softmax`、模型 `sha256` |

这是普通 `feature` 插件，`plugins/` 下任何目录都会被自动加载；不需要改配置、不需要重发版。
`target: tag` 的标签是**扁平叶子**（`插画` 一层，没有 `类型|` 前缀）；想让它落到树里，
用自定义规则把 `then.path` 写成 `["类型", "{display}"]`（见 [插件](PLUGINS.md)）。

插件发出的普通标签**不占** `max_general_tags_per_image` 的名额，也仍然受 denylist 约束：
25 个 WD14 标签不会把你自己的分类结果挤掉（`src/albumcat/rules.py` 第 4 段）。

## 边界与后续

- 四类互斥是**刻意**的：表情包常常是截图，梗图常常是插画加字，硬要四类各判一次只会互相矛盾。
- 只在 640 px 缩略图上判据足够；`/plan` 收到什么就以什么为准（手机端上传 640–1024 px）。
- 想加第五类（比如「游戏 3D 渲染」），建子目录重训即可；`labels.json` 和 `head.onnx` 会一起更新。
- 头很小，多挂几个互斥头（画风、色情度、水印有无）成本可以忽略。
