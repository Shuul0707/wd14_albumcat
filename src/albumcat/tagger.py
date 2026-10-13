"""Standalone WD14 ONNX tagger.

This deliberately *reimplements* -- rather than imports -- the preprocessing in
``third_party/wd14-API-onnx/wd14_onnx_simple.py`` so the project can tag images
without running the FastAPI service. The numbers must stay identical to that
file: if preprocessing drifts, results drift with it.

Preprocessing contract (ported from ``preprocess_image`` at
``wd14_onnx_simple.py:252-287``), in order:

1. flatten alpha onto a **white** background, then convert to RGB
   (``fill_transparent``, ``:201-210``)
2. ``RGB -> BGR`` channel swap (WD14 v1.4 models are trained on BGR)
3. pad to a **square** with white, at least 448 on a side
   (``make_square``, ``:213-235``)
4. resize that square to 448x448 (``smart_resize``, ``:238-249``)
5. cast to ``float32`` -- **no** ``/255``, **no** mean/std normalisation
6. add a batch axis: the model's input is **NHWC** ``(1, 448, 448, 3)``

The monolithic ``model.onnx`` output already has sigmoid applied, so the raw
values are confidences (``wd14_onnx_simple.py:522`` feeds them straight into
``_parse_output``). The *split* classifier is different -- see
``wd14_onnx_split.py:962``, which applies ``1/(1+exp(-x))`` itself.

Image embeddings
----------------
``kind: feature`` plugins need the penultimate feature vector, and ONNX Runtime
refuses to hand back an intermediate tensor by name (the graph is fused during
optimisation). ``tools/add_feature_output.py`` produces a sibling model that
declares it as a real graph output, which this class picks up automatically --
see ``_resolve_features``. One session then yields both the tag confidences and
the embedding, so embeddings are effectively free.
"""

from __future__ import annotations

import csv
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import cv2
import numpy as np
import onnxruntime as ort
from PIL import Image

log = logging.getLogger(__name__)

__all__ = [
    "CATEGORY_GENERAL",
    "CATEGORY_CHARACTER",
    "CATEGORY_RATING",
    "DEFAULT_THRESHOLD",
    "DEFAULT_CHARACTER_THRESHOLD",
    "TagTable",
    "TagResult",
    "Wd14Tagger",
    "preprocess_image",
]

# `category` column values in selected_tags.csv
CATEGORY_GENERAL = 0
CATEGORY_CHARACTER = 4
CATEGORY_RATING = 9

MODEL_INPUT_SIZE = 448

#: Output name added by ``tools/add_feature_output.py``.
FEATURE_OUTPUT_NAME = "features"

#: Confidence floor for general tags.
DEFAULT_THRESHOLD = 0.35
#: The upstream README recommends callers apply *at least* 0.5 to character
#: tags to cut false positives, because WD14 fires character labels readily.
DEFAULT_CHARACTER_THRESHOLD = 0.5


# --------------------------------------------------------------------------- #
# preprocessing
# --------------------------------------------------------------------------- #
def _fill_transparent(image: Image.Image) -> Image.Image:
    """Composite any alpha onto a white background, returning RGB."""
    image = image.convert("RGBA")
    background = Image.new("RGBA", image.size, "WHITE")
    background.paste(image, mask=image)
    return background.convert("RGB")


def _make_square(img: np.ndarray, desired_size: int) -> np.ndarray:
    """White-pad ``img`` to a square of at least ``desired_size`` on each side."""
    desired_size = max(desired_size, int(max(img.shape[:2])))
    height, width = img.shape[:2]
    top = (desired_size - height) // 2
    bottom = desired_size - height - top
    left = (desired_size - width) // 2
    right = desired_size - width - left
    return cv2.copyMakeBorder(
        img, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(255, 255, 255)
    )


def _smart_resize(img: np.ndarray, size: int) -> np.ndarray:
    """Shrink with INTER_AREA, grow with INTER_CUBIC -- as upstream does."""
    interpolation = cv2.INTER_AREA if img.shape[0] > size else cv2.INTER_CUBIC
    return cv2.resize(img, (size, size), interpolation=interpolation)


