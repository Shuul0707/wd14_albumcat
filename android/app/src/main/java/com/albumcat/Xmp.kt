package com.albumcat

import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.util.zip.CRC32
import java.util.zip.InflaterInputStream
import javax.xml.parsers.DocumentBuilderFactory
import org.w3c.dom.Element
import org.w3c.dom.Node

/**
 * Read and write XMP tags inside JPEG and PNG files.
 *
 * This is a faithful port of `src/albumcat/xmp.py` on the PC side: given the
 * same tags it produces the same bytes, which is what lets the two sides be
 * tested against each other.
 *
 * Why XMP at all: it is the only tag vocabulary third-party galleries agree on.
 * Renaming a folder only works by moving files (not acceptable -- the user's
 * photos must stay exactly where they are), and every gallery keeps its own
 * private database that another app cannot write. XMP travels *inside* the
 * photo, so the file never moves and the bytes of the image never change.
 *
 * Four properties are written, all four are cheap and together they cover every
 * gallery worth using:
 *
 *  - `dc:subject`             flat keywords (the oldest, most widely read).
 *                             Aves reads *only* this one.
 *  - `lr:HierarchicalSubject` Lightroom-style `角色|初音未来` paths -- this is
 *                             what Lightroom, digiKam and Immich read to build
 *                             a tag tree
 *  - `digiKam:TagsList`       same content; Immich and digiKam prefer this one
 *  - `xmp:Rating`             0-5 stars
 *
 * (Aves is the exception, hence `publish.tree_in_subject`: the hierarchical
 * paths go into `dc:subject` as well, or the phone shows no character to tap.)
 *
 * PNG matters here more than it sounds: an Android screenshot is a PNG, and
 * screenshots are exactly the images worth sorting. PNG is not a JPEG with a
 * different header, so [findBlock] returns format-specific bytes and only
 * [blockPacket] / [spliceBlock] may interpret them.
 *
 * Layout inside a JPEG:
 * ```
 * FFD8  SOI
 * FFE0  APP0  JFIF     (kept, must stay first)
 * FFE1  APP1  Exif     (kept)
 * FFE1  APP1  "http://ns.adobe.com/xap/1.0/\0" + <xpacket>...   <- ours
 * ...   other segments, unchanged, byte for byte
 * FFDA  SOS + entropy-coded image data ... FFD9 EOI
 * ```
 *
 * Layout inside a PNG:
 * ```
 * 89504E47 0D0A1A0A  signature
 * IHDR               (kept, must stay first)
 * iTXt  "XML:com.adobe.xmp" + <xpacket>...                      <- ours
 * ...   every other chunk, IDAT included, unchanged, byte for byte
 * IEND
 * ```
 * Nothing outside those one or two metadata blocks is touched, so the pixels
 * stay bit-identical either way.
 */
object Xmp {

    private val XMP_NS = "http://ns.adobe.com/xap/1.0/\u0000".toByteArray(Charsets.US_ASCII)
    private val XMP_EXT_NS = "http://ns.adobe.com/xmp/extension/\u0000".toByteArray(Charsets.US_ASCII)

    /** PNG: the 8-byte signature, and the iTXt keyword that carries a packet. */
    private val PNG_SIG = byteArrayOf(
        0x89.toByte(), 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,
    )
    private val PNG_XMP_KEYWORD = "XML:com.adobe.xmp".toByteArray(Charsets.US_ASCII)

    /** XMP packets are conventionally padded so an editor can rewrite in place. */
    const val DEFAULT_PAD = 2048

    class XmpException(message: String) : Exception(message)

    data class Fields(
        val flat: List<String> = emptyList(),
        val hierarchical: List<String> = emptyList(),
        val rating: Int? = null,
        val raw: Map<String, List<String>> = emptyMap(),
    ) {
        fun isEmpty(): Boolean = flat.isEmpty() && hierarchical.isEmpty() && rating == null
    }

    private const val NS_X = "adobe:ns:meta/"
    private const val NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    private const val NS_DC = "http://purl.org/dc/elements/1.1/"
    private const val NS_LR = "http://ns.adobe.com/lightroom/1.0/"
    private const val NS_DK = "http://www.digikam.org/ns/1.0/"
    private const val NS_XMP = "http://ns.adobe.com/xap/1.0/"

