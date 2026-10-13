"""Recognizer plugins -- the extension seam of the whole project.

The base tagger (WD14) answers "what is generically true about this image".
Plugins answer "what does *my* narrow thing say", e.g. "this is definitely
Hatsune Miku" or "this is my own original character". A plugin never replaces
WD14; it adds predictions into the same :class:`~albumcat.types.TagSet`, tagged
with its own ``source`` so provenance survives all the way to the tag plan.

A plugin is a **directory** under ``plugins/`` (config: ``[plugins] dir``)
containing ``plugin.json`` plus whatever payload its ``kind`` needs::

    plugins/
      my_vtuber_head/
        plugin.json          {"kind": "feature", "model": "head.onnx", ...}
        head.onnx            input  [N, 1024] float32
        labels.json          ["my_oc", "another_oc"]
      ocr_copyright/
        plugin.json          {"kind": "python", ...}
        plugin.py            def create(ctx): return MyRecognizer(...)
      series_rules/
        plugin.json          {"kind": "rule", "rules": "rules.json"}
        rules.json           [{"id": ..., "when": ..., "then": ...}]

Kinds
-----
``feature``
    The cheap, recommended kind. Consumes the image embedding the WD14 tower
    produces (768-d for ViT-B/16 -- see ``tools/add_feature_output.py``) and
    runs a small ONNX classification head on it. Zero image decoding, so adding
    ten of these costs almost nothing. This is the "train a head on my
    corrected labels" loop from the upstream README.
``image`` / ``python``
    Full Python, gets the PIL image. For anything the embedding cannot express
    (OCR of a watermark, perceptual-hash lookup, a face embedding model).
``rule``
    No model at all: a bundle of entries for the rule engine. Pure metadata
    logic, e.g. "if ``1girl`` and ``solo`` then also tag ``单人``".
``remote``
    Forwards the preview image to an external HTTP service and merges the
    predictions it returns. For models too heavy to inline (a detector, an
    IP-specific classifier hosted elsewhere).
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

import numpy as np

from .types import CHARACTER, GENERAL, RATING, Prediction, TagSet

__all__ = [
    "KIND_FEATURE",
    "KIND_IMAGE",
    "KIND_PYTHON",
    "KIND_REMOTE",
    "KIND_RULE",
    "PluginManifest",
    "PluginError",
    "ImageItem",
    "Recognizer",
    "RecognizerContext",
    "PluginRegistry",
]

log = logging.getLogger("albumcat.plugins")

KIND_FEATURE = "feature"
KIND_IMAGE = "image"
KIND_PYTHON = "python"
KIND_REMOTE = "remote"
KIND_RULE = "rule"
KINDS = (KIND_FEATURE, KIND_IMAGE, KIND_PYTHON, KIND_REMOTE, KIND_RULE)

MANIFEST_NAME = "plugin.json"
#: Kinds that need the WD14 image embedding.
EMBEDDING_KINDS = (KIND_FEATURE,)


class PluginError(RuntimeError):
    """A plugin is malformed, unavailable, or failed at runtime."""


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
@dataclass
class PluginManifest:
    id: str
    kind: str
    version: str = "0.0.0"
    name: str = ""
    description: str = ""
    author: str = ""
    target: str = CHARACTER
    """Which :data:`~albumcat.types.CATEGORIES` bucket the outputs land in."""

    priority: int = 100
    """Lower runs first. Plugins run after WD14 so they usually want > 0."""

    threshold: float = 0.5
    labels: tuple[str, ...] = ()
    #: Expected head input width. ``0`` disables the check -- the width is
    #: model-dependent (768 for ViT-B/16, 1024 for ViT-L/14), so defaulting to a
    #: literal would reject correct plugins on the most common WD14 export. Read
    #: the real value with `albumcat model`.
    input_dim: int = 0
    model: str = ""
    rules: str = ""
    endpoint: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    sha256: str = ""
    enabled: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return self.name or self.id

    @classmethod
    def from_dict(cls, data: dict[str, Any], source: Path | str = "<memory>") -> "PluginManifest":
        if "id" not in data:
            raise PluginError(f"{source}: manifest is missing 'id'")
        if "kind" not in data:
            raise PluginError(f"{source}: manifest is missing 'kind'")
        kind = str(data["kind"])
        if kind not in KINDS:
            raise PluginError(f"{source}: unknown kind {kind!r}; expected one of {KINDS}")
        target = str(data.get("target", CHARACTER))
        if target not in (CHARACTER, GENERAL, RATING):
            raise PluginError(f"{source}: unknown target {target!r}")

        known = set(cls.__dataclass_fields__) - {"extra"}
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs["kind"] = kind
        kwargs["target"] = target
        if "labels" in kwargs:
            kwargs["labels"] = tuple(kwargs["labels"])
        kwargs["extra"] = {k: v for k, v in data.items() if k not in known}
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        out = {
            k: v
            for k, v in vars(self).items()
            if k != "extra" and v not in ("", (), (), None, False)
        }
        out["labels"] = list(self.labels)
        out.update(self.extra)
        return out


# --------------------------------------------------------------------------- #
# runtime context
# --------------------------------------------------------------------------- #
@dataclass
class RecognizerContext:
    """What a recognizer is allowed to know about its host process."""

    plugin_dir: Path
    device: str = "auto"
    gpu_mem_limit: int = 0
    log: logging.Logger = field(default_factory=lambda: log)

    def path(self, relative: str) -> Path:
        return (self.plugin_dir / relative).resolve()


@dataclass
class ImageItem:
    """One image on its way through the pipeline.

    ``embedding`` is the WD14 penultimate feature vector (768-d for ViT-B/16),
    present only when the loaded model exposes a ``features`` output; ``feature``
    plugins are skipped without it.
    """

    path: str = ""
    asset_id: str = ""
    image: Any = None  # PIL.Image.Image
    embedding: np.ndarray | None = None
    base: TagSet = field(default_factory=TagSet)

    def preview_bytes(self, fmt: str = "JPEG", quality: int = 85) -> bytes:
        if self.image is None:
            raise PluginError("no decoded image available for this item")
        buffer = io.BytesIO()
        self.image.convert("RGB").save(buffer, fmt, quality=quality)
        return buffer.getvalue()


# --------------------------------------------------------------------------- #
# recognizer protocol
# --------------------------------------------------------------------------- #
class Recognizer(Protocol):
    manifest: PluginManifest

    def load(self) -> None: ...

    def recognize(self, item: ImageItem) -> Iterable[Prediction]: ...

    def available(self) -> tuple[bool, str]: ...


class _Base:
    """Shared bookkeeping; subclasses implement ``_predict``."""

    def __init__(self, manifest: PluginManifest, context: RecognizerContext) -> None:
        self.manifest = manifest
        self.context = context
        self._loaded = False
        self._reason = ""

    # -- lifecycle ----------------------------------------------------- #
    def load(self) -> None:
        self._loaded = True

    def available(self) -> tuple[bool, str]:
        if not self.manifest.enabled:
            return False, "disabled by config"
        return (True, "") if self._loaded else (False, "not loaded")

    # -- inference ----------------------------------------------------- #
    def recognize(self, item: ImageItem) -> list[Prediction]:
        ok, reason = self.available()
        if not ok:
            raise PluginError(f"plugin {self.manifest.id} unavailable: {reason}")
        return list(self._predict(item))

    def _predict(self, item: ImageItem) -> Iterable[Prediction]:
        raise NotImplementedError

    # -- helpers ------------------------------------------------------- #
    def _predictions(self, keys: Sequence[str], scores: Sequence[float]) -> list[Prediction]:
        threshold = self.manifest.threshold
        out: list[Prediction] = []
        for key, score in zip(keys, scores):
            score = float(score)
            if score >= threshold:
                out.append(
                    Prediction(key, score, self.manifest.target, self.manifest.id)
                )
        return out


# --------------------------------------------------------------------------- #
# concrete recognizers
# --------------------------------------------------------------------------- #
class OnnxHeadRecognizer(_Base):
    """``kind: feature`` -- a small ONNX classifier over WD14's embedding."""

    def __init__(self, manifest: PluginManifest, context: RecognizerContext) -> None:
        super().__init__(manifest, context)
        if not manifest.model:
            raise PluginError(f"{manifest.id}: kind 'feature' requires 'model'")
        self.model_path = context.path(manifest.model)
        self.session: Any = None
        self.labels: tuple[str, ...] = manifest.labels
        self._input_name = ""
        self._output_name = ""

    def load(self) -> None:
        import onnxruntime as ort

        if not self.model_path.exists():
            raise PluginError(f"{self.manifest.id}: model not found: {self.model_path}")
        self._verify_hash()
        labels_file = self.context.path("labels.json")
        if not self.labels and labels_file.exists():
            self.labels = tuple(json.loads(labels_file.read_text(encoding="utf-8")))

        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.enable_mem_pattern = False
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        providers = _plugin_providers(ort, self.context)
        self.session = ort.InferenceSession(str(self.model_path), sess_options=options, providers=providers)
        self._input_name = self.session.get_inputs()[0].name
        self._output_name = self.session.get_outputs()[0].name
        shape = self.session.get_inputs()[0].shape
        dim = shape[-1] if isinstance(shape[-1], int) else self.manifest.input_dim
        if self.manifest.input_dim and dim != self.manifest.input_dim:
            raise PluginError(
                f"{self.manifest.id}: head expects dim {dim} but manifest says "
                f"{self.manifest.input_dim} -- set \"input_dim\": {dim} in plugin.json"
            )
        self._loaded = True
        self.context.log.info(
            "plugin %s loaded (onnx head, %d labels, providers=%s)",
            self.manifest.id, len(self.labels), self.session.get_providers(),
        )

    def _verify_hash(self) -> None:
        if not self.manifest.sha256:
            return
        digest = hashlib.sha256(self.model_path.read_bytes()).hexdigest()
        if digest.lower() != self.manifest.sha256.lower():
            raise PluginError(
                f"{self.manifest.id}: sha256 mismatch for {self.model_path.name} "
                f"(expected {self.manifest.sha256[:12]}..., got {digest[:12]}...)"
            )

    def available(self) -> tuple[bool, str]:
        if not self.manifest.enabled:
            return False, "disabled by config"
        if not self._loaded:
            return False, "not loaded"
        if self.session is None:
            return False, "no session"
        if not self.labels:
            return False, "no labels (set 'labels' in plugin.json or add labels.json)"
        return True, ""

    def _predict(self, item: ImageItem) -> list[Prediction]:
        if item.embedding is None:
            return []
        vector = np.asarray(item.embedding, dtype=np.float32).reshape(1, -1)
        raw = np.asarray(
            self.session.run([self._output_name], {self._input_name: vector})[0]
        ).reshape(-1)
        # Heads are usually trained with a sigmoid but exported either way.
        if raw.min() < 0.0 or raw.max() > 1.0:
            raw = 1.0 / (1.0 + np.exp(-raw))
        if len(raw) != len(self.labels):
            raise PluginError(
                f"{self.manifest.id}: head produced {len(raw)} scores for "
                f"{len(self.labels)} labels"
            )
        return self._predictions(self.labels, raw)


