"""Build a labelled image set for the illustration / meme / photo / screenshot head.

Nothing here ships with the repository: the images land in ``data/datasets``
(``/data/`` is git-ignored next to ``.venv``), and only the derived embedding
table plus the trained head are meant to be kept. Re-run this when you want a
fresh or larger set.

Sources, all reachable without an account:

``illustration``
    safebooru.org's XML API. The site is SFW by policy; we still let you change
    the tag queries with ``--illustration-tags``.
``photo``
    picsum.photos (Unsplash photographs, no key needed).
``meme``
    ``neuralcatcher/hateful_memes`` on the Hugging Face hub -- Facebook's
    "hateful memes" set is a large bag of real internet memes with text burned
    in. We only use them as *negatives*, and we never look at the labels.
``screenshot``
    ``silatus/1k_Website_Screenshots_and_Metadata``, i.e. real UI screenshots.

Every download is re-encoded to a JPEG with a 640 px long edge, so a full set is
roughly 200 MB instead of a few gigabytes. Duplicates are dropped by SHA-256 of
the *source* bytes, and ``manifest.jsonl`` records where each file came from.

    python tools/fetch_illustration_dataset.py --out data/datasets/kind
    python tools/fetch_illustration_dataset.py --out data/datasets/kind --memes 0  # skip a source
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

LABELS = ("illustration", "meme", "photo", "screenshot")
UA = "albumcat-dataset/0.1 (+https://github.com/Shuul0707/wd14_albumcat)"
LONG_EDGE = 640
MIN_SIDE = 200
TIMEOUT = 45
RETRIES = 3


def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


OPENER = opener()
_LOCK = threading.Lock()
_SEEN: set[str] = set()


def get(url: str, timeout: int = TIMEOUT) -> bytes:
    """GET with retries; every source here is plain HTTPS."""
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", UA)
            with OPENER.open(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:  # noqa: PERF203
            last = exc
            time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def save_jpeg(data: bytes, out_dir: Path, index: int, label: str) -> tuple[Path, int, int] | None:
    """Decode, shrink to LONG_EDGE, write ``<label>-<index>.jpg``; None if unusable."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            if min(im.size) < MIN_SIDE:
                return None
            rgb = im.convert("RGB")
    except Exception:  # noqa: BLE001 -- a broken download is not an error, we skip it
        return None
    rgb.thumbnail((LONG_EDGE, LONG_EDGE), Image.LANCZOS)
    path = out_dir / f"{label}-{index:05d}.jpg"
    rgb.save(path, "JPEG", quality=88, optimize=True)
    return path, rgb.width, rgb.height


def record(manifest, label: str, url: str, path: Path, width: int, height: int) -> None:
    line = json.dumps({
        "path": str(path), "label": label, "url": url, "width": width, "height": height,
    }, ensure_ascii=False) + "\n"
    with _LOCK:
        manifest.write(line)


def already(data: bytes) -> bool:
    digest = hashlib.sha256(data).hexdigest()
    with _LOCK:
        if digest in _SEEN:
            return True
        _SEEN.add(digest)
    return False


# --------------------------------------------------------------------------- #
# illustration: safebooru XML
# --------------------------------------------------------------------------- #
DEFAULT_ILLUSTRATION_TAGS = (
    "1girl solo",
    "1boy solo",
    "2girls",
    "scenery",
    "no humans",
    "multiple boys",
    "original",
)


def safebooru_page(tags: str, page: int) -> list[str]:
    """Return image URLs for one page, preferring the 850 px ``sample_url``.

    Full originals are often 3-10 MB PNGs; the sample JPEG is ~150 KB and still
    far above the 640 px we keep, so the download is an order of magnitude cheaper.
    """
    url = (
        "https://safebooru.org/index.php?page=dapi&s=post&q=index"
        f"&limit=100&pid={page}&tags={urllib.request.quote(tags)}"
    )
    xml = get(url).decode("utf-8", "replace")
    found: list[str] = []
    for match in re.finditer(r"<post ([^>]+?)/?>", xml):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', match.group(1)))
        sample = attrs.get("sample_url") or ""
        file_url = attrs.get("file_url") or ""
        if sample:
            found.append(sample)
        elif file_url:
            found.append(file_url)
    return found