    // ------------------------------------------------------------------ #
    // packet building

    fun buildPacket(
        flat: List<String> = emptyList(),
        hierarchical: List<String> = emptyList(),
        rating: Int? = null,
        creatorTool: String = "albumcat",
        pad: Int = DEFAULT_PAD,
        preserve: Foreign = Foreign(),
    ): ByteArray {
        val f = dedupe(flat)
        val h = dedupe(hierarchical)
        val r = rating?.coerceIn(0, 5)

        // A preserved child may use a prefix our own packet never declares, so
        // bring its declaration along or the result is not well-formed XML.
        // Ours win on a clash: they are the ones our new children use.
        val nsLines = ArrayList<String>()
        nsLines += "    xmlns:dc=\"$NS_DC\""
        nsLines += "    xmlns:lr=\"$NS_LR\""
        nsLines += "    xmlns:digiKam=\"$NS_DK\""
        nsLines += "    xmlns:xmp=\"$NS_XMP\""
        val known = linkedSetOf("x", "rdf", "dc", "lr", "digiKam", "xmp")
        for ((prefix, uri) in preserve.namespaces) {
            if (known.add(prefix)) nsLines += "    xmlns:$prefix=\"${attrEscape(uri)}\""
        }
        nsLines[nsLines.size - 1] = nsLines[nsLines.size - 1] + ">"

        val lines = ArrayList<String>()
        lines += "<?xpacket begin=\"\uFEFF\" id=\"W5M0MpCehiHzreSzNTczkc9d\"?>"
        lines += "<x:xmpmeta xmlns:x=\"$NS_X\" x:xmptk=\"${xmlEscape(creatorTool)}\">"
        lines += " <rdf:RDF xmlns:rdf=\"$NS_RDF\">"
        lines += "  <rdf:Description rdf:about=\"\""
        lines += nsLines

        if (f.isNotEmpty()) lines += bag("   ", "dc", "subject", f)
        if (h.isNotEmpty()) {
            lines += bag("   ", "lr", "HierarchicalSubject", h)
            lines += bag("   ", "digiKam", "TagsList", h)
        }
        if (r != null) lines += "   <xmp:Rating>$r</xmp:Rating>"
        lines += preserve.children

        lines += "  </rdf:Description>"
        lines += " </rdf:RDF>"
        lines += "</x:xmpmeta>"

        var body = lines.joinToString("\n") + "\n"
        if (pad > 0) body += " ".repeat(pad) + "\n"
        return (body + "<?xpacket end=\"w\"?>").toByteArray(Charsets.UTF_8)
    }

    private fun bag(indent: String, prefix: String, name: String, values: List<String>): List<String> {
        val out = ArrayList<String>(values.size + 4)
        out += "$indent<$prefix:$name>"
        out += "$indent <rdf:Bag>"
        for (v in values) out += "$indent  <rdf:li>${xmlEscape(v)}</rdf:li>"
        out += "$indent </rdf:Bag>"
        out += "$indent</$prefix:$name>"
        return out
    }

    /** Matches `xml.sax.saxutils.escape`: only `&`, `<` and `>` need escaping. */
    private fun xmlEscape(s: String): String = buildString(s.length) {
        for (c in s) when (c) {
            '&' -> append("&amp;")
            '<' -> append("&lt;")
            '>' -> append("&gt;")
            else -> append(c)
        }
    }

    private fun dedupe(values: List<String>): List<String> {
        val seen = LinkedHashSet<String>()
        for (v in values) {
            val t = v.trim()
            if (t.isNotEmpty()) seen.add(t)
        }
        return seen.toList()
    }

    // ------------------------------------------------------------------ #
    // preserving somebody else's XMP

    /**
     * Parts of an existing packet that are not ours.
     *
     * We only understand four properties. A phone camera, Lightroom or a
     * screenshot tool may have written a caption, GPS or a vendor stamp into
     * the same packet, and those are not ours to delete -- so they are carried
     * through as source text, nothing re-serialised or re-ordered.
     */
    data class Foreign(
        val children: List<String> = emptyList(),
        val namespaces: List<Pair<String, String>> = emptyList(),
    ) {
        fun isEmpty(): Boolean = children.isEmpty()
    }

    private val OWNED = setOf(
        "$NS_DC\u0000subject",
        "$NS_LR\u0000HierarchicalSubject",
        "$NS_DK\u0000TagsList",
        "$NS_XMP\u0000Rating",
    )

