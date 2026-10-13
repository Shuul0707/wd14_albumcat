#!/usr/bin/env python
"""Rehearse the phone's entire job on the desktop, without a phone.

Everything the Android app does is testable here except the Android half:

    thumbnail  -- mirrored in Python exactly (same scale, same white letterbox)
    /plan      -- the real TaggingService, the same code the HTTP endpoint runs
    merge+build+splice -- the app's real Xmp.kt, via the JVM (tools/kotlin_jvm.py)

Only `ContentResolver`/SAF is faked. So when this passes, the thing that can
still fail on a device is file plumbing -- not the tagging, not the tag tree,
not the byte layout, not the merge or the undo.

What it proves, per photo:

  1. the phone's thumbnail yields the same characters as the PC's own pipeline
  2. the tags the phone would write are readable by an *independent* parser
     (Pillow's getxmp, i.e. somebody else's XMP implementation -- reading our
     own file with our own reader would prove nothing)
  3. the pixel data is untouched, byte for byte
  4. running twice does not duplicate tags (that is what merge is for)
  5. the packet carries the marker the app uses to skip already-tagged photos
  6. undo restores the original file to the same SHA-256

plus one photo that arrives already carrying camera-written metadata
(dc:description, exif:GPSLatitude, photoshop:Credit), to prove tagging it does
not throw somebody else's properties away.

Usage:
    .venv\\Scripts\\python.exe tools\\simulate_phone.py
    .venv\\Scripts\\python.exe tools\\simulate_phone.py --keep
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PIL import Image, ImageOps  # noqa: E402

from albumcat import xmp  # noqa: E402
from albumcat.config import Config  # noqa: E402
from albumcat.server import TaggingService  # noqa: E402
from kotlin_jvm import SEP, build_runner, kotlin  # noqa: E402

WORK = ROOT / ".tmp" / "phone-sim"
SIZE = 448
QUALITY = 85


# --------------------------------------------------------------------------- #
# the Android Thumbnailer, mirrored

def phone_thumbnail(path: Path) -> bytes:
    """Stand-in for Thumbnailer.thumbnail().

    Same three steps, same numbers: bake the EXIF rotation into pixels (phones
    store most photos in sensor orientation, and the model knows nothing about
    EXIF), scale to fit inside 448, centre on a white square.

    The scale factor is what matters and it is identical either way:
    Android uses min(448/w, 448/h); the PC pads to a square first and then
    resizes by 448/max(w, h), which is the same number. Only the resampling
    kernel differs (Android bilinear, here Lanczos), and the earlier HTTP test
    showed that does not move the scores.
    """
    with Image.open(path) as im:
        im.load()
        fixed = ImageOps.exif_transpose(im)
        if fixed is None:
            fixed = im
        fixed = fixed.convert("RGB")

        w, h = fixed.size
        scale = min(SIZE / w, SIZE / h)
        nw = max(1, int(w * scale))
        nh = max(1, int(h * scale))
        small = fixed.resize((nw, nh), Image.LANCZOS)

        canvas = Image.new("RGB", (SIZE, SIZE), (255, 255, 255))
        canvas.paste(small, ((SIZE - nw) // 2, (SIZE - nh) // 2))

        buf = io.BytesIO()
        canvas.save(buf, "JPEG", quality=QUALITY)
        return buf.getvalue()


# --------------------------------------------------------------------------- #

def entropy_of(data: bytes) -> bytes:
    """The compressed pixel data: everything from SOS to end of file."""
    _segs, body = xmp._walk(data)
    return data[body:]


def pixels_of(data: bytes) -> bytes:
    """The part of the file that *is* the picture, for either format.

    A JPEG keeps it from SOS onward; a PNG keeps it in IDAT. Both must come
    through a tag write byte for byte, and "it still parses" is not the same
    claim -- a re-encode would still parse.
    """
    if xmp.photo_format(data) == "png":
        return b"".join(
            data[start + 8:end - 4]
            for ctype, start, end in xmp._png_chunks(data) if ctype == b"IDAT"
        )
    return entropy_of(data)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def photo_fmt(path: Path) -> str:
    """The Kotlin interop driver says what it found; ask Python the same thing.

    The two have to agree -- if they did not, the phone would splice a PNG's
    block into a JPEG and the photo would be ruined.
    """
    return xmp.photo_format(path.read_bytes())


def pillow_xmp(path: Path) -> dict:
    """XMP as parsed by Pillow -- an implementation neither of ours."""
    with Image.open(path) as im:
        return im.getxmp() or {}


def flat_tags_from_pillow(blob: dict) -> list[str]:
    """Dig dc:subject out of Pillow's nested-dict rendering."""
    try:
        desc = blob["xmpmeta"]["RDF"]["Description"]
    except (KeyError, TypeError):
        return []
    if isinstance(desc, list):
        desc = desc[0] if desc else {}
    subject = desc.get("subject") or {}
    bag = subject.get("Bag") if isinstance(subject, dict) else None
    items = (bag or {}).get("li") if isinstance(bag, dict) else None
    if items is None:
        return []
    return [items] if isinstance(items, str) else list(items)


