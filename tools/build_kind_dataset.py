"""Assemble one four-class folder from the two downloaders' output.

    tools\\build_kind_dataset.py --out data\\datasets\\kindset5

Classes: 插画 (clean illustration), 梗图 (captioned / meme framing), 照片 (camera
photo), 截图 (UI screenshot).  Files are hardlinked when the file system allows it,
so keeping both the raw download and this view costs no extra disk space.

Images that look the same (64-bit average hash) are only used once, even across
classes -- the two downloads query safebooru with different tag lists, so the same
post can show up under two names, and a picture in both 插画 and 梗图 would teach
the head nothing but noise.
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

from PIL import Image

CLASSES = ("插画", "梗图", "照片", "截图")

TRAIN_HINT = (
    'tools\\train_feature_plugin.py --data data\\datasets\\kindset5 --loss ce '
    '--class-weight --cache data\\datasets\\kindset5\\embeddings.npz '
    '--out plugins\\illustration --id illustration --name "画面类型" '
    "--target tag --threshold 0.5 --binary-label 插画 --force"
)


def link(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def average_hash(path: Path) -> int | None:
    """64-bit average hash; None when the file cannot be decoded."""
    try:
        with Image.open(path) as image:
            small = image.convert("L").resize((8, 8), Image.BILINEAR)
            pixels = small.tobytes()
    except Exception:  # noqa: BLE001 - a broken download should not stop the build
        return None
    if len(pixels) != 64:
        return None
    average = sum(pixels) / 64.0
    bits = 0
    for value in pixels:
        bits = (bits << 1) | (1 if value >= average else 0)
    return bits


def collect(kind: Path, meme: Path) -> dict[str, list[Path]]:
    texted_root = meme / "_插画"
    plan: dict[str, list[Path]] = {name: [] for name in CLASSES}
    plan["插画"] += sorted(kind.glob("illustration-*.jpg"))
    plan["插画"] += [
        path for path in sorted(texted_root.glob("*.jpg"))
        if not path.name.startswith("texted-")
    ]
    plan["梗图"] += sorted((meme / "梗图").glob("*.jpg"))
    plan["梗图"] += sorted(kind.glob("meme-*.jpg"))
    plan["梗图"] += sorted(texted_root.glob("texted-*.jpg"))
    plan["照片"] += sorted(kind.glob("photo-*.jpg"))
    plan["截图"] += sorted(kind.glob("screenshot-*.jpg"))
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kind", default="data/datasets/kind",
                        help="folder the four source classes were downloaded to")
    parser.add_argument("--meme", default="data/datasets/meme_vs_illustration",
                        help="folder tools\\fetch_meme_dataset.py wrote to")
    parser.add_argument("--out", default="data/datasets/kindset5",
                        help="destination, one sub-folder per class")
    parser.add_argument("--no-dedupe", action="store_true",
                        help="keep visually identical images (default: use each once)")
    args = parser.parse_args(argv)

    kind, meme, out = Path(args.kind), Path(args.meme), Path(args.out)
    if not kind.is_dir() or not meme.is_dir():
        raise SystemExit(f"missing download folder: {kind} or {meme}")
    plan = collect(kind, meme)

    seen: dict[int, Path] = {}
    total = 0
    skipped = 0
    for name in CLASSES:
        added = 0
        for src in plan[name]:
            dst = out / name / src.name
            if not args.no_dedupe:
                digest = average_hash(src)
                if digest is not None and digest in seen:
                    skipped += 1
                    continue
                if digest is not None:
                    seen[digest] = src
            if dst.exists():
                continue
            link(src, dst)
            added += 1
        total += added
        print(f"  {name:6s} +{added:5d} linked  ({len(plan[name])} candidate(s))")
    print(f"{out}: {total} new file(s), {skipped} duplicate(s) skipped")
    if total == 0:
        print("nothing new -- delete the output folder to rebuild")
        return 1
    print("\ntrain with:\n  " + TRAIN_HINT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
