"""Shared value types: what comes out of the tagger/plugins, and what goes to a publisher.

The flow is::

    image -> Wd14Tagger ------.
                               >--> TagSet --> RuleEngine --> TagPlan --> Publisher
    image -> Plugin recognizers'

``TagSet`` is a *merge* structure: several sources may report the same tag and
we keep the highest score while remembering who said it (provenance), because
"WD14 guessed 0.52" and "my custom LoRA-scoped head said 0.99" deserve
different treatment downstream.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Sequence

__all__ = [
    "CHARACTER",
    "GENERAL",
    "RATING",
    "CATEGORIES",
    "RATING_NAMES",
    "Prediction",
    "TagSet",
    "PlannedTag",
    "TagPlan",
    "prettify",
    "split_series",
]

#: WD14's ``category`` column, as decoded by ``selected_tags.csv``.
CHARACTER = "character"  # category == 4
GENERAL = "tag"  # category == 0
RATING = "rating"  # category == 9
CATEGORIES = (CHARACTER, GENERAL, RATING)
RATING_NAMES = ("general", "sensitive", "questionable", "explicit")

_PAREN_RE = re.compile(r"\s*[（(]([^()（）]*)[)）]\s*$")


# ---------------------------------------------------------------------- #
# name helpers
# ---------------------------------------------------------------------- #
def prettify(key: str) -> str:
    """``hatsune_miku_(vocaloid)`` -> ``Hatsune Miku``.

    WD14 tag names are lowercase ASCII with underscores, and many carry the
    series in a trailing parenthetical -- see :func:`split_series`.
    """
    base, _ = split_series(key)
    base = base.replace("_", " ").strip()
    # Title-case, but leave short all-caps tokens (e.g. "3d", "r18") alone.
    words = [w if (len(w) <= 2 or not w.isalpha()) else w.capitalize() for w in base.split()]
    return " ".join(words) or key


def split_series(key: str) -> tuple[str, str | None]:
    """Split ``hatsune_miku_(vocaloid)`` into ``("hatsune_miku", "vocaloid")``.

    The trailing parenthetical in a WD14 character tag is usually the source
    work, which is exactly the grouping a user wants alongside the character.
    """
    match = _PAREN_RE.search(key)
    if not match:
        return key, None
    series = match.group(1).strip()
    # WD14 spells the separator as an underscore, so peeling the parenthetical
    # off leaves one behind: "hakurei_reimu_(touhou)" -> "hakurei_reimu_".
    base = key[: match.start()].strip().rstrip("_").strip()
    if not base or not series:
        return key, None
    return base, series


# ---------------------------------------------------------------------- #
# predictions
# ---------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Prediction:
    """One label with a confidence, from one source."""

    key: str
    """Raw label as the model/plugin spells it (``hatsune_miku``)."""

    score: float
    category: str = GENERAL
    source: str = "wd14"
    """Which recognizer produced this -- a plugin id, or ``wd14``."""

    label: str = ""
    """Optional human-facing name; falls back to :func:`prettify` of ``key``."""

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"unknown category {self.category!r}")

    @property
    def display(self) -> str:
        return self.label or prettify(self.key)

    @property
    def series(self) -> str | None:
        return split_series(self.key)[1]

    @property
    def ref(self) -> tuple[str, str]:
        """Merge identity: same category + same raw key."""
        return (self.category, self.key)


class TagSet:
    """A bag of predictions, merged by ``(category, key)`` keeping the best score."""

    __slots__ = ("_best", "_all")

    def __init__(self, predictions: Iterable[Prediction] = ()) -> None:
        self._best: dict[tuple[str, str], Prediction] = {}
        self._all: list[Prediction] = []
        self.update(predictions)

    # -- mutation ------------------------------------------------------ #
    def update(self, predictions: Iterable[Prediction]) -> "TagSet":
        for pred in predictions:
            self._all.append(pred)
            current = self._best.get(pred.ref)
            if current is None or pred.score > current.score:
                self._best[pred.ref] = pred
        return self

    def add(self, key: str, score: float, category: str = GENERAL, **kw: object) -> "TagSet":
        return self.update([Prediction(key, score, category, **kw)])  # type: ignore[arg-type]

    # -- access -------------------------------------------------------- #
    def __len__(self) -> int:
        return len(self._best)

    def __iter__(self) -> Iterator[Prediction]:
        return iter(self._best.values())

    def __contains__(self, key: object) -> bool:
        return any(p.key == key for p in self._best.values())

    @property
    def all_observations(self) -> tuple[Prediction, ...]:
        """Every prediction including suppressed duplicates (for logging)."""
        return tuple(self._all)

    def by_category(self, category: str) -> list[Prediction]:
        got = [p for p in self._best.values() if p.category == category]
        got.sort(key=lambda p: p.score, reverse=True)
        return got

    @property
    def characters(self) -> list[Prediction]:
        return self.by_category(CHARACTER)

    @property
    def general(self) -> list[Prediction]:
        return self.by_category(GENERAL)

    @property
    def ratings(self) -> dict[str, float]:
        return {p.key: p.score for p in self.by_category(RATING)}

    def get(self, key: str, category: str | None = None) -> Prediction | None:
        if category is not None:
            return self._best.get((category, key))
        for (cat, k), pred in self._best.items():
            if k == key:
                return pred
        return None

    def best_rating(self) -> tuple[str, float]:
        """Highest-scoring rating bucket. Falls back to ``("general", 0.0)``."""
        ratings = self.by_category(RATING)
        if not ratings:
            return ("general", 0.0)
        top = ratings[0]
        return (top.key, top.score)

    def sources(self) -> set[str]:
        return {p.source for p in self._best.values()}


# ---------------------------------------------------------------------- #
# plans
# ---------------------------------------------------------------------- #
@dataclass(slots=True)
class PlannedTag:
    """One tag the publisher should apply, with why it is there."""

    path: tuple[str, ...]
    score: float
    source: str = "wd14"
    reason: str = ""

    @property
    def leaf(self) -> str:
        return self.path[-1]

    @property
    def key(self) -> str:
        return "/".join(self.path)


@dataclass(slots=True)
class TagPlan:
    """The complete decision for one asset."""

    asset_id: str = ""
    source_path: str = ""
    tags: list[PlannedTag] = field(default_factory=list)
    star_rating: int | None = None
    needs_review: bool = False
    notes: list[str] = field(default_factory=list)

    def add(self, path: Sequence[str], score: float = 1.0, source: str = "wd14", reason: str = "") -> None:
        self.tags.append(PlannedTag(tuple(path), float(score), source, reason))

    @property
    def paths(self) -> list[tuple[str, ...]]:
        return [t.path for t in self.tags]

    def has_path(self, path: Sequence[str]) -> bool:
        return tuple(path) in {t.path for t in self.tags}

    def to_dict(self) -> dict:
        return {
            "asset_id": self.asset_id,
            "source_path": self.source_path,
            "needs_review": self.needs_review,
            "star_rating": self.star_rating,
            "notes": list(self.notes),
            "tags": [
                {"path": list(t.path), "key": t.key, "score": round(t.score, 4),
                 "source": t.source, "reason": t.reason}
                for t in self.tags
            ],
        }
