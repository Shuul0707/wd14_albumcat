"""The pipeline's lazy CLIP tower: built once, only when a head asks for it.

A fused head trained on ``concat(wd14, clip)`` cannot answer anything without
the CLIP vector, so the pipeline has to supply one -- including on the bare
command line, where no ``serve`` has built the encoder yet. These tests pin the
three outcomes that matter: usable, unavailable, and broken.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from albumcat.pipeline import Pipeline  # noqa: E402


class _Encoder:
    def __init__(self, available: bool = True, boom: bool = False) -> None:
        self.available = available
        self.boom = boom
        self.calls = 0

    def info(self):
        return SimpleNamespace(reason="model file missing")

    def encode_image(self, image):
        self.calls += 1
        if self.boom:
            raise RuntimeError("onnx session is on fire")
        return np.ones(512, dtype=np.float32)


def bare(semantic=None, *, enabled: bool = True) -> Pipeline:
    """A Pipeline without a tagger -- just the part these tests exercise."""
    pipe = Pipeline.__new__(Pipeline)
    pipe.semantic = semantic
    pipe._semantic_failed = False
    pipe.config = SimpleNamespace(semantic=SimpleNamespace(enabled=enabled))
    return pipe


def test_an_injected_encoder_is_reused_not_rebuilt():
    encoder = _Encoder()
    pipe = bare(encoder)
    vector = pipe._semantic_vector(object())
    assert vector.shape == (512,)
    assert encoder.calls == 1
    assert pipe.semantic is encoder


def test_an_unavailable_encoder_means_no_vector_rather_than_zeroes():
    pipe = bare(_Encoder(available=False))
    assert pipe._semantic_vector(object()) is None


def test_a_failing_encoder_never_loses_the_tagging_run():
    assert bare(_Encoder(boom=True))._semantic_vector(object()) is None


def test_without_an_encoder_the_tower_is_built_once_and_kept():
    # The first call is allowed to build it; here the config says disabled, so it
    # must give up immediately, latch the failure, and never try again per image.
    pipe = bare(enabled=False)
    assert pipe._semantic_vector(object()) is None
    assert pipe._semantic_failed is True
    assert pipe._semantic_vector(object()) is None


def test_no_semantic_attribute_on_the_config_is_not_a_crash():
    pipe = Pipeline.__new__(Pipeline)
    pipe.semantic = None
    pipe._semantic_failed = False
    pipe.config = SimpleNamespace()
    assert pipe._semantic_vector(object()) is None


@pytest.mark.parametrize("vector", [np.zeros(512, dtype=np.float32), np.ones(512, dtype=np.float32)])
def test_the_vector_is_passed_through_untouched(vector):
    encoder = _Encoder()
    encoder.encode_image = lambda image: vector
    assert bare(encoder)._semantic_vector(object()) is vector
