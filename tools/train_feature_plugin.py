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


def read_jsonl_rows(path: Path) -> list[dict]:
    """Parse a JSONL manifest into ``{path, labels, split, source}`` dicts.

    ``split`` ("train"/"val") is optional; when every row carries one the split is
    taken from the file instead of a random slice, which is the only way to keep
    augmented copies out of the validation set.  ``source`` is optional too and is
    only used to print per-source accuracy.
    """
    rows: list[dict] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{line_no}: not valid JSON -- {exc}") from exc
        labels = obj.get("labels") or ([obj["label"]] if obj.get("label") else [])
        rows.append({
            "path": Path(obj["path"]),
            "labels": [str(label) for label in labels],
            "split": str(obj.get("split") or ""),
            "source": str(obj.get("source") or ""),
        })
    return rows


def collect_from_jsonl(path: Path) -> list[tuple[Path, list[str]]]:
    return [(row["path"], list(row["labels"])) for row in read_jsonl_rows(path)]


#: Manifest keys a retrain must not drop. They are hand-tuned or hand-written and
#: the exporter cannot recompute them; `size_prior` in particular used to vanish
#: on every retrain, silently costing ~2 points of real-world accuracy.
CARRIED_KEYS = ("size_prior", "author", "homepage")


def read_manifest(path: Path) -> dict:
    """The existing manifest, or ``{}`` -- a half-written file is not fatal."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def merge_manifest(fresh: dict, previous: dict) -> dict:
    """Fresh export wins, except for keys the trainer has no opinion about."""
    merged = dict(fresh)
    for key in CARRIED_KEYS:
        if key in previous and key not in merged:
            merged[key] = previous[key]
    try:
        merged["version"] = str(int(previous.get("version", "0")) + 1)
    except (TypeError, ValueError):
        pass
    return merged


def embedding_fingerprint(rows: list[tuple[Path, list[str]]], config_path: str | None,
                          semantic: bool = False) -> str:
    """A cheap identity for "the same images, in the same order, same model".

    Size plus mtime catches the "I added/removed/replaced files" case; the
    head/tail digest catches an in-place edit when the filesystem's mtime
    granularity is coarser than the edit (it is, on Windows, for a rewrite of the
    same length in the same tick).

    ``semantic`` is part of the identity: a WD14-only cache must never be reused
    for a fused run, or the head would be trained on half its input.
    """
    digest = hashlib.sha256()
    digest.update(str(config_path or "").encode("utf-8"))
    digest.update(b"semantic" if semantic else b"wd14")
    for path, labels in rows:
        digest.update(str(path).encode("utf-8"))
        digest.update(",".join(labels).encode("utf-8"))
        try:
            stat = path.stat()
            with path.open("rb") as handle:
                head = handle.read(8192)
                if stat.st_size > 8192:
                    handle.seek(max(0, stat.st_size - 8192))
                    head += handle.read(8192)
        except OSError:
            continue
        digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8"))
        digest.update(hashlib.sha256(head).digest())
    return digest.hexdigest()


def load_embeddings(path: Path, fingerprint: str):
    """Return ``(X, Y, labels, kept)`` from a cache made for these exact images."""
    if not path.exists():
        return None
    try:
        with np.load(path, allow_pickle=False) as data:
            if str(data["fingerprint"]) != fingerprint:
                print(f"  cache {path} is stale (images changed), embedding again")
                return None
            labels = json.loads(str(data["labels"]))
            X, Y = data["X"], data["Y"]
            kept = ([int(value) for value in data["kept"]]
                    if "kept" in data.files else list(range(len(X))))
            return X, Y, list(labels), kept
    except (OSError, KeyError, ValueError) as exc:
        print(f"  cache {path} unusable ({type(exc).__name__}: {exc})")
        return None


def save_embeddings(path: Path, fingerprint: str, X, Y, labels: list[str],
                    kept: list[int] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        X=X.astype(np.float32),
        Y=Y.astype(np.float32),
        labels=np.asarray(json.dumps(labels, ensure_ascii=False)),
        fingerprint=np.asarray(fingerprint),
        kept=np.asarray(kept if kept is not None else range(len(X)), dtype=np.int64),
    )
    print(f"  cached embeddings -> {path}")


def embed_all(rows: list[tuple[Path, list[str]]], config_path: str | None,
              semantic: bool = False):
    """Run every image through WD14 and return (X, Y, labels, kept row indices).

    A corrupt file is skipped rather than fatal, so the caller needs ``kept`` to
    keep any per-row metadata (split, source) lined up with the matrix rows.

    With ``semantic`` the Chinese-CLIP image vector (512-d) is appended to WD14's
    768 features, giving a 1,280-d input. That fusion was the only change that
    beat every single-feature variant (AUC .855 -> .907): WD14 knows "texture,
    strokes, ink"; CLIP knows "this looks like a photograph of a beach".
    """
    from albumcat.config import Config
    from albumcat.pipeline import Pipeline

    config = Config.load(config_path)
    encoder = None
    if semantic:
        from albumcat.semantic import SemanticEncoder

        encoder = SemanticEncoder(config.semantic)
        if not encoder.available:
            raise SystemExit(
                "semantic encoding was requested but Chinese-CLIP is not available "
                f"(reason: {encoder.info().reason or 'unknown'}) -- run `albumcat doctor`"
            )
        print(f"  fusing WD14 features with {config.semantic.model_id} "
              f"({config.semantic.dim}-d)")
    pipe = Pipeline(config, semantic=encoder)
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
    kept: list[int] = []
    skipped = 0
    for index, (path, row_labels) in enumerate(rows, 1):
        try:
            if semantic:
                from PIL import Image, ImageOps

                with Image.open(path) as raw:
                    image = ImageOps.exif_transpose(raw).convert("RGB")
                result = tagger.interrogate(image)
            else:
                result = tagger.interrogate_path(path)
        except Exception as exc:  # unreadable/corrupt file should not kill a run
            print(f"  ! {path.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            skipped += 1
            continue
        if result.embedding is None:
            raise SystemExit("tagger produced no embedding mid-run; aborting")
        parts = [np.asarray(result.embedding, dtype=np.float32)]
        if semantic:
            try:
                parts.append(np.asarray(encoder.encode_image(image), dtype=np.float32))
            except Exception as exc:  # noqa: BLE001 - skip the row, keep the run
                print(f"  ! {path.name}: clip {type(exc).__name__}: {exc}", file=sys.stderr)
                skipped += 1
                continue
        vectors.append(np.concatenate(parts))
        kept.append(index - 1)
        target = np.zeros(len(labels), dtype=np.float32)
        for label in row_labels:
            target[labels.index(label)] = 1.0
        targets.append(target)
        if index % 50 == 0:
            print(f"  embedded {index}/{len(rows)}")

    print(f"  embedded {len(vectors)} image(s), skipped {skipped}")
    return np.stack(vectors), np.stack(targets), labels, kept


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
    loss: str = "bce",
    class_weights: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[float]]:
    """Logistic regression by Adam. Returns folded weights.

    ``loss="bce"`` treats the labels as independent (multi-label: one image can
    be both "Hatsune Miku" and "vocaloid"). ``loss="ce"`` treats them as mutually
    exclusive (one image is exactly one of illustration/meme/photo/screenshot)
    and trains a softmax head instead, which is what a "what kind of picture is
    this" classifier wants: a softmax cannot say 插画 0.8 梗图 0.7 at once.

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

    if class_weights is None:
        weights = np.ones(k, dtype=np.float64)
    else:
        weights = np.asarray(class_weights, dtype=np.float64)
        weights = weights / weights.mean()

    # Adam state
    mW = np.zeros_like(W); vW = np.zeros_like(W)
    mb = np.zeros_like(b); vb = np.zeros_like(b)
    beta1, beta2, eps = 0.9, 0.999, 1e-8

    history: list[float] = []
    softmax = loss == "ce"
    for step in range(1, epochs + 1):
        logits = Xs @ W.T + b
        if softmax:
            # Softmax cross-entropy. The one-hot target picks the true column.
            shifted = logits - logits.max(axis=1, keepdims=True)
            exp = np.exp(shifted)
            total = exp.sum(axis=1, keepdims=True)
            probs = exp / total
            row_weight = (Y * weights).sum(axis=1)  # weight of each row's class
            picked = np.clip((probs * Y).sum(axis=1), 1e-7, 1.0)
            loss_value = float(-(row_weight * np.log(picked)).sum() / row_weight.sum())
            grad_logits = (probs - Y) * (row_weight / row_weight.sum())[:, None]
        else:
            probs = 1.0 / (1.0 + np.exp(-logits))
            # BCE with a small floor/ceiling so log() never sees 0.
            p = np.clip(probs, 1e-7, 1 - 1e-7)
            loss_value = float(
                (-(Y * np.log(p) + (1 - Y) * np.log(1 - p)) * weights).mean()
            )
            grad_logits = (probs - Y) * weights / (n * k)
        loss_value += float(l2) * float((W * W).sum())

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
            history.append(loss_value)

    # Fold standardisation into the weights so the exported head eats raw
    # embeddings: ((X-mu)/sd) @ W.T + b == X @ (W/sd).T + (b - (W/sd) @ mu)
    W_folded = (W / sd).astype(np.float32)
    b_folded = (b - W_folded @ mu).astype(np.float32)
    return W_folded, b_folded, mu, sd, history


