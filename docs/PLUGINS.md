# 插件：插入特定 IP 的精准识别

> 在 WD14 之后接自己的模型或规则，以及怎么训练一个角色插件。

> 本文是 [README](../README.md) 的延伸阅读，可以直接跳读。

## 五种插件

这是设计时预留的核心能力。插件放在 `plugins/<id>/`，五种类型：

- **`feature`** —— 在 WD14 的图像特征（`wd-v1-4-vit-tagger-v2` 是 **768 维**）上跑一个小分类头。
  用你自己的修正样本训几百张，在这个 IP 上就能大幅超过 WD14 自带的角色标签，
  而且几乎不增加单图耗时（特征 WD14 已经算过了）。**这就是"精准识别特定角色"的正路。**
- **`python` / `image`** —— 任意 Python：OCR、感知哈希、人脸、读 sidecar 文件。
- **`remote`** —— 把预览图转发到局域网里另一个 HTTP 服务。
- **`rule`** —— 不带模型也不带代码，纯规则。

完整字段表和写法见 [`plugins/README.md`](../plugins/README.md)。
仓库里带了一个能直接跑的示例插件 `plugins/example-flat-image/`。

### 训练一个角色插件

`tools\train_feature_plugin.py` 把上面说的闭环做完了：你只要把修正好的图按角色分目录放好，
它就会用 WD14 的特征训一个多标签分类头，导出成可以直接丢进 `plugins/` 的 `feature` 插件。

```bat
:: data\ 下面每个子目录是一个角色；_ 开头的目录是 background（把误报往下压）
::   data\初音未来\*.jpg
::   data\巡音ルカ\*.jpg
::   data\_background\*.jpg
tools\albumcat.cmd train --data data --out plugins\my-ip --id my-ip --name "我的角色头"
```

等价的直接调用（参数更多）：

```bat
.venv\Scripts\python.exe tools\train_feature_plugin.py ^
  --data data --out plugins\my-ip --id my-ip ^
  --target character --priority 10 --threshold 0.5 ^
  --epochs 400 --val-frac 0.2
```

- 纯 numpy 训练（Adam + BCE + L2），**不需要 torch / sklearn**。
- 导出时会跑一遍 ONNX 与 numpy 的往返比对，误差超过 `1e-4` 就拒绝落盘。
- 训练集和验证集的 precision / recall / exact-match 分开打印，**看验证集那组**。
- 产出的 `head.onnx` 只有几 KB（就是一个矩阵），所以插件又小又快。

没有真实样本时可以先跑一遍自检，确认整条链路是通的：

```bat
.venv\Scripts\python.exe tools\make_synth_labels.py
.venv\Scripts\python.exe tools\train_feature_plugin.py --data .tmp\synth-labels --out .tmp\test-plugin --id synth-test --epochs 400 --force
```

（生成 32 张纯合成色块图，正常应当 loss 收敛到 0.00x、验证集准确率 100%。）

查看当前加载了哪些：

```bat
tools\albumcat.cmd plugins
```

### 互斥多分类头（`--loss ce`）

上面那套是**多标签**：一张图可以同时是「初音未来」和「巡音ルカ」。有些问题反过来——
一张图**只可能是其中一类**，比如「插画 / 梗图 / 照片 / 截图」。这时用 softmax 交叉熵：

```bat
:: data\kindset5\插画\*.jpg  data\kindset5\梗图\*.jpg  data\kindset5\照片\*.jpg  data\kindset5\截图\*.jpg
:: 每个子目录恰好一个类别，不再有 background 目录
.venv\Scripts\python.exe tools\train_feature_plugin.py ^
  --data data\kindset5 --out plugins\kind --id kind ^
  --name "画面类型" --target tag --threshold 0.5 ^
  --loss ce --class-weight ^
  --cache data\kindset5\embeddings.npz --binary-label 插画
```

- `--loss ce` 要求每张图**恰好一个**标签（有 background 目录会直接报错退出，因为「无标签」在互斥问题里没有定义）。
- `--class-weight` 按逆频率给稀有类加权，否则「截图」这种小类会被完全忽略。
- `--cache out.npz` 把 768 维特征缓存下来：调 `--epochs/--lr/--l2` 重训只要几秒，不再重新过模型。
  缓存带指纹（路径 + 大小 + mtime + 头尾各 8 KB 的哈希），图片换了会自动重算。
- `--binary-label 插画` 额外打印「插画 vs 其余」的二分类指标（precision / recall / AUC），
  以及 softmax 冠军判定下的混淆矩阵——你要的「是不是插画」就是这个数。
- 导出的 `plugin.json` 会多两行 `"exclusive": true`、`"activation": "softmax"`。
  `exclusive` 让服务端只取**冠军类**，而且冠军置信度低于 `threshold` 时**一个标签都不发**——
  拿不准的照片宁可留空，也不要往「插画」里塞一张真实照片。

⚠️ 抽特征前先关掉在跑的 `albumcat serve`：每个进程各持一个 DirectML 会话，抢起来会慢十倍
（实测 1.14 s/张 vs 83 ms/张）。仓库里 `plugins/kind/` 就是这个头的成品，数据集怎么攒
（三个下载器 + 跨类去重）和实测指标见 [图片类型分类](CLASSIFIER.md)。

> **不需要上游的拆分模型。** 上游 README 让你用 `split_model.py` 切出
> 1.2 GB 的 `feature_extractor.onnx` + 43 MB 的 `classifier.onnx`；本项目改用
> `tools\add_feature_output.py` 把特征张量声明成原模型的**额外输出**，
> 一个 373 MB 文件、一个 session 同时拿到标签和特征。该脚本会用分类头自己的权重
> 反算一遍输出做校验，对不上就不落盘。
>
> 跑一次即可，`albumcat` 会自动发现 `model_features.onnx`。用
> `tools\albumcat.cmd model` 查看真实的特征维度。
>
> （顺带一提：上游 README 说的「1024 维」只对 ViT-L/14 成立，本项目用的
> `wd-v1-4-vit-tagger-v2` 是 ViT-B/16，实际是 768 维。）

---

[← 回到 README](../README.md)
