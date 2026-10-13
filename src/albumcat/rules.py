"""Rule engine: turn a :class:`~albumcat.types.TagSet` into a :class:`~albumcat.types.TagPlan`.

Two layers, deliberately:

1. **A default policy** derived from :class:`~albumcat.config.PublishConfig`.
   This is what 95% of runs use -- characters into ``角色/<name>``, R18 into
   ``分级/R18``, and so on.
2. **User rules** loaded from JSON, which run *before* the default policy and
   can suppress, redirect or add tags. This is the escape hatch for "for this
   series I want a different album name" without writing code.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import Config, PublishConfig
from .types import (
    CHARACTER,
    GENERAL,
    RATING,
    PlannedTag,
    Prediction,
    TagPlan,
    TagSet,
    prettify,
)

__all__ = ["Condition", "Action", "Rule", "RuleEngine", "default_rules", "load_rules"]

#: Placeholders usable inside ``Action.path`` entries.
_TEMPLATE_RE = re.compile(r"\{(\w+)\}")


@dataclass
class Condition:
    """All set fields must match (logical AND). An empty condition matches everything."""

    category: str | None = None
    key: str | None = None
    key_regex: str | None = None
    score_gte: float | None = None
    score_lt: float | None = None
    series: str | None = None
    rank_lt: int | None = None
    """Only match if the prediction is among the top ``rank_lt`` of its category."""

    def matches(self, pred: Prediction, rank: int) -> bool:
        if self.category is not None and pred.category != self.category:
            return False
        if self.key is not None and pred.key != self.key:
            return False
        if self.key_regex is not None and not re.search(self.key_regex, pred.key):
            return False
        if self.score_gte is not None and pred.score < self.score_gte:
            return False
        if self.score_lt is not None and pred.score >= self.score_lt:
            return False
        if self.series is not None and (pred.series or "") != self.series:
            return False
        if self.rank_lt is not None and rank >= self.rank_lt:
            return False
        return True


@dataclass
class Action:
    """What a matched rule does.

    ``kind``:
      ``tag``   -- emit ``path`` (templated) as a tag
      ``drop``  -- suppress the prediction from the default policy
      ``star``  -- set the star rating (0-5) to ``value``
      ``review``-- mark the plan as needing human review
    """

    kind: str = "tag"
    path: list[str] = field(default_factory=list)
    value: int | None = None
    reason: str = ""


@dataclass
class Rule:
    id: str
    when: Condition = field(default_factory=Condition)
    then: Action = field(default_factory=Action)
    priority: int = 100
    """Lower runs first. User rules loaded from JSON default to 10, i.e. ahead
    of the default policy at 100 -- a later rule can still refine an earlier one
    because ``drop`` actions are tracked for the whole run."""
    enabled: bool = True
    description: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Rule":
        return cls(
            id=data["id"],
            when=Condition(**data.get("when", {})),
            then=Action(**data.get("then", {})),
            priority=int(data.get("priority", 10)),
            enabled=bool(data.get("enabled", True)),
            description=data.get("description", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "priority": self.priority,
            "enabled": self.enabled,
            "description": self.description,
            "when": {k: v for k, v in vars(self.when).items() if v is not None},
            "then": {k: v for k, v in vars(self.then).items() if v not in (None, [])},
        }


class RuleEngine:
    """Applies user rules, then the configured default policy."""

    def __init__(
        self,
        config: Config | PublishConfig,
        rules: Iterable[Rule] | None = None,
    ) -> None:
        self.publish = config.publish if isinstance(config, Config) else config
        user_rules = list(rules or [])
        self.rules: list[Rule] = sorted(
            [r for r in user_rules if r.enabled], key=lambda r: (r.priority, r.id)
        )
        self._reserved_paths_layer = True

    # ------------------------------------------------------------------ #
    def evaluate(self, tagset: TagSet, asset_id: str = "", source_path: str = "") -> TagPlan:
        plan = TagPlan(asset_id=asset_id, source_path=source_path)
        cfg = self.publish

        ranks = {
            pred.ref: rank
            for category in (CHARACTER, GENERAL, RATING)
            for rank, pred in enumerate(tagset.by_category(category))
        }

        dropped: set[tuple[str, str]] = set()
        emitted: set[tuple[str, ...]] = set()

        def emit(path: Sequence[str], pred: Prediction | None, score: float, reason: str,
                 source: str = "wd14") -> None:
            key = tuple(path)
            if not key or any(not part for part in key) or key in emitted:
                return
            emitted.add(key)
            plan.tags.append(PlannedTag(key, score, source, reason))

        # ---- 1. user rules ------------------------------------------- #
        for pred in tagset:
            if pred.category == RATING:
                continue
            for rule in self.rules:
                if not rule.when.matches(pred, ranks.get(pred.ref, 10**6)):
                    continue
                action = rule.then
                if action.kind == "drop":
                    dropped.add(pred.ref)
                elif action.kind == "tag":
                    display = pred.display
                    series = pred.series or ""
                    parts = [
                        _render(part, pred.key, display, series) for part in action.path
                    ]
                    emit(parts, pred, pred.score, action.reason or rule.id, pred.source)
                elif action.kind == "star" and action.value is not None:
                    plan.star_rating = int(action.value)
                elif action.kind == "review":
                    plan.needs_review = True

        # ---- 2. characters (default policy) -------------------------- #
        selected: list[tuple[Prediction, bool]] = []
        for index, pred in enumerate(tagset.characters):
            if pred.ref in dropped:
                continue
            if pred.score < cfg.review_low:
                continue
            in_band = pred.score < cfg.review_high
            if in_band:
                plan.needs_review = True
                if not cfg.publish_review_band:
                    plan.notes.append(
                        f"character {pred.key!r} scored {pred.score:.3f} "
                        f"(review band {cfg.review_low}-{cfg.review_high}) -- not published"
                    )
                    continue
            if index >= cfg.max_characters_per_image:
                continue
            selected.append((pred, in_band))

        for pred, in_band in selected:
            if cfg.character_root:
                emit((cfg.character_root, pred.display), pred, pred.score,
                     "character (review band)" if in_band else "character")
            else:
                emit((pred.display,), pred, pred.score,
                     "character (review band)" if in_band else "character")
            # The trailing parenthetical of a WD14 character tag is its source work.
            if pred.series:
                root = cfg.series_root
                emit((root, prettify(pred.series)) if root else (prettify(pred.series),),
                     pred, pred.score, "character series")

        # ---- 3. rating ----------------------------------------------- #
        if cfg.rating_enabled:
            bucket, score = tagset.best_rating()
            bucket_plan = _rating_bucket(bucket, score, cfg.rating_threshold)
            if bucket_plan:
                label, star = bucket_plan
                pred = tagset.get(bucket, RATING)
                emit((cfg.rating_root, label) if cfg.rating_root else (label,),
                     pred, score, f"rating {bucket} @ {score:.3f}")
                if cfg.write_star_rating and star is not None:
                    plan.star_rating = star

        # ---- 4. general tags ----------------------------------------- #
        if cfg.max_general_tags_per_image > 0:
            deny = {t.lower() for t in cfg.general_tag_denylist}
            kept = 0
            for pred in tagset.general:
                if kept >= cfg.max_general_tags_per_image:
                    break
                if pred.ref in dropped or pred.key.lower() in deny:
                    continue
                emit((prettify(pred.key),), pred, pred.score, "general tag")
                kept += 1

        return plan

    # ------------------------------------------------------------------ #
    def explain(self, path: Sequence[str]) -> str:
        for rule in self.rules:
            if tuple(rule.then.path) == tuple(path):
                return f"rule {rule.id}: {rule.description or rule.then.reason}"
        return "default policy"


def _render(template: str, key: str, display: str, series: str) -> str:
    def sub(match: re.Match[str]) -> str:
        name = match.group(1)
        return {"key": key, "display": display, "series": series}.get(name, match.group(0))

    return _TEMPLATE_RE.sub(sub, template)


def _rating_bucket(bucket: str, score: float, threshold: float) -> tuple[str, int | None] | None:
    """Map WD14's 4-way rating onto a tag label + optional star value."""
    if score < threshold:
        return None
    mapping = {
        "explicit": ("R18", 1),
        "questionable": ("敏感", 2),
        "sensitive": ("轻微", 4),
    }
    return mapping.get(bucket)


