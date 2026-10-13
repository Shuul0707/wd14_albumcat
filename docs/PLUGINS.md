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
