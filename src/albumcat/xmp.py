"""Read and write XMP tags inside JPEG and PNG files -- the core of "方案A".

Why XMP: it is the only tag vocabulary that third-party galleries agree on. A
folder rename only works if you move files (not acceptable here), and every
gallery keeps its own private database. XMP is the interchange format that
Lightroom, digiKam, Immich, exiftool and Aves all read.

Where the tags go, and why each one is written:

* ``dc:subject`` -- the flat keyword list. The oldest, most widely understood.
* ``lr:HierarchicalSubject`` -- Lightroom-style ``角色|初音未来`` paths. This is
  what Lightroom, digiKam and Immich read to build a *tag tree*, which is the
  whole point: ``角色`` becomes the group, the character the leaf.
* ``digiKam:TagsList`` -- same content; Immich prefers this one.
* ``xmp:Rating`` -- 0-5 stars.

Writing all four costs nothing and means one file works everywhere.
(Aves is the exception: it only reads ``dc:subject``, so
:func:`albumcat.publishers.plan_to_xmp_fields` copies the hierarchical paths
into the flat list too. See ``publish.tree_in_subject``.)

Layout inside a JPEG::

    FFD8  SOI
    FFE0  APP0  JFIF      (kept, must stay first)
    FFE1  APP1  Exif      (kept)
    FFE1  APP1  "http://ns.adobe.com/xap/1.0/\0" + <xpacket>...   <- ours
    ...   other segments, unchanged, byte for byte
    FFDA  SOS + entropy-coded data ... FFD9 EOI

Layout inside a PNG -- XMP goes in one ``iTXt`` chunk::

    89504E47 0D0A1A0A  signature
    IHDR                (kept, must stay first)
    iTXt  keyword "XML:com.adobe.xmp" + <xpacket>...              <- ours
    ...   every other chunk, IDAT included, unchanged, byte for byte
    IEND

Nothing outside those one or two metadata blocks is touched, so the image data
is bit-identical either way.
"""

from __future__ import annotations

import os
import re
import tempfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

__all__ = [
    "XmpFields",
    "build_packet",
    "parse_packet",
    "photo_format",
    # JPEG, on its own
    "find_in_jpeg",
    "read_jpeg",
    "write_jpeg",
    "restore_jpeg",
    # PNG, on its own
    "find_in_png",
    "read_png",
    "write_png",
    "restore_png",
    # either format -- what callers should normally use
    "find_block",
    "read_photo",
    "write_photo",
    "restore_photo",
    "XmpError",
]

# The APP1 namespace header Adobe specifies. The trailing NUL is part of it.
XMP_NS = b"http://ns.adobe.com/xap/1.0/\x00"
XMP_EXT_NS = b"http://ns.adobe.com/xmp/extension/\x00"

# PNG: signature, and the iTXt keyword that carries an XMP packet.
PNG_SIG = b"\x89PNG\r\n\x1a\n"
PNG_XMP_KEYWORD = b"XML:com.adobe.xmp"

NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "lr": "http://ns.adobe.com/lightroom/1.0/",
    "digiKam": "http://www.digikam.org/ns/1.0/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
}
_R = f"{{{NS['rdf']}}}"
_DC_SUBJECT = f"{{{NS['dc']}}}subject"
_LR_HIER = f"{{{NS['lr']}}}HierarchicalSubject"
_DK_TAGS = f"{{{NS['digiKam']}}}TagsList"
_XMP_RATING = f"{{{NS['xmp']}}}Rating"

#: XMP packets are conventionally padded so an editor can rewrite them in place
#: without shifting the rest of the file.
DEFAULT_PAD = 2048


class XmpError(ValueError):
    """Raised for files we cannot safely rewrite (non-JPEG, malformed, too big)."""


@dataclass
class XmpFields:
    """Tags pulled out of an XMP packet."""

    flat: list[str] = field(default_factory=list)
    hierarchical: list[str] = field(default_factory=list)
    rating: int | None = None
    #: every rdf:Description child we understood, keyed by local name
    raw: dict[str, list[str]] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.flat or self.hierarchical or self.rating)


