"""Install Android SDK packages without going through dl.google.com.

`dl.google.com` measures ~21 KiB/s on this machine, which is what stalled the
Aves fork build; Tencent mirrors the very same repository at ~26 MB/s, so this
drives that mirror directly.  Two things come out of the repository manifest
(`repository2-3.xml`):

  * the archive URL (rewritten to the Tencent mirror) and its sha1, and
  * the `<type-details>` / `<revision>` / `<display-name>` needed for the
    `package.xml` that AGP's `LocalRepoLoaderImpl` requires in order to see a
    package at all -- the zips do not contain one, which is why a hand-extracted
    `platforms;android-37.0` was invisible to the build.

`package.xml` is written by copying the one `sdkmanager` itself wrote for another
package in the same group (its schema is then known-good), and, when the group
has none at all -- `cmake`, `ndk`, `cmdline-tools` -- by synthesising a
`<localPackage>` from the manifest's `<remotePackage>`.

Usage:  python sdk_install.py "build-tools;36.0.0" "platforms;android-36"
        python sdk_install.py "cmake;3.22.1"

Verified here for: build-tools;36.0.0, platforms;android-34, platforms;android-36,
platforms;android-37.0, cmake;3.22.1.  Use `.tmp\\show_pkg.py "<path>"` to print a
manifest entry before installing it.
"""
from __future__ import annotations

import hashlib
import io
import re
import shutil
import sys
import tarfile
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

SDK = Path(r"E:\Android\sdk")
TMP = Path(r"E:\AlbumCategories\.tmp")
CACHE = TMP / "sdk"
CACHE.mkdir(parents=True, exist_ok=True)
MANIFEST = TMP / "repository2-3.xml"
UA = {"User-Agent": "albumcat-setup"}

MIRROR = "https://mirrors.cloud.tencent.com/AndroidSDK/"
ORIGINS = ("https://dl.google.com/android/repository/", "https://dl-ssl.google.com/android/repository/")


def manifest() -> str:
    if MANIFEST.exists():
        return MANIFEST.read_text("utf-8", "replace")
    url = MIRROR + "repository2-3.xml"
    print(f"[get] {url}")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
        data = r.read()
    MANIFEST.write_bytes(data)
    return data.decode("utf-8", "replace")


def local_dir(path: str) -> Path:
    group, _, name = path.partition(";")
    return SDK / group / name