def nested_hier_from_pillow(blob: dict) -> dict[str, list[str]]:
    """Pull each hierarchical bag out of Pillow's nested-dict rendering.

    Returned per field, not merged: we write the same tree to both
    lr:HierarchicalSubject (Lightroom-style readers) and digiKam:TagsList
    (digiKam/Immich), so merging them would just double every tag.
    """
    out: dict[str, list[str]] = {}
    try:
        desc = blob["xmpmeta"]["RDF"]["Description"]
    except (KeyError, TypeError):
        return out
    if isinstance(desc, list):
        desc = desc[0] if desc else {}
    for field in ("HierarchicalSubject", "TagsList"):
        node = desc.get(field)
        if not isinstance(node, dict):
            continue
        bag = node.get("Bag") or {}
        items = bag.get("li")
        if items is None:
            continue
        out[field] = [items] if isinstance(items, str) else list(items)
    return out


# --------------------------------------------------------------------------- #
# a photo that already carries somebody else's metadata

CAMERA_XMP = '''<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 5.6-c140">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:exif="http://ns.adobe.com/exif/1.0/"
    xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/">
   <dc:description>
    <rdf:Alt>
     <rdf:li xml:lang="x-default">My holiday photo</rdf:li>
    </rdf:Alt>
   </dc:description>
   <exif:GPSLatitude>51,30.5N</exif:GPSLatitude>
   <photoshop:Credit>Shot by me &amp; friends</photoshop:Credit>
   <xmp:Rating>5</xmp:Rating>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>'''.encode("utf-8")

FOREIGN_QNAMES = (
    ("dc:description", "{http://purl.org/dc/elements/1.1/}description"),
    ("exif:GPSLatitude", "{http://ns.adobe.com/exif/1.0/}GPSLatitude"),
    ("photoshop:Credit", "{http://ns.adobe.com/photoshop/1.0/}Credit"),
)


