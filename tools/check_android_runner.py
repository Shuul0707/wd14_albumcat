#!/usr/bin/env python
"""Run the phone's real `TagRunner.kt` on the desktop JVM, against a fake SAF.

`check_android_client.py` proves the HTTP client parses correctly.
`check_kotlin_xmp.py` proves the XMP writer is byte-identical to Python's.
`simulate_phone.py` proves the *semantics* of the write path -- but in Python.

`TagRunner.kt` is the file that actually decides what happens to the user's
photos, and until now nothing had ever executed it. Every failure mode there is
a data-loss-shaped one: writing the wrong bytes, losing a photo's original XMP,
journalling a state that undo cannot restore, or grinding through thousands of
photos failing one at a time because the server went away.

So this compiles the real `TagRunner.kt`, `Journal.kt`, `Xmp.kt`, `Rescan.kt`
and `DocumentPaths.kt`, and supplies a fake Android underneath them:

    ContentResolver  in-memory byte arrays, so "the file on disk" is inspectable
    DocumentFile     a tree built from a manifest the Python side writes
    MediaScannerConnection   records every scan, so the rescan step is verifiable
    Thumbnailer      a stub handing back real 448px thumbnails
    Context/Uri/Log/DocumentsContract   the minimum to link

Scenarios:
    dry       nothing is written, no journal rows
    write     4 images (3 JPEG + 1 PNG) -> re-run (all must skip) -> undo (bytes
              must match). The bytes are also dumped so Pillow can confirm the
              PNG is still a PNG with identical pixels and readable tags.
    batch     70 photos, to check the media rescan is batched, not per-photo
    abort     server unreachable: the run must stop early, not fail 12 times
    failwrite a write fails midway: the journal row must already exist
    stop      the user taps 停止: the run ends between photos, the ones already
              written stay valid and undoable, the rest are untouched

Note on `stop`: deleting the in-loop check makes eight assertions go red (the
run then writes all five photos while reporting success). Moving the check from
the top of the loop body to the bottom is *not* observable -- both end the run
before the next photo starts. What the test pins is that the check is inside the
loop, so a stop pressed while a photo is in flight still lets that photo finish
and be journaled instead of being abandoned half-done.

Usage:
    python tools\\check_android_runner.py [--samples DIR] [--port N]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from check_android_client import fetch_json_jar  # noqa: E402
from kotlin_jvm import build_runner, kotlin  # noqa: E402

WORK = ROOT / ".tmp" / "jvmrunner"
APP = ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "albumcat"

TAGGER_SRC = APP / "TaggerClient.kt"
JOURNAL_SRC = APP / "Journal.kt"
PATHS_SRC = APP / "DocumentPaths.kt"
XMP_SRC = APP / "Xmp.kt"
RESCAN_SRC = APP / "Rescan.kt"
RUNNER_SRC = APP / "TagRunner.kt"

# How many failures in a row TagRunner.Options defaults to. The abort scenario
# asserts the run stops here rather than trying all 12 files.
DEFAULT_MAX_FAILURES = 8

# --------------------------------------------------------------------------- #
# Android shims. One file per package: Kotlin allows one package per file.
# --------------------------------------------------------------------------- #

URI_KT = r'''package android.net

/**
 * Enough Uri for TagRunner/Rescan/Journal: parse, toString, authority and the
 * last path segment. Nothing here needs Android's full URI machinery.
 */
class Uri private constructor(private val raw: String) {

    val authority: String?
        get() {
            val i = raw.indexOf("//")
            if (i < 0) return null
            val rest = raw.substring(i + 2)
            val slash = rest.indexOf('/')
            val host = if (slash < 0) rest else rest.substring(0, slash)
            return host.ifEmpty { null }
        }

    val lastPathSegment: String?
        get() = java.net.URLDecoder.decode(raw.substringAfterLast('/', ""), "UTF-8")
            .ifEmpty { null }

    override fun toString(): String = raw
    override fun equals(other: Any?): Boolean = other is Uri && other.raw == raw
    override fun hashCode(): Int = raw.hashCode()

    companion object {
        fun parse(s: String): Uri = Uri(s)
    }
}
'''

CONTENT_KT = r'''package android.content

import android.net.Uri
import java.io.File
import java.io.InputStream
import java.io.OutputStream

/**
 * `Journal.kt` uses only `filesDir`; `TagRunner.kt` uses `contentResolver` and
 * the two open*Stream calls. Declared as Kotlin `val`s because the property
 * syntax only resolves automatically against *Java* getters.
 */
abstract class Context {
    abstract val filesDir: File
    abstract val contentResolver: ContentResolver
}

abstract class ContentResolver {
    abstract fun openInputStream(uri: Uri): InputStream?
    abstract fun openOutputStream(uri: Uri, mode: String): OutputStream?
}
'''

UTIL_KT = r'''package android.util

import java.util.Base64 as JBase64

/** Android's NO_WRAP means "one line, padded" -- exactly java.util's encoder. */
object Base64 {
    const val DEFAULT = 0
    const val NO_PADDING = 1
    const val NO_WRAP = 2
    const val CRLF = 4
    const val URL_SAFE = 8

    private fun enc(flags: Int) =
        (if (flags and URL_SAFE != 0) JBase64.getUrlEncoder() else JBase64.getEncoder())
            .let { if (flags and NO_PADDING != 0) it.withoutPadding() else it }

    private fun dec(flags: Int) =
        if (flags and URL_SAFE != 0) JBase64.getUrlDecoder() else JBase64.getDecoder()

    fun encodeToString(input: ByteArray, flags: Int): String = enc(flags).encodeToString(input)
    fun encode(input: ByteArray, flags: Int): ByteArray = enc(flags).encode(input)
    fun decode(str: String, flags: Int): ByteArray = dec(flags).decode(str)
    fun decode(input: ByteArray, flags: Int): ByteArray = dec(flags).decode(input)
}

object Log {
    val lines = ArrayList<String>()
    fun d(tag: String, msg: String): Int { lines += "d/$tag $msg"; return 0 }
    fun i(tag: String, msg: String): Int { lines += "i/$tag $msg"; return 0 }
    fun w(tag: String, msg: String): Int { lines += "w/$tag $msg"; return 0 }
    fun w(tag: String, msg: String, t: Throwable): Int {
        lines += "w/$tag $msg (${t.javaClass.simpleName})"; return 0
    }
    fun e(tag: String, msg: String): Int { lines += "e/$tag $msg"; return 0 }
    fun e(tag: String, msg: String, t: Throwable): Int {
        lines += "e/$tag $msg (${t.javaClass.simpleName})"; return 0
    }
}
'''

MEDIA_KT = r'''package android.media

import android.content.Context

/** Records every call so the rescan step can be asserted from Python. */
object MediaScannerConnection {
    val scanned = ArrayList<String>()
    var calls = 0

    fun scanFile(
        context: Context?,
        paths: Array<String>,
        mimeTypes: Array<String>?,
        callback: Any?,
    ) {
        calls++
        scanned += paths
    }
}
'''

PROVIDER_KT = r'''package android.provider

import android.net.Uri

/**
 * Parses the document id out of a real SAF document URI:
 *
 *   content://com.android.externalstorage.documents/tree/primary%3ADCIM
 *           /document/primary%3ADCIM%2FCamera%2FIMG_1.jpg
 *
 * Throws when there is no document segment, like the real one does -- Rescan
 * relies on that try/catch for tree URIs and exotic providers.
 */
object DocumentsContract {
    fun getDocumentId(uri: Uri): String {
        val s = uri.toString()
        val marker = "/document/"
        val i = s.lastIndexOf(marker)
        if (i < 0) throw IllegalArgumentException("no document id in $uri")
        return java.net.URLDecoder.decode(s.substring(i + marker.length), "UTF-8")
    }
}
'''

DOCFILE_KT = r'''package androidx.documentfile.provider

import android.content.Context
import android.net.Uri

/** TagRunner only ever calls fromTreeUri and the instance accessors. */
abstract class DocumentFile {
    abstract val name: String?
    abstract val uri: Uri
    abstract val isDirectory: Boolean
    abstract fun listFiles(): Array<DocumentFile>
    abstract fun length(): Long
    abstract fun lastModified(): Long

    companion object {
        var provider: ((Context, Uri) -> DocumentFile?)? = null
        fun fromTreeUri(context: Context, treeUri: Uri): DocumentFile? =
            provider?.invoke(context, treeUri)
    }
}
'''

THUMB_KT = r'''package com.albumcat

import android.content.ContentResolver
import android.net.Uri

/**
 * Stands in for the real Thumbnailer (which needs BitmapFactory/ExifInterface).
 * The Python side pre-renders real 448px thumbnails with the same code
 * simulate_phone uses, so the server is asked to tag a genuine thumbnail.
 */
object Thumbnailer {
    var source: ((Uri) -> ByteArray)? = null

    fun thumbnail(resolver: ContentResolver, uri: Uri): ByteArray =
        source?.invoke(uri) ?: throw IllegalStateException("Thumbnailer stub has no source")
}
'''

DRIVER_KT = r'''package com.albumcat

import android.content.ContentResolver
import android.content.Context
import android.media.MediaScannerConnection
import android.net.Uri
import android.util.Log
import androidx.documentfile.provider.DocumentFile
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.InputStream
import java.io.OutputStream
import java.net.URLEncoder

// ---------------------------------------------------------------- the fake SAF

private fun enc(s: String): String = URLEncoder.encode(s, "UTF-8")

private const val BASE = "content://com.android.externalstorage.documents"

class FakeDir(override val name: String, val docId: String) : DocumentFile() {
    val children = ArrayList<DocumentFile>()
    override val uri: Uri get() = Uri.parse("$BASE/tree/" + enc(docId))
    override val isDirectory: Boolean get() = true
    override fun listFiles(): Array<DocumentFile> = children.toTypedArray()
    override fun length(): Long = 0L
    override fun lastModified(): Long = 0L
}

class FakeFile(
    override val name: String,
    val docId: String,
    var content: ByteArray,
) : DocumentFile() {
    override val uri: Uri
        get() = Uri.parse("$BASE/tree/primary%3ADCIM/document/" + enc(docId))
    override val isDirectory: Boolean get() = false
    override fun listFiles(): Array<DocumentFile> = emptyArray()
    override fun length(): Long = content.size.toLong()
    override fun lastModified(): Long = 1700000000000L
}

class FakeResolver : ContentResolver() {
    val store = HashMap<String, ByteArray>()
    var reads = 0
    var writes = 0

    /** URIs whose writes throw, to model a full disk or a revoked grant. */
    val refuse = HashSet<String>()

    override fun openInputStream(uri: Uri): InputStream? {
        reads++
        val b = store[uri.toString()] ?: return null
        return ByteArrayInputStream(b)
    }

    override fun openOutputStream(uri: Uri, mode: String): OutputStream? {
        val key = uri.toString()
        if (key in refuse) {
            // What a full disk or a revoked SAF grant looks like: the mode is
            // accepted, the write itself blows up.
            throw java.io.IOException("模拟写入失败：存储空间不足")
        }
        return object : ByteArrayOutputStream() {
            private var committed = false
            override fun close() {
                if (!committed) {
                    committed = true
                    writes++
                    store[key] = toByteArray()
                }
                super.close()
            }
        }
    }
}

// ------------------------------------------------------------------ reporting

private fun sha(b: ByteArray): String {
    val d = java.security.MessageDigest.getInstance("SHA-256").digest(b)
    return d.joinToString("") { (it.toInt() and 0xFF).toString(16).padStart(2, '0') }
}

private fun report(ev: TagRunner.Event) {
    when (ev) {
        is TagRunner.Event.Scanning ->
            println("SCAN\t${ev.found}\t${ev.otherImages}\t${ev.truncated}")
        is TagRunner.Event.Message ->
            println("MSG\t" + ev.text.replace("\n", " "))
        is TagRunner.Event.Item ->
            println("ITEM\t${ev.index}\t${ev.total}\t${ev.name}\t${ev.bad}\t${ev.line}")
    }
}

private fun printSummary(prefix: String, s: TagRunner.Summary) {
    println(prefix + "\t" + listOf(
        s.runId, s.total, s.processed, s.written, s.skipped, s.failed,
        s.dryRun, s.otherImages, s.truncated, s.aborted,
    ).joinToString("\t") + "\t" + s.abortReason)
}

/**
 * Where to write a copy of every file the scenario touched, as real bytes.
 *
 * Hashes prove "unchanged" and text search proves "tags went in", but neither
 * can prove the container is still valid -- so when a directory is given the
 * bytes are dropped there and Python opens them with a real parser.
 */
private var dumpDir: String? = null

/** One line per file: what is "on disk" now, so Python can compare hashes. */
private fun printStore(prefix: String, files: List<FakeFile>, resolver: FakeResolver) {
    for (f in files) {
        // Read through the resolver, not the FakeFile field: the field is the
        // primed original, the resolver is the actual "disk".
        val content = resolver.store[f.uri.toString()] ?: f.content
        val text = String(content, Charsets.UTF_8)
        val rel = f.docId.removePrefix("primary:DCIM/")
        println(prefix + "\t" + listOf(
            rel,
            content.size,
            sha(content),
            text.contains("x:xmptk=\"albumcat\""),
            text.contains("角色|"),
            // First 8 bytes. The whole point of the PNG path is that a PNG
            // stays a PNG -- a writer that spliced a JPEG APP1 segment into it
            // would still pass every text-based check above.
            content.take(8).joinToString("") { "%02x".format(it) },
        ).joinToString("\t"))
        val d = dumpDir
        if (d != null) {
            val out = File(d, prefix + "-" + rel.replace('/', '_'))
            out.parentFile?.mkdirs()
            out.writeBytes(content)
        }
    }
}

private fun printScans(prefix: String) {
    println(prefix + "\t" + MediaScannerConnection.scanned.size + "\t" +
            MediaScannerConnection.scanned.joinToString("\u0001"))
}

private fun resetScans() {
    MediaScannerConnection.scanned.clear()
    MediaScannerConnection.calls = 0
}

// ----------------------------------------------------------------------- main

fun main(args: Array<String>) {
    val url = args[0]
    val token = args[1]
    val jdir = args[2]
    val scenario = args[3]
    val manifest = File(args[4])
    if (args.size > 5 && args[5].isNotEmpty()) dumpDir = args[5]

    val resolver = FakeResolver()
    val root = FakeDir("DCIM", "primary:DCIM")
    val dirs = HashMap<String, FakeDir>()
    dirs[""] = root
    val files = ArrayList<FakeFile>()
    val thumbs = HashMap<String, ByteArray>()
    val refused = ArrayList<String>()

    for (line in manifest.readLines()) {
        if (line.isBlank()) continue
        val p = line.split("\t")
        when (p[0]) {
            "dir" -> {
                val rel = p[1]
                val parent = dirs[rel.substringBeforeLast('/', "")] ?: root
                val d = FakeDir(rel.substringAfterLast('/'), "primary:DCIM/$rel")
                parent.children += d
                dirs[rel] = d
            }
            "file" -> {
                val rel = p[1]
                val bytes = File(p[2]).readBytes()
                val f = FakeFile(rel.substringAfterLast('/'), "primary:DCIM/$rel", bytes)
                val parent = dirs[rel.substringBeforeLast('/', "")] ?: root
                parent.children += f
                files += f
                resolver.store[f.uri.toString()] = bytes
                thumbs[f.uri.toString()] = File(p[3]).readBytes()
            }
            // A file whose content is irrelevant -- a PNG, a .nomedia, whatever.
            // Only its suffix matters, so it never enters `files` (nothing to
            // hash) and never reaches the resolver.
            "plain" -> {
                val rel = p[1]
                val f = FakeFile(rel.substringAfterLast('/'), "primary:DCIM/$rel", ByteArray(0))
                val parent = dirs[rel.substringBeforeLast('/', "")] ?: root
                parent.children += f
            }
            // This file's writes will throw.  Used to prove the journal entry
            // is on disk *before* the photo is touched.
            "refuse" -> refused += p[1]
        }
    }
    for (rel in refused) {
        resolver.refuse += Uri.parse(
            "$BASE/tree/primary%3ADCIM/document/" + enc("primary:DCIM/$rel")
        ).toString()
    }

    DocumentFile.provider = { _, _ -> root }
    // The abort scenario never reaches the server, so one thumbnail stands in
    // for all of them.
    Thumbnailer.source = { u -> thumbs[u.toString()] ?: thumbs.values.first() }

    val ctx = object : Context() {
        override val filesDir: File = File(jdir).also { it.mkdirs() }
        override val contentResolver: ContentResolver = resolver
    }
    val journal = Journal(ctx)
    val runner = TagRunner(ctx, TaggerClient(url, token), journal)

    when (scenario) {
        "dry" -> {
            val s = runner.run(root.uri, TagRunner.Options(dryRun = true)) { report(it) }
            printSummary("SUM", s)
            printStore("STORE", files, resolver)
            println("JOURNAL\t" + journal.read().size)
            printScans("SCANNED1")
            println("SCAN_CALLS\t${MediaScannerConnection.calls}")
        }

        "write" -> {
            val s1 = runner.run(root.uri, TagRunner.Options(dryRun = false)) { report(it) }
            printSummary("SUM", s1)
            printStore("STORE", files, resolver)
            printScans("SCANNED1")
            println("SCAN_CALLS\t${MediaScannerConnection.calls}")

            // Second pass: everything was just tagged, so it must all be
            // skipped -- and the bytes must not move.
            resetScans()
            val s2 = runner.run(root.uri, TagRunner.Options(dryRun = false)) { report(it) }
            printSummary("SUM2", s2)
            printStore("STORE2", files, resolver)
            printScans("SCANNED_SKIP")

            resetScans()
            val restored = runner.undo(s1.runId) { report(it) }
            println("UNDO\t$restored")
            printStore("STORE3", files, resolver)
            println("JOURNAL\t" + journal.read().size)
            println("WRITES\t${resolver.writes}")
            printScans("SCANNED2")
            println("SCAN_CALLS\t${MediaScannerConnection.calls}")
        }

        // One file refuses every write.  The journal entry for it must already
        // exist by then, otherwise a crash inside the write window would leave
        // a half-written photo with no recorded original.
        "failwrite" -> {
            val s = runner.run(root.uri, TagRunner.Options(dryRun = false)) { report(it) }
            printSummary("SUM", s)
            printStore("STORE", files, resolver)
            println("JOURNAL\t" + journal.read().size)

            resetScans()
            val restored = runner.undo(s.runId) { report(it) }
            println("UNDO\t$restored")
            printStore("STORE2", files, resolver)
            println("JOURNAL2\t" + journal.read().size)
        }

        "abort" -> {
            val s = runner.run(root.uri, TagRunner.Options(dryRun = false)) { report(it) }
            printSummary("SUM", s)
            printStore("STORE", files, resolver)
        }

        // The user taps 停止 while the fourth photo is in flight.
        //
        // Stop has to mean "end *between* photos": the three already written
        // must stay valid and re-readable, the two never reached must be
        // untouched to the byte, and the journal must still make undo restore
        // all five.  The tap is modelled where it really happens -- in the
        // event callback, i.e. the UI thread -- which is exactly why the flag
        // is volatile and why the check sits at the top of the loop.
        "stop" -> {
            var seen = 0
            val s = runner.run(root.uri, TagRunner.Options(dryRun = false)) { e ->
                report(e)
                if (e is TagRunner.Event.Item) {
                    seen++
                    if (seen == 3) runner.requestStop()
                }
            }
            printSummary("SUM", s)
            printStore("STORE", files, resolver)
            println("JOURNAL\t" + journal.read().size)
            println("WRITES\t${resolver.writes}")
            printScans("SCANNED1")

            resetScans()
            val restored = runner.undo(s.runId) { report(it) }
            println("UNDO\t$restored")
            printStore("STORE2", files, resolver)
            println("JOURNAL2\t" + journal.read().size)
        }

        // 70 files with Rescan.BATCH = 64 must produce exactly two scanFile
        // calls, not seventy.
        "batch" -> {
            val s = runner.run(root.uri, TagRunner.Options(dryRun = false)) { report(it) }
            printSummary("SUM", s)
            printScans("SCANNED1")
            println("SCAN_CALLS\t${MediaScannerConnection.calls}")
        }
    }

    println("LOG\t" + Log.lines.size)
}
'''


# --------------------------------------------------------------------------- #
# building the fake album
# --------------------------------------------------------------------------- #

def build_manifest(files: list[tuple[str, Path, Path]],
                   plain: list[str] = (),
                   refuse: list[str] = ()) -> str:
    """files: (relative path under DCIM, source jpeg, thumbnail) -> manifest text.

    `plain` entries are files whose bytes do not matter (a PNG, a .nomedia).
    `refuse` entries are files whose writes will throw.
    Parent directories must come before their children; DocumentFile's fake tree
    is assembled in read order.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for rel in [f[0] for f in files] + list(plain) + list(refuse):
        parts = rel.split("/")
        for depth in range(1, len(parts)):
            d = "/".join(parts[:depth])
            if d not in seen:
                seen.add(d)
                lines.append(f"dir\t{d}")
    for rel, src, thumb in files:
        lines.append(f"file\t{rel}\t{src}\t{thumb}")
    for rel in plain:
        lines.append(f"plain\t{rel}")
    for rel in refuse:
        lines.append(f"refuse\t{rel}")
    return "\n".join(lines) + "\n"


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def __call__(self, label: str, ok: bool, detail: str = "") -> bool:
        if ok:
            self.passed += 1
            print(f"  [OK  ] {label}" + (f"  {detail}" if detail else ""))
        else:
            self.failed.append(label)
            print(f"  [FAIL] {label}" + (f"  {detail}" if detail else ""))
        return ok