def fetch_illustrations(
    limit: int, tags_list: list[str], out_dir: Path, manifest, pages: int = 1, start: int = 0
) -> int:
    urls: list[str] = []
    seen_urls: set[str] = set()
    for pid in range(max(1, pages)):
        for tags in tags_list:
            if len(urls) >= limit * 2:
                break
            try:
                found = safebooru_page(tags, pid)
            except RuntimeError as exc:
                print(f"  safebooru {tags!r} page {pid}: {exc}")
                continue
            for url in found:
                if url not in seen_urls:
                    seen_urls.add(url)
                    urls.append(url)
        if len(urls) >= limit * 2:
            break
    urls = urls[:limit]
    print(f"  illustration: {len(urls)} urls queued over {pages} page(s) of each tag")

    def one(job):
        index, url = job
        try:
            data = get(url)
        except RuntimeError:
            return 0
        if already(data):
            return 0
        saved = save_jpeg(data, out_dir, index, "illustration")
        if not saved:
            return 0
        path, width, height = saved
        record(manifest, "illustration", url, path, width, height)
        return 1

    return run_pool(list(enumerate(urls, start)), one)


# --------------------------------------------------------------------------- #
# photo: picsum
# --------------------------------------------------------------------------- #
def fetch_photos(limit: int, out_dir: Path, manifest, start: int = 0) -> int:
    items: list[dict] = []
    page = 1
    while len(items) < limit and page <= 12:
        data = json.loads(get(f"https://picsum.photos/v2/list?page={page}&limit=100").decode())
        if not data:
            break
        items.extend(data)
        page += 1
    items = items[:limit]
    print(f"  photo: {len(items)} picsum ids queued")

    def one(job):
        index, item = job
        url = f"https://picsum.photos/id/{item['id']}/800/600"
        try:
            data = get(url)
        except RuntimeError:
            return 0
        if already(data):
            return 0
        saved = save_jpeg(data, out_dir, index, "photo")
        if not saved:
            return 0
        path, width, height = saved
        record(manifest, "photo", url, path, width, height)
        return 1

    return run_pool(list(enumerate(items, start)), one)


# --------------------------------------------------------------------------- #
# meme: HF hub, one file per request
# --------------------------------------------------------------------------- #
def hf_tree(dataset: str, path: str = "") -> list[dict]:
    url = f"https://huggingface.co/api/datasets/{dataset}/tree/main/{path}".rstrip("/")
    for suffix in ("?recursive=false&limit=1000", ""):
        try:
            return json.loads(get(url + suffix).decode())
        except RuntimeError as exc:
            print(f"  hf tree {dataset}/{path}: {exc}")
    return []


