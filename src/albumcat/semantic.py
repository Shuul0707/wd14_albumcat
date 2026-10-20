"""Chinese-CLIP semantic vectors: search the gallery by meaning, not by tag.

Why a second model when WD14 is already loaded? Because WD14 is a *closed*
vocabulary: it can only answer "is one of my 9083 labels in this picture?".
Ask it for "沙滩" and the best you can do is a tag that happens to contain
those pixels. Chinese-CLIP maps a photo and a free-form Chinese phrase into
the *same* 512-d space, so "沙滩" can match a photo that WD14 never described
that way -- and the ranking is a plain cosine.

The division of labour mirrors the rest of the project:

    PC   : run the *image* tower once per photo (ViT-B/16, heavy)
    phone: keep the vector next to `appTags`, run the *text* tower per query
           (or call `POST /encode_text` when it has no model of its own)

Only the image tower needs a GPU; the text tower is a single short forward
pass, which is what makes on-device search realistic.

Everything here degrades gracefully: with no model on disk the service still
answers tag requests, `/semantic` reports ``available: false``, and callers
get :class:`SemanticUnavailable` instead of a stack trace.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .config import SemanticConfig

__all__ = [
    "SemanticEncoder",
    "SemanticUnavailable",
    "BertTokenizer",
    "pack_vector",
    "unpack_vector",
    "cosine_scores",
    "apply_vector_policy",
]

log = logging.getLogger("albumcat.semantic")

# CLIP's fixed preprocessing constants (identical in the Chinese-CLIP repo).
CLIP_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
CLIP_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)


class SemanticUnavailable(RuntimeError):
    """Raised when semantic search is off, or the models are missing/broken."""


# --------------------------------------------------------------------------- #
# vector <-> text


def pack_vector(vector: np.ndarray, dtype: str = "float16") -> str:
    """Base64 a 1-D vector. float16 halves the payload (512-d -> 1 KB)."""
    arr = np.asarray(vector, dtype=np.float16 if dtype == "float16" else np.float32).reshape(-1)
    arr = np.ascontiguousarray(arr)
    return base64.b64encode(arr.tobytes()).decode("ascii")


def unpack_vector(encoded: str, dtype: str = "float16") -> np.ndarray:
    raw = base64.b64decode(encoded.encode("ascii"))
    arr = np.frombuffer(raw, dtype=np.float16 if dtype == "float16" else np.float32)
    return np.array(arr, dtype=np.float32)


def cosine_scores(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Cosine of ``query`` against every row of ``matrix``. Vectors are unit-norm."""
    q = np.asarray(query, dtype=np.float32).reshape(-1)
    m = np.asarray(matrix, dtype=np.float32)
    if m.ndim == 1:
        m = m.reshape(1, -1)
    qn = np.linalg.norm(q) or 1.0
    mn = np.linalg.norm(m, axis=1)
    mn[mn == 0] = 1.0
    return (m @ q) / (mn * qn)


