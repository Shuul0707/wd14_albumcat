"""Add the 768-d penultimate feature vector as an extra graph output.

Why this exists
---------------
`kind: feature` plugins want the image embedding that the classifier head
consumes. ONNX Runtime will not hand you an intermediate tensor on request --
the graph is fused during optimisation and `session.run(["<inner tensor>"])`
fails with ``Invalid output name``. That is exactly why upstream ships
``split_model.py``, which carves the network into a 1.2 GB
``feature_extractor.onnx`` plus a 43 MB ``classifier.onnx``.

We do not need two files. Declaring the tensor as an *additional graph output*
of the existing model makes it materialise, costs one 356 MB file, and keeps
everything in a single `InferenceSession` -- no second provider setup, no
second copy of the weights.

The embedding width is whatever the tower emits: 768 for ViT-B/16
(`wd-v1-4-vit-tagger-v2`), 1024 for ViT-L/14. Do not trust the "1024-d" figure
in upstream's README -- this script reports the real number.

Verification
------------
A wrong tensor name would silently yield plausible-looking garbage, so after
saving we re-run the model and check that

    sigmoid(features @ W.T + b) == predictions_sigmoid

using the head's own weights, read out of the Gemm initialisers. If that
identity holds to floating-point tolerance, the feature really is the input the
classifier sees.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import onnx
import onnxruntime as ort
from onnx import shape_inference

DEFAULT_MODEL = Path("models/vit/model.onnx")
DEFAULT_OUT = Path("models/vit/model_features.onnx")


def find_feature_tensor(model: onnx.ModelProto) -> str:
    """Walk back from the graph output to the tensor feeding the dense head."""
    graph = model.graph
    producer = {out: node for node in graph.node for out in node.output}

    output_name = graph.output[0].name
    node = producer[output_name]

    # The exported head is  Sigmoid(Gemm(pooled_features, W, b))
    # but tolerate a Sigmoid-free export too.
    if node.op_type == "Sigmoid":
        node = producer[node.input[0]]
    if node.op_type != "Gemm":
        raise SystemExit(
            f"expected the classifier head to be a Gemm, found {node.op_type}; "
            "this model has an unusual export and needs manual inspection"
        )
    return node.input[0]


def head_weights(model: onnx.ModelProto, feature_name: str) -> tuple[np.ndarray, np.ndarray, int]:
    """Return (W, b, transB) for the Gemm that consumes ``feature_name``."""
    graph = model.graph
    producer = {out: node for node in graph.node for out in node.output}
    node = producer[graph.output[0].name]
    if node.op_type == "Sigmoid":
        node = producer[node.input[0]]
    if node.input[0] != feature_name:
        raise SystemExit("internal error: traced the wrong Gemm")

    inits = {init.name: init for init in graph.initializer}
    w = onnx.numpy_helper.to_array(inits[node.input[1]])
    b = (
        onnx.numpy_helper.to_array(inits[node.input[2]])
        if len(node.input) > 2
        else np.zeros(w.shape[0], dtype=np.float32)
    )
    trans_b = 0
    for attr in node.attribute:
        if attr.name == "transB":
            trans_b = int(attr.i)
    return w, b, trans_b


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--name", default="features", help="name of the added output")
    ap.add_argument("--force", action="store_true", help="overwrite an existing --out")
    args = ap.parse_args(argv)

    if not args.model.exists():
        print(f"model not found: {args.model}", file=sys.stderr)
        print("run `python tools/download_model.py` first", file=sys.stderr)
        return 1
    if args.out.exists() and not args.force:
        print(f"{args.out} already exists (use --force to rebuild)")
        return 0

    print(f"loading {args.model} ({args.model.stat().st_size / 1e6:.0f} MB) ...")
    model = onnx.load(str(args.model))

    feature_name = find_feature_tensor(model)
    print(f"feature tensor : {feature_name}")

    w, b, trans_b = head_weights(model, feature_name)
    print(f"head weights   : {w.shape} (transB={trans_b}), bias {b.shape}")

    model = shape_inference.infer_shapes(model)

    # Gemm semantics: transB=0 -> Y = A @ B with B = [K, M]; transB=1 -> Y = A @ B.T
    # with B = [M, K]. Either way K (the embedding width) is:
    if trans_b:
        width = w.shape[1]
    else:
        width = w.shape[0]
    print(f"embedding width: {width}")

    existing = {o.name for o in model.graph.output}
    if args.name in existing:
        print(f"output {args.name!r} already present; nothing to add")
    else:
        # A graph output is identified by its tensor name -- there is no aliasing,
        # so declaring an output called `features` while the tensor is called
        # `.../predictions_globalavgpooling/Mean:0` produces a model ONNX Runtime
        # rejects with "Graph output (features) does not exist in the graph".
        # Copy it through an Identity so the exported name is readable.
        model.graph.node.append(
            onnx.helper.make_node(
                "Identity", [feature_name], [args.name], name=f"{args.name}_export"
            )
        )
        model.graph.output.append(
            onnx.helper.make_tensor_value_info(args.name, onnx.TensorProto.FLOAT, [None, width])
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(args.out))
    print(f"wrote          : {args.out} ({args.out.stat().st_size / 1e6:.0f} MB)")

    # -- verify the identity sigmoid(features @ W.T + b) == predictions ---- #
    print("\nverifying against the head...")
    session = ort.InferenceSession(str(args.out), providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name
    prediction_name = next(o.name for o in session.get_outputs() if o.name != args.name)
    dummy = np.random.default_rng(0).uniform(0, 255, (1, 448, 448, 3)).astype(np.float32)
    feats, preds = session.run([args.name, prediction_name], {input_name: dummy})

    logits = feats @ w.T + b if trans_b else feats @ w + b
    recomputed = 1.0 / (1.0 + np.exp(-logits.astype(np.float64)))
    delta = float(np.abs(recomputed - preds.astype(np.float64)).max())

    print(f"  feature shape : {feats.shape}")
    print(f"  predictions   : {preds.shape}")
    print(f"  max |Δ|       : {delta:.3e}")
    if delta > 1e-4:
        print("  FAILED: the feature tensor does not reproduce the head's output.")
        print("  Do not use this file; the traced tensor is not the classifier input.")
        return 1
    print("  OK: the embedding is exactly what the classifier consumes.")

    print(
        f"\nset model.features = \"{args.out.name}\" in config.toml, or just leave it --\n"
        "albumcat prefers `<model stem>_features.onnx` when it exists."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