def predict(X: np.ndarray, W: np.ndarray, b: np.ndarray, loss: str = "bce") -> np.ndarray:
    """Apply the head in numpy, matching the activation the export uses."""
    logits = X @ W.T + b
    if loss == "ce":
        shifted = logits - logits.max(axis=1, keepdims=True)
        exp = np.exp(shifted)
        return exp / exp.sum(axis=1, keepdims=True)
    return 1.0 / (1.0 + np.exp(-logits))


def auc(scores: np.ndarray, truth: np.ndarray) -> float:
    """Rank-based ROC AUC, no sklearn. Ties get their average rank."""
    positive = truth.astype(bool)
    n_pos = int(positive.sum())
    n_neg = int((~positive).sum())
    if not n_pos or not n_neg:
        return float("nan")
    order = np.argsort(scores, kind="mergesort")
    ranks = np.empty(len(scores), dtype=np.float64)
    sorted_scores = scores[order]
    index = 0
    while index < len(order):
        stop = index + 1
        while stop < len(order) and sorted_scores[stop] == sorted_scores[index]:
            stop += 1
        average_rank = (index + stop + 1) / 2.0  # 1-based average of the tie group
        ranks[order[index:stop]] = average_rank
        index = stop
    return float((ranks[positive].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def report(
    Y: np.ndarray,
    probs: np.ndarray,
    labels: list[str],
    threshold: float,
    loss: str = "bce",
) -> None:
    if loss == "ce":
        # The export is a softmax, so the decision is "which class wins", and the
        # threshold only gates how sure the winner has to be.
        winner = probs.argmax(axis=1)
        pred = np.zeros_like(probs, dtype=bool)
        pred[np.arange(len(probs)), winner] = probs.max(axis=1) >= threshold
    else:
        pred = probs >= threshold
    truth = Y >= 0.5
    exact = float((pred == truth).all(axis=1).mean())
    print(f"\n  exact-match (every label right): {exact * 100:.1f}%")
    print(
        f"  {'label':30s} {'n+':>5s} {'TP':>5s} {'FP':>5s} {'FN':>5s}"
        f"  precision  recall     auc"
    )
    for i, label in enumerate(labels):
        tp = int((pred[:, i] & truth[:, i]).sum())
        fp = int((pred[:, i] & ~truth[:, i]).sum())
        fn = int((~pred[:, i] & truth[:, i]).sum())
        precision = tp / (tp + fp) if tp + fp else float("nan")
        recall = tp / (tp + fn) if tp + fn else float("nan")
        print(
            f"  {label[:30]:30s} {int(truth[:, i].sum()):5d} {tp:5d} {fp:5d} {fn:5d}"
            f"     {precision:6.3f}   {recall:6.3f}   {auc(probs[:, i], truth[:, i]):6.3f}"
        )


def report_binary(
    Y: np.ndarray,
    probs: np.ndarray,
    labels: list[str],
    positive: str,
    loss: str = "bce",
) -> None:
    """Treat ``positive`` as the one class and everything else as the negative one.

    This is the number the phone's "only show me illustrations" filter lives or
    dies by: a meme filed under 插画 is visible pollution, an illustration filed
    under 梗图 is a missing picture.
    """
    if positive not in labels:
        return
    index = labels.index(positive)
    truth = Y[:, index] >= 0.5
    if loss == "ce":
        is_pos = probs.argmax(axis=1) == index
        rule = "softmax winner"
    elif probs.shape[1] == 1:
        # a single sigmoid output: there is nothing to out-score
        is_pos = probs[:, index] >= 0.5
        rule = f"{positive} >= 0.5"
    else:
        is_pos = probs[:, index] >= np.delete(probs, index, axis=1).max(axis=1)
        rule = f"{positive} > every other label"
    tp = int((is_pos & truth).sum())
    fp = int((is_pos & ~truth).sum())
    fn = int((~is_pos & truth).sum())
    tn = int((~is_pos & ~truth).sum())
    precision = tp / (tp + fp) if tp + fp else float("nan")
    recall = tp / (tp + fn) if tp + fn else float("nan")
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else float("nan")
    accuracy = (tp + tn) / max(1, tp + fp + fn + tn)
    print(f"\n  binary: {positive!r} vs everything else  ({rule})")
    print(f"    accuracy {accuracy * 100:5.1f}%   precision {precision:5.3f}"
          f"   recall {recall:5.3f}   f1 {f1:5.3f}   auc {auc(probs[:, index], truth):5.3f}")
    print(f"    confusion  tp {tp}  fp {fp}  fn {fn}  tn {tn}")


def confusion(
    Y: np.ndarray,
    probs: np.ndarray,
    labels: list[str],
    threshold: float = 0.0,
) -> None:
    """Rows are the truth, columns the winner; ``-`` is "no label won".

    A precision/recall table hides which class absorbs which; when the question is
    "why does 照片 only hit 53%", the column that ate it is the answer.
    """
    winner = probs.argmax(axis=1)
    sure = probs.max(axis=1) >= threshold
    truth = Y >= 0.5
    columns = list(labels) + ["-"]
    counts = {row: {col: 0 for col in columns} for row in labels}
    for index in range(len(Y)):
        true_labels = [labels[i] for i in np.flatnonzero(truth[index])]
        guess = labels[winner[index]] if sure[index] else "-"
        for true_label in true_labels or ["-"]:
            if true_label in counts:
                counts[true_label][guess] = counts[true_label].get(guess, 0) + 1
    width = max(len(col) for col in columns)
    width = max(width, 4)
    print(f"\n  truth \\ guess  " + " ".join(f"{col:>{width}s}" for col in columns))
    for row in labels:
        cells = " ".join(f"{counts[row].get(col, 0):>{width}d}" for col in columns)
        total = sum(counts[row].values())
        hit = counts[row].get(row, 0)
        share = f"{hit / total * 100:4.1f}%" if total else "   - "
        print(f"  {row[:14]:14s} " + cells + f"   {share} of {total}")


def report_by_source(
    Y: np.ndarray,
    probs: np.ndarray,
    labels: list[str],
    sources: list[str],
) -> None:
    """Exact-match per data source: a new source that scores 50% is a warning.

    Pooling every source into one accuracy hides a head that simply learned the
    biggest source; this line is what catches it.
    """
    if not any(sources):
        return
    winner = probs.argmax(axis=1)
    truth_index = Y.argmax(axis=1)
    buckets: dict[str, list[int]] = {}
    for index, source in enumerate(sources):
        buckets.setdefault(source or "?", []).append(index)
    print("\n  exact-match by source")
    for source in sorted(buckets):
        rows = np.asarray(buckets[source])
        hit = float((winner[rows] == truth_index[rows]).mean())
        print(f"    {source[:24]:24s} {len(rows):5d} image(s)   {hit * 100:5.1f}%")


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
def export_head(
    W: np.ndarray,
    b: np.ndarray,
    dim: int,
    out_path: Path,
    labels: list[str],
    activation: str = "sigmoid",
) -> None:
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    gemm = helper.make_node("Gemm", ["input", "W", "B"], ["logits"], transB=1)
    if activation == "softmax":
        tail = helper.make_node("Softmax", ["logits"], ["scores"], axis=1)
    else:
        tail = helper.make_node("Sigmoid", ["logits"], ["scores"])
    graph = helper.make_graph(
        [gemm, tail],
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
        "--loss",
        choices=("bce", "ce"),
        default="bce",
        help="bce: labels are independent (characters); "
             "ce: labels are mutually exclusive (one image is exactly one kind)",
    )
    ap.add_argument(
        "--class-weight",
        action="store_true",
        help="with --loss ce, weight rare classes up so a small class is not ignored",
    )
    ap.add_argument(
        "--binary-label",
        default=None,
        help="also report this label as positive vs all the others (e.g. illustration)",
    )
    ap.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="npz to reuse embeddings across runs; ~1500 images cost minutes to embed",
    )
    ap.add_argument(
        "--target",
        choices=("character", "tag", "rating"),
        default="character",
        help="which bucket the head's labels land in (default: character)",
    )
    ap.add_argument(
        "--semantic",
        action="store_true",
        help="stack the Chinese-CLIP image vector (512-d) onto WD14's features; "
             "needs the semantic towers, and the head then only runs where they load",
    )
    ap.add_argument("--force", action="store_true", help="overwrite an existing --out")
    args = ap.parse_args(argv)

    out: Path = args.out
    if out.exists() and any(out.iterdir()) and not args.force:
        print(f"{out} already exists and is not empty (use --force)", file=sys.stderr)
        return 2
    plugin_id = args.id or out.name
    semantic_dim = 0
    if args.semantic:
        from albumcat.config import Config

        semantic_dim = int(Config.load(args.config).semantic.dim)

    print(f"collecting images for plugin {plugin_id!r}")
    rows = collect_from_dirs(args.data) if args.data else collect_from_jsonl(args.jsonl)
    meta = read_jsonl_rows(args.jsonl) if args.jsonl else []
    if len(rows) < 4:
        print(f"only {len(rows)} image(s); a head needs more than that", file=sys.stderr)
    if args.loss == "ce":
        background = [path for path, row_labels in rows if not row_labels]
        if background:
            print(
                f"{len(background)} image(s) have no label; --loss ce needs exactly one "
                f"label per image (first: {background[0]})",
                file=sys.stderr,
            )
            return 2

    kept = list(range(len(rows)))
    cached = None
    if args.cache:
        fingerprint = embedding_fingerprint(rows, args.config, args.semantic)
        cached = load_embeddings(args.cache, fingerprint)
    if cached:
        X, Y, labels, kept = cached
        print(f"  matrix: {X.shape}, {len(labels)} label(s): {', '.join(labels)}  (from cache)")
    else:
        X, Y, labels, kept = embed_all(rows, args.config, args.semantic)
        print(f"  matrix: {X.shape}, {len(labels)} label(s): {', '.join(labels)}")
        if args.semantic and semantic_dim and X.shape[1] <= semantic_dim:
            print(f"the fused matrix is only {X.shape[1]}-d for a {semantic_dim}-d CLIP half",
                  file=sys.stderr)
            return 2
        if args.cache:
            save_embeddings(args.cache, embedding_fingerprint(rows, args.config, args.semantic),
                            X, Y, labels, kept)

    sources = [meta[i]["source"] if i < len(meta) else "" for i in kept]
    splits = [meta[i]["split"] if i < len(meta) else "" for i in kept]

    weights = None
    if args.class_weight:
        counts = Y.sum(axis=0)
        weights = np.where(counts > 0, len(Y) / (max(1, Y.shape[1]) * np.maximum(counts, 1)), 0.0)
        print("  class weights: " + ", ".join(
            f"{label}={weight:.2f}" for label, weight in zip(labels, weights)
        ))

    # Splits: a JSONL manifest that names every row's split wins, because that is
    # the only way to guarantee augmented copies stay out of validation. Otherwise
    # it is a plain shuffle-then-slice.
    if splits and all(value in ("train", "val") for value in splits):
        train_idx = [i for i, value in enumerate(splits) if value == "train"]
        val_idx = [i for i, value in enumerate(splits) if value == "val"]
        n_val = len(val_idx)
        print(f"  split from the manifest: {len(train_idx)} train / {n_val} val")
        if not train_idx or not val_idx:
            print("the manifest must contain both a train and a val row", file=sys.stderr)
            return 2
    else:
        if splits and any(splits):
            print("  ! some rows have no split; falling back to a random slice")
        rng = random.Random(args.seed)
        order = list(range(len(X)))
        rng.shuffle(order)
        n_val = max(1, int(len(order) * args.val_frac)) if len(order) >= 5 else 0
        val_idx, train_idx = order[:n_val], order[n_val:]

    print(f"\ntraining on {len(train_idx)} image(s), validating on {len(val_idx)}")
    W, b, _mu, _sd, history = train_head(
        X[train_idx], Y[train_idx], args.epochs, args.lr, args.l2, args.seed,
        loss=args.loss, class_weights=weights,
    )
    print("  loss: " + " -> ".join(f"{v:.4f}" for v in history))

    probs_train = predict(X[train_idx], W, b, args.loss)
    report(Y[train_idx], probs_train, labels, args.threshold, args.loss)
    if args.binary_label:
        report_binary(Y[train_idx], probs_train, labels, args.binary_label, args.loss)
    print("  (train)")
    if n_val:
        probs_val = predict(X[val_idx], W, b, args.loss)
        report(Y[val_idx], probs_val, labels, args.threshold, args.loss)
        if args.loss == "ce":
            confusion(Y[val_idx], probs_val, labels, args.threshold)
        report_by_source(Y[val_idx], probs_val, labels, [sources[i] for i in val_idx])
        if args.binary_label:
            report_binary(Y[val_idx], probs_val, labels, args.binary_label, args.loss)
        print("  (validation -- this is the number that matters)")

    head_path = out / "head.onnx"
    export_head(
        W, b, X.shape[1], head_path, labels,
        activation="softmax" if args.loss == "ce" else "sigmoid",
    )

    probs_all = predict(X, W, b, args.loss)
    delta = verify_export(head_path, X, probs_all, X.shape[1])
    print(f"\nexported {head_path} ({head_path.stat().st_size / 1024:.1f} KB)")
    print(f"  round-trip max |numpy - onnx| = {delta:.2e}")
    if delta > 1e-4:
        print("  FAILED: exported head does not match the trained head", file=sys.stderr)
        return 1

    (out / "labels.json").write_text(
        json.dumps(labels, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest = {
        "id": plugin_id,
        "kind": "feature",
        "name": args.name or plugin_id,
        "description": f"trained on {len(X)} images, {len(labels)} labels",
        "target": args.target,
        "priority": args.priority,
        "threshold": args.threshold,
        # Inline as well as labels.json: the phone shows these verbatim, and a
        # manifest that only points at a side file is easy to ship half-copied.
        "labels": list(labels),
        "input_dim": int(X.shape[1]),
        "model": "head.onnx",
        "sha256": hashlib.sha256(head_path.read_bytes()).hexdigest(),
    }
    if args.semantic:
        # The loader needs to know the split point: it appends the CLIP vector to
        # the WD14 features in exactly this order.
        manifest["semantic_dim"] = int(semantic_dim)
    if args.loss == "ce":
        # Not a knobs the loader reads: it tells whoever opens plugin.json that
        # the scores sum to 1 and that at most one label can win.
        manifest["exclusive"] = True
        manifest["activation"] = "softmax"

    # Retraining must not silently disarm post-processing that was tuned by hand.
    # The size prior lives in plugin.json only, so a fresh export used to delete
    # it and nobody noticed until the accuracy dropped.
    manifest = merge_manifest(manifest, read_manifest(out / "plugin.json"))

    (out / "plugin.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"wrote {out / 'labels.json'} and {out / 'plugin.json'}")
    print(f"\nnext: tools\\albumcat.cmd plugins   (then re-run plan/run)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
