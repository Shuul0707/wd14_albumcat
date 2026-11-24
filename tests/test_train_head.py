"""The training helpers behind ``tools/train_feature_plugin.py``.

Only the pure-numpy parts are tested here -- no WD14 model, no ONNX runtime. That
is deliberate: the maths (standardisation folded into the weights, softmax vs
sigmoid, rank AUC, the embedding cache) is where a silent mistake hides, and it
is exactly the part that does not need a 373 MB model to check.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "train_feature_plugin", ROOT / "tools" / "train_feature_plugin.py"
)
assert SPEC and SPEC.loader
trainer = importlib.util.module_from_spec(SPEC)
sys.modules["train_feature_plugin"] = trainer
SPEC.loader.exec_module(trainer)


def separable(n_per_class: int = 40, dim: int = 16, classes: int = 3, seed: int = 0):
    """Three well-separated blobs: a linear head must get these right."""
    rng = np.random.default_rng(seed)
    centres = rng.normal(0, 3.0, (classes, dim))
    X = np.concatenate([centres[c] + rng.normal(0, 0.3, (n_per_class, dim)) for c in range(classes)])
    Y = np.zeros((len(X), classes), dtype=np.float32)
    Y[np.arange(len(X)), np.repeat(np.arange(classes), n_per_class)] = 1.0
    return X.astype(np.float32), Y


class TestAuc:
    def test_perfect_ranking_is_one(self) -> None:
        scores = np.array([0.1, 0.2, 0.8, 0.9])
        truth = np.array([0, 0, 1, 1], dtype=bool)
        assert trainer.auc(scores, truth) == 1.0

    def test_reversed_ranking_is_zero(self) -> None:
        scores = np.array([0.9, 0.8, 0.2, 0.1])
        truth = np.array([0, 0, 1, 1], dtype=bool)
        assert trainer.auc(scores, truth) == 0.0

    def test_all_ties_is_one_half(self) -> None:
        scores = np.array([0.5, 0.5, 0.5, 0.5])
        truth = np.array([0, 1, 0, 1], dtype=bool)
        assert trainer.auc(scores, truth) == 0.5

    def test_a_single_class_is_undefined(self) -> None:
        assert np.isnan(trainer.auc(np.array([0.1, 0.2]), np.array([1, 1], dtype=bool)))


class TestTraining:
    def test_softmax_head_separates_three_blobs(self) -> None:
        X, Y = separable()
        W, b, mu, sd, history = trainer.train_head(X, Y, 300, 0.1, 0.0, 0, loss="ce")
        probs = trainer.predict(X, W, b, loss="ce")
        assert float((probs.argmax(axis=1) == Y.argmax(axis=1)).mean()) > 0.99
        assert np.allclose(probs.sum(axis=1), 1.0, atol=1e-5)
        assert history[-1] < history[0]

    def test_sigmoid_head_separates_three_blobs(self) -> None:
        X, Y = separable()
        W, b, *_ = trainer.train_head(X, Y, 300, 0.1, 0.0, 0, loss="bce")
        probs = trainer.predict(X, W, b, loss="bce")
        assert float((probs.argmax(axis=1) == Y.argmax(axis=1)).mean()) > 0.99
        assert np.all(probs >= 0.0) and np.all(probs <= 1.0)

    def test_folded_weights_match_the_standardised_head(self) -> None:
        # The export eats raw embeddings, so folding must be exact: undoing the
        # fold and feeding standardised input has to give the same scores.
        # Getting this wrong shifts every score by a constant the model never saw.
        X, Y = separable(n_per_class=15)
        W_folded, b_folded, mu, sd, _ = trainer.train_head(X, Y, 20, 0.05, 1e-3, 0)
        W_raw = W_folded * sd
        b_raw = b_folded + W_folded @ mu
        standardised = (X - mu) / sd @ W_raw.T + b_raw
        folded = X @ W_folded.T + b_folded
        assert np.allclose(standardised, folded, atol=1e-3)

    def test_class_weights_lift_a_rare_class(self) -> None:
        X, Y = separable(n_per_class=40)
        rare = Y.copy()
        rare[:, 2] = 0.0  # class 2 never actually appears
        rare[:5, 2] = 1.0  # ... except for five rows
        rare[:5, :2] = 0.0
        weights = np.array([1.0, 1.0, 8.0], dtype=np.float64)
        W, b, *_ = trainer.train_head(
            X, rare, 300, 0.1, 0.0, 0, loss="ce", class_weights=weights
        )
        probs = trainer.predict(X, W, b, loss="ce")
        assert probs[:5, 2].mean() > 0.5


class TestEmbeddingCache:
    def make_image(self, path: Path, payload: bytes) -> Path:
        path.write_bytes(payload)
        return path

    def test_round_trip(self, tmp_path: Path) -> None:
        X = np.zeros((3, 4), dtype=np.float32)
        Y = np.eye(3, dtype=np.float32)
        cache = tmp_path / "emb.npz"
        trainer.save_embeddings(cache, "fingerprint", X, Y, ["甲", "乙", "丙"])
        loaded = trainer.load_embeddings(cache, "fingerprint")
        assert loaded is not None
        assert loaded[2] == ["甲", "乙", "丙"]
        assert np.array_equal(loaded[0], X)

    def test_a_different_fingerprint_is_stale(self, tmp_path: Path) -> None:
        cache = tmp_path / "emb.npz"
        trainer.save_embeddings(cache, "one", np.zeros((1, 2), np.float32), np.zeros((1, 1), np.float32), ["甲"])
        assert trainer.load_embeddings(cache, "two") is None

    def test_fingerprint_tracks_image_bytes_and_labels(self, tmp_path: Path) -> None:
        image = self.make_image(tmp_path / "a.jpg", b"first version of the bytes")
        first = trainer.embedding_fingerprint([(image, ["甲"])], None)
        assert first == trainer.embedding_fingerprint([(image, ["甲"])], None)
        # Same length, and possibly the same mtime tick: only the head digest
        # can notice an in-place rewrite like this one.
        image.write_bytes(b"other version of the bytes")
        assert trainer.embedding_fingerprint([(image, ["甲"])], None) != first

    def test_fingerprint_tracks_the_label_not_just_the_file(self, tmp_path: Path) -> None:
        image = self.make_image(tmp_path / "a.jpg", b"bytes")
        one = trainer.embedding_fingerprint([(image, ["甲"])], None)
        assert trainer.embedding_fingerprint([(image, ["乙"])], None) != one

    def test_a_missing_file_does_not_raise(self, tmp_path: Path) -> None:
        assert trainer.embedding_fingerprint([(tmp_path / "gone.jpg", ["甲"])], None)

    def test_a_fused_cache_is_not_the_wd14_cache(self, tmp_path: Path) -> None:
        # 1,280-d rows must never be served out of a 768-d cache, or the head is
        # trained on half its input and the truncation is silent.
        image = self.make_image(tmp_path / "a.jpg", b"bytes")
        plain = trainer.embedding_fingerprint([(image, ["甲"])], None)
        fused = trainer.embedding_fingerprint([(image, ["甲"])], None, semantic=True)
        assert fused != plain
        assert trainer.embedding_fingerprint([(image, ["甲"])], None, semantic=True) == fused


class TestReport:
    def test_binary_report_reads_the_softmax_winner(self, capsys) -> None:
        # Rows: illustration, illustration, meme. The head gets all three right,
        # so precision and recall must both be 1.0.
        labels = ["illustration", "meme"]
        Y = np.array([[1, 0], [1, 0], [0, 1]], dtype=np.float32)
        probs = np.array([[0.9, 0.1], [0.8, 0.2], [0.2, 0.8]], dtype=np.float32)
        trainer.report_binary(Y, probs, labels, "illustration", loss="ce")
        out = capsys.readouterr().out
        assert "precision 1.000" in out
        assert "recall 1.000" in out
        assert "tp 2  fp 0  fn 0  tn 1" in out

    def test_binary_report_counts_a_missed_illustration(self, capsys) -> None:
        labels = ["illustration", "meme"]
        Y = np.array([[1, 0], [0, 1]], dtype=np.float32)
        probs = np.array([[0.4, 0.6], [0.2, 0.8]], dtype=np.float32)
        trainer.report_binary(Y, probs, labels, "illustration", loss="ce")
        out = capsys.readouterr().out
        assert "tp 0  fp 0  fn 1  tn 1" in out

    def test_report_ignores_an_unknown_binary_label(self, capsys) -> None:
        trainer.report_binary(
            np.array([[1, 0]], dtype=np.float32),
            np.array([[0.9, 0.1]], dtype=np.float32),
            ["illustration", "meme"],
            "screenshot",
        )
        assert capsys.readouterr().out == ""

    def test_a_single_sigmoid_column_reports_against_half(self, capsys) -> None:
        # One label + background directories is how the meme head is trained;
        # there is no second column to out-score, so the rule must be >= 0.5.
        labels = ["梗图"]
        Y = np.array([[1], [1], [0], [0]], dtype=np.float32)
        probs = np.array([[0.9], [0.4], [0.6], [0.1]], dtype=np.float32)
        trainer.report_binary(Y, probs, labels, "梗图")
        out = capsys.readouterr().out
        assert ">= 0.5" in out
        assert "tp 1  fp 1  fn 1  tn 1" in out


class TestLabelsJson:
    def test_labels_are_sorted_so_the_head_columns_are_stable(self) -> None:
        # collect_from_dirs yields directory order; embed_all sorts. If that ever
        # changes, plugin labels and head columns silently swap.
        labels = sorted({"meme", "illustration", "photo"})
        assert labels == ["illustration", "meme", "photo"]
        assert json.loads(json.dumps(labels)) == labels


class TestManifestRows:
    def write(self, tmp_path: Path, rows: list[dict]) -> Path:
        path = tmp_path / "manifest.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        return path

    def test_split_and_source_survive_the_round_trip(self, tmp_path: Path) -> None:
        path = self.write(tmp_path, [
            {"path": "a.jpg", "label": "插画", "split": "train", "source": "coco"},
            {"path": "b.jpg", "labels": ["梗图"], "split": "val", "source": "met-meme"},
        ])
        rows = trainer.read_jsonl_rows(path)
        assert [row["labels"] for row in rows] == [["插画"], ["梗图"]]
        assert [row["split"] for row in rows] == ["train", "val"]
        assert [row["source"] for row in rows] == ["coco", "met-meme"]

    def test_a_plain_manifest_still_works(self, tmp_path: Path) -> None:
        # the old {path, labels} form has no split; main() falls back to a slice
        path = self.write(tmp_path, [{"path": "a.jpg", "labels": ["插画"]}])
        rows = trainer.read_jsonl_rows(path)
        assert rows[0]["split"] == ""
        assert rows[0]["source"] == ""


class TestConfusion:
    def test_the_column_that_ate_a_class_is_visible(self, capsys) -> None:
        labels = ["插画", "梗图"]
        # two illustrations, one called a meme: the 梗图 column must say 1
        Y = np.array([[1, 0], [1, 0], [0, 1]], dtype=np.float32)
        probs = np.array([[0.9, 0.1], [0.2, 0.8], [0.1, 0.9]], dtype=np.float32)
        trainer.confusion(Y, probs, labels, 0.5)
        out = capsys.readouterr().out
        assert "truth \\ guess" in out
        assert "插画" in out and "50.0% of 2" in out
        assert "100.0% of 1" in out

    def test_an_unsure_winner_lands_in_the_dash_column(self, capsys) -> None:
        labels = ["插画", "梗图"]
        Y = np.array([[1, 0]], dtype=np.float32)
        probs = np.array([[0.45, 0.55]], dtype=np.float32)
        trainer.confusion(Y, probs, labels, 0.6)
        out = capsys.readouterr().out
        assert "0.0% of 1" in out


class TestReportBySource:
    def test_one_bad_source_cannot_hide_in_the_average(self, capsys) -> None:
        labels = ["插画", "梗图"]
        Y = np.array([[1, 0], [1, 0], [0, 1], [0, 1]], dtype=np.float32)
        probs = np.array([[0.9, 0.1], [0.9, 0.1], [0.2, 0.8], [0.7, 0.3]],
                         dtype=np.float32)
        trainer.report_by_source(Y, probs, labels, ["coco", "coco", "met-meme", "met-meme"])
        out = capsys.readouterr().out
        assert "coco" in out and "100.0%" in out
        assert "met-meme" in out and "50.0%" in out

    def test_no_source_column_prints_nothing(self, capsys) -> None:
        labels = ["插画", "梗图"]
        Y = np.array([[1, 0]], dtype=np.float32)
        probs = np.array([[0.9, 0.1]], dtype=np.float32)
        trainer.report_by_source(Y, probs, labels, ["", ""])
        assert capsys.readouterr().out == ""


class TestManifestMerge:
    """A retrain replaces the head, not the hand-tuned post-processing."""

    def test_the_size_prior_survives_a_retrain(self) -> None:
        fresh = {"id": "kind", "input_dim": 1280}
        previous = {
            "id": "kind",
            "input_dim": 768,
            "size_prior": {"label": "梗图", "small_edge": 640},
        }
        merged = trainer.merge_manifest(fresh, previous)
        assert merged["input_dim"] == 1280, "the fresh export must win on shape"
        assert merged["size_prior"]["small_edge"] == 640

    def test_the_version_bumps_instead_of_resetting(self) -> None:
        merged = trainer.merge_manifest({"id": "kind"}, {"version": "4"})
        assert merged["version"] == "5"

    def test_a_missing_or_broken_manifest_is_not_fatal(self, tmp_path: Path) -> None:
        assert trainer.read_manifest(tmp_path / "nope.json") == {}
        broken = tmp_path / "plugin.json"
        broken.write_text("{not json", encoding="utf-8")
        assert trainer.read_manifest(broken) == {}
        merged = trainer.merge_manifest({"id": "kind"}, {})
        assert merged["version"] == "1"

    def test_a_folder_without_a_manifest_keeps_the_fresh_block(self) -> None:
        assert trainer.merge_manifest({"id": "kind"}, {})["id"] == "kind"
