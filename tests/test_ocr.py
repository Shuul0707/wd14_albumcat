"""OCR tests that run without the OCR model.

The engine itself is a 15 MB ONNX bundle inside ``rapidocr_onnxruntime``; CI
should not download it to check the plumbing around it. Everything here is
about what happens *around* inference:

* which fragments survive the confidence filter,
* what "no text on this photo" looks like on the wire,
* and that a missing engine degrades instead of failing the tag request.

The one thing these tests cannot prove is that the engine reads Chinese
correctly -- that needs the real model, and it is verified end to end in
``docs/OCR.md`` with a measured example.
"""

from __future__ import annotations

import pytest

from albumcat.config import OcrConfig
from albumcat.ocr import OcrEngine, OcrInfo


def engine(**kwargs) -> OcrEngine:
    return OcrEngine(OcrConfig(**kwargs))


class TestCollect:
    """``_collect`` turns RapidOCR's raw tuples into the phone's index."""

    def test_joins_lines_in_reading_order(self) -> None:
        raw = [
            [None, "初音未来 演唱会 2026", "0.98"],
            [None, "上海站·3月21日", "0.95"],
        ]
        assert engine()._collect(raw) == ["初音未来 演唱会 2026", "上海站·3月21日"]

    def test_drops_low_confidence_fragments(self) -> None:
        # Stylised lettering makes the recogniser invent a character or two; a
        # wrong word in the index surfaces a photo the user never asked for.
        raw = [
            [None, "演唱会", "0.97"],
            [None, "岀", "0.31"],
        ]
        assert engine(min_score=0.5)._collect(raw) == ["演唱会"]

    def test_score_is_a_string_in_this_release(self) -> None:
        # RapidOCR returns the score as a string; a naive float() on the tuple
        # would raise instead of filtering.
        raw = [[None, "文字", "0.82"]]
        assert engine()._collect(raw) == ["文字"]

    def test_ignores_blank_and_malformed_items(self) -> None:
        raw = [
            [None, "  ", "0.99"],
            [None, "", "0.99"],
            [None],  # truncated tuple
            "not-a-row",
            [None, "保留这一行", "0.90"],
        ]
        assert engine()._collect(raw) == ["保留这一行"]

    def test_empty_result_means_no_text(self) -> None:
        assert engine()._collect(None) == []
        assert engine()._collect([]) == []


class TestDegrade:
    """A missing engine must never be fatal -- text is an extra index."""

    def test_disabled_reports_reason_and_not_available(self) -> None:
        info = engine(enabled=False).info()
        assert isinstance(info, OcrInfo)
        assert info.enabled is False
        assert info.available is False
        assert "disabled" in info.reason

    def test_disabled_read_returns_none(self) -> None:
        assert engine(enabled=False).read(object()) is None

    def test_info_serialises_for_the_wire(self) -> None:
        d = engine(enabled=False).info().to_dict()
        assert set(d) == {"enabled", "available", "reason", "engine", "max_side", "min_score"}


class TestConfig:
    def test_defaults_are_conservative(self) -> None:
        c = OcrConfig()
        assert c.enabled is True
        # 2.5-5.5 s per photo is the reason this is opt-in per request, not the
        # reason to disable it: the caller decides.
        assert c.max_side == 1600
        assert c.min_score == pytest.approx(0.5)

    def test_read_from_toml_section(self, tmp_path) -> None:
        from albumcat.config import Config

        path = tmp_path / "c.toml"
        path.write_text(
            "[ocr]\nenabled = false\nmax_side = 960\nmin_score = 0.7\n",
            encoding="utf-8",
        )
        cfg = Config.load(path)
        assert cfg.ocr.enabled is False
        assert cfg.ocr.max_side == 960
        assert cfg.ocr.min_score == pytest.approx(0.7)

    def test_missing_section_falls_back_to_defaults(self, tmp_path) -> None:
        from albumcat.config import Config

        path = tmp_path / "c.toml"
        path.write_text("[model]\nthreshold = 0.4\n", encoding="utf-8")
        assert Config.load(path).ocr.max_side == 1600