class RemoteRecognizer(_Base):
    """``kind: remote`` -- POST the preview image to an HTTP service."""

    def load(self) -> None:
        if not self.manifest.endpoint:
            raise PluginError(f"{self.manifest.id}: kind 'remote' requires 'endpoint'")
        self._loaded = True

    def _predict(self, item: ImageItem) -> list[Prediction]:
        import requests

        payload = {
            "image": base64.b64encode(item.preview_bytes()).decode("ascii"),
            "plugin": self.manifest.id,
            "path": item.path,
            "asset_id": item.asset_id,
        }
        response = requests.post(
            self.manifest.endpoint, json=payload, headers=self.manifest.headers, timeout=60
        )
        response.raise_for_status()
        body = response.json()
        rows = body.get("predictions", body if isinstance(body, list) else [])
        out: list[Prediction] = []
        for row in rows:
            key = row.get("key") or row.get("tag") or row.get("label")
            if not key:
                continue
            score = float(row.get("score", row.get("confidence", 1.0)))
            category = row.get("category", self.manifest.target)
            if score >= self.manifest.threshold:
                out.append(Prediction(str(key), score, category, self.manifest.id))
        return out


class RuleRecognizer(_Base):
    """``kind: rule`` -- contribute rule-engine entries, not predictions."""

    def load(self) -> None:
        from .rules import load_rules

        self.rules_path = self.context.path(self.manifest.rules or "rules.json")
        if not self.rules_path.exists():
            raise PluginError(f"{self.manifest.id}: rules file not found: {self.rules_path}")
        self.rules = load_rules(self.rules_path)
        for rule in self.rules:
            if not rule.id.startswith(self.manifest.id):
                rule.id = f"{self.manifest.id}.{rule.id}"
        self._loaded = True

    def _predict(self, item: ImageItem) -> Iterable[Prediction]:
        return ()  # this kind never emits predictions directly


