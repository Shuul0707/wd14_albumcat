#!/usr/bin/env python3
"""Download WD14 ONNX model files (``model.onnx`` + ``selected_tags.csv``).

Why this script exists instead of a plain ``curl``/``Invoke-WebRequest``:
this machine's Windows TLS stack (SChannel) is broken and every
PowerShell/.NET/curl HTTPS request fails with ``SEC_E_NO_CREDENTIALS``.
Python's own TLS works fine, so downloads go through Python.

Usage::

    python tools/download_model.py                     # -> models/vit/
    python tools/download_model.py --mirror            # force hf-mirror.com
    python tools/download_model.py --name vit --repo SmilingWolf/wd-v1-4-vit-tagger-v2
    python tools/download_model.py --force             # re-download from scratch

Runs on the *system* Python -- it only uses the standard library, so it works
before the project virtualenv exists.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_REPO = "SmilingWolf/wd-v1-4-vit-tagger-v2"
DEFAULT_NAME = "vit"
FILES = ("selected_tags.csv", "model.onnx")

HOSTS = {
    "hf": "https://huggingface.co",
    "mirror": "https://hf-mirror.com",
}

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# HuggingFace sits behind a CDN that occasionally 403s a bare urllib UA.
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) albumcat-model-fetch/1.0"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)}B" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


def head_size(url: str, timeout: int = 30) -> int | None:
    """Return Content-Length of ``url`` (following redirects), or None."""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            length = resp.headers.get("Content-Length")
            return int(length) if length is not None else None
    except Exception:
        return None


def download(url: str, dest: Path, expect_size: int | None = None, retries: int = 3) -> None:
    """Stream ``url`` into ``dest`` atomically, resuming a partial ``.part`` file."""
    part = dest.with_suffix(dest.suffix + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, retries + 1):
        already = part.stat().st_size if part.exists() else 0

        if expect_size is not None and already == expect_size:
            part.replace(dest)
            print(f"    already complete ({human(already)}) -- resumed from cache")
            return

        if expect_size is not None and already > expect_size:
            part.unlink()
            already = 0

        headers = {"User-Agent": UA}
        mode = "wb"
        if already:
            headers["Range"] = f"bytes={already}-"
            mode = "ab"

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                status = resp.status
                # A server that ignores Range replies 200 and we must start over.
                if already and status == 200:
                    print("    server ignored Range -- restarting from byte 0")
                    already, mode = 0, "wb"

                total = expect_size
                if total is None:
                    length = resp.headers.get("Content-Length")
                    total = (int(length) + already) if length else None

                done = already
                started = time.monotonic()
                last_draw = 0.0
                block = 1024 * 256

                with open(part, mode) as fh:
                    while True:
                        chunk = resp.read(block)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)

                        now = time.monotonic()
                        if now - last_draw >= 0.25:
                            last_draw = now
                            elapsed = now - started
                            speed = (done - already) / elapsed if elapsed > 0 else 0.0
                            if total:
                                pct = 100.0 * done / total
                                bar_len = 28
                                filled = int(bar_len * done / total)
                                bar = "#" * filled + "-" * (bar_len - filled)
                                sys.stdout.write(
                                    f"\r    [{bar}] {pct:5.1f}%  "
                                    f"{human(done)}/{human(total)}  {human(speed)}/s   "
                                )
                            else:
                                sys.stdout.write(f"\r    {human(done)}  {human(speed)}/s   ")
                            sys.stdout.flush()
                sys.stdout.write("\n")

            size = part.stat().st_size
            if expect_size is not None and size != expect_size:
                raise IOError(f"size mismatch: got {size}, expected {expect_size}")
            part.replace(dest)
            print(f"    saved -> {dest}  ({human(size)})")
            return

        except Exception as exc:  # noqa: BLE001 - we retry on anything transient
            if attempt >= retries:
                raise
            wait = 2 * attempt
            print(f"    attempt {attempt}/{retries} failed: {exc!r} -- retrying in {wait}s")
            time.sleep(wait)


def fetch_file(host: str, repo: str, filename: str, dest: Path, force: bool) -> None:
    url = f"{host}/{repo}/resolve/main/{filename}"
    print(f"  {filename}")
    print(f"    url: {url}")

    if dest.exists() and not force:
        existing = dest.stat().st_size
        remote = head_size(url)
        if remote is not None and existing == remote:
            print(f"    up to date ({human(existing)}) -- skipping")
            return
        if remote is None:
            print(f"    present ({human(existing)}), remote size unknown -- skipping (use --force)")
            return
        print(f"    size differs (local {human(existing)} vs remote {human(remote)}) -- refetching")

    expected = head_size(url)
    if expected is not None:
        print(f"    remote size: {human(expected)}")
    download(url, dest, expect_size=expected)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=DEFAULT_REPO, help=f"HF repo id (default: {DEFAULT_REPO})")
    ap.add_argument("--name", default=DEFAULT_NAME, help=f"local model dir name (default: {DEFAULT_NAME})")
    ap.add_argument("--dest", type=Path, default=None, help="destination dir (default: <root>/models/<name>)")
    ap.add_argument("--mirror", action="store_true", help="use hf-mirror.com instead of huggingface.co")
    ap.add_argument("--force", action="store_true", help="re-download even if the file looks current")
    args = ap.parse_args()

    dest = args.dest or (PROJECT_ROOT / "models" / args.name)
    host = HOSTS["mirror"] if args.mirror else HOSTS["hf"]

    print(f"model  : {args.repo}")
    print(f"host   : {host}")
    print(f"dest   : {dest}")
    print()

    failures: list[str] = []
    for filename in FILES:
        try:
            fetch_file(host, args.repo, filename, dest / filename, args.force)
        except Exception as exc:  # noqa: BLE001
            failures.append(filename)
            print(f"    FAILED: {exc!r}")
            if not args.mirror:
                print("    hint: retry with --mirror (hf-mirror.com is reachable from this machine)")

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1

    print("OK -- model files in place:")
    for filename in FILES:
        path = dest / filename
        print(f"  {path}  ({human(path.stat().st_size)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