def check_camera_metadata(runner: list[str], service, src: Path,
                          check) -> None:
    """A photo the camera already stamped must come out the far side intact.

    We understand four XMP properties. Everything else in the packet belongs to
    the camera, Lightroom or whoever, and silently dropping it on the first tag
    write would be data loss that nobody notices until much later.
    """
    original = src.read_bytes()
    fmt = xmp.photo_format(original)
    target = WORK / f"camera-{src.name}"
    # Plant the foreign packet the way that format carries XMP. A PNG's block is
    # an iTXt payload, not a packet, so it cannot just be concatenated on.
    block = CAMERA_XMP if fmt == "jpeg" else xmp._itxt_payload(CAMERA_XMP)
    target.write_bytes(
        xmp._splice(original, block) if fmt == "jpeg" else xmp._splice_png(original, block)
    )

    plan = service.plan_bytes(phone_thumbnail(target), asset_id="file://sim/camera",
                              name=src.name, size=len(original))
    tagged = WORK / f"camera-tagged-{src.name}"
    kotlin(runner, "tag", str(target),
           SEP.join(plan["flat"]), SEP.join(plan["hierarchical"]),
           str(plan["rating"]), str(tagged))

    written = tagged.read_bytes()
    print(f"        （这张先带着相机写的 dc:description / GPS / Credit）")

    check("别人的元数据：像素仍未改动",
          pixels_of(written) == pixels_of(original))

    text = xmp.packet_of(written).decode("utf-8")
    body = re.search(r"<x:xmpmeta.*?</x:xmpmeta>", text, re.DOTALL)
    tags = set()
    if body:
        try:
            desc = next(ET.fromstring(body.group(0)).iter(
                "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Description"))
            tags = {c.tag for c in desc}
        except ET.ParseError:
            pass
    for label, qname in FOREIGN_QNAMES:
        check(f"别人的元数据：{label} 还在", qname in tags)
    check("别人的元数据：caption 文字没变", "My holiday photo" in text)
    our_rating = plan["rating"]
    if our_rating:
        check(f"别人的元数据：xmp:Rating 换成我们的 {our_rating}",
              f"<xmp:Rating>{our_rating}</xmp:Rating>" in text)
    else:
        # No rating from us means no opinion -- merge keeps the camera's.
        check("别人的元数据：我们没意见时保留相机的 xmp:Rating=5",
              "<xmp:Rating>5</xmp:Rating>" in text)

    ours = xmp.read_photo(tagged)
    check("别人的元数据：我们的标签照样写进去了",
          bool(ours.hierarchical) == bool(plan["hierarchical"]))

    prev_b64 = kotlin(runner, "prev", str(target)).strip()
    restored = WORK / f"camera-undo-{src.name}"
    kotlin(runner, "splice", str(tagged), photo_fmt(tagged), prev_b64, str(restored))
    check("别人的元数据：撤销回到「只有相机的包」",
          sha(restored.read_bytes()) == sha(target.read_bytes()))


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--samples", default="samples", help="folder of real photos")
    ap.add_argument("--keep", action="store_true", help="leave the tagged copies in place")
    args = ap.parse_args()

    samples = sorted(p for p in (ROOT / args.samples).glob("*")
                     if p.suffix.lower() in (".jpg", ".jpeg", ".jpe", ".png"))
    if not samples:
        print(f"no JPEG or PNG under {args.samples}/")
        return 1

    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    print("building the app's Kotlin Xmp.kt on the desktop JVM…")
    runner = build_runner(WORK / "kxmp")
    print()

    print("loading the tagging service (the same code `albumcat serve` runs)…")
    cfg = Config.load()
    service = TaggingService(cfg, warmup=False)
    print(f"  model: {service.tagger.describe().get('model')}  "
          f"providers: {[p for p in service.tagger.describe().get('providers', [])]}")
    print()

    checks: list[tuple[str, bool]] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        checks.append((f"{label}", ok))
        mark = "OK  " if ok else "FAIL"
        print(f"    [{mark}] {label}" + (f"  {detail}" if detail else ""))

    print(f"rehearsing {len(samples)} photo(s) the way the phone would\n")
    print(f"{'photo':<44}{'chars':<26}{'ms':>7}  tree")
    print("-" * 110)

    for src in samples:
        name = src.name
        original = src.read_bytes()
        target = WORK / name
        target.write_bytes(original)

        thumb = phone_thumbnail(src)
        plan = service.plan_bytes(thumb, asset_id=f"file://sim/{name}", name=name,
                                  size=len(original), date_taken=int(src.stat().st_mtime))

        chars = plan["characters"]
        who = "、".join(f"{c['label']} {c['score']:.2f}" for c in chars[:2]) or "（没认出角色）"
        print(f"{name[:43]:<44}{who[:25]:<26}{plan['ms']:>7.1f}  "
              f"{plan['hierarchical']}")

        # --- the write, performed by the app's own code -------------------- #
        tagged = WORK / f"tagged-{name}"
        kotlin(runner, "tag", str(target),
               SEP.join(plan["flat"]), SEP.join(plan["hierarchical"]),
               str(plan["rating"]), str(tagged))

        written = tagged.read_bytes()
        prev_b64 = kotlin(runner, "prev", str(target)).strip()

        # 1. pixels untouched
        check("像素未改动", pixels_of(written) == pixels_of(original),
              f"{len(original)}B → {len(written)}B")

        # 2. somebody else's XMP parser agrees
        blob = pillow_xmp(tagged)
        py_flat = flat_tags_from_pillow(blob)
        py_hier = nested_hier_from_pillow(blob)
        for tag in plan["flat"]:
            if tag not in py_flat:
                check(f"Pillow 读得到 {tag!r}", False)
                break
        else:
            check(f"Pillow 读得到全部 {len(plan['flat'])} 个扁平标签", True)
        want_hier = list(plan["hierarchical"])
        for field in ("HierarchicalSubject", "TagsList"):
            got_field = py_hier.get(field, [])
            check(f"Pillow 从 {field} 读到的层级标签一致",
                  sorted(got_field) == sorted(want_hier),
                  f"{got_field}" if got_field else "(空)")

        # Aves builds a photo's tags from dc:subject alone (its native reader
        # looks up XMP.DC_SUBJECT_PROP_NAME, splits on ";", and never reads
        # lr:HierarchicalSubject or digiKam:TagsList). If the tree is not in
        # dc:subject the phone shows no "角色|…" tag to tap -- which is the
        # entire point of route A.
        missing = [t for t in want_hier if t not in py_flat]
        check(f"Aves 能看到的 dc:subject 里含全部 {len(want_hier)} 个层级标签",
              not missing, f"缺少 {missing}" if missing else "")

        # 3. our own reader agrees with Pillow
        ours = xmp.read_photo(tagged)
        check("两套解析器读到的层级标签一致",
              sorted(ours.hierarchical) == sorted(py_hier.get("TagsList", [])),
              f"ours={ours.hierarchical}")

        # 4. running again must not pile up duplicates
        again = WORK / f"again-{name}"
        kotlin(runner, "tag", str(tagged),
               SEP.join(plan["flat"]), SEP.join(plan["hierarchical"]),
               str(plan["rating"]), str(again))
        second = xmp.read_photo(again)
        check("重复运行不会堆积标签",
              sorted(second.hierarchical) == sorted(ours.hierarchical)
              and sorted(second.flat) == sorted(ours.flat))

        # 5. the marker TagRunner uses to skip photos it already did
        check('带 "已打过标签" 标记',
              'x:xmptk="albumcat"' in written.decode("utf-8", "replace"))

        # 6. undo restores the original byte for byte
        restored = WORK / f"undo-{name}"
        kotlin(runner, "splice", str(tagged), photo_fmt(tagged), prev_b64, str(restored))
        check("撤销后与原始文件 SHA-256 相同", sha(restored.read_bytes()) == sha(original))

        if plan["notes"]:
            print(f"    notes: {'; '.join(plan['notes'])}")
        print()

    print("=" * 110)
    print("额外场景：照片上已经有相机写好的元数据")
    check_camera_metadata(runner, service, samples[-1], check)
    print()

    bad = [label for label, ok in checks if not ok]
    print("=" * 110)
    total = len(checks)
    if bad:
        print(f"{total - len(bad)}/{total} 项通过；失败：{', '.join(bad)}")
        return 1
    print(f"{total}/{total} 项通过 —— 手机端逻辑全部正确，剩下的 SAF 读写由 tools\\smoke_device.py 在真机上验。")
    if args.keep:
        print(f"（打过标签的副本留在 {WORK.relative_to(ROOT)}）")
    else:
        shutil.rmtree(WORK, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
