#!/usr/bin/env python3
"""Tag images with WD14 and print what the model actually sees.

This is the "does it even recognise my characters" smoke test. It needs no
server, no Immich and no phone -- just the ONNX files and some pictures.

Usage::

    python tools/tag_images.py <file-or-dir-or-glob> [more...]
    python tools/tag_images.py D:\\samples --top 15
    python tools/tag_images.py D:\\samples --threshold 0.25 --char-threshold 0.3
    python tools/tag_images.py D:\\samples --json out.json
    python tools/tag_images.py --info          # show loaded model + tag table

By default the character threshold is 0.3 (low) so you can *see* the
candidates; the pipeline's real cut-off is 0.5 (see tagger.DEFAULT_CHARACTER_THRESHOLD).
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from albumcat.tagger import (  # noqa: E402
    DEFAULT_THRESHOLD,
    Wd14Tagger,
)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}


def probe_model_dir(root: Path, name: str | None) -> tuple[Path, Path]:
    base = root / "models"
    if name:
        candidates = [base / name]
    else:
        candidates = sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []

    for folder in candidates:
        model = folder / "model.onnx"
        tags = folder / "selected_tags.csv"
        if model.exists() and tags.exists():
            return model, tags

    looked = ", ".join(str(c) for c in candidates) or f"{base}/*"
    raise SystemExit(
        f"no complete model found (looked in: {looked})\n"
        f"run: python tools/download_model.py"
    )


def collect_images(inputs: list[str]) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            found.append(resolved)

    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() and child.suffix.lower() in IMAGE_SUFFIXES:
                    add(child)
        elif path.is_file():
            add(path)
        else:
            matches = sorted(glob.glob(raw, recursive=True))
            for match in matches:
                p = Path(match)
                if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES:
                    add(p)
            if not matches:
                print(f"  ! no match for: {raw}", file=sys.stderr)
    return found


def fmt_conf(value: float) -> str:
    return f"{value:.3f}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("inputs", nargs="*", help="image files, directories, or globs")
    ap.add_argument("--model", default=None, help="model dir name under models/ (default: first complete one)")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="general tag threshold (default: 0.35)")
    ap.add_argument("--char-threshold", type=float, default=0.3, help="character threshold (default: 0.3; prod uses 0.5)")
    ap.add_argument("--top", type=int, default=12, help="how many general tags to print (default: 12)")
    ap.add_argument("--top-chars", type=int, default=5, help="how many characters to print (default: 5)")
    ap.add_argument("--json", default=None, help="also write full results to this JSON file")
    ap.add_argument("--info", action="store_true", help="print model/session info and exit")
    ap.add_argument("--device", default=None, help="force provider, e.g. DmlExecutionProvider or CPUExecutionProvider")
    args = ap.parse_args()

    model_path, tags_path = probe_model_dir(PROJECT_ROOT, args.model)

    providers = [args.device] if args.device else None
    print("loading model ...")
    t0 = time.monotonic()
    tagger = Wd14Tagger(model_path, tags_path, providers=providers)
    load_s = time.monotonic() - t0

    info = tagger.describe()
    print(f"  model     : {info['model']}")
    print(f"  tags csv  : {info['tags_csv']}  ({sum(info['tag_counts'].values())} rows)")
    print(f"  input     : {info['input']['name']} {info['input']['shape']}")
    print(f"  providers : {', '.join(info['providers'])}")
    print(f"  load time : {load_s:.1f}s  (includes 3x warmup)")
    counts = info["tag_counts"]
    print(
        f"  categories: general={counts.get(0, 0)}  character={counts.get(4, 0)}  "
        f"rating={counts.get(9, 0)}"
    )

    if args.info:
        return 0
    if not args.inputs:
        print("\nno inputs given -- pass image files/dirs/globs to tag")
        return 2

    images = collect_images(args.inputs)
    if not images:
        print("no images found", file=sys.stderr)
        return 1

    print(f"\ntagging {len(images)} image(s)  "
          f"[general>={args.threshold}, character>={args.char_threshold}]\n")

    payload: list[dict] = []
    started = time.monotonic()

    for index, path in enumerate(images, 1):
        try:
            with __import__("PIL.Image", fromlist=["Image"]).open(path) as handle:
                size = handle.size
                t1 = time.monotonic()
                result = tagger.interrogate(
                    handle, threshold=args.threshold, character_threshold=args.char_threshold
                )
                elapsed = time.monotonic() - t1
        except Exception as exc:  # noqa: BLE001
            print(f"[{index}/{len(images)}] {path.name}: FAILED -- {exc!r}")
            continue

        print(f"[{index}/{len(images)}] {path.name}  ({size[0]}x{size[1]}, {elapsed * 1000:.0f} ms)")

        rating = result.top_rating()
        rating_bits = "  ".join(
            f"{name}={fmt_conf(score)}" for name, score in sorted(result.ratings.items())
        )
        print(f"    rating     : {rating_bits or '(none)'}")
        if rating:
            print(f"    -> top     : {rating[0]} ({fmt_conf(rating[1])})")

        chars = result.sorted_characters(args.top_chars)
        if chars:
            print("    characters : " + ", ".join(f"{n} {fmt_conf(s)}" for n, s in chars))
        else:
            print(f"    characters : (none >= {args.char_threshold})")

        tags = result.sorted_tags(args.top)
        if tags:
            print("    tags       : " + ", ".join(f"{n} {fmt_conf(s)}" for n, s in tags))
        print()

        payload.append(
            {
                "path": str(path),
                "width": size[0],
                "height": size[1],
                "ms": round(elapsed * 1000),
                "ratings": result.ratings,
                "characters": result.characters,
                "tags": result.tags,
            }
        )

    total = time.monotonic() - started
    if payload:
        print(f"done: {len(payload)} image(s) in {total:.1f}s "
              f"({total / len(payload) * 1000:.0f} ms/image average)")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