def default_rules(cfg: PublishConfig) -> list[Rule]:
    """Rules derived from config, for documentation/export. The engine applies
    this policy directly (it needs rank/limit state rules cannot express)."""
    name = cfg.character_root or "<character>"
    return [
        Rule(
            id="character.review_band",
            when=Condition(category=CHARACTER, score_gte=cfg.review_low, score_lt=cfg.review_high),
            then=Action(kind="review", reason="character confidence in review band"),
            priority=90,
            description=f"mark {cfg.review_low}-{cfg.review_high} character scores for review",
        ),
        Rule(
            id="character.publish",
            when=Condition(category=CHARACTER, score_gte=cfg.review_high),
            then=Action(kind="tag", path=[name, "{display}"], reason="confident character"),
            priority=100,
            description="publish confident characters under the character root",
        ),
        Rule(
            id="rating.r18",
            when=Condition(category=RATING, key="explicit", score_gte=cfg.rating_threshold),
            then=Action(kind="tag", path=[cfg.rating_root or "分级", "R18"], reason="explicit content"),
            priority=100,
        ),
    ]


def load_rules(path: Path | str) -> list[Rule]:
    """Load a JSON rule file -- either a bare list or ``{"rules": [...]}``."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("rules", [])
    return [Rule.from_dict(item) for item in data]


def save_rules(rules: Sequence[Rule], path: Path | str) -> Path:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps({"rules": [r.to_dict() for r in rules]}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return dest
