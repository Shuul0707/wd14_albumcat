"""Fetch the Android SDK pieces Gradle needs, without using sdkmanager.

Why not just run sdkmanager? Because this machine's Windows TLS stack is
broken (SChannel returns SEC_E_NO_CREDENTIALS for PowerShell and curl, and
`wsl --update` fails with 403). Python's TLS works fine, so we read Google's
repository manifest ourselves and download the zips directly.

The manifest is the same one sdkmanager reads:
    https://dl.google.com/android/repository/repository2-3.xml

Usage:
    python tools\\setup_android.py                 # install into E:\\Android\\sdk
    python tools\\setup_android.py --list          # show what it would fetch
    python tools\\setup_android.py --root D:\\sdk
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

REPO_URL = "https://dl.google.com/android/repository/repository2-3.xml"
BASE_URL = "https://dl.google.com/android/repository/"
DEFAULT_ROOT = Path(os.environ.get("ANDROID_SDK_ROOT") or r"E:\Android\sdk")

# (package path, where it lands inside the SDK root)
TARGETS: list[tuple[str, str]] = [
    ("cmdline-tools;latest", "cmdline-tools/latest"),
    ("platform-tools", "platform-tools"),
    ("platforms;android-35", "platforms/android-35"),
    ("build-tools;35.0.0", "build-tools/35.0.0"),
]


def _text(node: ET.Element | None, tag: str) -> str:
    if node is None:
        return ""
    found = node.find(tag)
    return (found.text or "").strip() if found is not None else ""


def fetch(url: str, dest: Path, *, expect_size: int = 0) -> Path:
    """Download with a .part file so an interrupted run resumes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    if expect_size and have == expect_size:
        part.replace(dest)
        print(f"  cached  {dest.name}")
        return dest
    req = urllib.request.Request(url, headers={"User-Agent": "albumcat-setup/1.0"})
    if have:
        req.add_header("Range", f"bytes={have}-")
    print(f"  get     {url.rsplit('/', 1)[-1]}  ({expect_size / 1e6:.1f} MB)" if expect_size
          else f"  get     {url}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        if have and resp.status != 206:
            have = 0  # server ignored the range; start over
        mode = "ab" if have else "wb"
        with open(part, mode) as fh:
            total = have
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
                if expect_size:
                    pct = 100.0 * total / expect_size
                    print(f"\r          {total / 1e6:7.1f} / {expect_size / 1e6:.1f} MB ({pct:5.1f}%)",
                          end="", flush=True)
    if expect_size:
        print()
    part.replace(dest)
    return dest


def load_manifest() -> dict[str, dict]:
    """path -> {'url', 'size', 'host_os'} for the newest stable win/x86_64 archive."""
    print(f"reading {REPO_URL}")
    req = urllib.request.Request(REPO_URL, headers={"User-Agent": "albumcat-setup/1.0"})
    raw = urllib.request.urlopen(req, timeout=60).read()
    root = ET.fromstring(raw)

    stable: set[str] = set()
    for ch in root.findall("channel"):
        stable.add(ch.get("id", ""))

    out: dict[str, dict] = {}
    for pkg in root.findall("remotePackage"):
        path = pkg.get("path") or ""
        refs = {r.get("ref") for r in pkg.findall("channelRef")}
        if refs and not (refs & stable):
            continue
        # Pick the archive that suits this host, preferring one with no host-os.
        best: dict | None = None
        for arch in pkg.findall("./archives/archive"):
            host = _text(arch, "host-os")
            if host and host != "windows":
                continue
            complete = arch.find("complete")
            if complete is None:
                continue
            url = _text(complete, "url")
            if not url:
                continue
            size = int(_text(complete, "size") or 0)
            # A host-specific entry beats a generic one.
            if best is None or (host and not best.get("host_os")):
                best = {"url": url, "size": size, "host_os": host}
        if best:
            out[path] = best
    print(f"  {len(out)} packages in the stable channel")
    return out


def extract(zip_path: Path, target: Path) -> None:
    """Unzip and flatten: the zip's single top-level folder becomes `target`."""
    staging = target.parent / (target.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(staging)
    entries = list(staging.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        shutil.move(str(entries[0]), str(target))
        staging.rmdir()
    else:
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(staging), str(target))
    print(f"  unpacked -> {target}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="SDK root (ANDROID_SDK_ROOT)")
    ap.add_argument("--list", action="store_true", help="only print what would be fetched")
    ap.add_argument("--keep-zips", action="store_true", help="keep downloaded archives")
    args = ap.parse_args(argv)

    root = Path(args.root)
    manifest = load_manifest()

    missing = [p for p, _ in TARGETS if p not in manifest]
    if missing:
        print(f"!! manifest has no entry for: {', '.join(missing)}", file=sys.stderr)
        return 2

    if args.list:
        for pkg, dest in TARGETS:
            info = manifest[pkg]
            print(f"{pkg:28s} {info['size'] / 1e6:8.1f} MB -> {dest}")
        return 0

    cache = root / ".zips"
    installed = 0
    for pkg, dest in TARGETS:
        target = root / dest
        info = manifest[pkg]
        if (target / "source.properties").exists() or (target / "package.xml").exists():
            print(f"already installed: {dest}")
            installed += 1
            continue
        name = info["url"].rsplit("/", 1)[-1]
        print(f"\n== {pkg} -> {dest}")
        zip_path = fetch(BASE_URL + info["url"], cache / name, expect_size=info["size"])
        extract(zip_path, target)
        installed += 1
        if not args.keep_zips:
            zip_path.unlink(missing_ok=True)

    print(f"\n{installed}/{len(TARGETS)} packages present under {root}")
    print("\nNow tell Gradle where it is. For this shell:")
    print(f'  $env:ANDROID_SDK_ROOT = "{root}"')
    print("or write it into android\\local.properties as  sdk.dir=...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
