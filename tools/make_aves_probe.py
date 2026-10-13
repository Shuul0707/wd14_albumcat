"""Generate probe photos to answer one question:

    Does Aves actually read the XMP tags we write into a JPEG, and into a PNG?

Everything else in route A is already verified on the PC side (the bytes are
correct, the pixels never change, undo restores the original SHA256). The one
thing a desktop test *cannot* prove is whether an Android gallery app honours
the tags -- Aves might read XMP, might read only sidecars, or might ignore
both. So we plant an unmistakable sentinel tag:

    角色|AVES-PROBE-1234

Why that exact shape: reading Aves' source (develop, Aug 2025) settled which
field matters. `MetadataFetchHandler.kt` fills its `xmpSubjects` metadata entry
from `XMP.DC_SUBJECT_PROP_NAME` -- that is, ``dc:subject`` -- and joins the
array items with ``";"``; the Dart side then does
``xmpSubjects.split(';')`` (`entry.dart`). Searching the whole `android/` and
`lib/` trees for `Hierarchical` returns nothing: Aves never reads
``lr:HierarchicalSubject`` or ``digiKam:TagsList``. So ``dc:subject`` is the
only channel, and that is what this probe writes. (The serving pipeline does
the same thing via ``PublishConfig.tree_in_subject``.)

If Aves shows that tag, route A works end to end. If it does not, no amount
of Android app code will help and the design needs to change.

The probe writes both a JPEG and a PNG, because on the Aves side they are two
*different* code paths: the JPEG carries XMP in an ``APP1`` segment, the PNG in
an ``iTXt`` chunk, and ``MetadataFetchHandler.kt`` reaches them through separate
branches ("PNG Textual Data" for the latter). A JPEG-only probe would leave the
half we care about most untested -- an Android screenshot is a PNG.

Usage:
    python tools\\make_aves_probe.py                 # -> .tmp\\aves-probe\\
    python tools\\make_aves_probe.py --out E:\\probe
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from albumcat import xmp  # noqa: E402

SENTINEL = "AVES-PROBE-1234"

# sample file -> (character, series)
SAMPLES: dict[str, tuple[str, str]] = {
    "f52b341d2a083d34bc607a4dd6457545.jpg": ("Kasane Teto", "Vocaloid"),
    "7fe97f53b11ab42ba94084450e3e92d4.jpg": ("Shameimaru Aya", "Touhou Project"),
    "97e5e12787c3d0b146edd718e57e6d7c.jpg": ("Hakurei Reimu", "Touhou Project"),
}

# One of the above, re-encoded as PNG. Only one: a PNG of a full-size photo is
# several MB, and one is enough to answer the question. See the module docstring
# for why a JPEG-only probe is not enough.
PNG_FROM = "97e5e12787c3d0b146edd718e57e6d7c.jpg"


def probe_fields(character: str, series: str) -> tuple[list[str], list[str]]:
    """The exact flat/hierarchical shape ``plan_to_xmp_fields`` produces.

    Tree paths go in ``dc:subject`` (so Aves can show them) *and* in
    ``lr:HierarchicalSubject`` / ``digiKam:TagsList`` (so digiKam and Lightroom
    group by them), while the bare names stay in ``dc:subject`` so plain keyword
    search still finds them.
    """
    hierarchical = [f"角色|{SENTINEL}", f"角色|{character}", f"作品|{series}"]
    leaf = [SENTINEL, character, series, "1girl"]
    return [*leaf, *hierarchical], hierarchical


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
    for name, (character, series) in SAMPLES.items():
        src = samples / name
        if not src.is_file():
            print(f"skip (missing): {src}")
            continue
        # Use an obviously-named copy so it is easy to find on the phone.
        dest = out / f"aves-probe-{character.split()[-1].lower()}.jpg"
        shutil.copyfile(src, dest)

        flat, hierarchical = probe_fields(character, series)
        fields = xmp.write_jpeg(
            dest,
            flat=flat,
            hierarchical=hierarchical,
            rating=4,
            merge=True,
        )
        print(f"{dest.name}: dc:subject has {len(fields.flat)} items, rating={fields.rating}")
        written += 1

    if PNG_FROM in SAMPLES:
        src = samples / PNG_FROM
        character, series = SAMPLES[PNG_FROM]
        if src.is_file():
            dest = out / f"aves-probe-{character.split()[-1].lower()}.png"
            to_png(src, dest)
            flat, hierarchical = probe_fields(character, series)
            fields = xmp.write_png(
                dest,
                flat=flat,
                hierarchical=hierarchical,
                rating=4,
                merge=True,
            )
            print(f"{dest.name}: dc:subject has {len(fields.flat)} items, rating={fields.rating}")
            written += 1
        else:
            print(f"skip (missing): {src}")

    print()
    print(f"wrote {written} file(s) to {out}")
    print()
    print("Now, on the phone:")
    print(f"  1. copy these files into a folder the gallery scans (e.g. DCIM/Camera)")
    print(f"  2. open Aves, wait for it to index, open the Tags page")
    print(f"  3. look for a tag  {SENTINEL}  under  角色")
    print()
    print("Seeing it means Aves honours embedded XMP -> route A is sound.")
    print("Not seeing it means we need a different delivery mechanism.")
    print("Check the .jpg and the .png separately -- if only the JPEG shows the")
    print("tag, then screenshots stay unclassified and the PNG path needs work.")
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
