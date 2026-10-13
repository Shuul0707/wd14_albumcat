"""Train a franchise-specific character head on top of WD14's embeddings.

This closes the loop the upstream README only describes: WD14 gets you 90% of the
way on popular characters, and for the IP you actually care about you correct a
few hundred of its mistakes by hand, then train a linear head that beats the
generic labels on that franchise.

Why a linear head is enough
---------------------------
The embedding is already a strong, general anime-image representation. The thing
it lacks is *your* labelling convention -- which of two near-identical designs
counts as which character, what to do with a costume variant, whether a blurry
background figure counts at all. That is a linear decision boundary on top of a
good representation, and a few hundred examples per class settles it. It is also
why nobody needs a GPU to train this: a 768xK least-squares-ish problem converges
in seconds on CPU.

Input layout
------------
Either a directory of directories, one per character::

    data/labels/
      Hatsune Miku/       *.jpg   -> label "Hatsune Miku"
      Kagamine Rin/       *.jpg   -> label "Kagamine Rin"
      _background/        *.jpg   -> belongs to *no* label

or a JSONL file with one object per image::

    {"path": "C:/pics/a.jpg", "labels": ["Hatsune Miku"]}

A directory whose name starts with ``_`` is "background": those images train
every label *down*. Without them the head will happily fire on anything, so a
``_background/`` folder of assorted other artwork matters more than extra
positives.

Output
------
A ready-to-drop-in plugin directory: ``head.onnx``, ``labels.json``,
``plugin.json``. ``albumcat`` picks it up on the next run; there is no state to
migrate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

import numpy as np

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".avif", ".heic"}

# Run straight from a checkout without anyone having to set PYTHONPATH first.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
def collect_from_dirs(root: Path) -> list[tuple[Path, list[str]]]:
    """``root/<label>/*.jpg`` -> [(path, [label]), ...]; ``_name`` = background."""
    if not root.is_dir():
        raise SystemExit(f"not a directory: {root}")
    rows: list[tuple[Path, list[str]]] = []
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        is_background = sub.name.startswith("_")
        label = sub.name.lstrip("_") if is_background else sub.name
        hits = 0
        for file in sorted(sub.iterdir()):
            if file.suffix.lower() in IMAGE_SUFFIXES:
                rows.append((file, [] if is_background else [label]))
                hits += 1
        print(f"  {sub.name:30s} {hits:5d} image(s)"
              f"{'  (background: trains every label down)' if is_background else ''}")
    return rows


def collect_from_jsonl(path: Path) -> list[tuple[Path, list[str]]]:
    rows: list[tuple[Path, list[str]]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{line_no}: not valid JSON -- {exc}") from exc
        image = Path(obj["path"])
        labels = obj.get("labels") or ([obj["label"]] if obj.get("label") else [])
        rows.append((image, list(labels)))
    return rows


def embed_all(rows: list[tuple[Path, list[str]]], config_path: str | None):
    """Run every image through WD14 and return (X, labels-per-row, known labels)."""
    from albumcat.config import Config
    from albumcat.pipeline import Pipeline

    pipe = Pipeline(Config.load(config_path))
    tagger = pipe.tagger
    if tagger.embedding_dim is None:
        raise SystemExit(
            "the loaded model exposes no embedding -- run "
            "`python tools/add_feature_output.py` first"
        )

    labels = sorted({label for _, row_labels in rows for label in row_labels})
    if not labels:
        raise SystemExit("no labels found; every image was background")

    vectors: list[np.ndarray] = []
    targets: list[np.ndarray] = []
    skipped = 0
    for index, (path, row_labels) in enumerate(rows, 1):
        try:
            result = tagger.interrogate_path(path)
        except Exception as exc:  # unreadable/corrupt file should not kill a run
            print(f"  ! {path.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            skipped += 1
            continue
        if result.embedding is None:
            raise SystemExit("tagger produced no embedding mid-run; aborting")
        vectors.append(np.asarray(result.embedding, dtype=np.float32))
        target = np.zeros(len(labels), dtype=np.float32)
        for label in row_labels:
            target[labels.index(label)] = 1.0
        targets.append(target)
        if index % 50 == 0:
            print(f"  embedded {index}/{len(rows)}")

    print(f"  embedded {len(vectors)} image(s), skipped {skipped}")
    return np.stack(vectors), np.stack(targets), labels


# --------------------------------------------------------------------------- #
# training
# --------------------------------------------------------------------------- #
def train_head(
    X: np.ndarray,
    Y: np.ndarray,
    epochs: int,
    lr: float,
    l2: float,
    seed: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[float]]:
    """Multilabel logistic regression by Adam. Returns folded weights.

    Returns ``(W, b, mu, sd, loss_history)`` where ``logits = ((X-mu)/sd) @ W.T + b``.
    Standardising first is what makes a single learning rate work for every
    dimension -- embeddings have wildly different scales per axis.
    """
    mu = X.mean(axis=0)
    sd = X.std(axis=0) + 1e-6
    Xs = (X - mu) / sd

    n, dim = Xs.shape
    k = Y.shape[1]
    rng = np.random.default_rng(seed)
    W = rng.normal(0.0, 0.01, (k, dim)).astype(np.float64)
    b = np.zeros(k, dtype=np.float64)

    # Adam state
    mW = np.zeros_like(W); vW = np.zeros_like(W)
    mb = np.zeros_like(b); vb = np.zeros_like(b)
    beta1, beta2, eps = 0.9, 0.999, 1e-8

    history: list[float] = []
    for step in range(1, epochs + 1):
        logits = Xs @ W.T + b
        probs = 1.0 / (1.0 + np.exp(-logits))
        # BCE with a small floor/ceiling so log() never sees 0.
        p = np.clip(probs, 1e-7, 1 - 1e-7)
        loss = float(-(Y * np.log(p) + (1 - Y) * np.log(1 - p)).mean())
        loss += float(l2) * float((W * W).sum())

        grad_logits = (probs - Y) / (n * k)
        gW = grad_logits.T @ Xs + 2.0 * l2 * W
        gb = grad_logits.sum(axis=0)

        for param, grad, m, v in ((W, gW, mW, vW), (b, gb, mb, vb)):
            m *= beta1
            m += (1 - beta1) * grad
            v *= beta2
            v += (1 - beta2) * grad * grad
            m_hat = m / (1 - beta1**step)
            v_hat = v / (1 - beta2**step)
            param -= lr * m_hat / (np.sqrt(v_hat) + eps)

        if step == 1 or step % max(1, epochs // 10) == 0:
            history.append(loss)

    # Fold standardisation into the weights so the exported head eats raw
    # embeddings: ((X-mu)/sd) @ W.T + b == X @ (W/sd).T + (b - (W/sd) @ mu)
    W_folded = (W / sd).astype(np.float32)
    b_folded = (b - W_folded @ mu).astype(np.float32)
    return W_folded, b_folded, mu, sd, history


def report(Y: np.ndarray, probs: np.ndarray, labels: list[str], threshold: float) -> None:
    pred = probs >= threshold
    truth = Y >= 0.5
    exact = float((pred == truth).all(axis=1).mean())
    print(f"\n  exact-match (every label right): {exact * 100:.1f}%")
    print(f"  {'label':30s} {'n+':>5s} {'TP':>5s} {'FP':>5s} {'FN':>5s}  precision  recall")
    for i, label in enumerate(labels):
        tp = int((pred[:, i] & truth[:, i]).sum())
        fp = int((pred[:, i] & ~truth[:, i]).sum())
        fn = int((~pred[:, i] & truth[:, i]).sum())
        precision = tp / (tp + fp) if tp + fp else float("nan")
        recall = tp / (tp + fn) if tp + fn else float("nan")
        print(
            f"  {label[:30]:30s} {int(truth[:, i].sum()):5d} {tp:5d} {fp:5d} {fn:5d}"
            f"     {precision:6.3f}   {recall:6.3f}"
        )


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
def export_head(
    W: np.ndarray, b: np.ndarray, dim: int, out_path: Path, labels: list[str]
) -> None:
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    gemm = helper.make_node("Gemm", ["input", "W", "B"], ["logits"], transB=1)
    sigmoid = helper.make_node("Sigmoid", ["logits"], ["scores"])
    graph = helper.make_graph(
        [gemm, sigmoid],
        "albumcat_head",
        [helper.make_tensor_value_info("input", TensorProto.FLOAT, [None, dim])],
        [helper.make_tensor_value_info("scores", TensorProto.FLOAT, [None, len(labels)])],
        [numpy_helper.from_array(W, "W"), numpy_helper.from_array(b, "B")],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 10  # ORT accepts this broadly; 11 is newer than needed
    onnx.checker.check_model(model)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(out_path))


def verify_export(
    out_path: Path, X: np.ndarray, expected: np.ndarray, dim: int
) -> float:
    """Run the exported file and compare with the numpy head."""
    import onnxruntime as ort

    session = ort.InferenceSession(str(out_path), providers=["CPUExecutionProvider"])
    name = session.get_inputs()[0].name
    got = session.run(None, {name: X})[0]
    return float(np.abs(got - expected).max())


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--data", type=Path, help="directory of per-label subdirectories")
    source.add_argument("--jsonl", type=Path, help="JSONL of {path, labels}")
    ap.add_argument("--out", type=Path, required=True, help="plugin directory to create")
    ap.add_argument("--id", default=None, help="plugin id (default: --out directory name)")
    ap.add_argument("--name", default=None, help="human-readable plugin name")
    ap.add_argument("--config", default=None, help="path to config.toml")
    ap.add_argument("--epochs", type=int, default=600)
    ap.add_argument("--lr", type=float, default=0.05)
    ap.add_argument("--l2", type=float, default=1e-3, help="L2 strength; raise if it overfits")
    ap.add_argument("--val-frac", type=float, default=0.2)
    ap.add_argument("--threshold", type=float, default=0.5, help="reported metrics + plugin default")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--priority", type=int, default=10, help="below 100 = runs before WD14's own guesses")
    ap.add_argument(
        "--target",
        choices=("character", "tag", "rating"),
        default="character",
        help="which bucket the head's labels land in (default: character)",
    )
    ap.add_argument("--force", action="store_true", help="overwrite an existing --out")
    args = ap.parse_args(argv)

    out: Path = args.out
    if out.exists() and any(out.iterdir()) and not args.force:
        print(f"{out} already exists and is not empty (use --force)", file=sys.stderr)
        return 2
    plugin_id = args.id or out.name

    print(f"collecting images for plugin {plugin_id!r}")
    rows = collect_from_dirs(args.data) if args.data else collect_from_jsonl(args.jsonl)
    if len(rows) < 4:
        print(f"only {len(rows)} image(s); a head needs more than that", file=sys.stderr)

    X, Y, labels = embed_all(rows, args.config)
    print(f"  matrix: {X.shape}, {len(labels)} label(s): {', '.join(labels)}")

    # Stratified-ish split: shuffle, then slice. Per-label stratification is
    # overkill at these sizes and complicates the background rows.
    rng = random.Random(args.seed)
    order = list(range(len(X)))
    rng.shuffle(order)
    n_val = max(1, int(len(order) * args.val_frac)) if len(order) >= 5 else 0
    val_idx, train_idx = order[:n_val], order[n_val:]

    print(f"\ntraining on {len(train_idx)} image(s), validating on {len(val_idx)}")
    W, b, _mu, _sd, history = train_head(
        X[train_idx], Y[train_idx], args.epochs, args.lr, args.l2, args.seed
    )
    print("  loss: " + " -> ".join(f"{v:.4f}" for v in history))

    probs_train = 1.0 / (1.0 + np.exp(-(X[train_idx] @ W.T + b)))
    report(Y[train_idx], probs_train, labels, args.threshold)
    print("  (train)")
    if n_val:
        probs_val = 1.0 / (1.0 + np.exp(-(X[val_idx] @ W.T + b)))
        report(Y[val_idx], probs_val, labels, args.threshold)
        print("  (validation -- this is the number that matters)")

    head_path = out / "head.onnx"
    export_head(W, b, X.shape[1], head_path, labels)

    probs_all = 1.0 / (1.0 + np.exp(-(X @ W.T + b)))
    delta = verify_export(head_path, X, probs_all, X.shape[1])
    print(f"\nexported {head_path} ({head_path.stat().st_size / 1024:.1f} KB)")
    print(f"  round-trip max |numpy - onnx| = {delta:.2e}")
    if delta > 1e-4:
        print("  FAILED: exported head does not match the trained head", file=sys.stderr)
        return 1

    (out / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "plugin.json").write_text(
        json.dumps(
            {
                "id": plugin_id,
                "kind": "feature",
                "name": args.name or plugin_id,
                "description": f"trained on {len(X)} images, {len(labels)} labels",
                "target": args.target,
                "priority": args.priority,
                "threshold": args.threshold,
                "input_dim": int(X.shape[1]),
                "model": "head.onnx",
                "sha256": hashlib.sha256(head_path.read_bytes()).hexdigest(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {out / 'labels.json'} and {out / 'plugin.json'}")
    print(f"\nnext: tools\\albumcat.cmd plugins   (then re-run plan/run)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
