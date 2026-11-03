"""The sample-folder truth map: folder names in, class labels out.

This is what lets the web page print an accuracy number: the folders people keep
their test images in are named inconsistently, and the flat ``all/`` copy has no
label at all, so both have to be handled here rather than in the page.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from albumcat.truth import build_truth_map, label_for_dir  # noqa: E402


def test_folder_names_map_both_ways():
    assert label_for_dir("chahua") == "插画"
    assert label_for_dir("插画") == "插画"
    assert label_for_dir("photo") == "照片"
    assert label_for_dir("screenshoot") == "截图"
    assert label_for_dir("screenshot") == "截图"
    assert label_for_dir("meme") == "梗图"


def test_folder_names_can_carry_a_prefix_or_suffix():
    # "1_chahua-2026" and "meme pool" are both names people actually use
    assert label_for_dir("1_chahua-2026") == "插画"
    assert label_for_dir("meme pool") == "梗图"
    assert label_for_dir("插画样本") == "插画"


def test_folders_that_say_nothing_stay_unlabeled():
    for name in ("all", "misc", "", "2026-08"):
        assert label_for_dir(name) is None


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")


def test_the_flat_all_folder_is_matched_by_name(tmp_path):
    root = tmp_path / "samples"
    _touch(root / "chahua" / "a1.jpg")
    _touch(root / "photo" / "b1.png")
    _touch(root / "all" / "a1.jpg")
    _touch(root / "all" / "b1.png")
    truth = build_truth_map(root)
    assert truth["exists"] is True
    assert truth["counts"] == {"截图": 0, "插画": 1, "梗图": 0, "照片": 1}
    assert truth["classes"]["a1.jpg"] == "插画"
    assert truth["classes"]["b1.png"] == "照片"
    assert truth["matched"] == 2
    assert truth["unlabeled"] == []


def test_files_without_a_home_are_reported_not_guessed(tmp_path):
    root = tmp_path / "samples"
    _touch(root / "chahua" / "a1.jpg")
    _touch(root / "all" / "aa1.jpg")
    _touch(root / "all" / "notes.txt")
    truth = build_truth_map(root)
    assert "aa1.jpg" in truth["unlabeled"]
    assert "notes.txt" not in truth["classes"] and "notes.txt" not in truth["unlabeled"]
    assert truth["stray"] == 1


def test_a_missing_folder_is_an_empty_map_not_a_crash(tmp_path):
    truth = build_truth_map(tmp_path / "nope")
    assert truth["exists"] is False
    assert truth["classes"] == {}
    assert truth["counts"] == {"截图": 0, "插画": 0, "梗图": 0, "照片": 0}


def test_two_folders_claiming_one_name_is_a_conflict(tmp_path):
    root = tmp_path / "samples"
    _touch(root / "chahua" / "same.jpg")
    _touch(root / "meme" / "same.jpg")
    truth = build_truth_map(root)
    assert truth["conflicts"] == ["same.jpg"]
    assert truth["classes"]["same.jpg"] == "插画"  # first folder wins, and it is reported
