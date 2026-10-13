#!/usr/bin/env python
"""Run the phone's own `TaggerClient.kt` on the desktop JVM against a real server.

`simulate_phone.py` exercises the whole write path but talks to the server from
*Python*. That leaves one file never executed anywhere: the app's HTTP client.
Its failure modes are exactly the ones that are painful to debug on a phone --
a misspelled JSON field silently becomes an empty tag list, and the run "works"
while publishing nothing.

So: compile the real `TaggerClient.kt` for the desktop JVM and point it at a real
`albumcat serve`. Two things it needs that a desktop JVM lacks, both supplied
here rather than by editing the app:

  * `android.util.Base64` -- a four-method shim over `java.util.Base64`. Android's
    NO_WRAP means "one line, padded", which is precisely `Base64.getEncoder()`.
  * `org.json` -- a real jar, fetched via Python (this machine's SChannel TLS is
    broken; see the README).

It also compiles `NetDiagnosis.kt`, the file that turns a connect failure into
advice, and `ServerAddress.kt`, the file that turns whatever the user typed into
a usable base URL. Neither needs any Android class at all -- that is the point of
keeping them separate from `MainActivity`, and it is what lets the `NET_CASES`
and `ADDRESS_CASES` tables below assert against the real exception strings
Android throws and the real spellings real keyboards produce.

`ADDRESS_CASES` exists because of a report: the user typed a correct address and
the app still said "请填写电脑端地址". The guard behind that message lived inline
in an Activity, where nothing could reach it; moving it here is what makes the
table possible.

What is asserted: the client's parsed view of a response equals Python's parsed
view of the *same* response, field by field. That is what catches a typo in
`optJSONArray("flat")` and friends.

Usage:
    python tools\\check_android_client.py [--samples DIR] [--port N]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from kotlin_jvm import build_runner, kotlin  # noqa: E402

WORK = ROOT / ".tmp" / "jvmclient"
JARS = ROOT / ".tmp" / "jars"

CLIENT_SRC = (
    ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "albumcat" / "TaggerClient.kt"
)
JOURNAL_SRC = (
    ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "albumcat" / "Journal.kt"
)
PATHS_SRC = (
    ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "albumcat" / "DocumentPaths.kt"
)
NETDIAG_SRC = (
    ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "albumcat" / "NetDiagnosis.kt"
)
ADDRESS_SRC = (
    ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "albumcat" / "ServerAddress.kt"
)

# Connecting to the PC is the very first thing anyone does with this app, and it
# is the step most likely to fail: the server may not be running, the Windows
# firewall may be dropping packets, the phone may be on a different network, or
# the address may be malformed. Android reports all four with *nearly identical*
# text, and each needs a different fix. NetDiagnosis.kt classifies them; this
# table is the oracle. `label, kind, phone-address-in-the-error, server-address,
# whether the two are on different /24s`.
NET_CASES = (
    # TCP reached the PC, nothing listening -> serve is not running (or wrong port).
    ("refused", "REFUSED", "192.168.1.5", "10.150.84.47", True),
    # Packets dropped. On a home LAN this is the firewall -- the case measured on
    # this machine, where 127.0.0.1 works and the LAN address does not.
    ("timeout", "TIMEOUT", "192.168.1.5", "10.150.84.47", True),
    # Same failure, same subnet: the diagnosis must NOT cry "different network"
    # when the two addresses agree.
    ("same-subnet", "TIMEOUT", "10.150.84.99", "10.150.84.47", False),
    # Some Android builds throw SocketTimeoutException with no `from /` part --
    # and, crucially, with no "timeout" word in the message either, so matching
    # text alone would file it as UNKNOWN. Only the exception *type* saves it.
    ("socket-timeout", "TIMEOUT", None, "10.150.84.47", False),
    # Same trap: a wrapped timeout whose message never says "timeout".
    ("wrapped-timeout", "TIMEOUT", None, None, False),
    # A connect failure with no marker at all is still a dropped packet.
    ("bare-connect", "TIMEOUT", "10.150.84.47", "10.150.84.47", False),
    ("no-route", "NO_ROUTE", "10.150.84.47", "192.168.1.99", True),
    ("net-unreach", "NO_ROUTE", "10.150.84.47", "192.168.1.99", True),
    # The ordering test: this message contains BOTH "No route to host" and
    # "Connection timed out". Classifying it as TIMEOUT would send the user off
    # to fix the firewall when the real problem is the address.
    ("unreach-with-timeout-text", "NO_ROUTE", "10.150.84.47", "192.168.1.99", True),
    ("bad-host", "BAD_ADDRESS", None, None, False),
    ("http-500", "SERVER_ERROR", None, None, False),
    ("weird", "UNKNOWN", None, None, False),
)

# What the address box accepts, and what each input must turn into.
#
# This table is the regression test for a bug that got all the way to the
# phone. The user typed `http://192.168.83.124:8899` -- a correct address for
# this very machine -- and the app replied "请填写电脑端地址". The old guard was
# `startsWith("http")` inline in MainActivity:
#
#   * case-sensitive, so an IME that capitalised the first letter failed it
#     while the box visibly held an address (the user retypes it, same error);
#   * full-width `：` / `／` from a Chinese IME passed the prefix test and then
#     died inside URL() with a message naming no cause;
#   * it never echoed what it saw, so "请填写地址" read as "the box is empty".
#
# Nothing could test that check while it lived in an Activity. Moving it into a
# pure-JDK object is what makes this table possible at all.
#
# `label, what the user typed, what it must become (None = must be refused),
#  a substring the refusal message must contain (None = no requirement)`.
ADDRESS_CASES = (
    # The exact input from the report. Accepting it is the point: an unhelpful
    # error is indistinguishable from a broken app, and retyping cannot help.
    ("reported-bug", "Http://192.168.83.124:8899", "http://192.168.83.124:8899", None),
    ("exact", "http://192.168.83.124:8899", "http://192.168.83.124:8899", None),
    ("upper-scheme", "HTTP://192.168.83.124:8899", "http://192.168.83.124:8899", None),
    ("https", "https://192.168.83.124:8899", "https://192.168.83.124:8899", None),
    ("no-scheme", "192.168.83.124:8899", "http://192.168.83.124:8899", None),
    ("bare-ip-gets-port", "192.168.83.124", "http://192.168.83.124:8899", None),
    ("hostname", "my-pc:8899", "http://my-pc:8899", None),
    # `localhost` parses as a syntactically valid scheme, so "there is a colon"
    # cannot decide whether a scheme was written. Only `//` (or http/https)
    # counts -- otherwise this becomes the nonsense `localhost://8899`.
    ("localhost", "localhost:8899", "http://localhost:8899", None),
    ("localhost-bare", "localhost", "http://localhost:8899", None),
    ("single-slash", "http:/192.168.83.124:8899", "http://192.168.83.124:8899", None),
    # A Chinese IME's colon and slashes: visually identical in the field, fatal
    # to the parser. This is the silent half of the bug.
    ("fullwidth-colon", "http://192.168.83.124\uff1a8899", "http://192.168.83.124:8899", None),
    ("fullwidth-slashes", "http\uff1a\uff0f\uff0f192.168.83.124:8899",
     "http://192.168.83.124:8899", None),
    ("fullwidth-digits", "\uff11\uff19\uff12.168.83.124", "http://192.168.83.124:8899", None),
    # Pasted out of a browser: keep the host, drop the rest.
    ("pasted-url", "http://192.168.83.124:8899/health", "http://192.168.83.124:8899", None),
    ("pasted-query", "http://192.168.83.124:8899/health?x=1",
     "http://192.168.83.124:8899", None),
    ("trailing-slash", "http://192.168.83.124:8899/", "http://192.168.83.124:8899", None),
    # Whitespace and invisibles ride along with copy/paste and cannot be spotted
    # by eye; each of these four looks identical to the working address.
    ("padded", "  http://192.168.83.124:8899\t", "http://192.168.83.124:8899", None),
    ("nbsp", "http://192.168.83.124:8899\u00a0", "http://192.168.83.124:8899", None),
    ("zero-width", "http://192.168.83.124:8899\u200b", "http://192.168.83.124:8899", None),
    ("ideographic-space", "http://192.168.83.124:8899\u3000",
     "http://192.168.83.124:8899", None),
    ("ipv6", "http://[::1]:8899", "http://[::1]:8899", None),
    # Genuine refusals. Each must name the offending character -- that is the
    # whole difference between this and the message that caused the report.
    ("empty", "", None, "还没填"),
    ("blank", "   ", None, "还没填"),
    ("ftp", "ftp://192.168.83.124", None, "只支持 http"),
    ("space-inside", "192.168.83.124 8899", None, "8899"),
    ("chinese-host", "\u7535\u8111:8899", None, "\u7535\u8111"),
    ("no-host", ":8899", None, "没有主机名"),
)

# `looksLikeAddress` answers a different question: not "turn this into a URL" but
# "could this text have been meant as the address?". It exists because the
# address box is the first row of the form, i.e. what a system bar covers first,
# and a user who can only see one box fills in that one. `normalize()` is far too
# permissive to decide it -- it completes a bare word to `http://reimu:8899` --
# so anything the rescue believes must look endpoint-shaped.
LOOKS_CASES = (
    # The reported shape: a whole address sitting in the wrong box. Believe it.
    ("full", "http://192.168.83.124:8899", True),
    ("no-scheme", "192.168.83.124:8899", True),
    ("bare-ip", "10.0.2.2", True),
    ("hostname-port", "my-pc:8899", True),
    ("localhost-port", "localhost:8899", True),
    ("ipv6", "[::1]:8899", True),
    ("fullwidth-colon", "http://192.168.83.124\uff1a8899", True),
    # A real token is not an address and must stay put: guessing wrong here would
    # silently empty the token box and then fail authentication, which is a worse
    # bug than the one being fixed.
    ("token", "s3cret-token", False),
    ("token-base64", "aGVsbG8td29ybGQ=", False),
    ("empty", "", False),
    ("blank", "   ", False),
    ("bare-word", "reimu", False),
    ("chinese", "\u7535\u8111", False),
    ("sentence", "\u6211\u7684\u7535\u8111\u5728\u8fd9\u53f0\u673a\u5668\u4e0a", False),
    ("ftp", "ftp://192.168.83.124", False),
)


def kt_str(s: str) -> str:
    """A Kotlin string literal for `s` -- escapes and, notably, `$`."""
    out = ['"']
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "$":
            out.append("\\$")
        elif ord(ch) < 0x20:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


ADDR_CASES_KT = "\n".join(
    f"        ACase({kt_str(label)}, {kt_str(typed)}),"
    for label, typed, _want, _hint in ADDRESS_CASES
)

LOOKS_CASES_KT = "\n".join(
    f"        LCase({kt_str(label)}, {kt_str(text)}),"
    for label, text, _want in LOOKS_CASES
)

# SAF document id -> filesystem path. The media-store rescan needs this mapping:
# MediaScannerConnection only accepts paths, and a wrong one would either scan
# something else or nothing at all, leaving Aves unaware of the new tags.
DOC_ID_CASES = (
    ("primary:DCIM/Camera/IMG_20240101_123456.jpg",
     "/storage/emulated/0/DCIM/Camera/IMG_20240101_123456.jpg"),
    ("primary:DCIM/My Photos/a b.jpg", "/storage/emulated/0/DCIM/My Photos/a b.jpg"),
    ("PRIMARY:DCIM/x.jpg", "/storage/emulated/0/DCIM/x.jpg"),
    ("1A2B-3C4D:DCIM/Camera/x.jpg", "/storage/1A2B-3C4D/DCIM/Camera/x.jpg"),
    ("primary:", None),
    (":DCIM/x.jpg", None),
    ("noColon", None),
    ("primary:/absolute/x.jpg", None),
    ("prim/ary:DCIM/x.jpg", None),
    ("", None),
)

# Maven coordinates: org.json:json. Newest first; the first that fetches wins.
JSON_VERSIONS = ("20250107", "20240303", "20231013", "20230227")
MAVEN = "https://maven.aliyun.com/repository/public/org/json/json/{v}/json-{v}.jar"

SHIM_KT = '''package android.util

import java.util.Base64 as JBase64

/**
 * Just enough of Android's Base64 to compile `TaggerClient.kt` on a desktop JVM.
 *
 * Android's NO_WRAP asks for a single unwrapped line, which is exactly what
 * `java.util.Base64.getEncoder()` produces (it never wraps and keeps padding).
 */
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
'''

CONTEXT_KT = '''package android.content

import java.io.File

/**
 * `Journal.kt` uses exactly one thing from Context: `filesDir`.
 *
 * Declared as a Kotlin `val` because the property syntax `context.filesDir`
 * only resolves automatically for *Java* getters; against a Kotlin-declared
 * `getFilesDir()` it does not.
 */
abstract class Context {
    abstract val filesDir: File
}
'''

PROBE_KT = '''package com.albumcat

import java.io.File

/**
 * Drives the app's real TaggerClient against a live server and prints what it
 * parsed. The Python side posts the same bytes and compares.
 */
fun main(args: Array<String>) {
    val url = args[0]
    val token = if (args.size > 3) args[3] else ""
    val client = TaggerClient(url, token)

    // 1. health -- mirrors MainActivity.testConnection(), which reads the
    // nested model object (status/service are only for us).
    try {
        val h = client.health()
        val model = h.optJSONObject("model")
        println("HEALTH\\t" + listOf(
            h.optString("status"),
            h.optString("service"),
            (model?.optString("session_model") ?: "?"),
            (model?.optJSONArray("providers")?.toString() ?: "[]"),
            model?.optInt("embedding_dim", 0).toString(),
            (h.optJSONArray("plugins")?.length() ?: 0).toString(),
        ).joinToString("\\t"))
    } catch (e: Exception) {
        println("HEALTH_ERR\\t" + (e.message ?: e.toString()))
    }

    // 2. plan
    val f = File(args[1])
    val id = args[2]
    val r = client.plan(f.readBytes(), id, f.name, f.length(), 1700000000000L)
    println("PLAN\\t" + listOf(
        if (r.ok) "1" else "0",
        r.error ?: "",
        r.flat.joinToString("\\u0001"),
        r.hierarchical.joinToString("\\u0001"),
        r.rating.toString(),
        if (r.needsReview) "1" else "0",
        r.characters.joinToString("\\u0001") { it.first + "=" + it.second },
        r.notes.joinToString("\\u0001"),
    ).joinToString("\\t"))

    // 3. DocumentPaths -- the SAF document id to filesystem path mapping used
    // when telling the media store a photo changed. A wrong path here means we
    // rescan some other file (or nothing), and Aves never re-reads the tags.
    val docIds = listOf(
        "primary:DCIM/Camera/IMG_20240101_123456.jpg",
        "primary:DCIM/My Photos/a b.jpg",
        "PRIMARY:DCIM/x.jpg",
        "1A2B-3C4D:DCIM/Camera/x.jpg",
        "primary:",
        ":DCIM/x.jpg",
        "noColon",
        "primary:/absolute/x.jpg",
        "prim/ary:DCIM/x.jpg",
        "",
    )
    println("PATHS\\t" + docIds.joinToString("\\u0001") {
        it + ">" + (DocumentPaths.forDocumentId(it) ?: "-")
    })

    // 4. Journal round-trip. Undo correctness rests on two invariants that are
    // easy to break and invisible until the day someone actually undoes:
    //   * `prev` comes back byte-identical (or stays null)
    //   * a retried photo contributes ONE row, the earliest -- that is the
    //     state before the run, not the state after the first attempt
    val jdir = args[4]
    if (jdir != "-") {
        val j = Journal(object : android.content.Context() {
            override val filesDir: File = File(jdir).also { it.mkdirs() }
        })
        val run = "20260101-000000"
        val prevA = byteArrayOf(1, 2, 3, 65, 66, 67, -1)
        val wroteA1 = byteArrayOf(9, 9)
        val wroteA2 = byteArrayOf(8, 8)
        j.record(run, "content://photo/a", prevA, wroteA1)
        j.record(run, "content://photo/a", prevA, wroteA2)   // retry
        j.record(run, "content://photo/b", null, wroteA1)   // had no XMP before
        j.record("20260101-000001", "content://photo/c", prevA, wroteA1)

        val rows = j.read(run)
        val plan = j.undoPlan(run)
        val a = plan.firstOrNull { it.uri.endsWith("/a") }
        val b = plan.firstOrNull { it.uri.endsWith("/b") }
        println("JOURNAL\\t" + listOf(
            rows.size.toString(),
            plan.size.toString(),
            (a?.prev?.contentEquals(prevA) == true).toString(),
            (a?.wrote?.contentEquals(wroteA1) == true).toString(),
            (b?.prev == null).toString(),
            j.runs().joinToString("\\u0001") { it.first + "=" + it.second },
        ).joinToString("\\t"))

        // a half-written line must not make the journal unreadable
        File(jdir, "journal.jsonl").appendText("{not json\\n", Charsets.UTF_8)
        println("GARBAGE\\t" + j.read().size)
        println("FORGET\\t" + j.forget(run) + "\\t" + j.read().size + "\\t" +
                j.read("20260101-000001").size)
    }

    // 5. NetDiagnosis -- the connect failures above, classified.
    //
    // The exception strings below are the shapes Android actually produces
    // (including the "from /<our own ip>" fragment that newer versions embed,
    // which is the only way the app can tell a firewall problem from a
    // wrong-network problem). Each is a different mistake with a different fix.
    // Kotlin has no `local class` modifier; a class declared inside a function
    // is already local.
    class NetCase(val label: String, val e: Throwable)
    val netCases = listOf(
        NetCase("refused", java.net.ConnectException(
            "failed to connect to /10.150.84.47:8899 (port 8899) from /192.168.1.5 " +
            "(port 41234) after 12ms: isConnected failed: ECONNREFUSED (Connection refused)")),
        NetCase("timeout", java.net.ConnectException(
            "failed to connect to /10.150.84.47:8899 (port 8899) from /192.168.1.5 " +
            "(port 41234) after 8000ms: isConnected failed: ETIMEDOUT (Connection timed out)")),
        NetCase("same-subnet", java.net.ConnectException(
            "failed to connect to /10.150.84.47:8899 (port 8899) from /10.150.84.99 " +
            "(port 41234) after 8000ms: isConnected failed: ETIMEDOUT (Connection timed out)")),
        NetCase("socket-timeout", java.net.SocketTimeoutException(
            "failed to connect to /10.150.84.47:8899 after 8000ms")),
        // The real reason sits on the cause chain, and no message anywhere says
        // "timeout" -- the type is the only signal.
        NetCase("wrapped-timeout", java.io.IOException("connect failed",
            java.net.SocketTimeoutException("failed to connect to /10.150.84.47:8899"))),
        NetCase("bare-connect", java.net.ConnectException(
            "failed to connect to /10.150.84.47:8899 (port 8899) from /10.150.84.47")),
        NetCase("no-route", java.net.NoRouteToHostException(
            "failed to connect to /192.168.1.99:8899 (port 8899) from /10.150.84.47 " +
            "(port 41234) after 8000ms: isConnected failed: EHOSTUNREACH (No route to host)")),
        NetCase("net-unreach", java.net.SocketException(
            "failed to connect to /192.168.1.99:8899 (port 8899) from /10.150.84.47 " +
            "(port 41234) after 8000ms: isConnected failed: ENETUNREACH (Network is unreachable)")),
        NetCase("unreach-with-timeout-text", java.net.NoRouteToHostException(
            "failed to connect to /192.168.1.99:8899 (port 8899) from /10.150.84.47 " +
            "(port 41234) after 8000ms: isConnected failed: EHOSTUNREACH (No route to host): " +
            "Connection timed out")),
        NetCase("bad-host", java.net.UnknownHostException(
            "Unable to resolve host \\"my-pc\\": No address associated with hostname")),
        NetCase("http-500", java.io.IOException("HTTP 500: {\\"error\\":\\"boom\\"}")),
        NetCase("weird", IllegalStateException("something else entirely")),
    )
    for (c in netCases) {
        val f = diagnoseNetworkError(c.e)
        println("NETDIAG\\t" + listOf(
            c.label, f.kind.name, f.phoneAddress ?: "-", f.serverAddress ?: "-",
            f.subnetMismatch.toString(), f.headline, f.hints.size.toString(),
        ).joinToString("\\t"))
        val ls = f.lines()
        println("NETLINES\\t" + c.label + "\\t" + ls.size + "\\t" +
                (ls.firstOrNull() == f.headline).toString())
    }

    // ---- ServerAddress ------------------------------------------------
    class ACase(val label: String, val typed: String)
    val addrCases = listOf(
__ADDR_CASES__
    )
    for (c in addrCases) {
        val out = try {
            "ok\\t" + ServerAddress.normalize(c.typed)
        } catch (e: ServerAddress.BadAddress) {
            // One line only -- the driver protocol is tab-separated lines, so a
            // multi-line message has to be folded before it goes out.
            "bad\\t" + (e.message ?: "").replace("\\n", "\\\\n")
        }
        println("ADDR\\t" + c.label + "\\t" + out)
    }

    // ---- ServerAddress.looksLikeAddress -------------------------------
    // Used to rescue an address typed into the token box, which is what a user
    // sees when the system bar covers the first row of the form.
    class LCase(val label: String, val text: String)
    val looksCases = listOf(
__LOOKS_CASES__
    )
    for (c in looksCases) {
        println("LOOKS\\t" + c.label + "\\t" + ServerAddress.looksLikeAddress(c.text))
    }
}
'''
PROBE_KT = PROBE_KT.replace("__ADDR_CASES__", ADDR_CASES_KT)
PROBE_KT = PROBE_KT.replace("__LOOKS_CASES__", LOOKS_CASES_KT)


def fetch_json_jar() -> Path:
    JARS.mkdir(parents=True, exist_ok=True)
    for v in JSON_VERSIONS:
        dest = JARS / f"json-{v}.jar"
        if dest.exists() and dest.stat().st_size > 1000:
            return dest
        url = MAVEN.format(v=v)
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                data = r.read()
        except Exception as exc:  # noqa: BLE001
            print(f"  json-{v}: {type(exc).__name__} {exc}")
            continue
        if len(data) < 1000:
            print(f"  json-{v}: too small ({len(data)} bytes)")
            continue
        dest.write_bytes(data)
        print(f"  fetched {dest.name} ({len(data) // 1024} KB)")
        return dest
    sys.exit("could not fetch an org.json jar from " + MAVEN)


def wait_for_health(base: str, proc: subprocess.Popen, timeout: float = 240.0) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            sys.exit(f"server exited early (rc={proc.returncode})")
        try:
            with urllib.request.urlopen(base + "/health", timeout=5) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            last = str(exc)
            time.sleep(1.0)
    sys.exit(f"server never became healthy: {last}")


def post_plan(base: str, jpeg: bytes, *, id_: str, name: str, size: int, token: str) -> dict:
    body = json.dumps({
        "id": id_, "name": name, "size": size, "date_taken": 1700000000000,
        "image": base64.b64encode(jpeg).decode("ascii"),
    }).encode("utf-8")
    req = urllib.request.Request(base + "/plan", data=body,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    if token:
        req.add_header("X-Albumcat-Token", token)
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))


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


def unjoin(s: str) -> list[str]:
    return [] if not s else s.split("\u0001")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--samples", default=str(ROOT / "samples"))
    ap.add_argument("--port", type=int, default=8901)
    args = ap.parse_args(argv)

    from simulate_phone import phone_thumbnail  # noqa: E402

    samples = sorted(p for p in Path(args.samples).glob("*") if p.suffix.lower() in
                     {".jpg", ".jpeg"})
    if not samples:
        sys.exit(f"no JPEGs in {args.samples}")

    print("fetching org.json")
    jar = fetch_json_jar()

    print("compiling TaggerClient.kt for the desktop JVM")
    shim = WORK / "AndroidShim.kt"
    WORK.mkdir(parents=True, exist_ok=True)
    shim.write_text(SHIM_KT, encoding="utf-8")
    (WORK / "Context.kt").write_text(CONTEXT_KT, encoding="utf-8")
    runner = build_runner(WORK,
                          sources=[CLIENT_SRC, JOURNAL_SRC, PATHS_SRC, NETDIAG_SRC,
                                   ADDRESS_SRC, shim, WORK / "Context.kt"],
                          driver=PROBE_KT, driver_name="Probe.kt",
                          main_class="com.albumcat.ProbeKt", extra_cp=[jar])

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
        health = wait_for_health(base, server)
        model = health.get("model") or {}
        print(f"server up: providers={model.get('providers')} "
              f"dim={model.get('embedding_dim')} "
              f"plugins={len(health.get('plugins') or [])}")
        wd = WORK / "thumbs"
        wd.mkdir(exist_ok=True)
        jdir = WORK / "journal"
        if jdir.exists():
            import shutil as _shutil
            _shutil.rmtree(jdir)

        for index, src in enumerate(samples):
            print(f"\n{src.name}")
            thumb = wd / (src.stem + ".jpg")
            thumb.write_bytes(phone_thumbnail(src))

            out = kotlin(runner, base, str(thumb), f"id-{src.stem}", "",
                         jdir if index == 0 else "-", check=False)
            lines = [ln for ln in out.splitlines() if ln.strip()]
            khealth = kplan = None
            fields: dict[str, list[str]] = {}
            for ln in lines:
                parts = ln.split("\t")
                if parts[0] == "HEALTH":
                    khealth = parts[1:]
                elif parts[0] == "HEALTH_ERR":
                    check("Kotlin 客户端 health() 成功", False, ln)
                elif parts[0] == "PLAN":
                    kplan = parts[1:]
                elif parts[0] in ("NETDIAG", "NETLINES", "ADDR", "LOOKS"):
                    # These repeat once per case, so they accumulate instead of
                    # overwriting the way the single-valued keys below do.
                    fields.setdefault(parts[0], []).append(parts[1:])
                else:
                    fields[parts[0]] = parts[1:]

            check("Kotlin 客户端 health() 成功", khealth is not None)
            if khealth and index == 0:
                check("health: status", khealth[0] == health.get("status"), khealth[0])
                check("health: service", khealth[1] == health.get("service"), khealth[1])
                check("health: model.session_model（App 连接测试读的就是这个）",
                      khealth[2] == str(model.get("session_model")), khealth[2])
                check("health: model.embedding_dim",
                      khealth[4] == str(model.get("embedding_dim")), khealth[4])
                check("health: plugins 数量",
                      khealth[5] == str(len(health.get("plugins") or [])), khealth[5])
                check("health: providers 里有 DmlExecutionProvider",
                      "DmlExecutionProvider" in khealth[3], khealth[3])

            check("Kotlin 客户端 plan() 拿到结果", kplan is not None)
            if not kplan:
                continue
            # kplan: ok, error, flat, hierarchical, rating, needs_review, chars, notes
            check("plan().ok 为真", kplan[0] == "1", kplan[1])

            # The oracle: the same request, parsed by Python.
            ref = post_plan(base, thumb.read_bytes(), id_=f"id-{src.stem}",
                            name=thumb.name, size=thumb.stat().st_size, token="")

            k_flat, k_hier = unjoin(kplan[2]), unjoin(kplan[3])
            check("flat 与 Python 解析同一响应一致",
                  k_flat == ref.get("flat"), f"{len(k_flat)} vs {len(ref.get('flat', []))}")
            check("hierarchical 与 Python 一致",
                  k_hier == ref.get("hierarchical"), f"{k_hier}")
            check("rating 与 Python 一致",
                  kplan[4] == str(ref.get("rating", 0)), f"{kplan[4]} vs {ref.get('rating')}")
            check("needs_review 与 Python 一致",
                  (kplan[5] == "1") == bool(ref.get("needs_review")),
                  f"{kplan[5]} vs {ref.get('needs_review')}")

            k_chars = unjoin(kplan[6])
            r_chars = [f"{c['label']}={c['score']}" for c in ref.get("characters", [])]
            check("characters（标签+分数）与 Python 一致",
                  k_chars == r_chars, f"{k_chars} vs {r_chars}")
            check("notes 与 Python 一致",
                  unjoin(kplan[7]) == ref.get("notes", []))
            check("服务端认下了我们发的 id/name",
                  ref.get("id") == f"id-{src.stem}", str(ref.get("id")))
            # A typo'd field name in TaggerClient would show up as "everything
            # empty", which is exactly the silent failure we are hunting.
            check("客户端确实读到了标签（不是静默空数组）",
                  bool(k_flat) or src.name in {"00-flat.jpg", "01-gradient.jpg"},
                  f"{len(k_flat)} 个 flat")

            if index == 0:
                paths = fields.get("PATHS")
                if not paths:
                    check("DocumentPaths: 探针有输出", False, "无输出")
                else:
                    # The PATHS line has a single column, already tab-split by
                    # the parser, so its join sits at index 0.
                    got = dict(part.split(">", 1) for part in unjoin(paths[0]))
                    bad = [doc for doc, want in DOC_ID_CASES
                           if got.get(doc) != (want if want is not None else "-")]
                    check(f"DocumentPaths: {len(DOC_ID_CASES)} 条文档 id 映射全部正确",
                          not bad, f"错的：{[got.get(d) for d in bad]}" if bad else "")

                j = fields.get("JOURNAL")
                check("Journal: read(run) 返回 3 行（含那条重试）",
                      j is not None and j[0] == "3", j[0] if j else "无输出")
                check("Journal: undoPlan 对重复 URI 只留 1 行，共 2 个文件",
                      j is not None and j[1] == "2", j[1] if j else "")
                check("Journal: prev 逐字节还原（撤销靠的就是它）",
                      j is not None and j[2] == "true", j[2] if j else "")
                check("Journal: 有重试时保留的是【最早】那行（= 运行前状态）",
                      j is not None and j[3] == "true", j[3] if j else "")
                check("Journal: 原本没有 XMP 的记成 null，不会变成空数组",
                      j is not None and j[4] == "true", j[4] if j else "")
                check("Journal: runs() 按 run 倒序且计数正确",
                      j is not None and j[5] == "20260101-000001=1\u000120260101-000000=3",
                      j[5] if j else "")
                g = fields.get("GARBAGE")
                check("Journal: 半行垃圾不会让整个日志读不出来（仍 4 行）",
                      g is not None and g[0] == "4", g[0] if g else "")
                fg = fields.get("FORGET")
                # 3 dropped; the 1 row from the other run stays readable, and
                # the garbage line is kept on disk but never parsed as a row.
                check("Journal: forget 删掉本次 run 的 3 行，其他 run 留下",
                      fg is not None and fg[0] == "3" and fg[1] == "1" and fg[2] == "1",
                      str(fg))

                nd = fields.get("NETDIAG") or []
                nl = fields.get("NETLINES") or []
                if not nd:
                    check("NetDiagnosis: 探针有输出", False, "无输出")
                else:
                    # row: label, kind, phone, server, subnetMismatch, headline, hints
                    got = {row[0]: row for row in nd}
                    miss = [label for label, *_ in NET_CASES if label not in got]
                    check(f"NetDiagnosis: {len(NET_CASES)} 种连接失败各有什么输出",
                          not miss, f"缺：{miss}" if miss else "")
                    bad = [f"{label}: {got.get(label, ['?'] * 2)[1]} != {kind}"
                           for label, kind, *_ in NET_CASES
                           if got.get(label, [None, None])[1] != kind]
                    check(f"NetDiagnosis: {len(NET_CASES)} 种连接失败各自归类正确",
                          not bad, "；".join(bad))
                    # "phone and PC are on different /24s" is advice the user can
                    # act on; getting it wrong either hides the real cause or
                    # invents a problem that is not there.
                    sub = [f"{label}: {got.get(label, ['?'] * 5)[4]}"
                           for label, _k, _p, _s, want in NET_CASES
                           if got.get(label, [None] * 5)[4] != str(want).lower()]
                    check("NetDiagnosis: 网段不同只在真的不同时才报（同网段不报）",
                          not sub, "；".join(sub))
                    addrs = [f"{label}: {got.get(label, ['?'] * 3)[2]}"
                             for label, _k, phone, _s, _x in NET_CASES
                             if got.get(label, [None] * 3)[2] != (phone or "-")]
                    check("NetDiagnosis: 从异常文本里抠出了手机自己的地址",
                          not addrs, "；".join(addrs))
                    # A headline with no advice leaves the user where they were.
                    empty = [row[0] for row in nd if row[6] == "0"]
                    check("NetDiagnosis: 每种失败都给出非空的可操作建议",
                          not empty, f"没建议的：{empty}" if empty else "")
                    check("NetDiagnosis: 渲染出的第一行就是结论",
                          bool(nl) and all(row[2] == "true" for row in nl),
                          f"{len(nl)} 条")

                ad = fields.get("ADDR") or []
                if not ad:
                    check("ServerAddress: 探针有输出", False, "无输出")
                else:
                    # row: label, ok|bad, the normalised address or the message
                    agot = {row[0]: row for row in ad}
                    amiss = [label for label, *_ in ADDRESS_CASES if label not in agot]
                    check(f"ServerAddress: {len(ADDRESS_CASES)} 种输入都有结果",
                          not amiss, f"缺：{amiss}" if amiss else "")

                    awrong = []
                    for label, _typed, want, _hint in ADDRESS_CASES:
                        row = agot.get(label)
                        if row is None:
                            continue
                        if want is None:
                            # Must be refused rather than turned into something
                            # plausible -- silently "repairing" a wrong host
                            # would dial somewhere unintended.
                            if row[1] != "bad":
                                awrong.append(f"{label}: 该拒绝却得到 `{row[2]}`")
                        elif row[1] != "ok" or row[2] != want:
                            awrong.append(f"{label}: {row[1]} `{row[2]}` != `{want}`")
                    check("ServerAddress: 该修的修对（含用户报的 `Http://`），该拒的拒绝",
                          not awrong, "；".join(awrong))

                    # The reported symptom was not a crash, it was a message that
                    # described the wrong problem. A refusal has to say what it
                    # saw, or it is the same bug again with a new string.
                    avague = [f"{label}: `{hint}` 不在 {row[2]!r}"
                              for label, _t, _w, hint in ADDRESS_CASES
                              if hint and (row := agot.get(label))
                              and hint not in row[2]]
                    check("ServerAddress: 拒绝时说出看到的是什么，不是笼统的「请填写地址」",
                          not avague, "；".join(avague))

                lk = fields.get("LOOKS") or []
                if not lk:
                    check("looksLikeAddress: 探针有输出", False, "无输出")
                else:
                    lgot = {row[0]: row for row in lk}
                    lwrong = [f"{label}: {lgot.get(label, ['?', '?'])[1]} != {want}"
                              for label, _text, want in LOOKS_CASES
                              if lgot.get(label, [None, None])[1] != str(want).lower()]
                    check(f"looksLikeAddress: {len(LOOKS_CASES)} 种输入判断对"
                          "（地址被信，令牌不被误当地址）",
                          not lwrong, "；".join(lwrong))

        # ------------------------------------------------------------------ #
        # Source guards: the things no compiled probe can see.
        #
        # Every check above runs Kotlin on the desktop, and none of them compile
        # MainActivity -- the layout, the theme and the manifest are never even
        # loaded. That blind spot is exactly where this bug lived: the framework
        # ActionBar was drawn *over* the first rows of the form, the address box
        # is the first row, and the only field left visible was the token box. A
        # user filled in the one box they could see, and the app said the address
        # was missing. Nothing here could have failed, so these are cheap text
        # checks on the files that decide it: no ActionBar, content clear of the
        # system bar, and both boxes labelled so they cannot be confused.
        guard = []
        manifest = (ROOT / "android" / "app" / "src" / "main" / "AndroidManifest.xml")
        themes = (ROOT / "android" / "app" / "src" / "main" / "res" / "values" / "themes.xml")
        v35 = (ROOT / "android" / "app" / "src" / "main" / "res" / "values-v35" / "themes.xml")
        activity = (
            ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "albumcat"
            / "MainActivity.kt"
        )
        if 'android:theme="@style/AppTheme"' not in manifest.read_text("utf-8"):
            guard.append("manifest 没用 @style/AppTheme")
        if "NoActionBar" not in themes.read_text("utf-8"):
            guard.append("AppTheme 不是 NoActionBar —— 框架 ActionBar 会压在表单第一行上")
        if not v35.exists():
            guard.append("缺 values-v35/themes.xml")
        elif "windowOptOutEdgeToEdgeEnforcement" not in v35.read_text("utf-8"):
            guard.append("values-v35 没退出 edge-to-edge")
        act = activity.read_text("utf-8")
        if "setOnApplyWindowInsetsListener" not in act:
            guard.append("MainActivity 没处理系统栏内边距")
        if "systemWindowInsetTop" not in act:
            guard.append("MainActivity 没把状态栏高度算进去")
        for label in ("电脑端地址", "令牌（电脑端没设 --token 就留空）"):
            if f'hint("{label}")' not in act:
                guard.append(f"输入框缺可见标签「{label}」")
        check("布局：无 ActionBar、内容避开系统栏、两个输入框都有可见标签",
              not guard, "；".join(guard))

        # aapt2 rejects an XML comment containing a double hyphen, and reports it
        # as mojibake through a GBK console, so catch it here and not in a log.
        dashes = []
        for p in sorted((ROOT / "android" / "app" / "src" / "main").rglob("*.xml")):
            for i, line in enumerate(p.read_text("utf-8").splitlines(), 1):
                if "--" in line and not line.lstrip().startswith("<!--") and "-->" not in line:
                    dashes.append(f"{p.relative_to(ROOT)}:{i}")
        check("资源 XML 的注释里没有 `--`（aapt2 会拒绝整个文件）",
              not dashes, "；".join(dashes))

        print()
        print("=" * 110)
        if check.failed:
            print(f"{check.passed} 项通过，{len(check.failed)} 项失败：")
            for f in check.failed:
                print(f"  - {f}")
            return 1
        print(f"{check.passed} 项通过 —— App 的 HTTP 客户端在真实服务端上跑通了。")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
