#!/usr/bin/env python3
"""扫一遍拒识阈值，回答一个问题：让模型「不确定就不判」，到底值不值。

背景：`exclusive` 头已经会在冠军低于 ``threshold`` 时什么都不发（见
``plugins.py::_predictions``），但阈值一直写死 0.5，没人算过换个值会怎样。
PPT 里那个 77.8% 是「全部硬判」的准确率——它没有回答「如果允许模型说不知道，
敢判的那部分能有多准」。

这个脚本把三个数摆在一起看：

    阈值      覆盖率      判对率      拒绝
    0.40      100%       77.8%       0
    0.60       78%       86.4%      89
    0.70       61%       91.2%     156

覆盖率 = 敢判的图占多少；判对率 = 只在敢判的图里算准确率。两者必然此消彼长，
要找的是「判对率明显上一个台阶、覆盖率还没塌」的那个拐点。

用法::

    # 队友机器上（有特征缓存）：
    python tools/eval_threshold.py --embeddings data/datasets/_cache/kind-emb.npz \\
        --plugin plugins/kind

    # 只有 onnx、没有 npz 时，用 --truth 指向带子目录标注的文件夹：
    python tools/eval_threshold.py --truth data/eval/kind --plugin plugins/kind
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np


def load_head(plugin_dir: Path):
    """从 plugin.json + head.onnx 里取权重和标签顺序。"""
    manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
    model_path = plugin_dir / manifest.get("model", "head.onnx")
    if not model_path.exists():
        raise SystemExit(f"找不到模型：{model_path}")
    try:
        import onnxruntime as ort
    except ImportError:
        raise SystemExit("需要 onnxruntime：pip install onnxruntime")
    sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    return manifest, sess


def softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max(axis=1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=1, keepdims=True)


def scan(probs: np.ndarray, truth: np.ndarray, labels: list[str], thresholds: np.ndarray) -> list[dict]:
    """对每个阈值算一遍覆盖率与判对率。"""
    pred = probs.argmax(axis=1)
    conf = probs.max(axis=1)
    correct = pred == truth

    rows = []
    n = len(truth)
    for t in thresholds:
        keep = conf >= t
        kept = int(keep.sum())
        if kept == 0:
            rows.append({"threshold": float(t), "coverage": 0.0, "accuracy": 0.0, "rejected": n})
            continue
        rows.append({
            "threshold": float(t),
            "coverage": kept / n,
            "accuracy": float(correct[keep].mean()),
            "rejected": n - kept,
        })
    return rows


def per_class(probs: np.ndarray, truth: np.ndarray, labels: list[str], threshold: float) -> list[dict]:
    """在选定阈值下，逐类看保留了多少、对了多少。"""
    pred = probs.argmax(axis=1)
    conf = probs.max(axis=1)
    keep = conf >= threshold
    out = []
    for i, name in enumerate(labels):
        mask = truth == i
        total = int(mask.sum())
        if not total:
            out.append({"label": name, "n": 0, "kept": 0, "acc": 0.0})
            continue
        kept = int((mask & keep).sum())
        acc = float((pred[mask & keep] == i).mean()) if kept else 0.0
        out.append({"label": name, "n": total, "kept": kept, "acc": acc})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--embeddings", type=Path, help="npz，含 X（N×768）与 Y（N）")
    ap.add_argument("--truth", type=Path, help="按类分文件夹的目录，每个子目录是一个类名")
    ap.add_argument("--plugin", type=Path, default=Path("plugins/kind"), help="插件目录")
    ap.add_argument("--low", type=float, default=0.30, help="阈值扫描起点")
    ap.add_argument("--high", type=float, default=0.95, help="阈值扫描终点")
    ap.add_argument("--step", type=float, default=0.05, help="步长")
    args = ap.parse_args()

    manifest, sess = load_head(args.plugin)
    labels = list(manifest["labels"])
    print(f"插件 {manifest['id']}：{len(labels)} 类 {labels}")
    print(f"当前阈值 {manifest.get('threshold', 0.5)}")

    if args.embeddings:
        d = np.load(args.embeddings, allow_pickle=True)
        X = d["X"].astype(np.float32)
        truth = d["Y"].astype(np.int64) if d["Y"].ndim == 1 else d["Y"].argmax(axis=1)
        print(f"从 npz 读 {len(X)} 条特征")
    elif args.truth:
        # 走图片路径：目录名就是标签，逐张过 onnx
        rows = []
        for i, name in enumerate(labels):
            folder = args.truth / name
            if not folder.is_dir():
                continue
            for f in sorted(folder.glob("*.jpg")) + sorted(folder.glob("*.png")):
                rows.append((f, i))
        if not rows:
            raise SystemExit(f"{args.truth} 下没有找到以类名命名的子目录")
        print(f"从 {args.truth} 读 {len(rows)} 张图——这个模式只算 onnx，不做 WD14 抽特征")
        raise SystemExit("--truth 模式需要先抽特征，建议先用 --embeddings")
    else:
        raise SystemExit("需要 --embeddings 或 --truth")

    input_name = sess.get_inputs()[0].name
    logits = sess.run(None, {input_name: X})[0]
    probs = softmax(logits) if manifest.get("activation") == "softmax" else logits

    thresholds = np.arange(args.low, args.high + 1e-9, args.step)
    table = scan(probs, truth, labels, thresholds)

    print(f"\n{'阈值':>6}  {'覆盖率':>8}  {'判对率':>8}  {'拒绝数':>7}")
    print("-" * 36)
    base = table[0]["accuracy"]
    for r in table:
        mark = ""
        # 标出「判对率比基线高 5 个点以上、覆盖率还过半」的行
        if r["accuracy"] >= base + 0.05 and r["coverage"] >= 0.5:
            mark = "  ← 候选"
        print(f"{r['threshold']:>6.2f}  {r['coverage']:>7.1%}  {r['accuracy']:>7.1%}  {r['rejected']:>7}{mark}")

    best = max(
        (r for r in table if r["coverage"] >= 0.5),
        key=lambda r: r["accuracy"],
        default=None,
    )
    if best:
        print(f"\n覆盖过半的阈值里，判对率最高的是 {best['threshold']:.2f}："
              f"敢判 {best['coverage']:.0%}，其中 {best['accuracy']:.1%} 判对")
        print("\n该阈值下的逐类表现：")
        for row in per_class(probs, truth, labels, best["threshold"]):
            print(f"  {row['label']:>6}  n={row['n']:>4}  保留 {row['kept']:>4}  准确 {row['acc']:>6.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