class PythonRecognizer(_Base):
    """``kind: python`` -- load ``plugin.py`` and delegate to its ``create(ctx)``."""

    def load(self) -> None:
        script = self.context.path("plugin.py")
        if not script.exists():
            raise PluginError(f"{self.manifest.id}: plugin.py not found: {script}")
        module = _import_from_path(f"albumcat_plugin_{self.manifest.id}", script)
        factory = getattr(module, "create", None)
        if factory is None:
            raise PluginError(f"{self.manifest.id}: plugin.py defines no create(ctx)")
        self.impl = factory(self.context)
        if hasattr(self.impl, "load"):
            self.impl.load()
        self._loaded = True

    def available(self) -> tuple[bool, str]:
        if not self.manifest.enabled:
            return False, "disabled by config"
        if not self._loaded:
            return False, "not loaded"
        impl = getattr(self, "impl", None)
        if impl is not None and hasattr(impl, "available"):
            return impl.available()
        return True, ""

    def _predict(self, item: ImageItem) -> Iterable[Prediction]:
        rows = self.impl.recognize(item)
        out: list[Prediction] = []
        for row in rows:
            if isinstance(row, Prediction):
                out.append(row)
                continue
            key = row.get("key") or row.get("tag") or row.get("label")
            if not key:
                continue
            score = float(row.get("score", row.get("confidence", 1.0)))
            if score >= self.manifest.threshold:
                out.append(
                    Prediction(
                        str(key),
                        score,
                        row.get("category", self.manifest.target),
                        self.manifest.id,
                    )
                )
        return out


