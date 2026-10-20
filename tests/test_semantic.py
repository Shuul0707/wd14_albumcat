"""Semantic-search unit tests.

These deliberately do **not** need the real Chinese-CLIP weights: everything
that could go wrong in production (tensor packing, ranking order, tokenizer
edge cases, graceful degradation, cache safety) is testable without a 300 MB
download. The ONNX path itself is covered by `tools/export_chinese_clip_onnx.py
--self-test`, which runs a real forward pass when the models are present.
"""

from __future__ import annotations

import numpy as np
import pytest

from albumcat.config import SemanticConfig
from albumcat.semantic import (
    BertTokenizer,
    SemanticEncoder,
    SemanticUnavailable,
    apply_vector_policy,
    cosine_scores,
    pack_vector,
    unpack_vector,
)


# --------------------------------------------------------------------------- #
# wire format


def test_vector_pack_roundtrip_float16():
    vector = np.array([0.1, -0.2, 0.3, 0.0], dtype=np.float32)
    encoded = pack_vector(vector, "float16")
    decoded = unpack_vector(encoded, "float16")

    assert isinstance(encoded, str) and encoded
    assert decoded.shape == (4,)
    assert np.allclose(decoded, vector, atol=1e-3)


def test_vector_pack_roundtrip_float32():
    vector = np.arange(8, dtype=np.float32) / 10
    decoded = unpack_vector(pack_vector(vector, "float32"), "float32")
    assert np.allclose(decoded, vector, atol=1e-6)


def test_float16_is_half_the_payload():
    vector = np.ones(512, dtype=np.float32)
    assert len(pack_vector(vector, "float16")) < len(pack_vector(vector, "float32"))


# --------------------------------------------------------------------------- #
# ranking


def test_cosine_puts_the_closest_first():
    query = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    near = np.array([0.95, 0.05, 0.0], dtype=np.float32)
    far = np.array([0.0, 1.0, 0.0], dtype=np.float32)

    scores = cosine_scores(query, np.stack([far, near]))

    assert scores.shape == (2,)
    assert scores[1] > scores[0]
    assert scores[1] == pytest.approx(0.9986, abs=1e-3)


def test_cosine_survives_zero_vectors():
    query = np.array([0.0, 0.0], dtype=np.float32)
    scores = cosine_scores(query, np.array([[0.0, 0.0], [1.0, 0.0]], dtype=np.float32))
    assert np.all(np.isfinite(scores))


def test_cosine_accepts_a_single_row():
    scores = cosine_scores(np.array([1.0, 0.0]), np.array([1.0, 0.0]))
    assert scores.shape == (1,)
    assert scores[0] == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# tokenizer


def _write_vocab(tmp_path, words):
    path = tmp_path / "vocab.txt"
    path.write_text(
        "\n".join(["[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]", *words]) + "\n",
        encoding="utf-8",
    )
    return path


def test_tokenizer_splits_cjk_per_character(tmp_path):
    tokenizer = BertTokenizer(_write_vocab(tmp_path, ["沙", "滩"]))
    encoded = tokenizer.encode("沙滩", max_length=8)

    inverse = {index: token for token, index in tokenizer.vocab.items()}
    tokens = [inverse[i] for i in encoded["input_ids"][0][:4]]

    assert tokens == ["[CLS]", "沙", "滩", "[SEP]"]
    # padded tail is masked out
    assert encoded["attention_mask"][0][:4].tolist() == [1, 1, 1, 1]
    assert encoded["attention_mask"][0][4:].tolist() == [0, 0, 0, 0]
    assert encoded["input_ids"].shape == (1, 8)


def test_tokenizer_applies_wordpiece(tmp_path):
    tokenizer = BertTokenizer(_write_vocab(tmp_path, ["play", "##ing"]))
    encoded = tokenizer.encode("playing", max_length=8)

    inverse = {index: token for token, index in tokenizer.vocab.items()}
    tokens = [inverse[i] for i in encoded["input_ids"][0][:4]]

    assert tokens == ["[CLS]", "play", "##ing", "[SEP]"]