    private val XMLNS_RE = Regex("""xmlns:([A-Za-z_][\w.\-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")

    private fun attrEscape(s: String): String = xmlEscape(s).replace("\"", "&quot;")

    /** Index just past the `>` closing the tag that starts at [start]. */
    private fun tagEnd(text: String, start: Int): Int {
        var quote = ' '
        for (i in start until text.length) {
            val c = text[i]
            if (quote != ' ') {
                if (c == quote) quote = ' '
            } else when (c) {
                '"', '\'' -> quote = c
                '>' -> return i + 1
            }
        }
        return -1
    }

    /**
     * Verbatim source of every element directly inside rdf:Description.
     *
     * A deliberately dumb scanner: it walks tags to find `>` outside quotes and
     * matches `</name>` to find the end of an element. None of that is a real
     * XML parser, which is the point -- the bytes come back exactly as they
     * were, instead of being re-serialised with different quoting and attribute
     * order. Returns null for anything unrecognised, so the caller falls back
     * rather than corrupting a packet.
     */
    private fun descriptionChildren(text: String): List<String>? {
        val openAt = text.indexOf("<rdf:Description")
        if (openAt < 0) return null
        val afterOpen = tagEnd(text, openAt)
        if (afterOpen < 0) return null
        val closeAt = text.indexOf("</rdf:Description", afterOpen)
        if (closeAt < 0) return null

        val out = ArrayList<String>()
        var i = afterOpen
        while (true) {
            val lt = text.indexOf('<', i)
            if (lt < 0 || lt >= closeAt) break
            if (text.startsWith("<!--", lt)) {
                val end = text.indexOf("-->", lt); if (end < 0) return null; i = end + 3; continue
            }
            if (text.startsWith("<?", lt)) {
                val end = text.indexOf("?>", lt); if (end < 0) return null; i = end + 2; continue
            }
            if (text.startsWith("<![CDATA[", lt)) {
                val end = text.indexOf("]]>", lt); if (end < 0) return null; i = end + 3; continue
            }
            if (text.startsWith("</", lt)) break

            val gt = tagEnd(text, lt)
            if (gt < 0) return null
            val name = TAG_NAME.find(text.substring(lt))?.groupValues?.get(1) ?: return null
            if (text.substring(gt - 2, gt) == "/>") {   // self-closing
                out += text.substring(lt, gt)
                i = gt
                continue
            }
            val close = text.indexOf("</$name", gt)
            if (close < 0) return null
            val end = tagEnd(text, close)
            if (end < 0) return null
            out += text.substring(lt, end)
            i = end
        }
        return out
    }

    private val TAG_NAME = Regex("^<([^\\s/>]+)")

    /**
     * Everything in [packet] that is not one of the four properties we own.
     *
     * Semantics come from the DOM (document order, resolved namespaces) and the
     * bytes come from the verbatim scanner; both must agree on the child count,
     * otherwise the packet is shaped oddly and we preserve nothing rather than
     * risk mangling it.
     */
    fun foreignParts(packet: ByteArray?): Foreign {
        if (packet == null || packet.isEmpty()) return Foreign()
        val text = String(packet, Charsets.UTF_8)

        val verbatim = descriptionChildren(text) ?: return Foreign()
        if (verbatim.isEmpty()) return Foreign()

        val start = text.indexOf("<x:xmpmeta")
        val end = text.lastIndexOf("</x:xmpmeta>")
        if (start < 0 || end < 0) return Foreign()
        val root = try {
            xmlParser().parse(ByteArrayInputStream(
                text.substring(start, end + "</x:xmpmeta>".length).toByteArray(Charsets.UTF_8)
            )).documentElement
        } catch (_: Exception) {
            return Foreign()
        }

        val descs = descendants(root, NS_RDF, "Description")
        if (descs.size != 1) return Foreign()
        val children = childElements(descs[0])
        if (children.size != verbatim.size) return Foreign()

        val kept = ArrayList<String>()
        for (k in children.indices) {
            val c = children[k]
            if ((c.namespaceURI ?: "") + "\u0000" + (c.localName ?: "") !in OWNED) kept += verbatim[k]
        }
        if (kept.isEmpty()) return Foreign()

        // Prefixes come from the original opening tag, in the order it declared them.
        val openAt = text.indexOf("<rdf:Description")
        val opening = text.substring(openAt, tagEnd(text, openAt))
        val ns = XMLNS_RE.findAll(opening).map { mm ->
            val quoted = mm.groups[2]
            mm.groupValues[1] to (quoted?.value ?: mm.groupValues[3])
        }.toList()
        return Foreign(kept, ns)
    }

    // ------------------------------------------------------------------ #
    // packet parsing

    /** Best effort. Throws [XmpException] when there is no usable packet. */
    fun parsePacket(packet: ByteArray): Fields {
        val text = String(packet, Charsets.UTF_8)
        // The xpacket processing instructions are not valid XML, so slice them off.
        val start = text.indexOf("<x:xmpmeta")
        val end = text.lastIndexOf("</x:xmpmeta>")
        if (start < 0 || end < 0) throw XmpException("no <x:xmpmeta> element in the packet")
        val xml = text.substring(start, end + "</x:xmpmeta>".length)

        val root = try {
            xmlParser().parse(ByteArrayInputStream(xml.toByteArray(Charsets.UTF_8))).documentElement
        } catch (e: Exception) {
            throw XmpException("malformed XMP: ${e.message}")
        }

        var flat = emptyList<String>()
        var hier = emptyList<String>()
        var rating: Int? = null
        val raw = LinkedHashMap<String, List<String>>()

        for (desc in descendants(root, NS_RDF, "Description")) {
            for (child in childElements(desc)) {
                val values = readValues(child)
                if (values.isEmpty()) continue
                val local = child.localName ?: child.nodeName.substringAfterLast(':')
                raw[local] = (raw[local] ?: emptyList()) + values
                when {
                    child.namespaceURI == NS_DC && local == "subject" ->
                        flat = dedupe(flat + values)
                    (child.namespaceURI == NS_LR && local == "HierarchicalSubject") ||
                        (child.namespaceURI == NS_DK && local == "TagsList") ->
                        hier = dedupe(hier + values)
                    child.namespaceURI == NS_XMP && local == "Rating" ->
                        rating = values.first().trim().toIntOrNull() ?: rating
                }
            }
        }
        return Fields(flat, hier, rating, raw)
    }

    private fun xmlParser() = DocumentBuilderFactory.newInstance().apply {
        isNamespaceAware = true
        for (feature in listOf(
            "http://apache.org/xml/features/nonvalidating/load-external-dtd",
            "http://xml.org/sax/features/external-general-entities",
            "http://xml.org/sax/features/external-parameter-entities",
        )) {
            // Android's parser rejects some of these; a missing feature is fine,
            // the packet we parse is our own and never has a DTD.
            try { setFeature(feature, false) } catch (_: Exception) { }
        }
    }.newDocumentBuilder()

    private fun childElements(e: Element): List<Element> {
        val out = ArrayList<Element>()
        var n = e.firstChild
        while (n != null) {
            if (n.nodeType == Node.ELEMENT_NODE) out += n as Element
            n = n.nextSibling
        }
        return out
    }

    private fun descendants(e: Element, ns: String?, local: String): List<Element> {
        val out = ArrayList<Element>()
        fun walk(x: Element) {
            if (x.namespaceURI == ns && (x.localName ?: "") == local) out += x
            for (c in childElements(x)) walk(c)
        }
        walk(e)
        return out
    }

    /** rdf:li values out of a Bag/Seq/Alt, or the element's own text. */
    private fun readValues(elem: Element): List<String> {
        val items = descendants(elem, NS_RDF, "li")
        if (items.isNotEmpty()) return items.map { it.textContent.orEmpty().trim() }
        val text = elem.textContent.orEmpty().trim()
        return if (text.isEmpty()) emptyList() else listOf(text)
    }

    // ------------------------------------------------------------------ #
    // JPEG byte surgery

    private class Seg(val marker: Int, val start: Int, val end: Int)

    private class Walk(val segs: List<Seg>, val bodyOffset: Int)

    private fun walk(data: ByteArray): Walk {
        if (data.size < 2 || data[0] != 0xFF.toByte() || data[1] != 0xD8.toByte()) {
            throw XmpException("not a JPEG (no SOI marker)")
        }
        val segs = ArrayList<Seg>()
        var i = 2
        val n = data.size
        while (i + 1 < n) {
            if (data[i] != 0xFF.toByte()) throw XmpException("malformed JPEG: expected a marker at offset $i")
            val marker = data[i + 1].toInt() and 0xFF
            if (marker == 0xFF) { i++; continue }              // fill byte
            if (marker == 0xDA || marker == 0xD9) break        // SOS / EOI: image data follows
            if (marker == 0x01 || marker in 0xD0..0xD7) {      // standalone markers
                segs += Seg(marker, i, i + 2); i += 2; continue
            }
            if (i + 4 > n) throw XmpException("truncated JPEG segment header")
            val seglen = ((data[i + 2].toInt() and 0xFF) shl 8) or (data[i + 3].toInt() and 0xFF)
            if (seglen < 2) throw XmpException("invalid JPEG segment length $seglen at offset $i")
            val end = i + 2 + seglen
            if (end > n) throw XmpException("truncated JPEG segment")
            segs += Seg(marker, i, end)
            i = end
        }
        return Walk(segs, i)
    }

    private fun isExifApp1(data: ByteArray, s: Int): Boolean {
        if (data[s + 1] != 0xE1.toByte()) return false
        if (s + 10 > data.size) return false
        val tag = byteArrayOf(0x45, 0x78, 0x69, 0x66, 0x00, 0x00) // "Exif\0\0"
        for (k in tag.indices) if (data[s + 4 + k] != tag[k]) return false
        return true
    }

    private fun isXmpApp1(data: ByteArray, s: Int, e: Int): Boolean {
        if (data[s + 1] != 0xE1.toByte()) return false
        return startsWithAt(data, s + 4, e, XMP_NS) || startsWithAt(data, s + 4, e, XMP_EXT_NS)
    }

    private fun startsWithAt(data: ByteArray, from: Int, to: Int, prefix: ByteArray): Boolean {
        if (from + prefix.size > to) return false
        for (k in prefix.indices) if (data[from + k] != prefix[k]) return false
        return true
    }

    private fun app1(packet: ByteArray): ByteArray {
        val payloadLen = XMP_NS.size + packet.size
        if (payloadLen + 2 > 0xFFFF) {
            throw XmpException("XMP packet is $payloadLen bytes; one APP1 segment holds at most 65533")
        }
        val out = ByteArray(4 + payloadLen)
        out[0] = 0xFF.toByte(); out[1] = 0xE1.toByte()
        val len = payloadLen + 2
        out[2] = ((len shr 8) and 0xFF).toByte(); out[3] = (len and 0xFF).toByte()
        System.arraycopy(XMP_NS, 0, out, 4, XMP_NS.size)
        System.arraycopy(packet, 0, out, 4 + XMP_NS.size, packet.size)
        return out
    }

    /** The raw XMP packet inside a JPEG, or null. */
    fun findInJpeg(data: ByteArray): ByteArray? {
        val w = walk(data)
        for (seg in w.segs) {
            if (isXmpApp1(data, seg.start, seg.end)) {
                if (startsWithAt(data, seg.start + 4, seg.end, XMP_NS)) {
                    val from = seg.start + 4 + XMP_NS.size
                    return data.copyOfRange(from, seg.end)
                }
            }
        }
        return null
    }

    /**
     * Tags out of a photo, whichever of the two formats it happens to be.
     *
     * A file that is neither (or carries no XMP) gives null rather than
     * throwing: "this photo has no tags yet" is an ordinary answer, not an
     * error. The format is read from the bytes, and both conversions go through
     * [blockPacket] because a PNG's block is not a packet.
     */
    fun readPhotoFields(data: ByteArray): Fields? {
        val format = try { photoFormat(data) } catch (_: XmpException) { return null }
        val packet = blockPacket(findBlock(data, format), format) ?: return null
        return try { parsePacket(packet) } catch (_: XmpException) { null }
    }

    /**
     * Return [data] with its XMP APP1 segment replaced by [packet].
     *
     * A null [packet] removes XMP entirely -- that is how a run is undone. Only
     * the APP1 segment changes: everything from SOS onward (the actual image
     * data) is copied through untouched, so no pixel is ever re-encoded.
     */
    fun splice(data: ByteArray, packet: ByteArray?): ByteArray {
        val w = walk(data)
        val out = ByteArrayOutputStream(data.size + (packet?.size ?: 0) + 64)
        out.write(data, 0, 2)

        // Keep the leading run of APP0 / Exif-APP1 segments first, per the XMP
        // spec, then put our APP1 right after it.
        var skipTo = 2
        for (seg in w.segs) {
            if (seg.marker == 0xE0 || isExifApp1(data, seg.start)) {
                out.write(data, seg.start, seg.end - seg.start)
                skipTo = seg.end
            } else break
        }
        if (packet != null) out.write(app1(packet))

        for (seg in w.segs) {
            if (seg.start < skipTo) continue
            if (isXmpApp1(data, seg.start, seg.end)) continue // drop the old packet
            out.write(data, seg.start, seg.end - seg.start)
        }
        out.write(data, w.bodyOffset, data.size - w.bodyOffset)
        return out.toByteArray()
    }

    /** Union [add] onto [current], keeping first-seen order. */
    fun merge(existing: Fields?, flat: List<String>, hierarchical: List<String>, rating: Int?): Triple<List<String>, List<String>, Int?> {
        val f = dedupe((existing?.flat ?: emptyList()) + flat)
        val h = dedupe((existing?.hierarchical ?: emptyList()) + hierarchical)
        val r = rating ?: existing?.rating
        return Triple(f, h, r)
    }

    // ------------------------------------------------------------------ #
    // PNG byte surgery
    //
    // A PNG has no APP1 segment. XMP goes in an `iTXt` chunk whose keyword is
    // "XML:com.adobe.xmp", placed right after IHDR. Android screenshots are
    // PNGs, so without this the most-screenshotted images in an anime album
    // would be the ones we cannot tag.
    //
    // The chunk layout after the keyword is four bytes -- compression flag,
    // compression method, empty language tag, empty translated keyword -- and
    // then the text. Getting that count wrong still round-trips through our own
    // reader, because the packet simply ends up behind a stray NUL; a real
    // parser rejects it. Python's writer had exactly that bug and it was caught
    // only by asking Pillow to read the file.

    private const val PNG_ITXT = "iTXt"
    private val PNG_HEAD = byteArrayOf(0, 0, 0, 0)

    private class Chunk(val type: String, val start: Int, val end: Int)

    /**
     * Returns the raw XMP `iTXt` payload (keyword included), or null.
     *
     * Deliberately *not* the packet. An APP1 segment is nothing but the
     * namespace URI followed by the packet, so a JPEG's block can be rebuilt
     * from the packet exactly -- but an iTXt chunk also carries a compression
     * flag, a compression method and a language tag that a packet cannot
     * express. Keeping the payload verbatim is what makes undo byte-for-byte
     * even for a packet some other program wrote.
     */
    fun findInPng(data: ByteArray): ByteArray? {
        for (c in pngChunks(data)) {
            if (c.type != PNG_ITXT) continue
            val payload = data.copyOfRange(c.start + 8, c.end - 4)
            if (itxtText(payload) != null) return payload
        }
        return null
    }

    /** The packet inside an iTXt payload we got from [findInPng]. */
    fun itxtText(payload: ByteArray): ByteArray? {
        if (!startsWith(payload, PNG_XMP_KEYWORD)) return null
        var i = PNG_XMP_KEYWORD.size
        if (i >= payload.size || payload[i].toInt() != 0) return null
        i++                                              // NUL after the keyword
        if (i + 2 > payload.size) return null
        val compressed = payload[i].toInt() != 0
        i += 2                                           // flag + method
        repeat(2) {                                      // language, translated keyword
            val nul = indexOf(payload, 0, i)
            if (nul < 0) return null
            i = nul + 1
        }
        val body = payload.copyOfRange(i, payload.size)
        if (!compressed) return body
        return try {
            InflaterInputStream(ByteArrayInputStream(body)).use { it.readBytes() }
        } catch (_: Exception) {
            null
        }
    }

    private fun pngChunks(data: ByteArray): List<Chunk> {
        if (!startsWith(data, PNG_SIG)) throw XmpException("not a PNG (bad signature)")
        val out = ArrayList<Chunk>()
        var i = 8
        while (i + 8 <= data.size) {
            val len = readInt(data, i)
            if (len < 0) throw XmpException("invalid PNG chunk length at offset $i")
            val type = String(data, i + 4, 4, Charsets.US_ASCII)
            val end = i + 12 + len
            if (end > data.size) throw XmpException("truncated PNG chunk $type at offset $i")
            out += Chunk(type, i, end)
            i = end
            if (type == "IEND") return out
        }
        throw XmpException("truncated PNG (no IEND chunk)")
    }

    private fun readInt(data: ByteArray, at: Int): Int =
        ((data[at].toInt() and 0xFF) shl 24) or
            ((data[at + 1].toInt() and 0xFF) shl 16) or
            ((data[at + 2].toInt() and 0xFF) shl 8) or
            (data[at + 3].toInt() and 0xFF)

    private fun writeInt(v: Int): ByteArray = byteArrayOf(
        (v ushr 24).toByte(), (v ushr 16).toByte(), (v ushr 8).toByte(), v.toByte(),
    )

    private fun pngChunk(type: String, payload: ByteArray): ByteArray {
        val typeBytes = type.toByteArray(Charsets.US_ASCII)
        val crc = CRC32()
        crc.update(typeBytes)
        crc.update(payload)
        return writeInt(payload.size) + typeBytes + payload + writeInt(crc.value.toInt())
    }

    /** The canonical payload carrying [packet]. */
    fun itxtPayload(packet: ByteArray): ByteArray =
        PNG_XMP_KEYWORD + byteArrayOf(0) + PNG_HEAD + packet

    private fun indexOf(data: ByteArray, what: Byte, from: Int): Int {
        for (i in from until data.size) if (data[i] == what) return i
        return -1
    }

    private fun startsWith(data: ByteArray, prefix: ByteArray): Boolean {
        if (data.size < prefix.size) return false
        for (k in prefix.indices) if (data[k] != prefix[k]) return false
        return true
    }

    /**
     * Replace (or, with [block] null, remove) a PNG's XMP iTXt chunk.
     *
     * Every other chunk -- IDAT above all -- is copied through byte for byte,
     * so no pixel is ever re-encoded.
     */
    fun splicePng(data: ByteArray, block: ByteArray?): ByteArray {
        val chunks = pngChunks(data)
        if (chunks.isEmpty() || chunks[0].type != "IHDR") {
            throw XmpException("malformed PNG (first chunk is not IHDR)")
        }
        val out = ByteArrayOutputStream(data.size + (block?.size ?: 0) + 64)
        out.write(data, 0, 8)
        for (c in chunks) {
            val isOurs = c.type == PNG_ITXT &&
                itxtText(data.copyOfRange(c.start + 8, c.end - 4)) != null
            if (isOurs) continue
            out.write(data, c.start, c.end - c.start)
            if (c.type == "IHDR" && block != null) out.write(pngChunk(PNG_ITXT, block))
        }
        return out.toByteArray()
    }

    // ------------------------------------------------------------------ #
    // format dispatch -- what TagRunner actually calls

    const val FORMAT_JPEG = "jpeg"
    const val FORMAT_PNG = "png"

    /** Decided by the file's own header, never by its name. */
    fun photoFormat(data: ByteArray): String {
        if (data.size >= 2 && data[0] == 0xFF.toByte() && data[1] == 0xD8.toByte()) return FORMAT_JPEG
        if (startsWith(data, PNG_SIG)) return FORMAT_PNG
        throw XmpException("既不是 JPEG 也不是 PNG")
    }

    /**
     * The undo blob for a photo, in whatever shape its format needs.
     *
     * JPEG: the XMP packet (an APP1 segment holds nothing else). PNG: the whole
     * iTXt payload. Only [blockPacket] and [spliceBlock] may interpret it.
     */
    fun findBlock(data: ByteArray, format: String): ByteArray? =
        if (format == FORMAT_JPEG) findInJpeg(data) else findInPng(data)

    /** The packet inside a block -- the thing [parsePacket] and [foreignParts] want. */
    fun blockPacket(block: ByteArray?, format: String): ByteArray? =
        if (block == null) null
        else if (format == FORMAT_JPEG) block
        else itxtText(block)

    /** Put a packet back into a photo, in that photo's own format. */
    fun spliceBlock(data: ByteArray, format: String, packet: ByteArray?): ByteArray =
        if (format == FORMAT_JPEG) splice(data, packet)
        else splicePng(data, packet?.let { itxtPayload(it) })
}
