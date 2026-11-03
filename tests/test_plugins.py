"""The plugin layer's contract: manifests, thresholds, and the ONNX head shape."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from albumcat.plugins import (  # noqa: E402
    GENERAL,
    PluginManifest,
    RecognizerContext,
    _Base,
)


class _Fixed(_Base):
    """A recognizer whose scores we dictate, to test ``_predictions``."""

    keys: list[str] = []
    values: list[float] = []

    def _predict(self, item):  # pragma: no cover - not used
        return []

    def run(self):
        return self._predictions(self.keys, self.values)


def make(threshold: float = 0.5, exclusive: bool = False) -> _Fixed:
    manifest = PluginManifest.from_dict(
        {
            "id": "t",
            "kind": "feature",
            "target": GENERAL,
            "threshold": threshold,
            **({"exclusive": True} if exclusive else {}),
        }
    )
    return _Fixed(manifest, RecognizerContext(plugin_dir=Path(".")))


def test_a_sigmoid_head_keeps_every_label_over_the_threshold():
    plugin = make(threshold=0.5)
    plugin.keys = ["插画", "梗图"]
    plugin.values = [0.9, 0.7]
    got = [p.key for p in plugin.run()]
    assert got == ["插画", "梗图"]


def test_a_sigmoid_head_drops_labels_under_the_threshold():
    plugin = make(threshold=0.5)
    plugin.keys = ["插画", "梗图"]
    plugin.values = [0.49, 0.7]
    assert [p.key for p in plugin.run()] == ["梗图"]


def test_a_softmax_head_emits_only_the_winner():
    plugin = make(threshold=0.3, exclusive=True)
    plugin.keys = ["插画", "梗图", "照片", "截图"]
    plugin.values = [0.62, 0.20, 0.10, 0.08]
    got = plugin.run()
    assert [p.key for p in got] == ["插画"]
    assert got[0].score == pytest.approx(0.62)


def test_a_softmax_head_stays_quiet_when_even_the_winner_is_unsure():
    plugin = make(threshold=0.5, exclusive=True)
    plugin.keys = ["插画", "梗图", "照片", "截图"]
    plugin.values = [0.34, 0.26, 0.24, 0.16]
    assert plugin.run() == []


def test_a_softmax_head_with_no_scores_is_quiet():
    plugin = make(threshold=0.5, exclusive=True)
    plugin.keys = []
    plugin.values = []
    assert plugin.run() == []


def test_an_unknown_target_is_refused():
    with pytest.raises(Exception) as info:
        PluginManifest.from_dict({"id": "t", "kind": "feature", "target": "vibe"})
    assert "unknown target" in str(info.value)


def test_extra_keys_survive_the_round_trip():
    manifest = PluginManifest.from_dict(
        {"id": "t", "kind": "feature", "exclusive": True, "activation": "softmax"}
    )
    assert manifest.extra["exclusive"] is True
    assert manifest.extra["activation"] == "softmax"

# --------------------------------------------------------------------------- #
# unfiltered scores -- what /classify and the web UI need
# --------------------------------------------------------------------------- #
class _Scored(_Fixed):
    """A recognizer that can also show the scores *before* the threshold."""

    def _scores(self, item):
        return list(zip(self.keys, self.values))


def make_scored(threshold: float = 0.5, exclusive: bool = False) -> _Scored:
    manifest = PluginManifest.from_dict(
        {
            "id": "t",
            "kind": "feature",
            "target": GENERAL,
            "threshold": threshold,
            **({"exclusive": True} if exclusive else {}),
        }
    )
    plugin = _Scored(manifest, RecognizerContext(plugin_dir=Path(".")))
    plugin.load()
    return plugin


def test_scores_keeps_the_runner_up_the_threshold_hides():
    plugin = make_scored(threshold=0.5)
    plugin.keys = ["插画", "梗图", "照片"]
    plugin.values = [0.45, 0.52, 0.03]
    assert [p.key for p in plugin.run()] == ["梗图"]
    assert plugin.scores(None) == [("插画", 0.45), ("梗图", 0.52), ("照片", 0.03)]


def test_scores_agree_with_the_winner_an_exclusive_head_emits():
    plugin = make_scored(threshold=0.3, exclusive=True)
    plugin.keys = ["插画", "梗图", "照片", "截图"]
    plugin.values = [0.62, 0.20, 0.10, 0.08]
    pairs = plugin.scores(None)
    best = max(pairs, key=lambda pair: pair[1])
    assert best[0] == plugin.run()[0].key == "插画"
    assert len(pairs) == 4, "the three losers must survive, the UI draws them"


def test_scores_are_floats_even_when_a_plugin_speaks_in_ints():
    plugin = make_scored()
    plugin.keys = ["插画"]
    plugin.values = [1]
    assert plugin.scores(None) == [("插画", 1.0)]


def test_a_recognizer_without_scores_reports_nothing():
    plugin = make(threshold=0.5)
    plugin.load()
    plugin.keys = ["插画"]
    plugin.values = [0.9]
    assert plugin.scores(None) == []


def test_a_recognizer_that_never_loaded_reports_no_scores():
    manifest = PluginManifest.from_dict(
        {"id": "t", "kind": "feature", "target": GENERAL, "threshold": 0.5}
    )
    plugin = _Scored(manifest, RecognizerContext(plugin_dir=Path(".")))
    plugin.keys = ["插画"]
    plugin.values = [0.9]
    assert plugin.scores(None) == [], "not loaded yet: nothing to show"
