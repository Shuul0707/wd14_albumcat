#!/usr/bin/env python
"""Compile and drive the phone's `Xmp.kt` on the desktop JVM.

`android/app/src/main/java/com/albumcat/Xmp.kt` is a port of
`src/albumcat/xmp.py`. A port that is merely "close" is a trap: the phone would
write one set of bytes, the PC would read a slightly different set, and the
mismatch would only surface on a real phone, weeks later.

`Xmp.kt` deliberately imports nothing but the JDK (java.io, javax.xml,
org.w3c.dom) -- no Android types -- so it compiles and runs on the desktop.
This module is the machinery that makes that possible, shared by
`check_kotlin_xmp.py` (byte-for-byte comparison) and `simulate_phone.py`
(whole write path, end to end).

Why the classpath is a pile of globs: `kotlin-compiler-embeddable` is not
self-contained at the CLI entry point. Launching K2JVMCompiler by hand trips
over kotlin/jvm/internal/Intrinsics, then kotlinx/coroutines/CoroutineScope,
then gnu/trove/TObjectHashingStrategy, then org/jetbrains/annotations/NotNull.
Feeding it every Kotlin-ish jar the Gradle cache holds is far simpler than
reverse-engineering its real dependency list.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

JAVA = Path(r"C:\Program Files\Java\jdk-17\bin\java.exe")
CACHE = Path.home() / ".gradle" / "caches" / "modules-2" / "files-2.1"
KT_SRC = ROOT / "android" / "app" / "src" / "main" / "java" / "com" / "albumcat" / "Xmp.kt"

# Lists ride through argv joined by this. It cannot occur in a tag.
SEP = "\u0001"

INTEROP_KT = '''package com.albumcat

import java.io.File
import java.util.Base64

private fun b64(b: ByteArray): String = Base64.getEncoder().encodeToString(b)

private fun list(s: String): List<String> =
    if (s.isEmpty()) emptyList() else s.split("\\u0001")

fun main(args: Array<String>) {
    when (args[0]) {
        // flat, hierarchical, rating  ->  base64 packet
        "build" -> print(b64(Xmp.buildPacket(
            flat = list(args[1]),
            hierarchical = list(args[2]),
            rating = args[3].takeIf { it != "null" }?.toInt(),
        )))

        // photo  ->  "jpeg" | "png"
        "format" -> print(Xmp.photoFormat(File(args[1]).readBytes()))

        // photo, format, packetB64 or "-", out
        //
        // Goes through spliceBlock, not splice: for a PNG the block is an iTXt
        // payload, and building one by hand would leave off its four header
        // bytes.
        "splice" -> {
            val data = File(args[1]).readBytes()
            val packet = if (args[3] == "-") null else Base64.getDecoder().decode(args[3])
            File(args[4]).writeBytes(Xmp.spliceBlock(data, args[2], packet))
        }

        // photo  ->  "flat<TAB>hier<TAB>rating" (each a \\u0001-joined list), or NONE
        "read" -> {
            val f = Xmp.readPhotoFields(File(args[1]).readBytes())
            if (f == null) print("NONE")
            else print(f.flat.joinToString("\\u0001") + "\\t" +
                       f.hierarchical.joinToString("\\u0001") + "\\t" +
                       (f.rating?.toString() ?: "null"))
        }

        // photo, flat, hier, rating, out
        //
        // The exact write TagRunner.run performs: find what XMP is already
        // there, merge the new tags into it, build the packet, splice it in.
        // Mirrored step for step so that what this produces is what a phone
        // produces -- including the "second run does not lose the first run's
        // tags" behaviour of merge.
        "tag" -> {
            val data = File(args[1]).readBytes()
            val format = Xmp.photoFormat(data)
            val prev = Xmp.findBlock(data, format)
            val prevPacket = Xmp.blockPacket(prev, format)
            val existing =
                prevPacket?.let { try { Xmp.parsePacket(it) } catch (_: Exception) { null } }
            // TagRunner passes plan.rating.takeIf { it > 0 }
            val rating = args[4].takeIf { it != "null" && it.toInt() > 0 }?.toInt()
            val (flat, hier, r) = Xmp.merge(existing, list(args[2]), list(args[3]), rating)
            File(args[5]).writeBytes(
                Xmp.spliceBlock(
                    data, format,
                    Xmp.buildPacket(
                        flat = flat, hierarchical = hier, rating = r,
                        preserve = Xmp.foreignParts(prevPacket),
                    ),
                )
            )
        }

        // photo  ->  what we consider somebody else's: "prefix=uri" joined by
        // \\u0001, newline, then one base64 per preserved child.
        "foreign" -> {
            val data = File(args[1]).readBytes()
            val format = Xmp.photoFormat(data)
            val foreign = Xmp.foreignParts(Xmp.blockPacket(Xmp.findBlock(data, format), format))
            print(foreign.namespaces.joinToString("\\u0001") { it.first + "=" + it.second })
            for (c in foreign.children) print("\\n" + b64(c.toByteArray(Charsets.UTF_8)))
        }

        // photo  ->  base64 of the existing XMP block, or "-" when there is none
        "prev" -> {
            val data = File(args[1]).readBytes()
            val p = Xmp.findBlock(data, Xmp.photoFormat(data))
            print(if (p == null) "-" else b64(p))
        }
    }
}
'''


def find_jar(prefix: str, prefer: str | None = None) -> Path:
    """The newest `<prefix>` jar in the Gradle cache (or the `prefer`ed one)."""
    hits = sorted(CACHE.glob(f"org.jetbrains.kotlin/{prefix}/*/*/{prefix}-*.jar"))
    hits = [h for h in hits if "sources" not in h.name]
    if prefer is not None:
        pinned = [h for h in hits if h.relative_to(CACHE).parts[2] == prefer]
        if pinned:
            return pinned[-1]
    if not hits:
        sys.exit(f"could not find {prefix} jar under {CACHE}\n"
                 f"（第一次构建成功后 Gradle 才会把它下载到这里）")
    return hits[-1]


def all_kotlin_jars(version: str) -> list[Path]:
    """Everything Kotlin-ish the Gradle cache holds *for one Kotlin version*.

    The cache accumulates every Kotlin version any build has ever used (this
    machine holds ten stdlibs and three compilers, because the Aves fork was
    built here), and some modules simply stop at an older version than the
    newest compiler. Mixing them breaks the compiler with a class-cast — e.g.
    `FirExtensionRegistrarAdapter$Companion cannot be cast to
    ProjectExtensionDescriptor`, which is `kotlin-scripting-compiler-embeddable`
    2.4.10 registering itself into a 2.4.20 compiler through ServiceLoader.

    So `org.jetbrains.kotlin` is pinned to `version` (the compiler's own), while
    the groups whose versions are independent of Kotlin (annotations, trove4j,
    kotlinx-*) contribute their newest version.
    """
    jars: set[Path] = set()
    newest: dict[tuple[str, str], tuple[str, Path]] = {}
    for group in ("org.jetbrains", "org.jetbrains.*"):
        # layout is <group>/<module>/<version>/<sha1>/<file>.jar
        for j in CACHE.glob(f"{group}/*/*/*/*.jar"):
            if "sources" in j.name:
                continue
            parts = j.relative_to(CACHE).parts
            kt = parts[0] == "org.jetbrains.kotlin" or parts[0].startswith("org.jetbrains.kotlin.")
            if kt:
                if parts[2] == version:
                    jars.add(j)
                continue
            # kotlinx-coroutines and friends version independently of Kotlin:
            # they are required (the compiler's own CoreApplicationEnvironment
            # needs CoroutineScope) but only one copy of each should be on the
            # classpath.
            key = (parts[0], parts[1])
            if key not in newest or parts[2] > newest[key][0]:
                newest[key] = (parts[2], j)
    jars |= {j for _, j in newest.values()}
    if not jars:
        sys.exit(f"no Kotlin jars under {CACHE}")
    return sorted(jars)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", **kw)


def javac_args(args: list[str], work: Path) -> list[str]:
    """Hand the JVM its arguments through a file instead of the command line.

    Windows caps a command line at 32767 characters. `all_kotlin_jars()` globs
    the *whole* Gradle cache, so once that cache has built a Kotlin/Android
    project (the Aves fork filled it with Kotlin 2.4 artifacts) the classpath
    alone reaches ~41 KB and CreateProcess dies with
    `FileNotFoundError: [WinError 206] 文件名或扩展名太长`.

    Everything is written with forward slashes: the launcher's argfile parser
    treats a backslash as an escape character, which would eat the separator
    in `C:\\Users\\...`. The file itself has no size limit.
    """
    path = work / "compile.args"
    lines = []
    for a in args:
        a = a.replace("\\", "/")
        # Inside quotes the argfile parser would treat `\` as an escape, but by
        # now there are none left; and the paths themselves contain no quotes.
        if any(c.isspace() for c in a):
            a = f'"{a}"'
        lines.append(a)
    path.write_text("\n".join(lines), encoding="utf-8")
    return ["@" + str(path).replace("\\", "/")]


def _short_enough(cmd: list[str]) -> bool:
    """Would this command line fit in a Windows CreateProcess call?

    `ALBUMCAT_KOTLIN_ARGFILE=1` forces the argfile branch, which is the only way
    to exercise it on a cache small enough for the direct command to fit.
    """
    if os.environ.get("ALBUMCAT_KOTLIN_ARGFILE"):
        return False
    return sum(len(a) + 1 for a in cmd) < 30_000


def build_runner(
    work: Path,
    *,
    quiet: bool = False,
    sources: list[Path] | None = None,
    driver: str | None = INTEROP_KT,
    driver_name: str = "Interop.kt",
    main_class: str = "com.albumcat.InteropKt",
    extra_cp: list[Path] | None = None,
) -> list[str]:
    """Compile app sources + a small driver. Returns a java command prefix.

    Defaults to `Xmp.kt` plus the XMP driver, which is what the XMP tests want.
    `sources` / `driver` / `main_class` / `extra_cp` exist so
    `check_android_client.py` can compile `TaggerClient.kt` instead -- that file
    needs `android.util.Base64` and `org.json`, neither of which is on a desktop
    JVM, so it brings its own shim and jar.
    """
    if not JAVA.exists():
        sys.exit(f"missing {JAVA}")

    compiler = find_jar("kotlin-compiler-embeddable")
    kt_version = compiler.relative_to(CACHE).parts[2]
    stdlib = find_jar("kotlin-stdlib", prefer=kt_version)
    extras = [j for j in all_kotlin_jars(kt_version) if j != compiler]
    if not quiet:
        print(f"kotlin: {compiler.name}  (+{len(extras)} supporting jars)")

    work.mkdir(parents=True, exist_ok=True)
    srcs = list(sources) if sources is not None else [KT_SRC]
    for s in srcs:
        if not s.exists():
            sys.exit(f"missing source {s}")
    if driver is not None:
        (work / driver_name).write_text(driver, encoding="utf-8")
        srcs.append(work / driver_name)

    classes = work / "classes"
    if classes.exists():
        shutil.rmtree(classes)
    classes.mkdir()

    args = [
        "-cp", ";".join(str(j) for j in [compiler, *extras]),
        "org.jetbrains.kotlin.cli.jvm.K2JVMCompiler",
        *[str(s) for s in srcs],
        "-classpath", ";".join(str(j) for j in [stdlib, *(extra_cp or [])]),
        "-d", str(classes),
        "-no-stdlib", "-no-reflect", "-nowarn",
    ]
    direct = [str(JAVA), *args]
    cp = run(direct if _short_enough(direct) else [str(JAVA), *javac_args(args, work)])
    if cp.returncode != 0:
        print(cp.stdout)
        print(cp.stderr)
        sys.exit("kotlin compile failed")
    return [str(JAVA),
            # without these the JVM prints Chinese in the platform codepage (GBK)
            # and Python decodes it as UTF-8, turning 角色 into ��ɫ. The bytes
            # written to files were always correct; this is only the console.
            "-Dfile.encoding=UTF-8", "-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8",
            "-cp", ";".join(str(p) for p in [classes, stdlib, *(extra_cp or [])]),
            main_class]


def kotlin(runner: list[str], *args: str, check: bool = True) -> str:
    p = run(runner + list(args))
    if check and p.returncode != 0:
        sys.exit(f"kotlin run failed ({' '.join(args[:1])}): {p.stderr}")
    return p.stdout
