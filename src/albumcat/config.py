"""Project configuration.

Everything user-tunable lives in ``config.toml`` at the project root; this
module turns it into dataclasses and supplies the defaults that file may omit.
Precedence, lowest to highest: defaults here -> ``config.toml`` -> environment
variables (``ALBUMCAT_*``) -> explicit CLI flags.

Read it with::

    from albumcat.config import Config
    cfg = Config.load()             # finds config.toml upward from cwd
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, TypeVar

__all__ = ["Config", "ModelConfig", "ImmichConfig", "PublishConfig", "PluginConfig"]

T = TypeVar("T")

#: Repository root -- ``<root>/src/albumcat/config.py`` -> ``<root>``.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class ModelConfig:
    dir: str = "models/vit"
    """Directory holding ``model.onnx`` and ``selected_tags.csv``."""

    device: str = "auto"
    """``auto`` | ``dml`` | ``cuda`` | ``cpu``. ``auto`` walks the provider list."""

    threshold: float = 0.35
    """Minimum score for a general tag to survive."""

    character_threshold: float = 0.5
    """Minimum score for a character tag.

    Upstream's README explicitly warns that WD14 character tags are
    false-positive-prone and asks callers to filter at >= 0.5 themselves.
    """

    batch_size: int = 8
    """Images per ONNX run. 8 keeps VRAM modest and is plenty on DirectML."""

    gpu_mem_limit_mb: int = 4096
    """DirectML only. 0 disables the limit."""

    @property
    def path(self) -> Path:
        p = Path(self.dir)
        return p if p.is_absolute() else (PROJECT_ROOT / p)


@dataclass
class ImmichConfig:
    base_url: str = "http://127.0.0.1:2283"
    api_key: str = ""
    verify_tls: bool = True
    timeout: float = 60.0
    download_concurrency: int = 4
    """How many originals to pull at once when tagging a library."""


@dataclass
class PublishConfig:
    """How predictions become Immich tags."""

    character_root: str = "角色"
    """Parent tag for ``角色/<name>``. Empty string = flat tags."""

    series_root: str = "作品"
    copyright_root: str = "版权"

    rating_root: str = "分级"
    rating_enabled: bool = True
    """Create ``分级/R18`` style tags."""

    rating_threshold: float = 0.5
    write_star_rating: bool = False
    """Also write Immich's 1-5 star rating from WD14's 4-way rating."""

    review_low: float = 0.5
    review_high: float = 0.7
    """Scores in ``[review_low, review_high)`` are marked "needs review"
    and, by default, are NOT published as character tags."""

    publish_review_band: bool = False
    """If True, mid-confidence characters are tagged anyway (marked in the log)."""

    max_characters_per_image: int = 3
    """Keep only the N highest-scoring characters. WD14 over-fires on busy art."""

    max_general_tags_per_image: int = 25
    general_tag_denylist: list[str] = field(default_factory=list)
    """Tags never published (e.g. ``"1girl"`` if you find it noise)."""

    general_tags_in_tree: bool = False
    """Route A only: put general tags in the gallery's tag *tree* too.

    The XMP tree (``lr:HierarchicalSubject`` / ``digiKam:TagsList``) is what a
    gallery groups by, so by default it carries only ``角色`` / ``作品`` /
    ``分级`` -- otherwise twenty-odd booru tags per photo bury the character
    tags. General tags still go into the flat ``dc:subject`` bag either way, so
    they stay searchable.
    """

    tree_in_subject: bool = True
    """Route A only: list the tree paths in flat ``dc:subject`` as well.

    Aves builds an entry's tags from XMP ``dc:subject`` and nothing else -- its
    native reader looks up ``XMP.DC_SUBJECT_PROP_NAME``, splits on ``;`` and
    stops there. It never reads ``lr:HierarchicalSubject`` or
    ``digiKam:TagsList``. So a tree that lives only in those two fields is
    invisible on the phone: the Tags page would show "Kasane Teto" but no
    ``角色|Kasane Teto`` to tap.

    Costs a few extra keywords per photo and makes the character tags usable in
    Aves. Turn it off if you only ever browse in digiKam/Lightroom, where the
    duplicate entries are just noise.
    """

    dry_run: bool = True
    """Safety default: plan but do not touch Immich until explicitly turned off."""


@dataclass
class PluginConfig:
    enabled: bool = True
    dir: str = "plugins"
    """Directory scanned for plugin subdirectories."""

    disabled: list[str] = field(default_factory=list)
    """Plugin ids to skip even if present."""

    @property
    def path(self) -> Path:
        p = Path(self.dir)
        return p if p.is_absolute() else (PROJECT_ROOT / p)


@dataclass
class Config:
    model: ModelConfig = field(default_factory=ModelConfig)
    immich: ImmichConfig = field(default_factory=ImmichConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)
    plugins: PluginConfig = field(default_factory=PluginConfig)
    root: Path = PROJECT_ROOT

    # ------------------------------------------------------------------ #
    # loading
    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        raw: dict[str, Any] = {}
        resolved: Path | None = None

        if path is not None:
            resolved = Path(path)
            if not resolved.exists():
                raise FileNotFoundError(f"config not found: {resolved}")
        else:
            resolved = _find_config()

        if resolved is not None:
            with open(resolved, "rb") as fh:
                raw = tomllib.load(fh)

        cfg = cls(
            model=_fill(ModelConfig, raw.get("model")),
            immich=_fill(ImmichConfig, raw.get("immich")),
            publish=_fill(PublishConfig, raw.get("publish")),
            plugins=_fill(PluginConfig, raw.get("plugins")),
        )
        if resolved is not None:
            cfg.root = resolved.parent
        cfg.apply_env()
        return cfg

    def apply_env(self, env: dict[str, str] | None = None) -> "Config":
        """Overlay ``ALBUMCAT_<SECTION>_<FIELD>`` environment variables."""
        env = os.environ if env is None else env
        for section_name in ("model", "immich", "publish", "plugins"):
            section = getattr(self, section_name)
            for f in fields(section):
                key = f"ALBUMCAT_{section_name.upper()}_{f.name.upper()}"
                if key not in env:
                    continue
                setattr(section, f.name, _coerce(env[key], getattr(section, f.name)))
        return self

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for section_name in ("model", "immich", "publish", "plugins"):
            out[section_name] = asdict(getattr(self, section_name))
        return out

    def save(self, path: Path | str) -> Path:
        """Write the config back out as TOML (no external dependency)."""
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(_to_toml(self.to_dict()), encoding="utf-8")
        return dest


# ---------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------- #
def _find_config(start: Path | None = None) -> Path | None:
    """Look for ``config.toml`` from ``start`` upward, then at the project root."""
    here = (start or Path.cwd()).resolve()
    for candidate_dir in (here, *here.parents):
        candidate = candidate_dir / "config.toml"
        if candidate.exists():
            return candidate
    root_candidate = PROJECT_ROOT / "config.toml"
    return root_candidate if root_candidate.exists() else None


def _fill(cls: type[T], data: dict[str, Any] | None) -> T:
    """Build ``cls`` from ``data``, ignoring keys it does not declare."""
    if not data:
        return cls()
    known = {f.name for f in fields(cls)}  # type: ignore[arg-type]
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"{cls.__name__}: unknown config keys {sorted(unknown)}")
    return cls(**data)  # type: ignore[arg-type]


def _coerce(value: str, reference: Any) -> Any:
    if isinstance(reference, bool):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(reference, int) and not isinstance(reference, bool):
        return int(value)
    if isinstance(reference, float):
        return float(value)
    if isinstance(reference, list):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def _to_toml(data: dict[str, Any], prefix: str = "") -> str:
    """Minimal TOML writer for the flat/nested shapes this config uses."""
    lines: list[str] = []
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    nested = {k: v for k, v in data.items() if isinstance(v, dict)}
    if prefix:
        lines.append(f"[{prefix}]")
    for key, value in scalars.items():
        lines.append(f"{key} = {_toml_value(value)}")
    if prefix and scalars:
        lines.append("")
    for key, value in nested.items():
        child = f"{prefix}.{key}" if prefix else key
        lines.append(_to_toml(value, child).rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


# Silence the unused-import warning for is_dataclass, kept for future nesting.
_ = is_dataclass
