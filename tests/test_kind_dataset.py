"""The dataset assembly tools: split assignment, manifests, blur copies.

These three scripts (``build_kind_dataset.py``, ``make_blur_copies.py``,
``fetch_extra_pools.py``) decide what the head trains on.  A bug here is invisible
in the training loss and shows up months later as "the phone keeps calling my
illustrations memes", so the rules worth pinning are:

* a validation image must never get a blurred twin (that would grade the model on
  its own augmentation);
* the split must be the same on every rebuild (a val number you cannot compare
  across runs is not a val number);
* a picture that appears in two classes must be used once, not once per class.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


builder = load("build_kind_dataset")
blurrer = load("make_blur_copies")
fetcher = load("fetch_extra_pools")


def write_image(path: Path, seed: int, size: tuple[int, int] = (64, 64)) -> Path:
    """A noise image: flat colours all hash the same and would look like duplicates."""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.effect_noise(size, 40 + (seed % 60)).convert("RGB")
    image.save(path, "JPEG", quality=90)
    return path


class TestAssignSplit:
    def test_every_fifth_row_is_val(self) -> None:
        splits = [builder.assign_split(i, 100, 0.2) for i in range(100)]
        assert splits[0] == "val"
        assert splits[1] == "train"
        assert splits.count("val") == 20

    def test_the_same_index_always_lands_in_the_same_split(self) -> None:
        first = [builder.assign_split(i, 50, 0.2) for i in range(50)]
        second = [builder.assign_split(i, 50, 0.2) for i in range(50)]
        assert first == second

    def test_a_tiny_group_stays_in_train(self) -> None:
        # 12 images: holding any out would leave too little to train on
        assert {builder.assign_split(i, 12, 0.2) for i in range(12)} == {"train"}


class TestBuildKindDataset:
    def make_pools(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        kind = tmp_path / "kind"
        meme = tmp_path / "meme"
        extra = tmp_path / "extra"
        for index in range(30):
            write_image(kind / f"illustration-{index:02d}.jpg", 100 + index)
            write_image(kind / f"meme-{index:02d}.jpg", 200 + index)
            write_image(kind / f"photo-{index:02d}.jpg", 300 + index)
            write_image(kind / f"screenshot-{index:02d}.jpg", 400 + index)
        write_image(meme / "梗图" / "m00.jpg", 500)
        (extra / "met-meme").mkdir(parents=True)
        (extra / "met-meme" / "meme-1.jpg").parent.mkdir(parents=True, exist_ok=True)
        write_image(extra / "met-meme" / "meme-1.jpg", 600)
        rows = [{"path": str(extra / "met-meme" / "meme-1.jpg"),
                 "source": "met-meme", "class": "梗图"}]
        (extra / "manifest.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        return kind, meme, extra

    def test_manifest_records_class_source_and_split(self, tmp_path: Path) -> None:
        kind, meme, extra = self.make_pools(tmp_path)
        out = tmp_path / "kind6"
        code = builder.main([
            "--kind", str(kind), "--meme", str(meme), "--extra", str(extra),
            "--out", str(out), "--val-frac", "0.2",
        ])
        assert code == 0
        manifest = out / "manifest.jsonl"
        rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
        assert rows
        assert {row["labels"][0] for row in rows} == {"插画", "梗图", "照片", "截图"}
        assert {"train", "val"} <= {row["split"] for row in rows}
        assert any(row["source"] == "met-meme" for row in rows)
        # every referenced file exists in the class folder
        for row in rows:
            assert Path(row["path"]).exists()

    def test_a_picture_in_two_classes_is_used_once(self, tmp_path: Path) -> None:
        kind, meme, extra = self.make_pools(tmp_path)
        # the same bytes under a different class: only the first class may take it
        write_image(meme / "梗图" / "photo-03.jpg", 303)
        out = tmp_path / "kind6"
        builder.main([
            "--kind", str(kind), "--meme", str(meme), "--extra", "",
            "--out", str(out),
        ])
        rows = [json.loads(line) for line in
                (out / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
        by_path = {row["path"] for row in rows}
        assert len(by_path) == len(rows)
        assert not any("梗图/safebooru-meme__photo-00" in path for path in by_path)


class TestMakeBlurCopies:
    def test_only_training_rows_get_a_twin(self, tmp_path: Path) -> None:
        train = write_image(tmp_path / "src" / "a.jpg", 700)
        val = write_image(tmp_path / "src" / "b.jpg", 701)
        manifest = tmp_path / "manifest.jsonl"
        rows = [
            {"path": str(train), "labels": ["插画"], "source": "s1", "split": "train"},
            {"path": str(val), "labels": ["插画"], "source": "s1", "split": "val"},
        ]
        manifest.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        out = tmp_path / "blur"
        code = blurrer.main(["--manifest", str(manifest), "--out", str(out)])
        assert code == 0
        merged = [json.loads(line) for line in
                  (out / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
        copies = [row for row in merged if row["source"].endswith("+blur")]
        assert len(copies) == 1
        assert copies[0]["split"] == "train"
        assert Path(copies[0]["path"]).exists()
        assert "b__blur" not in copies[0]["path"]

    def test_a_copy_keeps_the_frame_size(self, tmp_path: Path) -> None:
        source = write_image(tmp_path / "c.jpg", 702, size=(120, 80))
        for index in (0, 1):
            image = blurrer.degrade(source, index)
            assert image is not None
            assert image.size == (120, 80)

    def test_an_unreadable_file_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.jpg"
        broken.write_bytes(b"not an image")
        assert blurrer.degrade(broken, 0) is None


class TestSaver:
    def test_near_identical_images_are_kept_once(self, tmp_path: Path) -> None:
        saver = fetcher.Saver(tmp_path / "extra")
        image = Image.new("RGB", (300, 300), (12, 34, 56))
        assert saver.save(image.copy(), "coco", "coco-00001.jpg")
        assert not saver.save(image.copy(), "coco", "coco-00002.jpg")
        assert saver.skipped["dupe"] == 1
        assert len(saver.rows) == 1
        assert Path(saver.rows[0]["path"]).exists()

    def test_a_thumbnail_is_rejected(self, tmp_path: Path) -> None:
        saver = fetcher.Saver(tmp_path / "extra")
        assert not saver.save(Image.new("RGB", (64, 64), (1, 2, 3)), "coco", "x.jpg")
        assert saver.skipped["small"] == 1

    def test_the_long_edge_is_capped(self, tmp_path: Path) -> None:
        saver = fetcher.Saver(tmp_path / "extra")
        assert saver.save(Image.new("RGB", (2400, 1200), (5, 5, 5)), "coco", "big.jpg")
        with Image.open(saver.rows[0]["path"]) as saved:
            assert max(saved.size) == fetcher.MAX_EDGE


class TestSourceClasses:
    def test_every_downloaded_source_has_a_class(self) -> None:
        # the two tools keep the mapping twice; a source fetched but not mapped
        # lands on disk and is then silently skipped by the builder
        missing = set(fetcher.CLASS_OF_SOURCE) - set(builder.EXTRA_CLASSES)
        assert not missing, f"add these to build_kind_dataset.EXTRA_CLASSES: {sorted(missing)}"

    def test_the_classes_match(self) -> None:
        for source, label in fetcher.CLASS_OF_SOURCE.items():
            assert builder.EXTRA_CLASSES[source] == label, source

    def test_every_target_class_is_one_of_the_four(self) -> None:
        four = {"截图", "插画", "梗图", "照片"}
        assert set(fetcher.CLASS_OF_SOURCE.values()) <= four
        assert set(builder.EXTRA_CLASSES.values()) <= four
