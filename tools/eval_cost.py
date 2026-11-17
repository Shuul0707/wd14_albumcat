#!/usr/bin/env python3
"""把「准确率」换成「用户实际会踩的坑有多深」。

现在的评估只报一个数：77.8% 全对。但这个数把所有错判当成等价的，而实际上——

    梗图被判成插画  → 梗图混进插画集，正是这个项目要解决的问题     很严重
    照片被判成插画  → 私人照片混进收藏，翻到会尴尬               中
    插画被判成梗图  → 整理时多看几张                             轻

所以「准确率 77.8%」和「用户感受」之间还差一层：**错在哪儿**。

这个脚本算三样东西：

1. 混淆矩阵（谁被判成谁）
2. 加权损失（按 CONSEQUENCES 加权重）
3. 「损失归因」——总损失里有多少来自最严重的那一类错判

如果归因显示「60% 的损失来自梗图→插画」，那下一步该做什么就清楚了，
不必再盯着总体准确率。

用法::

    python tools/eval_cost.py --embeddings data/datasets/_cache/kind-emb.npz \\
        --plugin plugins/kind
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

# 代价矩阵：CONSEQUENCES[真类][预测类] = 这一格错判有多疼。
# 0 = 判对；数值越大越严重。顺序必须和 plugin.json 的 labels 一致，
# 所以运行时会校验，不一致直接报错而不是默默算错。
#
# 为什么梗图→插画是 3：那正是「我在整理插画，结果翻出一堆表情包」——
# 这个项目的立项理由。反过来插画→梗图只是少几张，1 就够。
DEFAULT_CONSEQUENCES = {
    ("截图", "插画"): 2, ("截图", "梗图"): 1, ("截图", "照片"): 1,
    ("插画", "截图"): 2, ("插画", "梗图"): 1, ("插画", "照片"): 2,
    ("梗图", "截图"): 1, ("梗图", "插画"): 3, ("梗图", "照片"): 2,
    ("照片", "截图"): 2, ("照片", "插画"): 2, ("照片", "梗图"): 1,
}


def build_matrix(labels: list[str]) -> np.ndarray:
    """按标签顺序铺开代价矩阵。"""
    n = len(labels)
    m = np.ones((n, n), dtype=np.float64)
    np.fill_diagonal(m, 0.0)
    for (a, b), v in DEFAULT_CONSEQUENCES.items():
        if a in labels and b in labels:
            m[labels.index(a), labels.index(b)] = float(v)
    return m


def load(plugin_dir: Path, embeddings: Path):
    manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
    labels = list(manifest["labels"])
    d = np.load(embeddings, allow_pickle=True)
    X = d["X"].astype(np.float32)
    Y = d["Y"].astype(np.int64) if d["Y"].ndim == 1 else d["Y"].argmax(axis=1)
    try:
        import onnxruntime as ort
    except ImportError:
        raise SystemExit("需要 onnxruntime：pip install onnxruntime")
    sess = ort.InferenceSession(str(plugin_dir / manifest.get("model", "head.onnx")),
                                providers=["CPUExecutionProvider"])
    logits = sess.run(None, {sess.get_inputs()[0].name: X})[0]
    if manifest.get("activation") == "softmax":
        z = logits - logits.max(axis=1, keepdims=True)
        e = np.exp(z)
        probs = e / e.sum(axis=1, keepdims=True)
    else:
        probs = logits
    return labels, probs, Y, manifest


def report(labels: list[str], probs: np.ndarray, truth: np.ndarray) -> None:
    n, k = len(truth), len(labels)
    pred = probs.argmax(axis=1)
    conf = probs.max(axis=1)
    cost = build_matrix(labels)

    # -- 混淆矩阵 ------------------------------------------------------- #
    cm = np.zeros((k, k), dtype=int)
    for t, p in zip(truth, pred):
        cm[t, p] += 1

    print(f"\n混淆矩阵（行=真实，列=预测，n={n}）")
    header = "真\\预".ljust(8) + "".join(c.rjust(7) for c in labels)
    print(header)
    for i, name in enumerate(labels):
        # 每行非对角线上最大的那格标个星，一眼看出它最容易混成谁
        row = cm[i].copy()
        row[i] = 0
        worst = int(row.argmax()) if row.sum() else -1
        cells = []
        for j in range(k):
            s = str(cm[i, j]).rjust(7)
            if j == worst and cm[i, j] > 0:
                s = (str(cm[i, j]) + "*").rjust(7)
            cells.append(s)
        print(name.ljust(8) + "".join(cells))

    acc = float((pred == truth).mean())
    print(f"\n准确率：{acc:.1%}（{int((pred == truth).sum())}/{n}）")

    # -- 加权损失 ------------------------------------------------------- #
    cell_cost = cost[truth, pred]
    per_image = float(cell_cost.mean())
    worst_possible = float(cost.max())
    print(f"加权损失：{per_image:.2f} / 每张（满分 {worst_possible:.0f}，"
          f"归一化 {per_image / worst_possible:.1%}）")

    # 同一个准确率下，错判「错在哪」可以让加权损失差一倍——
    # 打个比方：把 30 张插画判成梗图，和把 30 张梗图判成插画，准确率一样，
    # 但后者是项目要解决的问题。所以这个数比准确率更贴近体感。
    print("\n损失归因（总损失里每一类错判占多少）")
    total = float(cell_cost.sum())
    rows = []
    for i, a in enumerate(labels):
        for j, b in enumerate(labels):
            if i == j or cm[i, j] == 0:
                continue
            contributed = float(cm[i, j]) * float(cost[i, j])
            rows.append((contributed, a, b, int(cm[i, j]), float(cost[i, j])))
    for contributed, a, b, count, unit in sorted(rows, reverse=True):
        share = contributed / total if total else 0.0
        bar = "█" * max(1, round(share * 30))
        print(f"  {a}→{b:<6} {count:>4} 张 × 代价 {unit:.0f} = {contributed:>6.0f}  "
              f"{share:>6.1%}  {bar}")

    # -- 一句话结论 ----------------------------------------------------- #
    if rows:
        top = max(rows)
        share = top[0] / total if total else 0
        if share >= 0.4:
            print(f"\n结论：{share:.0%} 的损失来自「{top[1]}→{top[2]}」这一格。"
                  f"再优化总体准确率之前，先把这一格打下去。")
        else:
            print(f"\n结论：损失比较分散（最大一格 {share:.0%}），没有单一瓶颈。")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--embeddings", type=Path, required=True)
    ap.add_argument("--plugin", type=Path, default=Path("plugins/kind"))
    args = ap.parse_args()
    labels, probs, truth, manifest = load(args.plugin, args.embeddings)
    print(f"插件 {manifest['id']}：{labels}")
    report(labels, probs, truth)
    return 0


if __name__ == "__main__":
    sys.exit(main())
