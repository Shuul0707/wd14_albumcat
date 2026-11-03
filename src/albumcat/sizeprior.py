"""Resolution as a prior, not as a feature.

WD14 sees a 448×448 square: whatever the original dimensions were is gone before
the model runs, so a head trained on its embedding simply cannot know that the
image it is looking at is a 320 px reaction face rather than a 6,000 px wallpaper.
Resolution is nonetheless a real signal for the classes we care about -- something
that small has almost certainly been through a chat app and been recompressed,
while something that big is not a saved meme -- so it is applied *after* the head
as a transparent logit shift, on one label, and only when the client tells us the
true size.

Three design choices worth keeping:

* **Optional and silent by default.** A plugin that does not declare
  ``size_prior`` behaves exactly as before; a client that does not report the
  original size gets no shift at all (a 448 px thumbnail must never be mistaken
  for a 448 px photo).
* **Log-linear in log-size.** A cliff at "≤ 500 px" would make 501 px behave like
  4,000 px; interpolating between two anchors keeps the shift smooth, and the
  anchors are clamped so a 40 px icon or a 20,000 px panorama cannot dominate.
* **Tunable from ``plugin.json``.** The numbers live in the manifest next to the
  threshold, not in code, because they are a judgement call about somebody's
  photo library rather than a law of nature.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

__all__ = ["SizePrior"]


@dataclass(frozen=True)
class SizePrior:
    """A logit shift for one label, as a function of the image's long edge."""

    label: str
    small_edge: int = 640
    large_edge: int = 2400
    boost: float = 0.7
    suppress: float = -0.7

    @classmethod
    def from_manifest(cls, raw: Any) -> "SizePrior | None":
        """Build from the manifest's ``size_prior`` block, or ``None``."""
        if not isinstance(raw, Mapping):
            return None
        label = str(raw.get("label") or "").strip()
        if not label:
            return None
        try:
            prior = cls(
                label=label,
                small_edge=int(raw.get("small_edge", 640)),
                large_edge=int(raw.get("large_edge", 2400)),
                boost=float(raw.get("boost", 0.7)),
                suppress=float(raw.get("suppress", -0.7)),
            )
        except (TypeError, ValueError):
            return None
        if prior.small_edge <= 0 or prior.large_edge <= prior.small_edge:
            return None
        return prior

    # ------------------------------------------------------------------ #
    def delta(self, long_edge: int) -> float:
        """How much to add to this label's logit at that long edge.

        ``boost`` at or below ``small_edge``, ``suppress`` at or above
        ``large_edge``, a straight line in log2 between the two.
        """
        if long_edge <= 0:
            return 0.0
        lo = math.log2(self.small_edge)
        hi = math.log2(self.large_edge)
        t = (math.log2(max(long_edge, 1)) - lo) / (hi - lo)
        t = min(1.0, max(0.0, t))
        return self.boost + (self.suppress - self.boost) * t

    def apply(self, pairs: Iterable[tuple[str, float]], long_edge: int) -> list[tuple[str, float]]:
        """Shift ``label``'s logit and renormalise, leaving order/labels intact."""
        rows = [(str(key), float(score)) for key, score in pairs]
        names = [key for key, _ in rows]
        if self.label not in names:
            return rows
        delta = self.delta(long_edge)
        if delta == 0.0:
            return rows
        values = [max(0.0, score) for _, score in rows]
        total = sum(values)
        if total <= 0.0:
            return rows
        # p -> log p is a logit up to a constant, which the softmax below cancels
        logits = [math.log(value / total + 1e-12) for value in values]
        logits[names.index(self.label)] += delta
        top = max(logits)
        weights = [math.exp(value - top) for value in logits]
        mass = sum(weights)
        return [(key, weight / mass) for (key, _), weight in zip(rows, weights)]
