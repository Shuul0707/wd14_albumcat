#!/usr/bin/env python3
"""Time a ranged download from each candidate host to pick the fastest mirror.

The full model is ~356 MB and the HF CDN was throttling us to ~350 KB/s, so
it is worth measuring before committing to a ~20 minute download.

Usage::

    python tools/speedtest_hosts.py            # 8 MB per host
    python tools/speedtest_hosts.py --mb 16
"""

from __future__ import annotations

import argparse
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) albumcat-model-fetch/1.0"

REPO = "SmilingWolf/wd-v1-4-vit-tagger-v2"
FILE = "model.onnx"

HOSTS = {
    "huggingface.co": f"https://huggingface.co/{REPO}/resolve/main/{FILE}",
    "hf-mirror.com": f"https://hf-mirror.com/{REPO}/resolve/main/{FILE}",
    "modelscope.cn": f"https://modelscope.cn/models/AI-ModelScope/wd-v1-4-vit-tagger-v2/resolve/master/{FILE}",
}


def measure(label: str, url: str, nbytes: int, timeout: int = 30) -> tuple[str, float, str]:
    headers = {"User-Agent": UA, "Range": f"bytes=0-{nbytes - 1}"}
    req = urllib.request.Request(url, headers=headers)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            got = 0
            while got < nbytes:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                got += len(chunk)
        elapsed = max(time.monotonic() - started, 1e-6)
        speed = got / elapsed
        note = f"HTTP {status}"
        if status == 200:
            note += " (Range IGNORED - full file)"
        return label, speed, f"{got / 1e6:.1f}MB in {elapsed:.1f}s  {speed / 1024:.0f} KB/s  [{note}]"
    except Exception as exc:  # noqa: BLE001
        elapsed = max(time.monotonic() - started, 1e-6)
        return label, -1.0, f"FAILED after {elapsed:.1f}s: {exc!r}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mb", type=float, default=8.0)
    args = ap.parse_args()
    nbytes = int(args.mb * 1_000_000)

    print(f"probing {args.mb:g} MB from each host (concurrently) ...\n")
    with ThreadPoolExecutor(max_workers=len(HOSTS)) as pool:
        results = list(pool.map(lambda kv: measure(kv[0], kv[1], nbytes), HOSTS.items()))

    for label, speed, detail in sorted(results, key=lambda r: -r[1]):
        print(f"{label:>18}  {detail}")
    print()
    best = max(results, key=lambda r: r[1])
    if best[1] > 0:
        print(f"fastest: {best[0]}  ({best[1] / 1024:.0f} KB/s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
