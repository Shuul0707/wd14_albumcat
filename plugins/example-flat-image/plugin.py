"""Example ``kind: python`` plugin.

The host loads this file and calls ``create(ctx)``. Whatever you return must
implement ``recognize(item)``; ``load()`` and ``available()`` are optional.

``recognize`` may yield either:

* ``albumcat.types.Prediction`` objects, or
* plain dicts -- ``{"key": ..., "score": ..., "category": ...}``
  (``tag``/``label`` and ``confidence`` are accepted as aliases)

Anything scoring below the plugin's ``threshold`` in ``plugin.json`` is
discarded by the host, and the ``category`` decides which bucket the
prediction lands in for the rules engine:

    "character" -> 角色/<name>      (subject to the review band)
    "tag"       -> a general tag
    "rating"    -> 分级/<name>

The item you are handed carries:

    item.path        local path of the cached image (for reference only)
    item.asset_id    photo id from the phone, empty when tagging local files
    item.image       PIL.Image -- what WD14 actually saw (already squared
                     off at 448x448, so aspect ratio is NOT original)
    item.embedding   the split model's 1024-d vector, or None
    item.base        TagSet of what WD14 already found
"""

from __future__ import annotations

import numpy as np

from albumcat.types import GENERAL, Prediction

# Below this luminance standard deviation an image counts as flat. A photo or
# an illustration is usually well above 6; a solid colour or a black frame is
# close to 0.
FLAT_STDDEV = 6.0


class FlatImageDetector:
    def load(self) -> None:
        """Called once at startup. Load models, open files, warm up here."""

    def available(self) -> tuple[bool, str]:
        """Return ``(usable, reason)``. The host skips the plugin when False."""
        return True, ""

    def recognize(self, item) -> list[Prediction]:
        if item.image is None:
            return []

        # item.image is 448x448; shrink hard -- we only want coarse statistics.
        grey = item.image.convert("L").resize((64, 64))
        std = float(np.asarray(grey, dtype=np.float32).std())
        if std >= FLAT_STDDEV:
            return []

        # Map std -> confidence: 0.0 -> 1.0, FLAT_STDDEV -> 0.5, so the score
        # stays above the default 0.5 threshold exactly when we fire.
        score = 0.5 + 0.5 * (1.0 - std / FLAT_STDDEV)
        return [Prediction("低质量图片", score, GENERAL, "example-flat-image")]


def create(ctx):
    """Entry point. ``ctx`` is a RecognizerContext."""
    ctx.log.debug("flat-image detector ready (dir=%s)", ctx.plugin_dir)
    return FlatImageDetector()
