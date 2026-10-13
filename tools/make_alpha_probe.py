# -*- coding: utf-8 -*-
"""Build a transparent-background PNG probe for ThumbnailFetcher.getByDecoding().

Every near-white pixel of the Reimu sample becomes alpha=0, so the file is a
"cut-out on transparent background" in the shape Android galleries are full of.
With the white flattening the phone re-creates essentially the original image, so
WD14 should return the same tag as the opaque probe (角色|Hakurei Reimu); if the
alpha were dropped the way plain RGB_565 decoding does, the background -- and the
white patches inside the character -- would come out black instead.
"""
from pathlib import Path

from PIL import Image

ROOT = Path(r"E:\AlbumCategories")
SRC = ROOT / "samples" / "05acb463acd2427a65412bf1dbf9ceb5.jpg"
DEST = ROOT / ".tmp" / "aves-probe" / "zz-alpha-reimu.png"

im = Image.open(SRC).convert("RGB")
w, h = im.size
src = im.load()
out = Image.new("RGBA", (w, h))
dst = out.load()
cleared = 0
for y in range(h):
    for x in range(w):
        r, g, b = src[x, y]
        if r >= 245 and g >= 245 and b >= 245:
            dst[x, y] = (255, 255, 255, 0)
            cleared += 1
        else:
            dst[x, y] = (r, g, b, 255)
out.save(DEST, "PNG", optimize=True)
print("wrote %s: %dx%d cleared=%d (%.1f%%) %d B"
      % (DEST.name, w, h, cleared, 100.0 * cleared / (w * h), DEST.stat().st_size))

ctrl = Image.new("RGB", (w, h), (255, 255, 255))
ctrl.paste(out, (0, 0), out)
cpath = DEST.with_name("zz-alpha-control.png")
ctrl.save(cpath, "PNG", optimize=True)
s = sum(abs(a - b) for a, b in zip(im.tobytes(), ctrl.tobytes()))
n = len(im.tobytes())
print("white-flattened control vs original: sum|diff|=%d over %d bytes (mean %.3f)"
      % (s, n, s / n))