def install(path: str, xml: str) -> None:
    m = re.search(r'<remotePackage path="' + re.escape(path) + r'".*?</remotePackage>', xml, re.S)
    if not m:
        sys.exit(f"[FAIL] {path} is not in the manifest")
    blk = m.group(0)

    # ---- archive url + checksum -------------------------------------------
    arch = None
    for a in re.findall(r"<archive>.*?</archive>", blk, re.S):
        if "<host-os>windows</host-os>" not in a and "<host-os>" in a:
            continue
        for tag in ("complete", "url-package"):
            c = re.search(rf"<{tag}>.*?</{tag}>", a, re.S)
            if not c:
                continue
            u = re.search(r"<url>(.*?)</url>", c.group(0), re.S)
            if not u:
                continue
            url = u.group(1).strip()
            if url.endswith(".zip") or url.endswith(".tar.gz") or url.endswith(".tgz"):
                sz = re.search(r"<size>(\d+)</size>", c.group(0))
                sh = re.search(r"<checksum[^>]*>(.*?)</checksum>", c.group(0), re.S)
                arch = (url, int(sz.group(1)) if sz else 0, sh.group(1).strip() if sh else "")
                break
        if arch:
            break
    if not arch:
        sys.exit(f"[FAIL] {path}: no Windows archive in the manifest")
    fname, size, sha1 = arch
    print(f"[pkg] {path}  archive={fname}  size={size}  sha1={sha1[:12]}...")

    dst = local_dir(path)
    # Always re-fetch from the mirror: cheap (26 MB/s) and avoids trusting a
    # half-downloaded cache.
    src = CACHE / fname
    url = MIRROR + fname
    if not (src.exists() and size and src.stat().st_size == size):
        print(f"[dl] {url}")
        t0 = time.time()
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r, open(src, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        dt = time.time() - t0
        print(f"[ok] {src.stat().st_size} B in {dt:.1f}s "
              f"({src.stat().st_size / 1024 / max(dt, 1e-6):.0f} KiB/s)")
    if sha1 and sha1 != "0" * 40:
        h = hashlib.sha1(src.read_bytes()).hexdigest()
        print(f"[sha1] {h} {'MATCH' if h == sha1 else 'MISMATCH (expected ' + sha1 + ')'}")

    # ---- extract -----------------------------------------------------------
    dst.parent.mkdir(parents=True, exist_ok=True)
    stage = TMP / ("stage_" + path.replace(";", "_").replace(".", "_"))
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    with zipfile.ZipFile(src) as z:
        z.extractall(stage)
    roots = sorted({p.relative_to(stage).parts[0] for p in stage.iterdir()})
    print(f"[zip] top-level: {roots}")
    payload = stage
    if len(roots) == 1 and (stage / roots[0]).is_dir():
        payload = stage / roots[0]
    if dst.exists():
        shutil.rmtree(dst)
    shutil.move(str(payload), str(dst))
    shutil.rmtree(stage, ignore_errors=True)
    print(f"[ok] extracted {sum(1 for _ in dst.rglob('*'))} entries -> {dst}")

    # ---- package.xml -------------------------------------------------------
    # Reuse a package.xml that sdkmanager itself wrote for this installation --
    # its schema and namespace prefixes are then known-good for the parser in
    # use.  Only path / api-level / extension-level / revision differ.
    group = path.split(";")[0]
    template = None
    for cand in sorted((SDK / group).glob("*/package.xml")):
        template = cand
        break
    if template is not None:
        rev = re.search(r"<revision>.*?</revision>", blk, re.S)
        api = re.search(r"<api-level>(.*?)</api-level>", blk, re.S)
        ext = re.search(r"<extension-level>(.*?)</extension-level>", blk, re.S)
        ver = path.split(";")[1]
        name = re.search(r"<display-name>(.*?)</display-name>", blk, re.S)
        text = template.read_text("utf-8")
        text = re.sub(rf'path="{re.escape(group)};[^"]*"', f'path="{path}"', text)
        if api:
            text = re.sub(r"<api-level>[^<]*</api-level>", f"<api-level>{api.group(1)}</api-level>", text)
        if ext:
            text = re.sub(r"<extension-level>[^<]*</extension-level>",
                          f"<extension-level>{ext.group(1)}</extension-level>", text)
        if name:
            text = re.sub(r"<display-name>.*?</display-name>",
                          f"<display-name>{name.group(1)}</display-name>", text, flags=re.S)
        # replace the revision block so the reported version matches the manifest
        if rev:
            text = re.sub(r"<revision>.*?</revision>", lambda _: rev.group(0), text, count=1, flags=re.S)
        (dst / "package.xml").write_text(text, encoding="utf-8")
        print(f"[ok] package.xml from template {template.parent.name} "
              f"({(dst / 'package.xml').stat().st_size} B)")
        print("     " + re.sub(r"\s+", " ", re.search(r"<localPackage.*?</localPackage>", text, re.S).group(0)))
    else:
        # Nothing in this group was ever written by sdkmanager (cmake is one),
        # so build the <localPackage> straight from the manifest's
        # <remotePackage>: keep the identity block (type-details / revision /
        # display-name / uses-license) and drop <channelRef> and <archives>,
        # which sdkmanager's own file does not carry either.  The `generic`
        # namespace has to be declared because that is the prefix cmake, ndk
        # and cmdline-tools all use in their type-details.
        details = re.search(r"<type-details.*?</type-details>|<type-details[^>]*/>", blk, re.S)
        rev = re.search(r"<revision>.*?</revision>", blk, re.S)
        display = re.search(r"<display-name>(.*?)</display-name>", blk, re.S)
        lic = re.search(r'<license id="android-sdk-license" type="text">(.*?)</license>', xml, re.S)
        if not (details and rev and display and lic):
            print(f"[warn] no template for {path} and the manifest lacks its identity block; package.xml NOT written")
            return
        text = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<ns2:repository'
            ' xmlns:ns2="http://schemas.android.com/repository/android/common/02"'
            ' xmlns:generic="http://schemas.android.com/repository/android/generic/02"'
            ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
            f'  <license id="android-sdk-license" type="text">{lic.group(1)}</license>\n'
            f'  <localPackage path="{path}" obsolete="false">\n'
            f'    {details.group(0).strip()}\n'
            f'    {rev.group(0).strip()}\n'
            f'    <display-name>{display.group(1)}</display-name>\n'
            '    <uses-license ref="android-sdk-license" />\n'
            '  </localPackage>\n'
            '</ns2:repository>\n'
        )
        (dst / "package.xml").write_text(text, encoding="utf-8")
        print(f"[ok] package.xml synthesised from the manifest ({(dst / 'package.xml').stat().st_size} B)")

    srcprops = dst / "source.properties"
    if srcprops.exists():
        print("[source.properties]")
        for line in srcprops.read_text("utf-8", "replace").splitlines():
            if line.strip() and not line.startswith("#"):
                print("    " + line.strip())


xml = manifest()
for p in sys.argv[1:]:
    install(p, xml)
print("[done]")