def _l2_normalize(vector: np.ndarray) -> np.ndarray:
    arr = np.asarray(vector, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm else arr


def apply_vector_policy(payload: dict, include_vector: bool, dtype: str | None = None) -> dict:
    """Strip the vector unless asked, and honour the caller's wire format.

    Lives here rather than on the HTTP service so it can be tested without
    pulling in onnxruntime / OpenCV / the model files.

    Existing phones never send ``include_vector``; handing them a 1 KB base64
    blob per photo would bloat every batch for nothing.

    ``dtype`` matters because the plan cache is shared: an entry encoded as
    float16 must be re-packed when the caller asked for float32, or the phone
    reads 2-byte halves as 4-byte floats and ranks noise.
    """
    if not include_vector:
        if "semantic" not in payload:
            return payload
        out = dict(payload)
        out.pop("semantic", None)
        return out

    block = payload.get("semantic")
    if dtype and block and block.get("dtype") != dtype:
        vector = unpack_vector(block["vector"], block.get("dtype", "float16"))
        out = dict(payload)
        out["semantic"] = {**block, "dtype": dtype, "vector": pack_vector(vector, dtype)}
        return out
    return payload


# --------------------------------------------------------------------------- #
# minimal BERT tokenizer
#
# Chinese-CLIP ships a `BertTokenizer` (wordpiece, do_lower_case=True). Using
# `transformers` at runtime would drag in ~500 MB of dependencies for what is a
# 200-line job, and this project deliberately keeps the PC side on
# onnxruntime+numpy+Pillow. The rules below follow the reference implementation:
# clean -> CJK split -> punctuation split -> wordpiece, plus full-width folding.

_PUNCT = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_SPECIALS = ("[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]")


class BertTokenizer:
    """WordPiece tokenizer reading a plain ``vocab.txt``."""

    def __init__(self, vocab_file: Path, *, do_lower_case: bool = True) -> None:
        self.do_lower_case = do_lower_case
        self.vocab: dict[str, int] = {}
        with open(vocab_file, "r", encoding="utf-8") as fh:
            for index, line in enumerate(fh):
                self.vocab[line.rstrip("\n")] = index
        missing = [t for t in _SPECIALS if t not in self.vocab]
        if missing:
            raise ValueError(f"vocab is missing special tokens: {missing}")
        self.cls_id = self.vocab["[CLS]"]
        self.sep_id = self.vocab["[SEP]"]
        self.pad_id = self.vocab["[PAD]"]
        self.unk_id = self.vocab["[UNK]"]
        self.max_input_chars_per_word = 100

    # -- public --------------------------------------------------------- #

    def encode(self, text: str, max_length: int = 52) -> dict[str, np.ndarray]:
        # Reserve one slot for [CLS] and one for [SEP], the way BERT does.
        # Truncating the tail instead would drop [SEP], and the pooled output
        # (what CLIP actually uses) changes when the closing token is missing.
        body = self._tokenize(text)[: max_length - 2]
        tokens = ["[CLS]", *body, "[SEP]"]
        ids = [self.vocab.get(t, self.unk_id) for t in tokens]
        # pad to a fixed length: keeps the ONNX text tower's shape static
        pad = max_length - len(ids)
        mask = [1] * len(ids) + [0] * pad
        ids = ids + [self.pad_id] * pad
        return {
            "input_ids": np.array([ids], dtype=np.int64),
            "attention_mask": np.array([mask], dtype=np.int64),
            "token_type_ids": np.zeros((1, max_length), dtype=np.int64),
        }

    # -- internals ------------------------------------------------------ #

    def _tokenize(self, text: str) -> list[str]:
        out: list[str] = []
        for word in self._basic_tokenize(text):
            out.extend(self._wordpiece(word))
        return out

    def _basic_tokenize(self, text: str) -> list[str]:
        cleaned = []
        for ch in text:
            code = ord(ch)
            if code == 0 or code == 0xFFFD or _is_control(ch):
                continue
            if _is_whitespace(ch):
                cleaned.append(" ")
            else:
                cleaned.append(_fold_fullwidth(ch))
        text = "".join(cleaned)

        words: list[str] = []
        buffer: list[str] = []
        for ch in text:
            if ch == " ":
                if buffer:
                    words.append("".join(buffer))
                    buffer = []
            elif _is_cjk(ch):
                if buffer:
                    words.append("".join(buffer))
                    buffer = []
                words.append(ch)
            elif _is_punct(ch):
                if buffer:
                    words.append("".join(buffer))
                    buffer = []
                words.append(ch)
            else:
                buffer.append(ch)
        if buffer:
            words.append("".join(buffer))

        if self.do_lower_case:
            words = [w.lower() for w in words]
        return words

    def _wordpiece(self, word: str) -> list[str]:
        if len(word) > self.max_input_chars_per_word:
            return ["[UNK]"]
        pieces: list[str] = []
        start = 0
        while start < len(word):
            end = len(word)
            found: str | None = None
            while start < end:
                chunk = word[start:end]
                if start > 0:
                    chunk = "##" + chunk
                if chunk in self.vocab:
                    found = chunk
                    break
                end -= 1
            if found is None:
                return ["[UNK]"]
            pieces.append(found)
            start = end
        return pieces


def _is_whitespace(ch: str) -> bool:
    if ch in (" ", "\t", "\n", "\r"):
        return True
    return unicodedata.category(ch) == "Zs"


def _is_control(ch: str) -> bool:
    if ch in ("\t", "\n", "\r"):
        return False
    return unicodedata.category(ch) in ("Cc", "Cf")


def _is_punct(ch: str) -> bool:
    if ch in _PUNCT:
        return True
    return unicodedata.category(ch).startswith("P")


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x20000 <= code <= 0x2A6DF
        or 0xF900 <= code <= 0xFAFF
        or 0x3040 <= code <= 0x30FF  # kana, for mixed-script queries
    )