_BUILDERS = {
    KIND_FEATURE: OnnxHeadRecognizer,
    KIND_REMOTE: RemoteRecognizer,
    KIND_RULE: RuleRecognizer,
    KIND_IMAGE: PythonRecognizer,
    KIND_PYTHON: PythonRecognizer,
}


def _plugin_providers(ort: Any, context: RecognizerContext) -> list:
    """Plugins are small; keep them on the GPU only when it is already in use."""
    available = ort.get_available_providers()
    wanted = {
        "dml": "DmlExecutionProvider",
        "cuda": "CUDAExecutionProvider",
        "cpu": "CPUExecutionProvider",
        "auto": "DmlExecutionProvider" if "DmlExecutionProvider" in available else "CPUExecutionProvider",
    }.get(context.device, "CPUExecutionProvider")
    if wanted not in available:
        wanted = "CPUExecutionProvider"
    return [wanted]


def _import_from_path(module_name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PluginError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    # Make the plugin directory importable so plugin.py can ship helper modules.
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        try:
            sys.path.remove(str(path.parent))
        except ValueError:
            pass
    return module


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
class PluginRegistry:
    """Discovers, loads and runs the plugins under a directory."""

    def __init__(
        self,
        directory: Path | str,
        device: str = "auto",
        disabled: Iterable[str] = (),
        gpu_mem_limit: int = 0,
    ) -> None:
        self.directory = Path(directory)
        self.device = device
        self.disabled = set(disabled)
        self.gpu_mem_limit = gpu_mem_limit
        self.recognizers: list[_Base] = []
        self.rule_bundles: list[Any] = []
        self._manifests: list[PluginManifest] = []
        #: manifest id -> the directory it was actually found in. `id` is the
        #: provenance label and *should* match the directory name, but resolving
        #: files through it means a rename silently breaks the plugin. Resolve
        #: against the real directory instead and warn on the mismatch.
        self._dirs: dict[str, Path] = {}
        self.errors: list[tuple[str, str]] = []

    # -- discovery ----------------------------------------------------- #
    def discover(self) -> list[PluginManifest]:
        self._manifests = []
        self._dirs = {}
        self.errors = []
        if not self.directory.exists():
            return []
        for entry in sorted(self.directory.iterdir()):
            manifest_path = entry / MANIFEST_NAME
            if not entry.is_dir() or not manifest_path.exists():
                continue
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest = PluginManifest.from_dict(data, manifest_path)
            except Exception as exc:  # noqa: BLE001
                self.errors.append((entry.name, f"manifest: {exc}"))
                continue
            if manifest.id != entry.name:
                log.warning(
                    "plugin %s lives in directory %r; files resolve relative to "
                    "the directory, but the id stays %r",
                    manifest.id,
                    entry.name,
                    manifest.id,
                )
            if manifest.id in self._dirs:
                self.errors.append(
                    (manifest.id, f"duplicate id: also declared in {self._dirs[manifest.id]}")
                )
                continue
            self._dirs[manifest.id] = entry
            if manifest.id in self.disabled:
                manifest.enabled = False
            self._manifests.append(manifest)
        return self._manifests

    # -- loading ------------------------------------------------------- #
    def load(self) -> None:
        if not self._manifests:
            self.discover()
        self.recognizers = []
        self.rule_bundles = []
        for manifest in self._manifests:
            context = RecognizerContext(
                plugin_dir=self._dirs.get(manifest.id, self.directory / manifest.id),
                device=self.device,
                gpu_mem_limit=self.gpu_mem_limit,
            )
            builder = _BUILDERS[manifest.kind]
            try:
                recognizer = builder(manifest, context)
                recognizer.load()
            except Exception as exc:  # noqa: BLE001
                self.errors.append((manifest.id, str(exc)))
                log.warning("plugin %s failed to load: %s", manifest.id, exc)
                continue
            if manifest.kind == KIND_RULE:
                self.rule_bundles.extend(getattr(recognizer, "rules", []))
            else:
                self.recognizers.append(recognizer)

    # -- use ----------------------------------------------------------- #
    def active(self, include_disabled: bool = False) -> list[PluginManifest]:
        """Manifests that are loaded and usable, in run order."""
        out: list[PluginManifest] = []
        for rec in self.recognizers:
            ok, _ = rec.available()
            if ok or include_disabled:
                out.append(rec.manifest)
        return sorted(out, key=lambda m: (m.priority, m.id))

    @property
    def needs_embedding(self) -> bool:
        return any(r.manifest.kind in EMBEDDING_KINDS for r in self.recognizers)

    def rules(self) -> list[Any]:
        return list(self.rule_bundles)

    def recognize(self, item: ImageItem) -> list[Prediction]:
        """Run every usable recognizer; a failing plugin is skipped, not fatal."""
        predictions: list[Prediction] = []
        for rec in sorted(self.recognizers, key=lambda r: (r.manifest.priority, r.manifest.id)):
            ok, reason = rec.available()
            if not ok:
                if reason not in ("not loaded",):
                    log.debug("skipping plugin %s: %s", rec.manifest.id, reason)
                continue
            try:
                predictions.extend(rec.recognize(item))
            except Exception as exc:  # noqa: BLE001
                self.errors.append((rec.manifest.id, str(exc)))
                log.warning("plugin %s failed on %s: %s", rec.manifest.id, item.path, exc)
        return predictions

    def describe(self) -> list[dict[str, Any]]:
        rows = []
        for manifest in self._manifests:
            rec = next((r for r in self.recognizers if r.manifest.id == manifest.id), None)
            ok, reason = rec.available() if rec is not None else (False, "not loaded")
            rows.append(
                {
                    "id": manifest.id,
                    "kind": manifest.kind,
                    "version": manifest.version,
                    "target": manifest.target,
                    "priority": manifest.priority,
                    "threshold": manifest.threshold,
                    "ready": ok,
                    "reason": reason,
                }
            )
        return rows