def preprocess_image(image: Image.Image, size: int = MODEL_INPUT_SIZE) -> np.ndarray:
    """Return the ``(1, size, size, 3)`` float32 NHWC tensor the model expects."""
    image = _fill_transparent(image)
    array = np.asarray(image)
    array = array[:, :, ::-1]  # RGB -> BGR
    array = np.ascontiguousarray(array)  # cv2 dislikes the negative stride
    array = _make_square(array, size)
    array = _smart_resize(array, size)
    array = array.astype(np.float32)  # values stay in 0..255 on purpose
    return np.expand_dims(array, axis=0)


# --------------------------------------------------------------------------- #
# tag table
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TagTable:
    """The ``selected_tags.csv`` sidecar: one row per output neuron."""

    names: tuple[str, ...]
    categories: np.ndarray  # int32, same length as names

    @classmethod
    def from_csv(cls, path: Path | str) -> "TagTable":
        path = Path(path)
        names: list[str] = []
        categories: list[int] = []
        # utf-8-sig: the upstream CSV ships with a BOM.
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                names.append(row["name"])
                categories.append(int(row["category"]))
        if not names:
            raise ValueError(f"{path} has no rows")
        return cls(names=tuple(names), categories=np.asarray(categories, dtype=np.int32))

    def __len__(self) -> int:
        return len(self.names)

    def indices_for(self, category: int) -> np.ndarray:
        return np.flatnonzero(self.categories == category)

    def counts_by_category(self) -> dict[int, int]:
        return {
            int(cat): int(np.count_nonzero(self.categories == cat))
            for cat in np.unique(self.categories)
        }


# --------------------------------------------------------------------------- #
# results
# --------------------------------------------------------------------------- #
@dataclass
class TagResult:
    """One image's tagged output, mirroring the API's response shape."""

    ratings: dict[str, float]
    characters: dict[str, float]
    tags: dict[str, float]
    #: Penultimate feature vector for this image, when the model exposes one.
    #: ``None`` with a stock ``model.onnx``; a ``float32`` array of length
    #: ``Wd14Tagger.embedding_dim`` when a ``*_features.onnx`` sibling is in use.
    embedding: np.ndarray | None = None

    def sorted_characters(self, limit: int | None = None) -> list[tuple[str, float]]:
        return _top(self.characters, limit)

    def sorted_tags(self, limit: int | None = None) -> list[tuple[str, float]]:
        return _top(self.tags, limit)

    def top_rating(self) -> tuple[str, float] | None:
        pairs = _top(self.ratings, 1)
        return pairs[0] if pairs else None

    def as_dict(self) -> dict[str, dict[str, float]]:
        return {"ratings": self.ratings, "characters": self.characters, "tags": self.tags}


def _top(mapping: dict[str, float], limit: int | None) -> list[tuple[str, float]]:
    pairs = sorted(mapping.items(), key=lambda kv: kv[1], reverse=True)
    return pairs if limit is None else pairs[:limit]


# --------------------------------------------------------------------------- #
# session
# --------------------------------------------------------------------------- #
_PROVIDER_ORDER = (
    "CUDAExecutionProvider",
    "DmlExecutionProvider",
    "CPUExecutionProvider",
)


def build_providers(
    requested: Sequence[str] | None = None,
    gpu_mem_limit: int = 2 * 1024**3,
    device_id: int = 0,
) -> list:
    """Pick execution providers, mirroring ``Config.detect_hardware`` upstream."""
    available = ort.get_available_providers()

    if requested:
        unknown = [name for name in requested if name not in available]
        if unknown:
            raise ValueError(
                f"provider(s) not available: {unknown}; available: {available}"
            )
        order: Iterable[str] = requested
    else:
        order = _PROVIDER_ORDER

    providers: list = []
    for name in order:
        if name not in available:
            continue
        if name == "DmlExecutionProvider":
            options: dict = {"gpu_mem_limit": gpu_mem_limit}
            # Upstream only passes device_id when the DML device mask is unset;
            # passing both makes ORT-DML complain.
            if "DML_VISIBLE_DEVICES" not in os.environ:
                options["device_id"] = device_id
            providers.append((name, options))
        elif name == "CUDAExecutionProvider":
            providers.append((name, {"device_id": device_id}))
        else:
            providers.append(name)
    return providers