def _fold_fullwidth(ch: str) -> str:
    """Full-width ASCII -> half-width (BERT's _is_whitespace/_clean_text step)."""
    code = ord(ch)
    if code == 0x3000:
        return " "
    if 0xFF01 <= code <= 0xFF5E:
        return chr(code - 0xFEE0)
    return ch


# --------------------------------------------------------------------------- #
# encoder


@dataclass
class SemanticInfo:
    enabled: bool
    available: bool
    model: str
    dim: int
    dtype: str
    image_model: str
    text_model: str
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "available": self.available,
            "model": self.model,
            "dim": self.dim,
            "dtype": self.dtype,
            "image_model": self.image_model,
            "text_model": self.text_model,
            "reason": self.reason,
        }


class SemanticEncoder:
    """Loads the two ONNX towers lazily and encodes images/text to unit vectors."""

    def __init__(self, config: SemanticConfig, *, warmup: bool = False) -> None:
        self.config = config
        self._image_session = None
        self._text_session = None
        self._tokenizer: BertTokenizer | None = None
        self._lock = threading.Lock()
        self._reason = ""

        if not config.enabled:
            self._reason = "disabled in config ([semantic] enabled = false)"
            return
        try:
            self._load()
        except Exception as exc:  # noqa: BLE001 - a missing model must not kill the server
            self._reason = f"{type(exc).__name__}: {exc}"
            log.warning("semantic search unavailable: %s", self._reason)
            return

        if warmup:
            try:
                self.encode_text("测试")
            except Exception as exc:  # noqa: BLE001
                log.debug("text warmup failed: %s", exc)

    # -- loading -------------------------------------------------------- #

    def _load(self) -> None:
        import onnxruntime as ort

        directory = self.config.path
        image_path = directory / self.config.image_model
        text_path = directory / self.config.text_model

        if not image_path.exists():
            raise FileNotFoundError(f"image tower not found: {image_path}")
        self._image_session = ort.InferenceSession(
            str(image_path), providers=self._providers()
        )

        # The text tower is optional: with only the image tower the phone can
        # still call POST /encode_text elsewhere, or use its own on-device model.
        if text_path.exists():
            self._text_session = ort.InferenceSession(
                str(text_path), providers=self._providers()
            )
            vocab = directory / self.config.vocab_file
            if not vocab.exists():
                raise FileNotFoundError(f"vocab not found: {vocab}")
            self._tokenizer = BertTokenizer(vocab)
        else:
            log.info("text tower missing (%s) - /encode_text will be unavailable", text_path)

    def _providers(self) -> list[str]:
        import onnxruntime as ort

        wanted = self.config.device
        available = set(ort.get_available_providers())
        order = {
            "auto": ["DmlExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"],
            "dml": ["DmlExecutionProvider", "CPUExecutionProvider"],
            "cuda": ["CUDAExecutionProvider", "CPUExecutionProvider"],
            "cpu": ["CPUExecutionProvider"],
        }.get(wanted, ["CPUExecutionProvider"])
        picked = [p for p in order if p in available] or ["CPUExecutionProvider"]
        return picked

    # -- introspection --------------------------------------------------- #

    @property
    def available(self) -> bool:
        return self._image_session is not None

    @property
    def text_available(self) -> bool:
        return self._text_session is not None

    def info(self) -> SemanticInfo:
        return SemanticInfo(
            enabled=self.config.enabled,
            available=self.available,
            model=self.config.model_id,
            dim=self.config.dim,
            dtype=self.config.dtype,
            image_model=self.config.image_model,
            text_model=self.config.text_model if self.text_available else "",
            reason=self._reason,
        )

    # -- encoding -------------------------------------------------------- #

    def encode_image(self, image) -> np.ndarray:
        if self._image_session is None:
            raise SemanticUnavailable(self._reason or "image tower not loaded")

        tensor = self._preprocess(image)
        name = _find_input(self._image_session, ("pixel_values", "image", "input"))
        with self._lock:
            outputs = self._image_session.run(None, {name: tensor})
        return _l2_normalize(_flatten(outputs))

    def encode_text(self, text: str) -> np.ndarray:
        if self._text_session is None or self._tokenizer is None:
            raise SemanticUnavailable(
                "text tower not loaded (put text_encoder.onnx + vocab.txt under the semantic dir, "
                "or encode the query on the phone)"
            )
        text = (text or "").strip()
        if not text:
            raise ValueError("empty query")

        encoded = self._tokenizer.encode(text, max_length=self.config.max_text_length)
        feeds: dict[str, np.ndarray] = {}
        for item in self._text_session.get_inputs():
            if item.name in encoded:
                feeds[item.name] = encoded[item.name]
        with self._lock:
            outputs = self._text_session.run(None, feeds)
        return _l2_normalize(_flatten(outputs))

    # -- preprocessing ---------------------------------------------------- #

    def _preprocess(self, image) -> np.ndarray:
        """Shortest side -> 224 (bicubic), centre crop 224, CLIP normalisation.

        Note the difference from the WD14 path: that model wants 0-255 and
        NHWC, CLIP wants 0-1 and CHW. Mixing the two up produces vectors that
        look plausible and rank garbage, so this lives in one place only.
        """
        from PIL import Image

        size = self.config.image_size
        image = image.convert("RGB")
        width, height = image.size
        if width <= 0 or height <= 0:
            raise ValueError("empty image")

        scale = size / min(width, height)
        new_w, new_h = max(size, round(width * scale)), max(size, round(height * scale))
        if (new_w, new_h) != (width, height):
            image = image.resize((new_w, new_h), Image.BICUBIC)
        left = (new_w - size) // 2
        top = (new_h - size) // 2
        image = image.crop((left, top, left + size, top + size))

        arr = np.asarray(image, dtype=np.float32) / 255.0
        arr = (arr - CLIP_MEAN) / CLIP_STD
        arr = np.transpose(arr, (2, 0, 1))
        return np.ascontiguousarray(arr[None, ...], dtype=np.float32)


def _find_input(session, candidates: tuple[str, ...]) -> str:
    names = [i.name for i in session.get_inputs()]
    for candidate in candidates:
        if candidate in names:
            return candidate
    # fall back to the first non-mask input; ONNX exports vary in naming
    for name in names:
        if "mask" not in name and "token" not in name:
            return name
    return names[0]


def _flatten(outputs: list[Any]) -> np.ndarray:
    """Pick the pooled embedding out of whatever the export produced."""
    for output in outputs:
        arr = np.asarray(output, dtype=np.float32)
        if arr.ndim == 2 and arr.shape[0] == 1:
            return arr[0]
        if arr.ndim == 1:
            return arr
    # last resort: flatten whatever came back
    return np.asarray(outputs[0], dtype=np.float32).reshape(-1)
