"""Generate the four probe photos the on-device check asserts against.

The probes are plain copies of three pictures in ``samples/``, plus one of them
re-encoded as PNG. Nothing is written *into* them -- no tag, no metadata, not a
single byte of difference from the originals (except the PNG, whose whole point
is a different container). They exist so that ``tools\\verify_aves_tags.py``
has a fixed, known set of files to make its real-device assertions about:

    aves-probe-aya.jpg     Shameimaru Aya
    aves-probe-reimu.jpg   Hakurei Reimu
    aves-probe-reimu.png   Hakurei Reimu   (the same picture, PNG container)
    aves-probe-teto.jpg    Kasane Teto

Push the folder to the phone, run ``verify_aves_tags.py snapshot``, tag that
folder from Aves, then run ``verify_aves_tags.py verify``. The PNG is there
because the fork's tagging path has to be format-agnostic -- an Android
screenshot is a PNG.

No sentinel tag is planted any more: the tags come from the PC service and live
in Aves' own database, so there is nothing to put in the file. A probe that
carried a tag would in fact prove the opposite of what is being checked.

Usage:
    python tools\\make_aves_probe.py                 # -> .tmp\\aves-probe\\
    python tools\\make_aves_probe.py --out E:\\probe
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# sample file -> (character, series)
SAMPLES: dict[str, tuple[str, str]] = {
    "f52b341d2a083d34bc607a4dd6457545.jpg": ("Kasane Teto", "Vocaloid"),
    "7fe97f53b11ab42ba94084450e3e92d4.jpg": ("Shameimaru Aya", "Touhou Project"),
    "05acb463acd2427a65412bf1dbf9ceb5.jpg": ("Hakurei Reimu", "Touhou Project"),
}

# One of the above, re-encoded as PNG. Only one: a PNG of a full-size photo is
# several MB, and one is enough to cover the second container.
PNG_FROM = "05acb463acd2427a65412bf1dbf9ceb5.jpg"


def to_png(src: Path, dest: Path) -> None:
    from PIL import Image

    with Image.open(src) as im:
        im.convert("RGB").save(dest, "PNG", optimize=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--samples", default=str(ROOT / "samples"), help="source folder")
    ap.add_argument("--out", default=str(ROOT / ".tmp" / "aves-probe"), help="output folder")
    args = ap.parse_args(argv)

    samples = Path(args.samples)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    written = 0
    for name, (character, _series) in SAMPLES.items():
        src = samples / name
        if not src.is_file():
            print(f"skip (missing): {src}")
            continue
        # A plain copy under an obvious name, so it is easy to find on the phone.
        dest = out / f"aves-probe-{character.split()[-1].lower()}.jpg"
        shutil.copyfile(src, dest)
        print(f"{dest.name}: copy of {name} ({dest.stat().st_size:,d} B, bytes untouched)")
        written += 1

    if PNG_FROM in SAMPLES:
        src = samples / PNG_FROM
        character, _series = SAMPLES[PNG_FROM]
        if src.is_file():
            dest = out / f"aves-probe-{character.split()[-1].lower()}.png"
            to_png(src, dest)
            print(f"{dest.name}: PNG re-encode of {PNG_FROM} ({dest.stat().st_size:,d} B)")
            written += 1
        else:
            print(f"skip (missing): {src}")

    print()
    print(f"wrote {written} file(s) to {out}")
    print()
    print("Now, on the phone:")
    print("  1. push this folder somewhere the gallery scans")
    print("     (e.g. adb push ... /sdcard/DCIM/AlbumcatProbe)")
    print("  2. tools\\verify_aves_tags.py snapshot   -- record sha256/size/mtime")
    print("  3. open Aves and tag that folder with the AI button, then wait")
    print("  4. tools\\verify_aves_tags.py verify     -- tags present AND files unchanged")
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
