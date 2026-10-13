"""Build a small synthetic labelled set so train_feature_plugin.py can be tested.

Real validation needs the user's own artwork; this only proves the plumbing
(embedding extraction -> training -> ONNX export -> round-trip check). The three
classes are deliberately far apart in colour so a linear head can separate them.
"""

from __future__ import annotations

import random
import shutil
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1] / ".tmp" / "synth-labels"


def make(base_rgb, out_dir: Path, count: int, seed: int) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    for i in range(count):
        w = rng.randint(360, 640)
        h = rng.randint(360, 720)
        jitter = tuple(
            max(0, min(255, c + rng.randint(-30, 30))) for c in base_rgb
        )
        image = Image.new("RGB", (w, h), jitter)
        draw = ImageDraw.Draw(image)
        # A few blobs so the image is not perfectly flat (that trips the
        # example flat-image plugin and would be an unrealistic test).
        for _ in range(rng.randint(3, 8)):
            x, y = rng.randint(0, w), rng.randint(0, h)
            r = rng.randint(20, 90)
            tone = tuple(max(0, min(255, c + rng.randint(-60, 60))) for c in jitter)
            draw.ellipse([x, y, x + r, y + r], fill=tone)
        image.save(out_dir / f"{i:03d}.jpg", quality=88)


def main() -> None:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    make((200, 60, 60), ROOT / "Alpha", 12, seed=1)
    make((60, 80, 200), ROOT / "Beta", 12, seed=2)
    make((120, 120, 120), ROOT / "_background", 8, seed=3)
    total = sum(1 for _ in ROOT.rglob("*.jpg"))
    print(f"wrote {total} images under {ROOT}")


if __name__ == "__main__":
    main()