@dataclass
class XmpForeign:
    """Parts of somebody else's XMP that we carry through untouched.

    We only understand four properties (dc:subject, lr:HierarchicalSubject,
    digiKam:TagsList, xmp:Rating). A phone camera, Lightroom or a screenshot
    tool may have written a caption, GPS, a creator stamp or vendor-specific
    boxes into the same packet. Those are not ours to delete, so they are kept
    verbatim -- as source text, so nothing is re-serialised or re-ordered.
    """

    #: verbatim source of each rdf:Description child we do not own
    children: list[str] = field(default_factory=list)
    #: xmlns declarations those children need, as (prefix, uri)
    namespaces: list[tuple[str, str]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.children


# --------------------------------------------------------------------------- #
# packet building
# --------------------------------------------------------------------------- #


def build_packet(
    *,
    flat: list[str] | tuple[str, ...] = (),
    hierarchical: list[str] | tuple[str, ...] = (),
    rating: int | None = None,
    creator_tool: str = "albumcat",
    pad: int = DEFAULT_PAD,
    preserve: XmpForeign | None = None,
) -> bytes:
    """Build a complete XMP packet, ready to drop into an APP1 segment.

    ``preserve`` carries through the parts of an existing packet that belong to
    somebody else; without it those properties would be dropped on the floor.
    """
    flat = _dedupe(flat)
    hierarchical = _dedupe(hierarchical)
    if rating is not None:
        rating = max(0, min(5, int(rating)))

    ns_lines = [
        f'    xmlns:dc="{NS["dc"]}"',
        f'    xmlns:lr="{NS["lr"]}"',
        f'    xmlns:digiKam="{NS["digiKam"]}"',
        f'    xmlns:xmp="{NS["xmp"]}"',
    ]
    if preserve is not None:
        # A preserved child may use a prefix our own packet never declares, so
        # bring its declaration along or the result is not well-formed XML.
        # Ours win on a clash: they are the ones our new children use.
        known = {"x", "rdf", "dc", "lr", "digiKam", "xmp"}
        for prefix, uri in preserve.namespaces:
            if prefix not in known:
                known.add(prefix)
                ns_lines.append(f'    xmlns:{prefix}="{_attr(uri)}"')
    ns_lines[-1] += ">"

    lines = [
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>',
        f'<x:xmpmeta xmlns:x="{NS["x"]}" x:xmptk="{escape(creator_tool)}">',
        f' <rdf:RDF xmlns:rdf="{NS["rdf"]}">',
        "  <rdf:Description rdf:about=\"\"",
        *ns_lines,
    ]

    if flat:
        lines += _bag("   ", _DC_SUBJECT, "dc", flat)
    if hierarchical:
        lines += _bag("   ", _LR_HIER, "lr", hierarchical)
        lines += _bag("   ", _DK_TAGS, "digiKam", hierarchical)
    if rating is not None:
        lines.append(f"   <xmp:Rating>{rating}</xmp:Rating>")
    if preserve is not None:
        lines += preserve.children

    lines += [
        "  </rdf:Description>",
        " </rdf:RDF>",
        "</x:xmpmeta>",
    ]

    body = "\n".join(lines) + "\n"
    # pad before the closing PI so the packet can be edited in place
    trailer = "<?xpacket end=\"w\"?>"
    if pad > 0:
        body = body + (" " * pad) + "\n"
    return (body + trailer).encode("utf-8")


def _bag(indent: str, prop: str, prefix: str, values: list[str]) -> list[str]:
    out = [f"{indent}<{prefix}:{prop.split('}')[1]}>", f"{indent} <rdf:Bag>"]
    for v in values:
        out.append(f"{indent}  <rdf:li>{escape(v)}</rdf:li>")
    out += [f"{indent} </rdf:Bag>", f"{indent}</{prefix}:{prop.split('}')[1]}>"]
    return out


def _dedupe(values) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        v = str(v).strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


# --------------------------------------------------------------------------- #
# packet parsing
# --------------------------------------------------------------------------- #


def parse_packet(packet: bytes) -> XmpFields:
    """Parse an XMP packet. Best effort -- unknown properties are kept in ``raw``."""
    text = packet.decode("utf-8", "replace")

    # The xpacket processing instructions are not valid XML, so slice them off.
    m = re.search(r"<x:xmpmeta.*?</x:xmpmeta>", text, re.DOTALL)
    if not m:
        raise XmpError("no <x:xmpmeta> element in the packet")
    try:
        root = ET.fromstring(m.group(0))
    except ET.ParseError as exc:
        raise XmpError(f"malformed XMP: {exc}") from exc

    fields = XmpFields()
    for desc in root.iter(f"{_R}Description"):
        for child in desc:
            values = _read_values(child)
            if not values:
                continue
            local = _local(child.tag)
            fields.raw.setdefault(local, []).extend(values)
            if child.tag == _DC_SUBJECT:
                fields.flat = _dedupe([*fields.flat, *values])
            elif child.tag in (_LR_HIER, _DK_TAGS):
                fields.hierarchical = _dedupe([*fields.hierarchical, *values])
            elif child.tag == _XMP_RATING:
                try:
                    fields.rating = int(values[0])
                except ValueError:
                    pass
    return fields


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_values(elem) -> list[str]:
    """Pull rdf:li values out of a Bag/Seq/Alt, or take the element's own text."""
    items = list(elem.iter(f"{_R}li"))
    if items:
        return [("".join(li.itertext()) or "").strip() for li in items]
    text = "".join(elem.itertext()).strip()
    return [text] if text else []


# --------------------------------------------------------------------------- #
# preserving somebody else's XMP
# --------------------------------------------------------------------------- #

_OWNED_TAGS = frozenset({_DC_SUBJECT, _LR_HIER, _DK_TAGS, _XMP_RATING})

_XMLNS_RE = re.compile(r"""xmlns:([A-Za-z_][\w.\-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def _attr(value: str) -> str:
    return escape(str(value), {'"': "&quot;"})


def _tag_end(text: str, start: int) -> int:
    """Index just past the ``>`` closing the tag that starts at ``start``."""
    quote = ""
    for i in range(start, len(text)):
        c = text[i]
        if quote:
            if c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c == ">":
            return i + 1
    return -1


def _description_children(text: str) -> list[str] | None:
    """Verbatim source of every element directly inside rdf:Description.

    A deliberately dumb scanner: it walks tags to find ``>`` outside quotes and
    matches ``</name>`` to find the end of an element. None of that is a real
    XML parser, which is the point -- the bytes come back exactly as they were,
    instead of being re-serialised with different quoting and attribute order.

    Returns None for anything it does not recognise, so the caller can fall back
    to the old behaviour rather than corrupting a packet.
    """
    open_at = text.find("<rdf:Description")
    if open_at < 0:
        return None
    after_open = _tag_end(text, open_at)
    if after_open < 0:
        return None
    close_at = text.find("</rdf:Description", after_open)
    if close_at < 0:
        return None

    out: list[str] = []
    i = after_open
    while True:
        lt = text.find("<", i)
        if lt < 0 or lt >= close_at:
            break
        if text.startswith("<!--", lt):
            end = text.find("-->", lt)
            if end < 0:
                return None
            i = end + 3
            continue
        if text.startswith("<?", lt):
            end = text.find("?>", lt)
            if end < 0:
                return None
            i = end + 2
            continue
        if text.startswith("<![CDATA[", lt):
            end = text.find("]]>", lt)
            if end < 0:
                return None
            i = end + 3
            continue
        if text.startswith("</", lt):
            break

        gt = _tag_end(text, lt)
        if gt < 0:
            return None
        m = re.match(r"<([^\s/>]+)", text[lt:])
        if not m:
            return None
        name = m.group(1)
        if text[gt - 2:gt] == "/>":  # self-closing
            out.append(text[lt:gt])
            i = gt
            continue
        close = text.find(f"</{name}", gt)
        if close < 0:
            return None
        end = _tag_end(text, close)
        if end < 0:
            return None
        out.append(text[lt:end])
        i = end
    return out


def foreign_parts(packet: bytes | None) -> XmpForeign:
    """Everything in ``packet`` that is not one of the four properties we own.

    Semantics come from ElementTree (document order, resolved namespaces), and
    the bytes come from the verbatim scanner. Both must agree on how many
    children there are; if they do not, the packet is shaped oddly and we
    preserve nothing rather than risk mangling it.
    """
    if not packet:
        return XmpForeign()
    text = packet.decode("utf-8", "replace")

    verbatim = _description_children(text)
    if not verbatim:
        return XmpForeign()

    m = re.search(r"<x:xmpmeta.*?</x:xmpmeta>", text, re.DOTALL)
    if not m:
        return XmpForeign()
    try:
        root = ET.fromstring(m.group(0))
    except ET.ParseError:
        return XmpForeign()

    descs = list(root.iter(f"{_R}Description"))
    if len(descs) != 1:
        return XmpForeign()
    children = list(descs[0])
    if len(children) != len(verbatim):
        return XmpForeign()

    kept = [src for elem, src in zip(children, verbatim) if elem.tag not in _OWNED_TAGS]
    if not kept:
        return XmpForeign()

    # Prefixes come from the original opening tag, in the order it declared them.
    opening = text[text.find("<rdf:Description"):_tag_end(text, text.find("<rdf:Description"))]
    namespaces = [
        (mm.group(1), mm.group(2) if mm.group(2) is not None else mm.group(3))
        for mm in _XMLNS_RE.finditer(opening)
    ]
    return XmpForeign(children=kept, namespaces=namespaces)


# --------------------------------------------------------------------------- #
# JPEG byte surgery
# --------------------------------------------------------------------------- #


def _walk(data: bytes) -> tuple[list[tuple[int, int, int]], int]:
    """Return (segments, body_offset); each segment is (marker, start, end)."""
    if data[:2] != b"\xff\xd8":
        raise XmpError("not a JPEG (no SOI marker)")
    segs: list[tuple[int, int, int]] = []
    i, n = 2, len(data)
    while i + 1 < n:
        if data[i] != 0xFF:
            raise XmpError(f"malformed JPEG: expected a marker at offset {i}")
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0xDA, 0xD9):  # SOS / EOI -- everything after is image data
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:  # standalone markers
            segs.append((marker, i, i + 2))
            i += 2
            continue
        if i + 4 > n:
            raise XmpError("truncated JPEG segment header")
        seglen = int.from_bytes(data[i + 2 : i + 4], "big")
        if seglen < 2:
            raise XmpError(f"invalid JPEG segment length {seglen} at offset {i}")
        end = i + 2 + seglen
        if end > n:
            raise XmpError("truncated JPEG segment")
        segs.append((marker, i, end))
        i = end
    return segs, i


def _is_exif_app1(data: bytes, s: int, e: int) -> bool:
    return data[s + 1] == 0xE1 and data[s + 4 : s + 10] == b"Exif\x00\x00"


def _is_xmp_app1(data: bytes, s: int, e: int) -> bool:
    if data[s + 1] != 0xE1:
        return False
    payload = data[s + 4 : e]
    return payload.startswith(XMP_NS) or payload.startswith(XMP_EXT_NS)


def _app1(packet: bytes) -> bytes:
    payload = XMP_NS + packet
    if len(payload) + 2 > 0xFFFF:
        raise XmpError(
            f"XMP packet is {len(payload)} bytes; one APP1 segment holds at most 65533"
        )
    return b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload


def find_in_jpeg(data: bytes) -> bytes | None:
    """Return the raw XMP packet inside a JPEG, or None."""
    segs, _ = _walk(data)
    for marker, s, e in segs:
        if _is_xmp_app1(data, s, e):
            payload = data[s + 4 : e]
            if payload.startswith(XMP_NS):
                return payload[len(XMP_NS) :]
    return None


def read_jpeg(path: Path | str) -> XmpFields | None:
    """Read tags already present in a JPEG. None when it has no XMP."""
    packet = find_in_jpeg(Path(path).read_bytes())
    return parse_packet(packet) if packet else None


def _merge_fields(
    existing: bytes | None,
    *,
    flat: list[str] | tuple[str, ...],
    hierarchical: list[str] | tuple[str, ...],
    rating: int | None,
    merge: bool,
) -> tuple[list[str], list[str], int | None]:
    """Combine what the file already has with what the caller wants to add."""
    if existing:
        try:
            current = parse_packet(existing)
        except XmpError:
            current = XmpFields()
    else:
        current = XmpFields()

    if merge:
        flat = _dedupe([*current.flat, *flat])
        hierarchical = _dedupe([*current.hierarchical, *hierarchical])
        if rating is None:
            rating = current.rating
    else:
        flat = _dedupe(flat)
        hierarchical = _dedupe(hierarchical)
    return list(flat), list(hierarchical), rating


def write_jpeg(
    path: Path | str,
    *,
    flat: list[str] | tuple[str, ...] = (),
    hierarchical: list[str] | tuple[str, ...] = (),
    rating: int | None = None,
    merge: bool = True,
    pad: int = DEFAULT_PAD,
    dry_run: bool = False,
) -> XmpFields:
    """Write XMP tags into a JPEG, replacing any previous albumcat-written XMP.

    ``merge=True`` unions with tags already in the file, so a second run never
    drops what an earlier one added. The write is atomic: a temp file in the
    same directory is renamed over the original, so an interrupted run cannot
    leave a half-written photo behind.
    """
    path = Path(path)
    data = path.read_bytes()

    existing = find_in_jpeg(data)
    flat, hierarchical, rating = _merge_fields(
        existing, flat=flat, hierarchical=hierarchical, rating=rating, merge=merge
    )
    packet = build_packet(
        flat=flat, hierarchical=hierarchical, rating=rating, pad=pad,
        preserve=foreign_parts(existing),
    )
    result = XmpFields(
        flat=list(flat), hierarchical=list(hierarchical), rating=rating
    )
    if dry_run:
        return result

    _atomic_write(path, _splice(data, packet))
    return result


def _splice(data: bytes, packet: bytes | None) -> bytes:
    """Return ``data`` with its XMP APP1 segment replaced by ``packet``.

    ``packet=None`` removes XMP entirely, which is how a run is undone. Only
    the APP1 segment changes: everything from SOS onward (the actual image
    data) is copied through untouched, so no pixel is ever re-encoded.
    """
    segs, body = _walk(data)
    out = bytearray(data[:2])

    # Keep the leading run of APP0 / Exif-APP1 segments first, per the XMP spec,
    # then put our APP1 right after it.
    skip_to = 2
    for marker, s, e in segs:
        if marker == 0xE0 or _is_exif_app1(data, s, e):
            out += data[s:e]
            skip_to = e
        else:
            break
    if packet is not None:
        out += _app1(packet)

    for marker, s, e in segs:
        if s < skip_to:
            continue
        if _is_xmp_app1(data, s, e):
            continue  # drop the old packet; ours replaces it
        out += data[s:e]
    out += data[body:]
    return bytes(out)


def restore_jpeg(path: Path | str, packet: bytes | None, *, dry_run: bool = False) -> None:
    """Put a JPEG's XMP block back exactly as it was (``None`` = remove it).

    Undo path for :class:`~albumcat.publishers.JpegXmpSink`: the publisher
    journals the original packet before its first write, so a run can be
    reversed without touching the pixels.
    """
    path = Path(path)
    if dry_run:
        return
    _atomic_write(path, _splice(path.read_bytes(), packet))


# --------------------------------------------------------------------------- #
# PNG
# --------------------------------------------------------------------------- #
#
# A PNG has no APP1 segment. XMP goes in an ``iTXt`` chunk whose keyword is
# ``XML:com.adobe.xmp``, placed right after IHDR (before PLTE/IDAT). That is
# the convention ExifTool, digiKam, Lightroom and AndroidX ExifInterface all
# read, and it is how Aves sees PNG keywords at all -- which matters, because
# an Android screenshot is a PNG and those are exactly the images worth
# classifying.
#
# One wrinkle makes this differ from JPEG. ``find_in_png`` returns the *whole
# raw iTXt payload* (keyword included), not the packet. An APP1 segment is
# nothing but the namespace URI followed by the packet, so rebuilding one from
# the packet is exact; an iTXt chunk also carries a compression flag/method and
# a language tag, and a packet that came out of a chunk we did not write would
# not survive a trip through ``build_packet``. Saving the payload verbatim is
# what keeps undo byte-for-byte even then. ``restore_*`` always takes back
# whatever its own ``find_*`` returned.

#: Exactly four NULs, which is what the spec asks for after the keyword: one
#: compression flag, one compression method, an empty language tag, and an
#: empty translated keyword. Getting this one byte wrong still round-trips
#: through our own reader -- the packet just ends up with a stray NUL in front
#: of it that a real parser rejects. Pillow refused to read it, which is how
#: this was caught.
_ITXT_HEAD = b"\x00\x00\x00\x00"


def _png_chunks(data: bytes) -> list[tuple[bytes, int, int]]:
    """Return ``(type, start, end)`` for each chunk; start is the length field."""
    if data[:8] != PNG_SIG:
        raise XmpError("not a PNG (bad signature)")
    out: list[tuple[bytes, int, int]] = []
    i, n = 8, len(data)
    while i + 8 <= n:
        length = int.from_bytes(data[i : i + 4], "big")
        ctype = data[i + 4 : i + 8]
        end = i + 12 + length
        if end > n:
            raise XmpError(f"truncated PNG chunk {ctype!r} at offset {i}")
        out.append((ctype, i, end))
        i = end
        if ctype == b"IEND":
            return out
    raise XmpError("truncated PNG (no IEND chunk)")


def _png_chunk(ctype: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(ctype + payload) & 0xFFFFFFFF
    return (
        len(payload).to_bytes(4, "big")
        + ctype
        + payload
        + crc.to_bytes(4, "big")
    )


def _itxt_payload(packet: bytes) -> bytes:
    """The canonical iTXt payload carrying ``packet``."""
    return PNG_XMP_KEYWORD + b"\x00" + _ITXT_HEAD + packet


def _itxt_text(payload: bytes) -> bytes | None:
    """The text of an iTXt payload, or None if it is not the XMP one."""
    if not payload.startswith(PNG_XMP_KEYWORD + b"\x00"):
        return None
    rest = payload[len(PNG_XMP_KEYWORD) + 1 :]
    if len(rest) < 2:
        return None
    compressed = rest[0]
    rest = rest[2:]  # compression flag + method
    for _ in range(2):  # language tag, then translated keyword
        nul = rest.find(b"\x00")
        if nul < 0:
            return None
        rest = rest[nul + 1 :]
    if compressed:
        try:
            rest = zlib.decompress(rest)
        except zlib.error:
            return None
    return rest


def find_in_png(data: bytes) -> bytes | None:
    """Return the raw XMP iTXt payload of a PNG, or None.

    This is the undo blob, not the packet -- see the section comment above.
    """
    for ctype, s, e in _png_chunks(data):
        if ctype != b"iTXt":
            continue
        payload = data[s + 8 : e - 4]
        if _itxt_text(payload) is not None:
            return payload
    return None


def _png_packet(block: bytes | None) -> bytes | None:
    return _itxt_text(block) if block else None


def read_png(path: Path | str) -> XmpFields | None:
    """Read tags already present in a PNG. None when it has no XMP."""
    packet = _png_packet(find_in_png(Path(path).read_bytes()))
    return parse_packet(packet) if packet else None


def write_png(
    path: Path | str,
    *,
    flat: list[str] | tuple[str, ...] = (),
    hierarchical: list[str] | tuple[str, ...] = (),
    rating: int | None = None,
    merge: bool = True,
    pad: int = DEFAULT_PAD,
    dry_run: bool = False,
) -> XmpFields:
    """Write XMP tags into a PNG. Same contract as :func:`write_jpeg`."""
    path = Path(path)
    data = path.read_bytes()

    block = find_in_png(data)
    existing = _png_packet(block)
    flat, hierarchical, rating = _merge_fields(
        existing, flat=flat, hierarchical=hierarchical, rating=rating, merge=merge
    )
    packet = build_packet(
        flat=flat, hierarchical=hierarchical, rating=rating, pad=pad,
        preserve=foreign_parts(existing),
    )
    result = XmpFields(
        flat=list(flat), hierarchical=list(hierarchical), rating=rating
    )
    if dry_run:
        return result

    _atomic_write(path, _splice_png(data, _itxt_payload(packet)))
    return result


def _splice_png(data: bytes, block: bytes | None) -> bytes:
    """Return ``data`` with its XMP iTXt chunk replaced by ``block``.

    ``block=None`` removes XMP entirely, which is how a run is undone. Every
    other chunk -- IDAT above all -- is copied through byte for byte, so no
    pixel is ever re-encoded.
    """
    chunks = _png_chunks(data)
    if chunks[0][0] != b"IHDR":
        raise XmpError("malformed PNG (first chunk is not IHDR)")

    out = bytearray(data[:8])
    for ctype, s, e in chunks:
        if ctype == b"iTXt" and _itxt_text(data[s + 8 : e - 4]) is not None:
            continue  # drop the old packet; ours replaces it
        out += data[s:e]
        if ctype == b"IHDR" and block is not None:
            out += _png_chunk(b"iTXt", block)
    return bytes(out)


def restore_png(path: Path | str, block: bytes | None, *, dry_run: bool = False) -> None:
    """Put a PNG's XMP block back exactly as it was (``None`` = remove it)."""
    path = Path(path)
    if dry_run:
        return
    _atomic_write(path, _splice_png(path.read_bytes(), block))


# --------------------------------------------------------------------------- #
# format dispatch -- what the publisher and the phone app use
# --------------------------------------------------------------------------- #


def photo_format(data: bytes) -> str:
    """``"jpeg"``, ``"png"``, or raise. Decided by content, not by suffix."""
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    if data[:8] == PNG_SIG:
        return "png"
    raise XmpError("not a JPEG or PNG")


def read_photo(path: Path | str) -> XmpFields | None:
    """Read XMP from a JPEG or PNG, whichever it turns out to be."""
    data = Path(path).read_bytes()
    if photo_format(data) == "jpeg":
        packet = find_in_jpeg(data)
        return parse_packet(packet) if packet else None
    return read_png(path)


def write_photo(
    path: Path | str,
    *,
    flat: list[str] | tuple[str, ...] = (),
    hierarchical: list[str] | tuple[str, ...] = (),
    rating: int | None = None,
    merge: bool = True,
    pad: int = DEFAULT_PAD,
    dry_run: bool = False,
) -> XmpFields:
    """Write XMP into a JPEG or PNG, whichever the file turns out to be."""
    if photo_format(Path(path).read_bytes()) == "jpeg":
        return write_jpeg(path, flat=flat, hierarchical=hierarchical,
                          rating=rating, merge=merge, pad=pad, dry_run=dry_run)
    return write_png(path, flat=flat, hierarchical=hierarchical,
                     rating=rating, merge=merge, pad=pad, dry_run=dry_run)


def find_block(path: Path | str) -> bytes | None:
    """The undo blob for a photo -- see ``find_in_png`` on why JPEG and PNG differ."""
    data = Path(path).read_bytes()
    return find_in_jpeg(data) if photo_format(data) == "jpeg" else find_in_png(data)


def packet_of(data: bytes) -> bytes | None:
    """The XMP *packet* inside a photo's bytes, whichever format it is.

    The counterpart of :func:`find_block`: same search, but it unwraps a PNG's
    iTXt payload so what comes back is always XML. Tests and the Kotlin interop
    driver want this view; the publisher and the undo log want ``find_block``.
    """
    if photo_format(data) == "jpeg":
        return find_in_jpeg(data)
    return _png_packet(find_in_png(data))


def restore_photo(path: Path | str, block: bytes | None, *, dry_run: bool = False) -> None:
    """Undo path: put back whatever :func:`find_block` returned for this file."""
    path = Path(path)
    data = path.read_bytes()
    if dry_run:
        return
    if photo_format(data) == "jpeg":
        restore_jpeg(path, block)
    else:
        restore_png(path, block)


def _atomic_write(path: Path, payload: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".albumcat-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# tiny CLI, for inspecting a file by hand
# --------------------------------------------------------------------------- #


def _main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    for arg in argv:
        p = Path(arg)
        print(f"=== {p} ===")
        try:
            fields = read_jpeg(p)
        except (XmpError, OSError) as exc:
            print(f"  error: {exc}")
            continue
        if fields is None:
            print("  (no XMP)")
            continue
        print(f"  rating       : {fields.rating}")
        print(f"  flat         : {fields.flat}")
        print(f"  hierarchical : {fields.hierarchical}")
        for k, v in fields.raw.items():
            if k not in ("subject", "HierarchicalSubject", "TagsList", "Rating"):
                print(f"  {k:12s} : {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(__import__("sys").argv[1:]))
