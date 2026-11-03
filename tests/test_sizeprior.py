"""The size prior: a logit shift for one label, as a function of the long edge.

The prior exists because WD14 never sees the original resolution -- every photo
is squashed into a 448 px square first -- and a 320 px sticker really does behave
differently from a 6,000 px wallpaper. These tests pin the three things that make
it safe to leave switched on: it is off when the size is unknown, it is smooth
rather than a cliff, and it never invents or drops a label.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from albumcat.plugins import (  # noqa: E402
    GENERAL,
    ImageItem,
    PluginManifest,
    RecognizerContext,
    _Base,
)
from albumcat.sizeprior import SizePrior  # noqa: E402


def prior(**kwargs) -> SizePrior:
    base = dict(label="梗图", small_edge=640, large_edge=2400, boost=0.8, suppress=-0.4)
    base.update(kwargs)
    return SizePrior(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# delta
# --------------------------------------------------------------------------- #
def test_a_small_image_gets_the_full_boost():
    assert prior().delta(320) == pytest.approx(0.8)
    assert prior().delta(640) == pytest.approx(0.8)


def test_a_huge_image_gets_the_full_suppression():
    assert prior().delta(2400) == pytest.approx(-0.4)
    assert prior().delta(6000) == pytest.approx(-0.4)


def test_an_unknown_size_gets_nothing():
    assert prior().delta(0) == 0.0


def test_the_shift_is_monotone_between_the_anchors():
    deltas = [prior().delta(edge) for edge in (640, 900, 1200, 1800, 2400)]
    assert deltas == sorted(deltas, reverse=True)
    assert deltas[0] > deltas[-1]


def test_the_midpoint_is_halfway_in_log_space():
    # 640 and 2400 are 1.907 octaves apart, so the geometric mean is 1239 px.
    assert prior().delta(1239) == pytest.approx(0.2, abs=0.01)


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #
SCORES = [("截图", 0.05), ("插画", 0.30), ("梗图", 0.35), ("照片", 0.30)]


def test_a_small_meme_overtakes_the_runaway_illustration():
    out = prior().apply(SCORES, 480)
    labels = [label for label, _ in out]
    assert labels == ["截图", "插画", "梗图", "照片"]  # order is never reshuffled
    scores = dict(out)
    assert scores["梗图"] == max(scores.values())
    assert scores["梗图"] > 0.35


def test_a_huge_image_cannot_stay_a_meme():
    lopsided = [("截图", 0.01), ("插画", 0.20), ("梗图", 0.75), ("照片", 0.04)]
    out = dict(prior().apply(lopsided, 4000))
    assert out["梗图"] < 0.75
    assert out["插画"] > 0.20


def test_the_result_is_still_a_distribution():
    out = prior().apply(SCORES, 480)
    assert sum(score for _, score in out) == pytest.approx(1.0)


def test_a_label_the_head_never_scores_changes_nothing():
    other = [("插画", 0.6), ("照片", 0.4)]
    assert prior().apply(other, 480) == other


def test_an_unknown_size_returns_the_head_s_answer():
    assert prior().apply(SCORES, 0) == [(label, pytest.approx(score)) for label, score in SCORES]


def test_all_zero_scores_do_not_divide_by_zero():
    zeros = [("插画", 0.0), ("梗图", 0.0)]
    assert prior().apply(zeros, 480) == zeros


# --------------------------------------------------------------------------- #
# manifest parsing
# --------------------------------------------------------------------------- #
def test_a_manifest_without_a_prior_leaves_the_plugin_alone():
    assert SizePrior.from_manifest(None) is None
    assert SizePrior.from_manifest({}) is None
    assert SizePrior.from_manifest({"label": "  "}) is None
    assert SizePrior.from_manifest("梗图") is None


def test_a_broken_range_is_ignored_rather_than_half_applied():
    assert SizePrior.from_manifest({"label": "梗图", "small_edge": 0}) is None
    assert SizePrior.from_manifest({"label": "梗图", "small_edge": 900, "large_edge": 900}) is None


def test_defaults_fill_in_what_the_manifest_omits():
    parsed = SizePrior.from_manifest({"label": "梗图"})
    assert parsed == SizePrior(label="梗图")


# --------------------------------------------------------------------------- #
# the plugin seam
# --------------------------------------------------------------------------- #
class _Fixed(_Base):
    values: list[float] = []

    def _predict(self, item):  # pragma: no cover - not used
        return []

    def _scores(self, item):
        return list(zip(["插画", "梗图", "照片"], self.values))


def make_prior_plugin(extra: dict) -> _Fixed:
    manifest = PluginManifest.from_dict(
        {
            "id": "kind",
            "kind": "feature",
            "target": GENERAL,
            "threshold": 0.5,
            "labels": ["插画", "梗图", "照片"],
            **extra,
        }
    )
    plugin = _Fixed(manifest, RecognizerContext(plugin_dir=Path(".")))
    plugin.load()
    return plugin


def test_the_plugin_reports_raw_and_adjusted_scores_separately():
    plugin = make_prior_plugin({"size_prior": {"label": "梗图", "boost": 0.8, "suppress": -0.4}})
    plugin.values = [0.30, 0.35, 0.30]
    item = ImageItem(width=480, height=480)
    scored = plugin.scored(item)
    assert scored.delta == pytest.approx(0.8)
    assert dict(scored.raw)["梗图"] == pytest.approx(0.35)
    assert dict(scored.adjusted)["梗图"] > dict(scored.raw)["梗图"]
    assert plugin.scores(item) == scored.adjusted


def test_a_plugin_without_a_prior_never_shifts_anything():
    plugin = make_prior_plugin({})
    plugin.values = [0.30, 0.35, 0.30]
    scored = plugin.scored(ImageItem(width=200, height=200))
    assert scored.delta == 0.0
    assert scored.adjusted == scored.raw


def test_only_the_reported_original_size_counts():
    """An item with no dimensions must never be judged as small.

    The phone's payload *is* a 448 px square, so a plugin that fell back to the
    embedded image's own size would boost every meme-shaped thumbnail as if it
    had been saved at 448 px. The server therefore always passes the reported
    size explicitly, ``(0, 0)`` included, and this is what that looks like from
    the plugin's side.
    """
    plugin = make_prior_plugin({"size_prior": {"label": "梗图", "boost": 0.8}})
    plugin.values = [0.30, 0.35, 0.30]
    assert plugin.scored(ImageItem()).delta == 0.0
    assert plugin.scored(ImageItem(width=448, height=0)).delta == 0.0
    assert plugin.scored(ImageItem(width=0, height=0)).delta == 0.0
    # A real 448 px original, reported as such, is small -- and that is correct:
    # it is the server's job to send the truth, not the plugin's to guess.
    assert plugin.scored(ImageItem(width=448, height=448)).delta == pytest.approx(0.8)