def fetch_memes(limit: int, out_dir: Path, manifest, start: int = 0) -> int:
    dataset = "neuralcatcher/hateful_memes"
    listed = hf_tree(dataset, "img")
    entries = sorted(
        (e for e in listed if e.get("type") == "file"),
        key=lambda e: e["path"],
    )
    step = max(1, len(entries) // max(1, limit))
    entries = entries[::step][:limit]
    print(f"  meme: {len(entries)} files queued out of {len(listed)}")

    def one(job):
        index, entry = job
        url = f"https://huggingface.co/datasets/{dataset}/resolve/main/{entry['path']}"
        try:
            data = get(url)
        except RuntimeError:
            return 0
        if already(data):
            return 0
        saved = save_jpeg(data, out_dir, index, "meme")
        if not saved:
            return 0
        path, width, height = saved
        record(manifest, "meme", url, path, width, height)
        return 1

    return run_pool(list(enumerate(entries, start)), one)


# --------------------------------------------------------------------------- #
# screenshot: HF hub, one directory per sample
# --------------------------------------------------------------------------- #
def fetch_screenshots(limit: int, out_dir: Path, manifest, start: int = 0) -> int:
    dataset = "silatus/1k_Website_Screenshots_and_Metadata"
    dirs = sorted(
        (e for e in hf_tree(dataset) if e.get("type") == "directory"),
        key=lambda e: e["path"],
    )[:limit]
    print(f"  screenshot: {len(dirs)} dirs queued")

    def one(job):
        index, entry = job
        url = f"https://huggingface.co/datasets/{dataset}/resolve/main/{entry['path']}/screenshot.png"
        try:
            data = get(url)
        except RuntimeError:
            return 0
        if already(data):
            return 0
        saved = save_jpeg(data, out_dir, index, "screenshot")
        if not saved:
            return 0
        path, width, height = saved
        record(manifest, "screenshot", url, path, width, height)
        return 1

    return run_pool(list(enumerate(dirs, start)), one)


def run_pool(jobs: list, fn, workers: int = 16) -> int:
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(fn, jobs):
            done += result
            if done % 50 == 0 and done:
                print(f"    ... {done} saved", flush=True)
    return done


def next_index(out_dir: Path, label: str) -> int:
    """First free ``<label>-NNNNN.jpg`` number, so a top-up appends."""
    highest = -1
    if not out_dir.exists():
        return 0
    for file in out_dir.glob(f"{label}-*.jpg"):
        stem = file.stem.split("-", 1)[-1]
        if stem.isdigit():
            highest = max(highest, int(stem))
    return highest + 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch a labelled set for the illustration head.")
    parser.add_argument("--out", default="data/datasets/kind", help="output root (git-ignored)")
    parser.add_argument("--illustrations", type=int, default=1200, help="0 skips the source")
    parser.add_argument("--photos", type=int, default=700, help="0 skips the source")
    parser.add_argument("--memes", type=int, default=500, help="0 skips the source")
    parser.add_argument("--screenshots", type=int, default=150, help="0 skips the source")
    parser.add_argument(
        "--illustration-tags",
        default=",".join(DEFAULT_ILLUSTRATION_TAGS),
        help="comma-separated safebooru tag queries, cycled one per page",
    )
    parser.add_argument(
        "--pages",
        type=int,
        default=1,
        help="safebooru result pages per tag query; raise it when a first pass ran dry",
    )
    parser.add_argument(
        "--top-up",
        action="store_true",
        help="append to a dataset that already exists instead of refusing to run",
    )
    args = parser.parse_args(argv)

    root = Path(args.out).expanduser().resolve()
    if root.exists() and any(root.iterdir()) and not args.top_up:
        print(
            f"{root} is not empty; pass --top-up to append, or delete it to start over.",
            file=sys.stderr,
        )
        return 2
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.jsonl"

    starts = {label: next_index(root, label) for label in LABELS}
    if args.top_up:
        print("top-up starts: " + ", ".join(f"{k}={v}" for k, v in starts.items()))

    counts: dict[str, int] = {}
    with manifest_path.open("a" if args.top_up else "w", encoding="utf-8") as manifest:
        plan = [
            ("illustration", args.illustrations, lambda: fetch_illustrations(
                args.illustrations, [t.strip() for t in args.illustration_tags.split(",") if t.strip()],
                root, manifest, pages=args.pages, start=starts["illustration"])),
            ("meme", args.memes, lambda: fetch_memes(
                args.memes, root, manifest, start=starts["meme"])),
            ("photo", args.photos, lambda: fetch_photos(
                args.photos, root, manifest, start=starts["photo"])),
            ("screenshot", args.screenshots, lambda: fetch_screenshots(
                args.screenshots, root, manifest, start=starts["screenshot"])),
        ]
        for label, limit, task in plan:
            if limit <= 0:
                print(f"{label}: skipped")
                continue
            print(f"{label}: fetching up to {limit} ...")
            started = time.time()
            counts[label] = task()
            print(f"{label}: {counts[label]} saved in {time.time() - started:.0f}s")

    total = sum(counts.values())
    print(f"\n{total} new images in {root}")
    for label in LABELS:
        have = len(list(root.glob(f"{label}-*.jpg")))
        print(f"  {label:13s} +{counts.get(label, 0):4d}  total {have:4d}")
    return 0 if total >= 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
