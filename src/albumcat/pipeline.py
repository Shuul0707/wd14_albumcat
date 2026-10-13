"""The pipeline: image -> tags -> plan.

Given a decoded image this produces a :class:`~albumcat.types.TagPlan` -- the
finite, policy-applied answer that the phone turns into its own tag entries.
Everything here is source- and destination-agnostic.

    Wd14Tagger  ->  TagSet  ->  plugins  ->  TagSet  ->  RuleEngine  ->  TagPlan
      (448px)        (wd14)     (adds)       (merged)      (policy)

Two entry points are provided: :meth:`Pipeline.plan_paths` for a local folder
(useful for the first "does WD14 even recognise my characters" test) and
:meth:`Pipeline.plan_image` for one already-decoded image, which is what the
HTTP service calls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from PIL import Image

from .config import Config
from .plugins import ImageItem, PluginRegistry
from .rules import RuleEngine
from .tagger import (
    CATEGORY_CHARACTER,
    CATEGORY_GENERAL,
    CATEGORY_RATING,
    TagResult,
    Wd14Tagger,
)
from .types import (
    CHARACTER,
    GENERAL,
    RATING,
    Prediction,
    TagPlan,
    TagSet,
)

__all__ = ["Pipeline", "PipelineStats", "load_tagger", "tagresult_to_tagset"]

log = logging.getLogger("albumcat.pipeline")

#: WD14's numeric ``category`` -> our string categories.
_CATEGORY_MAP = {
    CATEGORY_CHARACTER: CHARACTER,
    CATEGORY_GENERAL: GENERAL,
    CATEGORY_RATING: RATING,
}

IMAGE_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif",
    ".tif", ".tiff", ".avif", ".heic", ".heif",
}


# --------------------------------------------------------------------------- #
def tagresult_to_tagset(result: TagResult, source: str = "wd14") -> TagSet:
    """Convert a tagger result into the merge structure the rules consume."""
    tagset = TagSet()
    for category, mapping in (
        (RATING, result.ratings),
        (CHARACTER, result.characters),
        (GENERAL, result.tags),
    ):
        for key, score in mapping.items():
            tagset.update([Prediction(key, float(score), category, source)])
    return tagset


def load_tagger(config: Config, warmup: bool = True) -> Wd14Tagger:
    """Build a :class:`Wd14Tagger` from config, with a helpful error if unset."""
    model_dir = config.model.path
    model_path = model_dir / "model.onnx"
    if not model_path.exists():
        raise FileNotFoundError(
            f"no model at {model_path}\n"
            f"  download it with: python tools/download_model.py"
        )
    device = config.model.device
    providers = None if device == "auto" else {
        "dml": ["DmlExecutionProvider"],
        "cuda": ["CUDAExecutionProvider"],
        "cpu": ["CPUExecutionProvider"],
    }.get(device)
    if device not in ("auto", "dml", "cuda", "cpu"):
        raise ValueError(f"unknown device {device!r}; use auto|dml|cuda|cpu")
    return Wd14Tagger(
        model_path,
        providers=providers,
        gpu_mem_limit=config.model.gpu_mem_limit_mb * 1024**2,
        warmup=warmup,
    )


# --------------------------------------------------------------------------- #
@dataclass
class PipelineStats:
    images: int = 0
    failed: int = 0
    plugin_hits: int = 0
    needs_review: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)

    def note_error(self, subject: str, exc: object) -> None:
        self.failed += 1
        self.errors.append((subject, str(exc)))


class Pipeline:
    """Wires together tagger, plugins and rules."""

    def __init__(
        self,
        config: Config,
        tagger: Wd14Tagger | None = None,
        registry: PluginRegistry | None = None,
        engine: RuleEngine | None = None,
    ) -> None:
        self.config = config
        self.tagger = tagger if tagger is not None else load_tagger(config)
        if registry is None:
            registry = PluginRegistry(
                config.plugins.path,
                device=config.model.device,
                disabled=config.plugins.disabled,
            ) if config.plugins.enabled else PluginRegistry(config.plugins.path, device="cpu")
            if config.plugins.enabled:
                registry.discover()
                registry.load()
        self.registry = registry
        self.engine = engine if engine is not None else RuleEngine(config, self.registry.rules())

    # ------------------------------------------------------------------ #
    @property
    def plugins_need_embedding(self) -> bool:
        return self.registry.needs_embedding

    def warn_if_plugins_starved(self) -> list[str]:
        """Warnings about plugins that cannot run in this configuration."""
        warnings: list[str] = []
        if self.registry.needs_embedding and self.tagger.embedding_dim is None:
            warnings.append(
                "some plugins consume the image embedding, but the loaded model "
                "exposes none -- run `python tools/add_feature_output.py` to build "
                "a features-capable model. Those plugins will be skipped."
            )
        for plugin_id, reason in self.registry.errors:
            warnings.append(f"plugin {plugin_id!r} failed: {reason}")
        return warnings

    # ------------------------------------------------------------------ #
    def plan_image(
        self,
        image: Image.Image,
        *,
        asset_id: str = "",
        path: str = "",
        threshold: float | None = None,
        character_threshold: float | None = None,
    ) -> tuple[TagPlan, TagSet]:
        """Run one decoded image through the whole chain."""
        result = self.tagger.interrogate(
            image,
            threshold=self.config.model.threshold if threshold is None else threshold,
            character_threshold=(
                self.config.model.character_threshold
                if character_threshold is None
                else character_threshold
            ),
        )
        tagset = tagresult_to_tagset(result)

        if self.registry.recognizers:
            item = ImageItem(
                path=path,
                asset_id=asset_id,
                image=image,
                base=tagset,
                embedding=result.embedding,
            )
            tagset.update(self.registry.recognize(item))

        return self.engine.evaluate(tagset, asset_id=asset_id, source_path=path), tagset

    # ------------------------------------------------------------------ #
    def plan_paths(
        self,
        paths: Iterable[Path | str],
        expand_dirs: bool = True,
    ) -> Iterator[tuple[Path, TagPlan, TagSet]]:
        """Tag local files. Directories are expanded to the images inside."""
        files: list[Path] = []
        for path in paths:
            p = Path(path)
            if p.is_dir() and expand_dirs:
                files.extend(sorted(q for q in p.rglob("*") if q.suffix.lower() in IMAGE_SUFFIXES))
            elif p.is_file():
                files.append(p)
        for file in files:
            try:
                with Image.open(file) as image:
                    plan, tagset = self.plan_image(image, path=str(file))
            except Exception as exc:  # noqa: BLE001
                log.warning("failed on %s: %s", file, exc)
                continue
            yield file, plan, tagset
