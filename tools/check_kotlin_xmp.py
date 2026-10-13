#!/usr/bin/env python
"""Cross-check the Android XMP writer against the Python one.

`android/.../Xmp.kt` is a port of `src/albumcat/xmp.py`. A port that is merely
"close" is a trap: the phone would write one set of bytes and the PC would read
a slightly different set, and the mismatch would only show up on a real phone.

The compilation and JVM plumbing live in `tools/kotlin_jvm.py` (shared with
`simulate_phone.py`); this script is the comparison itself:

  * buildPacket  -- same input must produce the same bytes
  * splice       -- same JPEG + same packet must produce the same file
  * parsePacket  -- each side must read back what the other wrote
  * PNG          -- same thing for the iTXt chunk, which is *not* a packet

Usage:
    .venv\\Scripts\\python.exe tools\\check_kotlin_xmp.py
"""

from __future__ import annotations

import base64
import re
import struct
import sys
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from albumcat import xmp  # noqa: E402
from kotlin_jvm import SEP, build_runner, kotlin  # noqa: E402

WORK = ROOT / ".tmp" / "kxmp"

# A packet in the shape a camera or Lightroom would leave behind: properties we
# do not understand, in namespaces our own packet never declares.
FOREIGN = '''<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
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


# --------------------------------------------------------------------------- #

def check_build(runner: list[str]) -> list[str]:
    failures = []
    cases = [
        ("empty", [], [], None),
        ("tags only", ["1girl", "long hair"], [], None),
        ("full", ["Kasane Teto", "1girl", "Vocaloid"],
         ["角色|Kasane Teto", "作品|Vocaloid"], 4),
        ("escapes", ['a & b', '<tag>', '"quoted"', "5 > 3"], ["角色|a & b"], 3),
        ("unicode", ["初音未来", "角色|初音未来"], ["角色|初音未来", "作品|Vocaloid"], 5),
        ("dupes", ["x", "x", "y"], ["角色|x", "角色|x"], 2),
        ("rating 0", ["z"], [], 0),
    ]
    for name, flat, hier, rating in cases:
        want = base64.b64encode(
            xmp.build_packet(flat=flat, hierarchical=hier, rating=rating)
        ).decode()
        got = kotlin(runner, "build", SEP.join(flat), SEP.join(hier),
                     "null" if rating is None else str(rating))
        ok = want == got
        print(f"  build  {name:<12} {'OK' if ok else 'MISMATCH'}")
        if not ok:
            failures.append(f"build/{name}")
            py = base64.b64decode(want).decode("utf-8", "replace")
            kt = base64.b64decode(got).decode("utf-8", "replace")
            print(f"    python: {py[:200]!r}")
            print(f"    kotlin: {kt[:200]!r}")
    return failures


def check_splice(runner: list[str], sample: Path) -> list[str]:
    failures = []
    original = sample.read_bytes()

    packets = {
        "with-tags": xmp.build_packet(
            flat=["Kasane Teto", "1girl"],
            hierarchical=["角色|Kasane Teto", "作品|Vocaloid"],
            rating=4,
        ),
        "remove": None,
    }
    for name, packet in packets.items():
        ref = WORK / f"py-{name}.jpg"
        ref.write_bytes(xmp._splice(original, packet))

        pack_b64 = "-" if packet is None else base64.b64encode(packet).decode()
        got = WORK / f"kt-{name}.jpg"
        kotlin(runner, "splice", str(sample), "jpeg", pack_b64, str(got))

        same = ref.read_bytes() == got.read_bytes()
        # Whatever else happens, the pixels must not move.
        body_same = _entropy(ref.read_bytes()) == _entropy(original)
        print(f"  splice {name:<12} {'OK' if same else 'MISMATCH'}"
              f"  (entropy preserved: {body_same})")
        if not same:
            failures.append(f"splice/{name}")
            a, b = ref.read_bytes(), got.read_bytes()
            print(f"    python {len(a)}B  kotlin {len(b)}B")
            for i in range(min(len(a), len(b))):
                if a[i] != b[i]:
                    print(f"    first diff at {i}: py={a[i]:#x} kt={b[i]:#x}")
                    print(f"    py  ...{a[max(0,i-60):i+60]!r}")
                    print(f"    kt  ...{b[max(0,i-60):i+60]!r}")
                    break
    return failures


def _entropy(data: bytes) -> bytes:
    segs, body = xmp._walk(data)
    return data[body:]


def check_readback(runner: list[str], sample: Path) -> list[str]:
    """Each side must read what the other wrote."""
    failures = []

    # Python writes -> Kotlin reads
    py_written = WORK / "py-written.jpg"
    py_written.write_bytes(sample.read_bytes())
    xmp.write_jpeg(
        py_written, flat=["Kasane Teto", "1girl"],
        hierarchical=["角色|Kasane Teto", "作品|Vocaloid"], rating=4,
    )
    got = kotlin(runner, "read", str(py_written))
    print(f"  kotlin reads python's file : {got!r}")
    if "Kasane Teto" not in got or "角色|Kasane Teto" not in got or not got.endswith("\t4"):
        failures.append("readback/py->kt")

    # Kotlin writes -> Python reads
    packet = base64.b64decode(kotlin(
        runner, "build",
        SEP.join(["Kasane Teto", "1girl"]),
        SEP.join(["角色|Kasane Teto", "作品|Vocaloid"]),
        "4",
    ))
    kt_written = WORK / "kt-written.jpg"
    kotlin(runner, "splice", str(sample), "jpeg",
           base64.b64encode(packet).decode(), str(kt_written))
    fields = xmp.read_jpeg(kt_written)
    print(f"  python reads kotlin's file : flat={fields.flat}")
    print(f"                               hier={fields.hierarchical} rating={fields.rating}")
    if "Kasane Teto" not in fields.flat or "角色|Kasane Teto" not in fields.hierarchical:
        failures.append("readback/kt->py")
    if fields.rating != 4:
        failures.append("readback/kt->py/rating")

    return failures


def check_foreign(runner: list[str], sample: Path) -> list[str]:
    """A packet somebody else wrote must survive both implementations intact.

    Phone cameras, Lightroom and screenshot tools all write into the same XMP
    packet we do. We only understand four properties, so the rest has to be
    carried through verbatim -- and carried through *the same way* by both
    implementations, since the phone writes and the PC reads.
    """
    failures = []
    planted = WORK / "foreign.jpg"
    planted.write_bytes(xmp._splice(sample.read_bytes(), FOREIGN))

    def repr_(parts) -> str:
        head = SEP.join(f"{p}={u}" for p, u in parts.namespaces)
        tail = "".join(
            "\n" + base64.b64encode(c.encode("utf-8")).decode() for c in parts.children
        )
        return head + tail

    want = repr_(xmp.foreign_parts(xmp.find_in_jpeg(planted.read_bytes())))
    got = kotlin(runner, "foreign", str(planted))
    ok = want == got
    print(f"  foreignParts agree        {'OK' if ok else 'MISMATCH'}")
    if not ok:
        failures.append("foreign/parts")
        print(f"    python: {want!r}")
        print(f"    kotlin: {got!r}")

    flat = ["Kasane Teto", "1girl"]
    hier = ["角色|Kasane Teto", "作品|Vocaloid"]

    py_out = WORK / "foreign-py.jpg"
    py_out.write_bytes(planted.read_bytes())
    xmp.write_jpeg(py_out, flat=flat, hierarchical=hier, rating=4)

    kt_out = WORK / "foreign-kt.jpg"
    kotlin(runner, "tag", str(planted), SEP.join(flat), SEP.join(hier), "4", str(kt_out))

    same = py_out.read_bytes() == kt_out.read_bytes()
    print(f"  tag with foreign XMP      {'OK' if same else 'MISMATCH'}")
    if not same:
        failures.append("foreign/tag")
        a, b = py_out.read_bytes(), kt_out.read_bytes()
        print(f"    python {len(a)}B  kotlin {len(b)}B")
        for i in range(min(len(a), len(b))):
            if a[i] != b[i]:
                print(f"    first diff at {i}: py={a[i]:#x} kt={b[i]:#x}")
                print(f"    py  ...{a[max(0, i - 60):i + 60]!r}")
                print(f"    kt  ...{b[max(0, i - 60):i + 60]!r}")
                break

    # And the point of the whole exercise: somebody else's properties are still
    # in there, in their original namespaces.
    text = xmp.find_in_jpeg(kt_out.read_bytes()).decode("utf-8")
    body = re.search(r"<x:xmpmeta.*?</x:xmpmeta>", text, re.DOTALL).group(0)
    try:
        desc = next(ET.fromstring(body).iter(
            "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Description"))
        tags = {c.tag for c in desc}
    except Exception as exc:                      # noqa: BLE001
        tags, body = set(), repr(exc)
    for label, qname in (
        ("dc:description", "{http://purl.org/dc/elements/1.1/}description"),
        ("exif:GPSLatitude", "{http://ns.adobe.com/exif/1.0/}GPSLatitude"),
        ("photoshop:Credit", "{http://ns.adobe.com/photoshop/1.0/}Credit"),
    ):
        present = qname in tags
        print(f"  survived {label:<18} {'OK' if present else 'LOST'}")
        if not present:
            failures.append(f"foreign/lost/{label}")
    return failures


def make_png(width: int = 8, height: int = 8) -> bytes:
    """A small, valid greyscale PNG, built chunk by chunk by hand.

    No Pillow here: this fixture has to stay identical between runs, and the
    only thing under test is what the *writers* do to it.
    """
    raw = b"".join(
        b"\x00" + bytes((x * 31 + y * 17) % 256 for x in range(width))
        for y in range(height)
    )
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        xmp.PNG_SIG
        + xmp._png_chunk(b"IHDR", ihdr)
        + xmp._png_chunk(b"IDAT", zlib.compress(raw))
        + xmp._png_chunk(b"IEND", b"")
    )


def _idat(data: bytes) -> bytes:
    """Every IDAT chunk's payload, concatenated. Pixels live in here."""
    return b"".join(
        data[start + 8:end - 4] for ctype, start, end in xmp._png_chunks(data)
        if ctype == b"IDAT"
    )


def _first_diff(a: bytes, b: bytes) -> str:
    if a == b:
        return ""
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return (f"\n    python {len(a)}B  kotlin {len(b)}B"
                    f"\n    first diff at {i}: py={a[i]:#x} kt={b[i]:#x}"
                    f"\n    py  ...{a[max(0, i - 60):i + 60]!r}"
                    f"\n    kt  ...{b[max(0, i - 60):i + 60]!r}")
    return f"\n    python {len(a)}B  kotlin {len(b)}B (one is a prefix of the other)"


def check_png(runner: list[str], sample: Path) -> list[str]:
    """The PNG half of the port.

    PNG is where the two implementations are most able to drift apart quietly,
    because the block a PNG carries is *not* a packet: it is an iTXt payload
    with a keyword, a compression flag, a method and two empty text fields in
    front of the XML. A writer that gets those four bytes wrong still reads back
    correctly through our own reader -- the extra NUL just sits in front of
    `<?xpacket` -- and only a real parser objects. So this section checks both
    halves: byte equality with Python, and a third, independent reader.
    """
    failures = []
    png = make_png()
    plain = WORK / "plain.png"
    plain.write_bytes(png)

    # -- the format probe reads headers, not names -------------------------- #
    got = kotlin(runner, "format", str(plain)).strip()
    py = xmp.photo_format(png)
    print(f"  format  png                {'OK' if got == py == 'png' else 'MISMATCH'}"
          f"  (python {py!r}, kotlin {got!r})")
    if got != py or got != "png":
        failures.append("png/format")

    got = kotlin(runner, "format", str(sample)).strip()
    py = xmp.photo_format(sample.read_bytes())
    print(f"  format  jpeg               {'OK' if got == py == 'jpeg' else 'MISMATCH'}"
          f"  (python {py!r}, kotlin {got!r})")
    if got != py or got != "jpeg":
        failures.append("png/format-jpeg")

    # -- splice: byte-for-byte, including removal --------------------------- #
    packets = {
        "with-tags": xmp.build_packet(
            flat=["Kasane Teto", "1girl"],
            hierarchical=["角色|Kasane Teto", "作品|Vocaloid"],
            rating=4,
        ),
        "remove": None,
    }
    for name, packet in packets.items():
        block = None if packet is None else xmp._itxt_payload(packet)
        ref = WORK / f"py-{name}.png"
        ref.write_bytes(xmp._splice_png(png, block))

        pack_b64 = "-" if packet is None else base64.b64encode(packet).decode()
        kt = WORK / f"kt-{name}.png"
        kotlin(runner, "splice", str(plain), "png", pack_b64, str(kt))

        same = ref.read_bytes() == kt.read_bytes()
        pixels = _idat(kt.read_bytes()) == _idat(png)
        print(f"  splice  {name:<12} {'OK' if same else 'MISMATCH'}"
              f"  (pixels preserved: {pixels})")
        if not same:
            failures.append(f"png/splice/{name}")
            print("   " + _first_diff(ref.read_bytes(), kt.read_bytes()))
        if not pixels:
            failures.append(f"png/splice/{name}/pixels")

    # -- the dispatcher really dispatches ----------------------------------- #
    packet = packets["with-tags"]
    ref = WORK / "py-dispatch.jpg"
    ref.write_bytes(xmp._splice(sample.read_bytes(), packet))
    kt = WORK / "kt-dispatch.jpg"
    kotlin(runner, "splice", str(sample), "jpeg",
           base64.b64encode(packet).decode(), str(kt))
    same = ref.read_bytes() == kt.read_bytes()
    print(f"  spliceBlock  jpeg path     {'OK' if same else 'MISMATCH'}")
    if not same:
        failures.append("png/dispatch-jpeg")

    # -- cross-readback ------------------------------------------------------ #
    py_written = WORK / "py-written.png"
    py_written.write_bytes(png)
    xmp.write_png(
        py_written, flat=["Kasane Teto", "1girl"],
        hierarchical=["角色|Kasane Teto", "作品|Vocaloid"], rating=4,
    )
    got = kotlin(runner, "read", str(py_written))
    print(f"  kotlin reads python's png  : {got!r}")
    if "Kasane Teto" not in got or "角色|Kasane Teto" not in got or not got.endswith("\t4"):
        failures.append("png/readback/py->kt")

    kt_written = WORK / "kt-written.png"
    kotlin(runner, "tag", str(plain), SEP.join(["Kasane Teto", "1girl"]),
           SEP.join(["角色|Kasane Teto", "作品|Vocaloid"]), "4", str(kt_written))
    fields = xmp.read_png(kt_written)
    print(f"  python reads kotlin's png  : flat={fields.flat}")
    print(f"                               hier={fields.hierarchical} rating={fields.rating}")
    if "Kasane Teto" not in fields.flat or "角色|Kasane Teto" not in fields.hierarchical:
        failures.append("png/readback/kt->py")
    if fields.rating != 4:
        failures.append("png/readback/kt->py/rating")

    # -- and somebody who is neither of us ----------------------------------- #
    # Byte equality between two ports of one writer proves they agree; it does
    # not prove they agree with the spec. An extra NUL in the iTXt header would
    # satisfy every check above and still be rejected by Aves.
    try:
        from PIL import Image
    except ImportError:
        print("  pillow reads kotlin's png   SKIPPED (no Pillow)")
    else:
        with Image.open(kt_written) as im:
            meta = im.getxmp()
            size = im.size
            px = im.convert("RGB").getpixel((3, 5))
        desc = meta.get("xmpmeta", {}).get("RDF", {}).get("Description", {})
        subjects = as_list(desc.get("subject"))
        print(f"  pillow reads kotlin's png  : size={size} pixel(3,5)={px}")
        print(f"                               subject={subjects}")
        if size != (8, 8):
            failures.append("png/pillow/size")
        if "Kasane Teto" not in subjects:
            failures.append("png/pillow/subject")
        if px != ((3 * 31 + 5 * 17) % 256,) * 3:
            failures.append("png/pillow/pixels")

    return failures


def as_list(value) -> list[str]:
    """Pillow collapses a one-item rdf:Bag into a bare string."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    items = value.get("Bag", {}).get("li", []) if isinstance(value, dict) else value
    return [items] if isinstance(items, str) else list(items)


def main() -> int:
    samples = sorted((ROOT / "samples").glob("*.jpg"))
    if not samples:
        return int(bool(print("no JPEG in samples/")))
    sample = samples[0]
    print(f"sample: {sample.relative_to(ROOT)}  ({sample.stat().st_size} bytes)\n")

    runner = build_runner(WORK)
    print()

    failures: list[str] = []
    print("1. buildPacket bytes")
    failures += check_build(runner)
    print("\n2. splice into a real JPEG")
    failures += check_splice(runner, sample)
    print("\n3. cross-readback")
    failures += check_readback(runner, sample)
    print("\n4. somebody else's XMP survives")
    failures += check_foreign(runner, sample)
    print("\n5. PNG (XMP lives in an iTXt chunk, not a packet)")
    failures += check_png(runner, sample)

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("All checks passed -- the Kotlin XMP writer is byte-identical to the Python one.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
