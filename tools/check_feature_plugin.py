"""Throwaway end-to-end check of the ``kind: feature`` plugin path.

Builds a classification head with *known* weights, installs it as a plugin, runs
the real pipeline, and checks the emitted score equals an independently computed
sigmoid(W @ e + b). A plumbing bug (wrong tensor order, missing embedding,
double-applied sigmoid) would show up as a mismatch rather than a plausible tag.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import numpy as np
import onnx
from onnx import TensorProto, helper, numpy_helper
from PIL import Image

ROOT = Path(r"E:\AlbumCategories")
sys.path.insert(0, str(ROOT / "src"))

from albumcat.config import Config  # noqa: E402
from albumcat.pipeline import load_tagger  # noqa: E402

PLUGIN_DIR = ROOT / "plugins" / "zz-smoke-feature"
LABELS = ["测试角色甲", "测试角色乙"]
DIM = 768


def build_head(W: np.ndarray, b: np.ndarray, path: Path) -> None:
    gemm = helper.make_node("Gemm", ["input", "W", "B"], ["logits"], transB=1)
    sigmoid = helper.make_node("Sigmoid", ["logits"], ["scores"])
    graph = helper.make_graph(
        [gemm, sigmoid],
        "smoke_head",
        [helper.make_tensor_value_info("input", TensorProto.FLOAT, [None, DIM])],
        [helper.make_tensor_value_info("scores", TensorProto.FLOAT, [None, len(LABELS)])],
        [numpy_helper.from_array(W, "W"), numpy_helper.from_array(b, "B")],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 10
    onnx.checker.check_model(model)
    path.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(path))


def main() -> int:
    if PLUGIN_DIR.exists():
        shutil.rmtree(PLUGIN_DIR)

    cfg = Config.load()
    tagger = load_tagger(cfg)
    assert tagger.embedding_dim == DIM, f"expected {DIM}-d embeddings, got {tagger.embedding_dim}"

    rng = np.random.default_rng(7)
    W = rng.normal(0, 0.08, (len(LABELS), DIM)).astype(np.float32)
    b = np.array([0.25, -0.75], dtype=np.float32)

    model_file = PLUGIN_DIR / "head.onnx"
    build_head(W, b, model_file)
    (PLUGIN_DIR / "labels.json").write_text(
        json.dumps(LABELS, ensure_ascii=False), encoding="utf-8"
    )
    (PLUGIN_DIR / "plugin.json").write_text(
        json.dumps(
            {
                "id": "zz-smoke-feature",
                "kind": "feature",
                "name": "smoke test head",
                "target": "character",
                "priority": 5,
                "threshold": 0.0,
                "input_dim": DIM,
                "model": "head.onnx",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"installed synthetic head -> {PLUGIN_DIR}")

    # Reload the pipeline so the new plugin is discovered.
    from albumcat.pipeline import Pipeline

    pipe = Pipeline(cfg, tagger=tagger)
    print("warnings:", pipe.warn_if_plugins_starved())

    ok = True
    for name in ("00-flat.jpg", "01-gradient.jpg"):
        image = Image.open(ROOT / "samples" / name)
        plan, tagset = pipe.plan_image(image, path=name)

        # The embedding is not stored on the TagSet, so recompute it directly.
        embedding = tagger.interrogate(image).embedding
        expected = 1.0 / (1.0 + np.exp(-(W @ embedding + b)))

        print(f"\n{name}")
        for i, label in enumerate(LABELS):
            pred = tagset.get(label, "character")
            if pred is None:
                print(f"  {label}: MISSING")
                ok = False
                continue
            delta = abs(pred.score - float(expected[i]))
            flag = "OK " if delta < 1e-4 else "BAD"
            print(
                f"  {label}: score={pred.score:.6f} expected={expected[i]:.6f} "
                f"|d|={delta:.2e} source={pred.source} {flag}"
            )
            if delta >= 1e-4:
                ok = False

    shutil.rmtree(PLUGIN_DIR)
    print(f"\nplugin removed; {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
