"""Write degraded copies of every *training* image, for blur robustness.

    tools\\make_blur_copies.py

Real-world phones hold pictures that have been through a screenshot pipeline, a
chat app's re-compressor or a sticker maker: soft, blocky, small.  The head was
trained on clean downloads, so "same picture, resaved badly" was effectively a new
distribution it had never seen.

This tool writes one degraded copy for every other training row (``--every 1`` for
all of them) and a manifest that adds those copies to the train split only.
Alternating between two degradations keeps the augmentation from teaching one
specific artifact:

* even rows — Gaussian blur, sigma 0.7–1.6 (from a hash of the file name, so runs
  are reproducible) plus JPEG q65;
* odd rows — downscale to 45% and back up (resampling blur) plus JPEG q70.

Validation rows are never copied: a blurred twin of a validation image would make
the validation number meaningless.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageFilter

CLASSES = ("插画", "梗图", "照片", "截图")


def sigma_for(name: str, strength: str = "mild") -> float:
    """Stable-per-file blur strength: 0.7-1.6 (mild) or 1.2-2.4 (strong)."""
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()
    base = 1.2 if strength == "strong" else 0.7
    return base + (int(digest[:4], 16) % 10) * (0.12 if strength == "strong" else 0.1)


def degrade(path: Path, index: int, quality: int = 65,
            strength: str = "mild") -> Image.Image | None:
    try:
        with Image.open(path) as image:
            rgb = image.convert("RGB")
    except Exception:  # noqa: BLE001 - a broken file should not stop the run
        return None
    if index % 2 == 0:
        out = rgb.filter(ImageFilter.GaussianBlur(radius=sigma_for(path.name, strength)))
    else:
        ratio = 0.30 if strength == "strong" else 0.45
        small = rgb.resize((max(1, int(rgb.width * ratio)), max(1, int(rgb.height * ratio))),
                           Image.BILINEAR)
        out = small.resize(rgb.size, Image.BILINEAR)
    return out


def one(row: dict, out: Path, quality: int, strength: str = "mild") -> dict | None:
    src = Path(row["path"])
    name = f"{src.stem}__blur{src.suffix.lower()}"
    dst = out / row["labels"][0] / name
    if dst.exists():
        return {**row, "path": str(dst).replace("\\", "/"),
                "source": f"{row['source']}+blur"}
    image = degrade(src, len(name), quality=quality, strength=strength)
    if image is None:
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    image.save(dst, "JPEG", quality=quality, optimize=True)
    return {**row, "path": str(dst).replace("\\", "/"),
            "source": f"{row['source']}+blur", "split": "train"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path,
                        default=Path("data/datasets/kind6/manifest.jsonl"))
    parser.add_argument("--out", type=Path, default=Path("data/datasets/kind6_blur"))
    parser.add_argument("--out-manifest", type=Path, default=None,
                        help="default: <out>/manifest.jsonl (originals + copies)")
    parser.add_argument("--quality", type=int, default=65)
    parser.add_argument("--every", type=int, default=2,
                        help="degrade every Nth training row (1 = all of them); "
                             "a 1:1 mix of clean and degraded rows dilutes the clean signal")
    parser.add_argument("--strength", choices=("mild", "strong"), default="mild",
                        help="strong = sigma 1.2-2.4, 30%% downscale, q45")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=0, help="stop after N copies (debugging)")
    args = parser.parse_args(argv)

    if not args.manifest.exists():
        raise SystemExit(f"no manifest: {args.manifest}")
    rows = [json.loads(line) for line in
            args.manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    train = [row for row in rows if row.get("split") == "train"]
    if args.every > 1:
        train = train[:: args.every]
    if args.limit:
        train = train[: args.limit]
    print(f"{len(rows)} row(s), {len(train)} training row(s) to degrade -> {args.out}")

    extra: list[dict] = []
    failed = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(one, row, args.out, args.quality) for row in train]
        for index, future in enumerate(futures, 1):
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {type(exc).__name__}: {exc}")
                result = None
            if result is None:
                failed += 1
            else:
                extra.append(result)
            if index % 200 == 0:
                print(f"  {index}/{len(train)} degraded, {failed} failed")

    merged = rows + extra
    out_manifest = args.out_manifest or (args.out / "manifest.jsonl")
    out_manifest.parent.mkdir(parents=True, exist_ok=True)
    out_manifest.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in merged) + "\n",
        encoding="utf-8",
    )
    counts: dict[str, int] = {}
    for row in merged:
        key = row["source"]
        counts[key] = counts.get(key, 0) + 1
    print(f"\n  wrote {len(extra)} copy(ies), {failed} failed")
    print(f"  manifest: {len(merged)} row(s) -> {out_manifest}")
    for key in sorted(counts):
        print(f"    {key:26s} {counts[key]:5d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