def parse(out: str) -> tuple[dict[str, list[str]], list[list[str]], list[list[str]]]:
    """-> (single-value lines, listed lines, item lines)"""
    listed_keys = {"STORE", "STORE2", "STORE3", "MSG", "ITEM"}
    single_keys = {"SCAN", "SUM", "SUM2", "UNDO", "JOURNAL", "JOURNAL2",
                   "WRITES", "LOG", "SCANNED1", "SCANNED2", "SCANNED_SKIP",
                   "SCAN_CALLS"}
    single: dict[str, list[str]] = {}
    listed: list[list[str]] = []
    items: list[list[str]] = []
    for ln in out.splitlines():
        if not ln.strip():
            continue
        p = ln.split("\t")
        if p[0] in listed_keys:
            listed.append(p)
        elif p[0] in single_keys:
            single[p[0]] = p[1:]
    return single, listed, items


def store_of(listed: list[list[str]], tag: str) -> dict[str, list[str]]:
    return {p[1]: p[2:] for p in listed if p[0] == tag}


def as_list_pillow(value) -> list[str]:
    """Pillow collapses a one-item rdf:Bag into a bare string."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    items = value.get("Bag", {}).get("li", []) if isinstance(value, dict) else value
    return [items] if isinstance(items, str) else list(items)


def wait_for_health(base: str, proc: subprocess.Popen, timeout: float = 240.0) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            sys.exit(f"server exited early (rc={proc.returncode})")
        try:
            with urllib.request.urlopen(base + "/health", timeout=5) as r:
                json.loads(r.read().decode("utf-8"))
                return
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            last = str(exc)
            time.sleep(1.0)
    sys.exit(f"server never became healthy: {last}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--samples", default=str(ROOT / "samples"))
    ap.add_argument("--port", type=int, default=8902)
    args = ap.parse_args(argv)

    from simulate_phone import phone_thumbnail  # noqa: E402
    from PIL import Image  # noqa: E402

    samples = sorted(p for p in Path(args.samples).glob("*")
                     if p.suffix.lower() in {".jpg", ".jpeg"})
    if len(samples) < 3:
        sys.exit(f"need at least 3 JPEGs in {args.samples}, found {len(samples)}")

    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    thumb_dir = WORK / "thumbs"
    thumb_dir.mkdir()

    # A deliberately messy album: nested dirs, a dotfile, a non-image file, and
    # a format we cannot write.  All 4 images are work -- including the PNG,
    # because an Android screenshot *is* a PNG and refusing those would refuse
    # exactly the pictures this project exists for.  The HEIC must be *counted*
    # (so the user learns why it was skipped) while the .nomedia and the .txt
    # are not images at all and must not be.
    work_rels = [
        "Camera/a.jpg",
        "Camera/b.jpg",
        "Pictures/deep/d.jpg",
        "Screenshots/shot.png",
    ]
    plain_files = [
        "Camera/.nomedia",
        "Camera/note.txt",
        "Pictures/doll.heic",          # an image we cannot write -> counted
    ]
    # Shaped like a screenshot: same picture, PNG container. Deliberately the
    # sample that *does* have a recognisable character, so the PNG checks below
    # cannot pass vacuously on an empty tag list.
    shot = WORK / "shot.png"
    with Image.open(samples[2]) as im:
        im.convert("RGB").save(shot, "PNG")
    work_sources = [samples[0], samples[1], samples[2], shot]

    work_files: list[tuple[str, Path, Path]] = []
    originals: dict[str, tuple[int, str]] = {}
    for rel, src in zip(work_rels, work_sources):
        thumb = thumb_dir / (Path(rel).stem + ".jpg")
        thumb.write_bytes(phone_thumbnail(src))
        work_files.append((rel, src, thumb))
        data = src.read_bytes()
        originals[rel] = (len(data), hashlib.sha256(data).hexdigest())

    manifest = WORK / "manifest.txt"
    manifest.write_text(build_manifest(work_files, plain_files), encoding="utf-8")

    # Abort scenario: 12 JPEGs, same source, against a port nothing listens on.
    abort_files = []
    for i in range(12):
        rel = f"Bulk/f{i:02d}.jpg"
        thumb = thumb_dir / "abort.jpg"
        if not thumb.exists():
            thumb.write_bytes(phone_thumbnail(samples[0]))
        abort_files.append((rel, samples[0], thumb))
    abort_manifest = WORK / "manifest_abort.txt"
    abort_manifest.write_text(build_manifest(abort_files), encoding="utf-8")

    # Batch scenario: 70 files against Rescan.BATCH = 64.  A per-photo scanFile
    # on a real album is thousands of binder round-trips, so the flushing has to
    # be verified, not assumed.
    batch_files = []
    for i in range(70):
        rel = f"Batch/g{i:03d}.jpg"
        thumb = thumb_dir / "batch.jpg"
        if not thumb.exists():
            thumb.write_bytes(phone_thumbnail(samples[0]))
        batch_files.append((rel, samples[0], thumb))
    batch_manifest = WORK / "manifest_batch.txt"
    batch_manifest.write_text(build_manifest(batch_files), encoding="utf-8")

    # Fail-write scenario: three photos, the middle one refuses every write the
    # way a full disk does.  The journal entry for it must already be on disk
    # before the write is attempted -- that ordering *is* the safety property.
    fail_rels = ["Fail/a.jpg", "Fail/b.jpg", "Fail/c.jpg"]
    fail_files: list[tuple[str, Path, Path]] = []
    fail_originals: dict[str, tuple[int, str]] = {}
    for rel, src in zip(fail_rels, samples[:3]):
        thumb = thumb_dir / ("fail-" + Path(rel).stem + ".jpg")
        thumb.write_bytes(phone_thumbnail(src))
        fail_files.append((rel, src, thumb))
        data = src.read_bytes()
        fail_originals[rel] = (len(data), hashlib.sha256(data).hexdigest())
    fail_manifest = WORK / "manifest_fail.txt"
    fail_manifest.write_text(
        build_manifest(fail_files, refuse=["Fail/b.jpg"]), encoding="utf-8")

    # Stop scenario: five photos, and the tap lands after the third.  The first
    # three must be complete and valid, the last two must not have been touched
    # at all -- "I pressed stop" must never mean "one photo is half written".
    stop_files: list[tuple[str, Path, Path]] = []
    stop_originals: dict[str, tuple[int, str]] = {}
    for i in range(5):
        rel = f"Stop/s{i}.jpg"
        src = samples[i % len(samples)]
        thumb = thumb_dir / f"stop-{i}.jpg"
        thumb.write_bytes(phone_thumbnail(src))
        stop_files.append((rel, src, thumb))
        data = src.read_bytes()
        stop_originals[rel] = (len(data), hashlib.sha256(data).hexdigest())
    stop_manifest = WORK / "manifest_stop.txt"
    stop_manifest.write_text(build_manifest(stop_files), encoding="utf-8")

    print("fetching org.json")
    jar = fetch_json_jar()

    print("compiling TagRunner.kt + Xmp/Journal/Rescan/DocumentPaths for the desktop JVM")
    shims = {}
    for name, text in [
        ("Uri.kt", URI_KT), ("Content.kt", CONTENT_KT), ("Util.kt", UTIL_KT),
        ("Media.kt", MEDIA_KT), ("Provider.kt", PROVIDER_KT),
        ("DocumentFile.kt", DOCFILE_KT), ("Thumbnailer.kt", THUMB_KT),
    ]:
        p = WORK / name
        p.write_text(text, encoding="utf-8")
        shims[name] = p

    runner = build_runner(
        WORK,
        sources=[RUNNER_SRC, XMP_SRC, JOURNAL_SRC, RESCAN_SRC, PATHS_SRC,
                 TAGGER_SRC, *shims.values()],
        driver=DRIVER_KT, driver_name="RunnerProbe.kt",
        main_class="com.albumcat.RunnerProbeKt", extra_cp=[jar],
    )

    base = f"http://127.0.0.1:{args.port}"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    print(f"starting albumcat serve on {base}")
    server = subprocess.Popen(
        [sys.executable, "-m", "albumcat", "serve", "--host", "127.0.0.1",
         "--port", str(args.port)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    check = Checker()
    try:
        wait_for_health(base, server)
        model = json.loads(urllib.request.urlopen(base + "/health", timeout=10)
                           .read().decode("utf-8")).get("model") or {}
        print(f"server up: dim={model.get('embedding_dim')}")
        print()

        # Ask the server what it says about each thumbnail, so the XMP checks
        # below can be "the picture contains exactly the tag tree that came
        # back" instead of a guess about which sample happens to have a
        # recognisable character in it.
        expected_char: dict[str, bool] = {}
        for rel, _src, thumb in work_files:
            req = urllib.request.Request(
                base + "/plan",
                data=json.dumps({"image": base64.b64encode(thumb.read_bytes()).decode(),
                                 "id": rel}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=180) as r:
                res = json.loads(r.read().decode("utf-8"))
            chars = res.get("characters") or []
            expected_char[rel] = bool(chars)
            print(f"  plan {rel}: {[c['label'] for c in chars] or '（没有角色）'}"
                  f"  flat={len(res.get('flat') or [])} review={res.get('needs_review')}")
        print()

        # ---------------------------------------------------------- dry run
        print("scenario: dry (nothing may be written)")
        jdir = WORK / "journal-dry"
        out = kotlin(runner, base, "", str(jdir), "dry", str(manifest), check=False)
        single, listed, _ = parse(out)
        scan = single.get("SCAN")
        if not check("探针有输出", scan is not None, out.splitlines()[-1] if out else "无输出"):
            return 1

        check("扫描：找到 4 张能写的图（3 JPEG + 1 PNG）", scan[0] == "4", scan[0])
        check("扫描：1 张 HEIC 被计数而不是被无视", scan[1] == "1", scan[1])
        check("扫描：没触发截断", scan[2] == "false", scan[2])

        s = single["SUM"]
        check("dry run：一张都没写", s[3] == "0" and s[8] == "false", f"written={s[3]}")
        check("dry run：没有日志行，无从撤销也无从误撤销",
              single["JOURNAL"][0] == "0", single["JOURNAL"][0])
        store = store_of(listed, "STORE")
        check("dry run：4 张照片的字节一个都没动",
              all(v[2] == "false" for v in store.values()) and len(store) == 4,
              str(sorted(store)))

        # -------------------------------------------------------- write path
        print()
        print("scenario: write (write -> re-run -> undo)")
        jdir = WORK / "journal-write"
        dumped = WORK / "dumped"
        out = kotlin(runner, base, "", str(jdir), "write", str(manifest),
                     str(dumped), check=False)
        single, listed, items = parse(out)
        s1, s2 = single["SUM"], single["SUM2"]

        check("写入：4 张全部成功", s1[3] == "4" and s1[5] == "0", f"written={s1[3]} failed={s1[5]}")
        check("写入：没有半路停", s1[9] == "false", s1[9])

        after = store_of(listed, "STORE")
        check("写入后：4 张（含 PNG 那张）都带上了 albumcat 标记（重复跑才知道要跳过）",
              len(after) == 4 and all(v[2] == "true" for v in after.values()),
              str(sorted((k, v[2]) for k, v in after.items())))
        check("写入后：扁平 dc:subject 里带了层级路径（Aves 只读这个字段）",
              all(v[3] == ("true" if expected_char[rel] else "false")
                  for rel, v in after.items()),
              str(sorted((rel, v[3], expected_char[rel]) for rel, v in after.items())))
        check("写入后：至少有一张真的认出了角色，否则上一条等于没验",
              any(expected_char.values()), str(expected_char))
        check("写入后：文件变大了（插了 XMP 段，像素没重编码）",
              all(int(after[k][0]) > originals[k][0] for k in originals),
              str([(k, after[k][0], originals[k][0]) for k in originals]))

        # ---------------------------------------------------- the PNG one
        # Everything above would also pass if TagRunner had written a JPEG APP1
        # segment into the middle of a PNG: the marker text would be there and
        # the file would be bigger. So look at the bytes with a real parser.
        png_rel = "Screenshots/shot.png"
        kt_png = dumped / f"STORE-{png_rel.replace('/', '_')}"
        check(f"PNG：写完后头 8 字节仍是 PNG 签名（没被当成 JPEG 处理）",
              after[png_rel][4] == "89504e470d0a1a0a", after[png_rel][4])
        if kt_png.exists():
            with Image.open(kt_png) as im:
                im.load()
                got_size = im.size
                got_px = im.convert("RGB").tobytes()
                meta = im.getxmp()
            with Image.open(shot) as ref:
                want_px = ref.convert("RGB").tobytes()
                want_size = ref.size
            desc = meta.get("xmpmeta", {}).get("RDF", {}).get("Description", {})
            subjects = as_list_pillow(desc.get("subject"))
            hier = as_list_pillow(desc.get("HierarchicalSubject"))
            check(f"PNG：Pillow 能打开 App 写出来的文件，尺寸不变",
                  got_size == want_size, f"{got_size} vs {want_size}")
            check(f"PNG：像素逐个相同（App 只加了元数据）",
                  got_px == want_px, f"{len(got_px)} bytes")
            check(f"PNG：Pillow 从 iTXt 里读到了 dc:subject",
                  bool(subjects), repr(subjects[:4]))
            check(f"PNG：认出的角色写进了 dc:subject（不是只写了 general 标签）",
                  any(s.startswith("角色|") for s in subjects),
                  repr([s for s in subjects if s.startswith("角色|")]))
            check(f"PNG：层级路径也在 dc:subject 里（Aves 只读这个字段，所以必须并进 flat）",
                  bool(hier) and all(h in subjects for h in hier),
                  f"hier={hier[:3]} subjects={subjects[:6]}")
        else:
            check(f"PNG：App 写出的字节被导出到 {kt_png.relative_to(ROOT)}", False,
                  "文件不存在")

        check("重跑：4 张全部跳过，一张都没重写",
              s2[3] == "0" and s2[4] == "4", f"written={s2[3]} skipped={s2[4]}")
        again = store_of(listed, "STORE2")
        check("重跑：字节完全没变（跳过是真的跳过，不是又写了一遍一样的内容）",
              all(again[k][1] == after[k][1] for k in after), "ok")

        restored = single["UNDO"][0]
        check("撤销：4 张（含 PNG）全部还原", restored == "4", restored)
        back = store_of(listed, "STORE3")
        for rel, (size, digest) in originals.items():
            v = back.get(rel)
            check(f"撤销后 {rel} 与原件逐字节相同",
                  v is not None and v[1] == digest,
                  f"{v[1][:12] if v else '?'} vs {digest[:12]}")
        check("撤销：albumcat 标记也没了（XMP 段被整段摘掉）",
              all(v[2] == "false" for v in back.values()), "ok")
        check("撤销成功后：本次 run 的日志行已被清掉（再点一次撤销不会重放）",
              single["JOURNAL"][0] == "0", single["JOURNAL"][0])

        scans = single["SCANNED1"]
        paths = scans[1].split("\u0001") if len(scans) > 1 and scans[1] else []
        check("通知媒体库：写完的 4 张都报了路径",
              scans[0] == "4" and len(paths) == 4, f"{scans[0]} -> {len(paths)}")
        check("通知媒体库：路径是 /storage/emulated/0/... 这样扫描器认的形式",
              all(p.startswith("/storage/emulated/0/DCIM/") for p in paths),
              str(paths[:1]))
        check("通知媒体库：撤销也把还原的 4 张报了一遍",
              single["SCANNED2"][0] == "4", single["SCANNED2"][0])
        check("通知媒体库：重跑时全部跳过，于是没有多余的扫描",
              single["SCANNED_SKIP"][0] == "0", single["SCANNED_SKIP"][0])

        # ------------------------------------------------------------ batch
        print()
        print("scenario: batch (70 张 -> 媒体扫描要分批，不是一张一次)")
        out = kotlin(runner, base, "", str(WORK / "journal-batch"),
                     "batch", str(batch_manifest), check=False)
        single, listed, items = parse(out)
        s = single["SUM"]
        check("批量：70 张全部写入成功", s[3] == "70" and s[5] == "0",
              f"written={s[3]} failed={s[5]}")
        check("批量：70 张都报给了媒体库", single["SCANNED1"][0] == "70",
              single["SCANNED1"][0])
        check("批量：scanFile 只被调了 2 次（64 + 6），不是 70 次",
              single["SCAN_CALLS"][0] == "2", single["SCAN_CALLS"][0])

        # ------------------------------------------------------------ abort
        print()
        print("scenario: abort (服务端不可达)")
        dead = args.port + 1
        jdir = WORK / "journal-abort"
        out = kotlin(runner, f"http://127.0.0.1:{dead}", "", str(jdir),
                     "abort", str(abort_manifest), check=False)
        single, listed, items = parse(out)
        s = single["SUM"]
        msgs = [p[1] for p in listed if p[0] == "MSG"]

        check("断线：扫描到 12 张", s[1] == "12", s[1])
        check(f"断线：连续失败 {DEFAULT_MAX_FAILURES} 次就停了，没有把 12 张全试一遍",
              s[9] == "true" and s[2] == str(DEFAULT_MAX_FAILURES) and s[5] == str(DEFAULT_MAX_FAILURES),
              f"processed={s[2]} failed={s[5]} aborted={s[9]}")
        check("断线：明确告诉了用户为什么停下",
              any("连续" in m and "失败" in m for m in msgs),
              next((m[:60] for m in msgs if "连续" in m), "没有这样的提示"))
        check("断线：说明了常见原因（服务关了/休眠了/不在同一个 WiFi）",
              any("albumcat serve" in m and "WiFi" in m for m in msgs),
              next((m[:60] for m in msgs if "WiFi" in m), "没有这样的提示"))
        broken = store_of(listed, "STORE")
        check("断线：一张照片都没被改动过",
              all(v[2] == "false" for v in broken.values()), "ok")

        # -------------------------------------------------------- failwrite
        print()
        print("scenario: failwrite (写到一半失败 -> 日志必须已经记下了原件)")
        out = kotlin(runner, base, "", str(WORK / "journal-fail"),
                     "failwrite", str(fail_manifest), check=False)
        single, listed, items = parse(out)
        s = single["SUM"]
        check("写失败：2 张成功、1 张失败，没有把整批都算成坏",
              s[3] == "2" and s[5] == "1", f"written={s[3]} failed={s[5]}")
        # THE point of this scenario.  If the journal were still written after
        # the write, the row for Fail/b.jpg would not exist -- and a crash in
        # `writeAll`'s truncate-then-stream window would then have destroyed the
        # only copy of that photo's original XMP.
        check("写失败：失败的第三张也已经记进日志（证明是先记日志再写字）",
              single["JOURNAL"][0] == "3", single["JOURNAL"][0])

        u = single["UNDO"][0]
        check("写失败：撤销还原了 2 张，失败那张本来就没动",
              u == "2", u)
        check("写失败：撤销后日志清空", single["JOURNAL2"][0] == "0",
              single["JOURNAL2"][0])
        bad_store = []
        for rel, (size, sha) in fail_originals.items():
            v = store_of(listed, "STORE2").get(rel)
            if v is None or v[0] != str(size) or v[1] != sha:
                bad_store.append(f"{rel}: {v[0] if v else '-'}/{v[1][:12] if v else '-'}"
                                 f" want {size}/{sha[:12]}")
        check("写失败：三张（含写失败那张）都回到了原件的字节",
              not bad_store, "; ".join(bad_store) or "ok")

        # ------------------------------------------------------------- stop
        print()
        print("scenario: stop (第 4 张时按下停止)")
        out = kotlin(runner, base, "", str(WORK / "journal-stop"),
                     "stop", str(stop_manifest), check=False)
        single, listed, items = parse(out)
        s = single["SUM"]
        check("停止：5 张里只处理了 3 张就真的停了",
              s[2] == "3" and s[1] == "5", f"processed={s[2]} total={s[1]}")
        check("停止：算作「用户按停」而不是「出错」",
              s[9] == "true" and s[5] == "0" and s[10] == "手动停止",
              f"aborted={s[9]} failed={s[5]} reason={s[10]!r}")
        check("停止：已处理的 3 张都写完了（不是第 3 张写一半）",
              s[3] == "3", f"written={s[3]}")

        stopped = store_of(listed, "STORE")
        stop_rels = [rel for rel, _s, _t in stop_files]
        written_rels, untouched_rels = stop_rels[:3], stop_rels[3:]
        check("停止：写过的 3 张都完整地带着 albumcat 标记",
              all(stopped[r][2] == "true" for r in written_rels),
              str([(r, stopped[r][2]) for r in written_rels]))
        broken = [r for r in untouched_rels
                  if stopped[r][1] != stop_originals[r][1]]
        check("停止：没轮到的 2 张一个字节都没动过",
              not broken, "; ".join(f"{r} 变了" for r in broken) or "ok")
        check("停止：写盘次数正好是 3，停下来之后没有偷偷再写",
              single["WRITES"][0] == "3", single["WRITES"][0])
        check("停止：已经写好的 3 张照样通知了媒体库（Aves 才能立刻看到）",
              single["SCANNED1"][0] == "3", single["SCANNED1"][0])

        # A stop is not a failure, so the journal must still hold a usable undo
        # for everything that was written -- and for nothing that was not.
        check("停止：日志里正好 3 行（没轮到的没记）",
              single["JOURNAL"][0] == "3", single["JOURNAL"][0])
        u = single["UNDO"][0]
        check("停止：撤销还原了这 3 张", u == "3", u)
        back_all = store_of(listed, "STORE2")
        bad_all = [r for r in stop_rels
                   if back_all[r][0] != str(stop_originals[r][0])
                   or back_all[r][1] != stop_originals[r][1]]
        check("停止：撤销后 5 张全部与原件逐字节相同",
              not bad_all,
              "; ".join(f"{r}: {back_all[r][1][:12]}" for r in bad_all) or "ok")
        check("停止：撤销后日志清空", single["JOURNAL2"][0] == "0",
              single["JOURNAL2"][0])

        print()
        print("=" * 110)
        if check.failed:
            print(f"{check.passed} 项通过，{len(check.failed)} 项失败：")
            for f in check.failed:
                print(f"  - {f}")
            return 1
        print(f"{check.passed} 项通过 —— TagRunner 整条写回/跳过/撤销/断线路径都在"
              f"真实的 App 代码上跑过了。")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