def _session_options(use_dml: bool) -> ort.SessionOptions:
    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    if use_dml:
        # Upstream sets both of these for DirectML compatibility; on CPU they
        # only cost throughput, so keep them scoped to DML.
        options.enable_mem_pattern = False
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    return options


class Wd14Tagger:
    """Loads ``model.onnx`` + ``selected_tags.csv`` and tags images."""

    def __init__(
        self,
        model_path: Path | str,
        tags_path: Path | str | None = None,
        providers: Sequence[str] | None = None,
        gpu_mem_limit: int = 2 * 1024**3,
        device_id: int = 0,
        warmup: bool = True,
        features_path: Path | str | None = None,
        use_features: bool = True,
    ) -> None:
        self.model_path = Path(model_path)
        if not self.model_path.exists():
            raise FileNotFoundError(f"model not found: {self.model_path}")

        if tags_path is None:
            tags_path = self.model_path.with_name("selected_tags.csv")
        self.tags_path = Path(tags_path)
        if not self.tags_path.exists():
            raise FileNotFoundError(f"tag table not found: {self.tags_path}")

        self.tag_table = TagTable.from_csv(self.tags_path)

        self.features_path = (
            self._resolve_features(features_path) if use_features else None
        )
        session_model = self.features_path or self.model_path

        chosen = build_providers(providers, gpu_mem_limit, device_id)
        use_dml = any(
            (p[0] if isinstance(p, tuple) else p) == "DmlExecutionProvider"
            for p in chosen
        )
        self.session = ort.InferenceSession(
            str(session_model),
            sess_options=_session_options(use_dml),
            providers=chosen,
        )
        self.active_providers = self.session.get_providers()
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        self.input_shape = self.session.get_inputs()[0].shape

        # Locate the embedding output, if the model exposes one.
        self.features_name: str | None = None
        self.embedding_dim: int | None = None
        outputs = {out.name: out for out in self.session.get_outputs()}
        if FEATURE_OUTPUT_NAME in outputs:
            self.features_name = FEATURE_OUTPUT_NAME
            shape = outputs[FEATURE_OUTPUT_NAME].shape
            if len(shape) == 2 and isinstance(shape[1], int):
                self.embedding_dim = shape[1]

        # Most WD14 exports pin the batch axis to a literal 1, so feeding two
        # images at once fails with "Got invalid dimensions ... Got: 2
        # Expected: 1". Record what the graph actually accepts -- None means
        # the axis is dynamic and we are free to batch.
        first = self.input_shape[0] if self.input_shape else None
        self.fixed_batch: int | None = (
            first if isinstance(first, int) and first > 0 else None
        )

        # Pre-split the table into the three category index sets once.
        self._idx_general = self.tag_table.indices_for(CATEGORY_GENERAL)
        self._idx_character = self.tag_table.indices_for(CATEGORY_CHARACTER)
        self._idx_rating = self.tag_table.indices_for(CATEGORY_RATING)

        if warmup:
            self._warmup()

    def _resolve_features(self, explicit: Path | str | None) -> Path | None:
        """Find the embedding-capable model, if any.

        ``tools/add_feature_output.py`` writes ``<model stem>_features.onnx``
        beside the model, so the common case needs no configuration. An
        explicit path wins; a missing explicit path is an error, because
        silently falling back to a model without embeddings would leave
        ``feature`` plugins mysteriously starved.
        """
        if explicit is not None:
            path = Path(explicit)
            if not path.is_absolute():
                path = self.model_path.parent / path
            if not path.exists():
                raise FileNotFoundError(f"features model not found: {path}")
            return path

        candidate = self.model_path.with_name(f"{self.model_path.stem}_features.onnx")
        return candidate if candidate.exists() else None

    # -- introspection ----------------------------------------------------- #
    def describe(self) -> dict:
        return {
            "model": str(self.model_path),
            "session_model": str(self.features_path or self.model_path),
            "tags_csv": str(self.tags_path),
            "input": {"name": self.input_name, "shape": list(self.input_shape)},
            "output": {"name": self.output_name},
            "providers": list(self.active_providers),
            "tag_counts": self.tag_table.counts_by_category(),
            "fixed_batch": self.fixed_batch,
            "features": self.features_name,
            "embedding_dim": self.embedding_dim,
        }

    def _warmup(self) -> None:
        _, height, width, _ = self.input_shape
        dummy = np.zeros((1, height, width, 3), dtype=np.float32)
        for _ in range(3):
            self._run(dummy)

    # -- inference --------------------------------------------------------- #
    def _run(self, batch: np.ndarray) -> tuple[np.ndarray, np.ndarray | None]:
        """Run one batch, returning ``(confidences, embeddings)``.

        When the model exposes an embedding output we request both in a single
        ``run`` call: the tower is computed either way, so the embedding costs
        no extra inference time.
        """
        if self.features_name is not None:
            embeddings, confidences = self.session.run(
                [self.features_name, self.output_name], {self.input_name: batch}
            )
            return confidences, embeddings
        confidences = self.session.run([self.output_name], {self.input_name: batch})[0]
        return confidences, None

    def _parse(
        self,
        confidences: np.ndarray,
        threshold: float,
        character_threshold: float | None,
        embedding: np.ndarray | None = None,
    ) -> TagResult:
        row = np.asarray(confidences).reshape(-1)
        if row.shape[0] != len(self.tag_table):
            raise ValueError(
                f"model produced {row.shape[0]} scores but the tag table has "
                f"{len(self.tag_table)} rows -- mismatched model/csv pair"
            )
        char_cut = threshold if character_threshold is None else character_threshold

        names = self.tag_table.names

        def collect(indices: np.ndarray, cut: float | None) -> dict[str, float]:
            picked: dict[str, float] = {}
            for i in indices:
                score = float(row[i])
                if cut is None or score >= cut:
                    picked[names[i]] = score
            return dict(sorted(picked.items(), key=lambda kv: kv[1], reverse=True))

        return TagResult(
            ratings=collect(self._idx_rating, None),  # never filtered
            characters=collect(self._idx_character, char_cut),
            tags=collect(self._idx_general, threshold),
            embedding=embedding,
        )

    def interrogate(
        self,
        image: Image.Image,
        threshold: float = DEFAULT_THRESHOLD,
        character_threshold: float | None = None,
    ) -> TagResult:
        tensor = preprocess_image(image, self.input_shape[1] or MODEL_INPUT_SIZE)
        confidences, embeddings = self._run(tensor)
        embedding = None if embeddings is None else np.asarray(embeddings[0], dtype=np.float32)
        return self._parse(confidences, threshold, character_threshold, embedding)

    def interrogate_path(
        self,
        path: Path | str,
        threshold: float = DEFAULT_THRESHOLD,
        character_threshold: float | None = None,
    ) -> TagResult:
        with Image.open(path) as image:
            return self.interrogate(image, threshold, character_threshold)

    def interrogate_batch(
        self,
        images: Sequence[Image.Image],
        threshold: float = DEFAULT_THRESHOLD,
        character_threshold: float | None = None,
        batch_size: int = 8,
    ) -> list[TagResult]:
        """Batch inference.

        ``batch_size`` is a request, not a promise: if the graph pins its batch
        axis (the usual case for WD14 exports) we are forced down to that size,
        which is why this still works when handed a model that only accepts one
        image per call.
        """
        results: list[TagResult] = []
        height = self.input_shape[1] or MODEL_INPUT_SIZE
        if self.fixed_batch is not None:
            if batch_size > self.fixed_batch:
                log.debug(
                    "model pins batch size to %d; ignoring requested %d",
                    self.fixed_batch,
                    batch_size,
                )
            batch_size = self.fixed_batch
        batch_size = max(1, batch_size)
        for start in range(0, len(images), batch_size):
            chunk = images[start : start + batch_size]
            tensors = [preprocess_image(img, height) for img in chunk]
            batch = np.concatenate(tensors, axis=0)
            confidences, embeddings = self._run(batch)
            for i in range(len(chunk)):
                embedding = (
                    None
                    if embeddings is None
                    else np.asarray(embeddings[i], dtype=np.float32)
                )
                results.append(
                    self._parse(
                        confidences[i], threshold, character_threshold, embedding
                    )
                )
        return results