def test_tokenizer_folds_fullwidth_and_lowercases(tmp_path):
    tokenizer = BertTokenizer(_write_vocab(tmp_path, ["abc"]))
    encoded = tokenizer.encode("ＡＢＣ", max_length=6)

    inverse = {index: token for token, index in tokenizer.vocab.items()}
    tokens = [inverse[i] for i in encoded["input_ids"][0][:3]]

    assert tokens == ["[CLS]", "abc", "[SEP]"]


def test_tokenizer_marks_unknown_words(tmp_path):
    tokenizer = BertTokenizer(_write_vocab(tmp_path, ["沙"]))
    encoded = tokenizer.encode("沙zzz", max_length=8)

    inverse = {index: token for token, index in tokenizer.vocab.items()}
    tokens = [inverse[i] for i in encoded["input_ids"][0][:4]]

    assert tokens == ["[CLS]", "沙", "[UNK]", "[SEP]"]


def test_tokenizer_truncates_to_max_length(tmp_path):
    tokenizer = BertTokenizer(_write_vocab(tmp_path, list("沙滩海边日落")))
    encoded = tokenizer.encode("沙滩海边日落" * 20, max_length=8)
    assert encoded["input_ids"].shape == (1, 8)
    # the closing [SEP] survives truncation
    inverse = {index: token for token, index in tokenizer.vocab.items()}
    assert inverse[int(encoded["input_ids"][0][-1])] == "[SEP]"


# --------------------------------------------------------------------------- #
# graceful degradation


def test_encoder_reports_unavailable_without_models(tmp_path):
    encoder = SemanticEncoder(SemanticConfig(enabled=True, dir=str(tmp_path)))

    assert encoder.available is False
    assert encoder.text_available is False
    info = encoder.info()
    assert info.available is False
    assert info.dim == 512
    assert info.dtype == "float16"
    assert info.reason  # says *why*, rather than failing silently


def test_encoder_raises_a_typed_error_when_unavailable(tmp_path):
    encoder = SemanticEncoder(SemanticConfig(enabled=True, dir=str(tmp_path)))

    with pytest.raises(SemanticUnavailable):
        encoder.encode_image(object())
    with pytest.raises(SemanticUnavailable):
        encoder.encode_text("沙滩")


def test_encoder_disabled_by_config():
    encoder = SemanticEncoder(SemanticConfig(enabled=False))
    assert encoder.available is False
    assert "disabled" in encoder.info().reason


# --------------------------------------------------------------------------- #
# response shape / cache safety


def test_semantic_info_shape_without_models():
    info = SemanticEncoder(SemanticConfig(enabled=False)).info().to_dict()

    assert info["available"] is False
    assert info["dim"] == 512
    assert info["model"] == "chinese-clip-vit-base-patch16"
    assert info["reason"]


def test_vector_policy_strips_by_default_without_mutating_the_cache():
    cached = {"id": "a", "semantic": {"vector": "AAAA"}}

    stripped = apply_vector_policy(cached, include_vector=False)

    assert "semantic" not in stripped
    # the cached object must survive untouched, or the next caller loses the vector
    assert "semantic" in cached


def test_vector_policy_keeps_the_vector_when_asked():
    cached = {"id": "a", "semantic": {"vector": "AAAA"}}
    kept = apply_vector_policy(cached, include_vector=True)

    assert kept is cached
    assert kept["semantic"]["vector"] == "AAAA"


def test_vector_policy_repacks_when_the_caller_wants_float32():
    vector = np.linspace(-1, 1, 8, dtype=np.float32)
    payload = {
        "semantic": {
            "dtype": "float16",
            "dim": 8,
            "vector": pack_vector(vector, "float16"),
        }
    }

    out = apply_vector_policy(payload, include_vector=True, dtype="float32")

    block = out["semantic"]
    assert block["dtype"] == "float32"
    # twice the bytes: 8 dims × 4 B = 32 B -> base64
    assert len(block["vector"]) > len(payload["semantic"]["vector"])
    assert np.allclose(unpack_vector(block["vector"], "float32"), vector, atol=1e-2)
    # and the original is untouched
    assert payload["semantic"]["dtype"] == "float16"
