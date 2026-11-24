"""Assemble one four-class dataset — and its train/val manifest — from every pool.

    tools\\build_kind_dataset.py --out data\\datasets\\kind6 --manifest

Reads the original downloaders' output AND the extra pools fetched by
``tools\\fetch_extra_pools.py``, then produces one folder per class plus a
``manifest.jsonl`` that records, for every file, its class, its **source** and its
**split**.

Why the extra bookkeeping:

* source — pooling four datasets into one accuracy hides a head that just learned
  the biggest dataset.  ``tools\\train_feature_plugin.py`` can print accuracy per
  source instead, so a new pool that scores 50% is visible immediately.
* split — the blur augmentation writes *new files* next to the originals, and a
  blurred twin of a validation image would make validation meaningless.  The split
  is decided here, once, before any copy exists.

Files are hardlinked, so keeping the download pools and this view costs no disk.
Visually identical images are used once, even across classes: the safebooru
queries use different tag lists, so one post can arrive under two names.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

from PIL import Image

CLASSES = ("插画", "梗图", "照片", "截图")

# Where each class's files come from, in the order they are added.  The name is
# the "source" written to the manifest and printed per-source at validation time.
KIND_SOURCES = {
    "插画": [
        ("safebooru-illust", lambda kind, meme: sorted(kind.glob("illustration-*.jpg"))),
        ("safebooru-texted", lambda kind, meme: [
            path for path in sorted((meme / "_插画").glob("*.jpg"))
            if not path.name.startswith("texted-")
        ]),
    ],
    "梗图": [
        ("safebooru-meme", lambda kind, meme: sorted((meme / "梗图").glob("*.jpg"))),
        ("safebooru-meme2", lambda kind, meme: sorted(kind.glob("meme-*.jpg"))),
        ("safebooru-texted", lambda kind, meme: sorted((meme / "_插画").glob("texted-*.jpg"))),
    ],
    "照片": [
        ("kind-photo", lambda kind, meme: sorted(kind.glob("photo-*.jpg"))),
    ],
    "截图": [
        ("kind-screenshot", lambda kind, meme: sorted(kind.glob("screenshot-*.jpg"))),
    ],
}

EXTRA_CLASSES = {"met-meme": "梗图", "chinesebqb": "梗图", "coco": "照片",
                 "commons": "照片", "picsum": "照片", "landscape": "照片",
                 "coco-hf": "照片", "celeba": "照片",
                 "rico": "截图", "desktop": "截图"}

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

def train_hint(out: Path) -> str:
    """The training command that matches whatever folder was just built."""
    folder = out.as_posix()
    return (
        f'tools\\train_feature_plugin.py --jsonl {folder}/manifest.jsonl '
        f'--loss ce --class-weight --cache {folder}/embeddings.npz '
        '--out plugins\\kind --id kind --name "画面类型" --target tag '
        "--threshold 0.5 --binary-label 插画 --epochs 400 --l2 3e-3 --force"
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


def extra_rows(extra: Path) -> list[dict]:
    """Rows of ``extra/manifest.jsonl``; falls back to walking the folders."""
    manifest = extra / "manifest.jsonl"
    if manifest.exists():
        rows = []
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            path = Path(row["path"])
            source = row.get("source") or path.parent.name
            rows.append({"path": path, "source": source,
                         "class": row.get("class") or EXTRA_CLASSES.get(source, "")})
        return rows
    rows = []
    for sub in sorted(p for p in extra.iterdir() if p.is_dir()):
        for file in sorted(sub.iterdir()):
            if file.suffix.lower() in IMAGE_SUFFIXES:
                rows.append({"path": file, "source": sub.name,
                             "class": EXTRA_CLASSES.get(sub.name, "")})
    return rows


def assign_split(index: int, total: int, val_frac: float) -> str:
    """Deterministic, source-independent hold-out.

    ``index % 5 == 0`` rather than a random draw: the same file lands in the same
    split on every rebuild, so a val number is comparable across runs.
    """
    stride = max(2, int(round(1.0 / val_frac))) if val_frac > 0 else 0
    if not stride or total < 20:
        return "train"
    return "val" if index % stride == 0 else "train"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kind", default="data/datasets/kind",
                        help="folder the four source classes were downloaded to")
    parser.add_argument("--meme", default="data/datasets/meme_vs_illustration",
                        help="folder tools\\fetch_meme_dataset.py wrote to")
    parser.add_argument("--extra", default="data/datasets/extra",
                        help="folder tools\\fetch_extra_pools.py wrote to ('' to skip)")
    parser.add_argument("--out", default="data/datasets/kind6",
                        help="destination, one sub-folder per class")
    parser.add_argument("--val-frac", type=float, default=0.2,
                        help="share of each (class, source) group held out")
    parser.add_argument("--manifest", default=None,
                        help="JSONL manifest path (default: <out>/manifest.jsonl; '' to skip)")
    parser.add_argument("--no-dedupe", action="store_true",
                        help="keep visually identical images (default: use each once)")
    args = parser.parse_args(argv)

    kind, meme, out = Path(args.kind), Path(args.meme), Path(args.out)
    if not kind.is_dir() or not meme.is_dir():
        raise SystemExit(f"missing download folder: {kind} or {meme}")
    extra = Path(args.extra) if args.extra else None

    # (class, source) -> candidate paths, in a stable order.
    groups: list[tuple[str, str, list[Path]]] = []
    for name in CLASSES:
        for source, select in KIND_SOURCES[name]:
            files = select(kind, meme)
            if files:
                groups.append((name, source, files))
    if extra and extra.is_dir():
        by_pair: dict[tuple[str, str], list[Path]] = {}
        for row in extra_rows(extra):
            if row["class"] not in CLASSES:
                print(f"  ! {row['path']}: unknown class {row['class']!r}, skipped")
                continue
            by_pair.setdefault((row["class"], row["source"]), []).append(row["path"])
        for (name, source), files in sorted(by_pair.items()):
            groups.append((name, source, sorted(files)))

    seen: dict[int, Path] = {}
    manifest_rows: list[dict] = []
    totals: dict[str, int] = {name: 0 for name in CLASSES}
    skipped = 0
    for name, source, files in groups:
        added = 0
        total = len(files)
        for index, src in enumerate(files):
            if not src.exists():
                continue
            label = ""
            if not args.no_dedupe:
                digest = average_hash(src)
                if digest is not None and digest in seen:
                    skipped += 1
                    continue
                if digest is not None:
                    seen[digest] = src
            # Name: <source>__<original stem>, unique per class folder.
            stem = f"{source}__{src.stem}"
            dst = out / name / f"{stem}{src.suffix.lower()}"
            if not dst.exists():
                link(src, dst)
                added += 1
            manifest_rows.append({
                "path": str(dst).replace("\\", "/"),
                "labels": [name],
                "source": source,
                "split": assign_split(index, total, args.val_frac),
            })
        totals[name] += added
        print(f"  {name:4s} {source:20s} +{added:5d} linked "
              f"({total} candidate(s), split held out)")

    print(f"\n{out}: " + ", ".join(f"{name} {count}" for name, count in totals.items())
          + f" = {sum(totals.values())} new file(s), {skipped} duplicate(s) skipped")
    if not manifest_rows:
        print("nothing to do -- check --kind/--meme/--extra")
        return 1

    manifest_path = Path(args.manifest) if args.manifest else out / "manifest.jsonl"
    if args.manifest == "":
        manifest_path = None
    if manifest_path is not None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in manifest_rows) + "\n",
            encoding="utf-8",
        )
        val = sum(1 for row in manifest_rows if row["split"] == "val")
        per_class: dict[str, dict[str, int]] = {}
        for row in manifest_rows:
            bucket = per_class.setdefault(row["labels"][0], {"train": 0, "val": 0})
            bucket[row["split"]] += 1
        print(f"  manifest: {len(manifest_rows)} row(s), {val} val -> {manifest_path}")
        for name, bucket in per_class.items():
            print(f"    {name:4s} train {bucket['train']:5d}  val {bucket['val']:4d}")

    print("\ntrain with:\n  " + train_hint(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
