"""Fetch the binary set: anime memes (safebooru ``meme``) vs illustrations.

The layout is exactly what ``tools/train_feature_plugin.py`` expects for a
single-label head::

    <root>/梗图/     positives   (safebooru posts tagged ``meme``)
    <root>/_插画/    background  (illustrations; the leading underscore means
                                 "trains every label down", i.e. negatives)

Roughly a third of the negatives carry text (``english_text``, ``chinese_text``,
``speech_bubble``) so the head cannot take the shortcut "has text == meme".

Nothing here ships with the repository: images land under ``data/datasets``
(git-ignored) and only the trained head plus its labels are kept.

    python tools/fetch_meme_dataset.py --out data/datasets/meme_vs_illustration
    python tools/fetch_meme_dataset.py --out ... --positive 4000   # more memes
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

UA = "albumcat-dataset/0.1 (+https://github.com/Shuul0707/wd14_albumcat)"
LONG_EDGE = 640
MIN_SIDE = 200
TIMEOUT = 45
RETRIES = 3
WORKERS = 16

POSITIVE_LABEL = "梗图"
BACKGROUND_LABEL = "_插画"
POSITIVE_NAME = "meme"
BACKGROUND_NAME = "illustration"

# Plain illustrations. Kept as several single-tag queries because the XML API
# takes one tag expression per request and a broad one burns through pages fast.
ILLUSTRATION_TAGS = (
    "1girl solo",
    "1boy solo",
    "scenery",
    "no humans",
    "original",
    "2girls",
)

# Illustrations that *do* carry text: the hard negatives. A post that also
# carries ``meme`` is dropped by ``--exclude-negative-tags``.
TEXT_NEGATIVE_TAGS = (
    "english_text 1girl",
    "chinese_text 1girl",
    "speech_bubble 1girl",
)

_MEME_TAG = "meme"


def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


OPENER = opener()
_LOCK = threading.Lock()
_SEEN_HASH: set[int] = set()
_SEEN_BYTES: set[str] = set()


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


def safebooru_posts(tags: str, page: int) -> list[dict[str, str]]:
    """One XML page as ``[{"url": ..., "tags": "a b c"}, ...]``.

    Prefers the 850 px ``sample_url``: the original is often a 3-10 MB PNG and we
    keep 640 px, so the sample is an order of magnitude cheaper to download.
    """
    url = (
        "https://safebooru.org/index.php?page=dapi&s=post&q=index"
        f"&limit=100&pid={page}&tags={urllib.request.quote(tags)}"
    )
    xml = get(url).decode("utf-8", "replace")
    found: list[dict[str, str]] = []
    for match in re.finditer(r"<post ([^>]+?)/?>", xml):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', match.group(1)))
        link = attrs.get("sample_url") or attrs.get("file_url") or ""
        if link:
            found.append({"url": link, "tags": attrs.get("tags", "")})
    return found


def ahash(rgb: Image.Image) -> int:
    """64-bit average hash -- catches the same picture reposted/re-encoded."""
    small = rgb.convert("L").resize((8, 8), Image.BILINEAR)
    pixels = small.tobytes()
    mean = sum(pixels) / len(pixels)
    bits = 0
    for index, value in enumerate(pixels):
        if value >= mean:
            bits |= 1 << index
    return bits


def decode(data: bytes) -> tuple[Image.Image, int, str] | None:
    """Decode to RGB, shrink to LONG_EDGE; ``None`` when unusable or a duplicate."""
    digest = hashlib.sha256(data).hexdigest()
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            if min(im.size) < MIN_SIDE:
                return None
            rgb = im.convert("RGB")          # GIFs contribute their first frame
    except Exception:  # noqa: BLE001 -- a broken download is not an error, skip it
        return None
    key = ahash(rgb)
    with _LOCK:
        if digest in _SEEN_BYTES or key in _SEEN_HASH:
            return None
        _SEEN_BYTES.add(digest)
        _SEEN_HASH.add(key)
    rgb.thumbnail((LONG_EDGE, LONG_EDGE), Image.LANCZOS)
    return rgb, key, digest


def record(manifest, label: str, url: str, path: Path, width: int, height: int) -> None:
    line = json.dumps({
        "path": str(path), "label": label, "url": url, "width": width, "height": height,
    }, ensure_ascii=False) + "\n"
    with _LOCK:
        manifest.write(line)


def next_index(out_dir: Path, prefix: str) -> int:
    """First free ``<prefix>-NNNNN.jpg`` number, so a re-run appends."""
    highest = -1
    if not out_dir.exists():
        return 0
    for file in out_dir.glob(f"{prefix}-*.jpg"):
        stem = file.stem.split("-", 1)[-1]
        if stem.isdigit():
            highest = max(highest, int(stem))
    return highest + 1


def seed_from_disk(root: Path) -> None:
    """Remember what is already on disk so a re-run keeps the set duplicate-free."""
    for sub in root.glob("*"):
        if not sub.is_dir():
            continue
        for file in sub.glob("*.jpg"):
            try:
                with Image.open(file) as im:
                    im.load()
                    key = ahash(im.convert("RGB"))
            except Exception:  # noqa: BLE001
                continue
            _SEEN_HASH.add(key)
    print(f"  seeded {len(_SEEN_HASH)} hashes from {root}")


def collect(tag_queries, exclude_tags=(), want: int = 0, max_pages: int = 60,
            label: str = "") -> list[str]:
    """Walk pages of each tag query until ``want`` URLs are queued."""
    urls: list[str] = []
    seen: set[str] = set()
    started = time.time()
    for page in range(max_pages):
        for tags in tag_queries:
            try:
                posts = safebooru_posts(tags, page)
            except RuntimeError as exc:
                print(f"  safebooru {tags!r} page {page}: {exc}")
                continue
            for post in posts:
                words = set(post["tags"].split())
                if exclude_tags and words & set(exclude_tags):
                    continue
                if post["url"] in seen:
                    continue
                seen.add(post["url"])
                urls.append(post["url"])
            if len(urls) >= want:
                break
        if len(urls) >= want:
            break
        print(f"    {label}: {len(urls)}/{want} urls after page {page} "
              f"({time.time() - started:.0f}s)", flush=True)
    print(f"  {label}: {len(urls)} urls queued", flush=True)
    return urls[:want]


def fetch_group(
    urls: list[str],
    out_dir: Path,
    prefix: str,
    label: str,
    manifest,
    start: int = 0,
) -> int:
    """Download one pool; returns how many files were actually kept."""

    def one(job):
        index, url = job
        try:
            data = get(url)
        except RuntimeError as exc:
            print(f"    ! {exc}")
            return 0
        decoded = decode(data)
        if decoded is None:
            return 0
        rgb, _key, _digest = decoded
        path = out_dir / f"{prefix}-{start + index:05d}.jpg"
        try:
            rgb.save(path, "JPEG", quality=88, optimize=True)
        except OSError:
            return 0
        record(manifest, label, url, path, rgb.width, rgb.height)
        return 1

    saved = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for result in pool.map(one, list(enumerate(urls))):
            saved += result
            if saved and saved % 100 == 0:
                print(f"    {label}: {saved}/{len(urls)} kept", flush=True)
    return saved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch memes vs illustrations.")
    parser.add_argument("--out", default="data/datasets/meme_vs_illustration")
    parser.add_argument("--positives", type=int, default=3000,
                        help="memes to keep (safebooru tag 'meme')")
    parser.add_argument("--negatives", type=int, default=2000,
                        help="plain illustrations to keep")
    parser.add_argument("--hard-negatives", type=int, default=1000,
                        help="illustrations that carry text")
    parser.add_argument("--max-pages", type=int, default=60)
    args = parser.parse_args(argv)

    root = Path(args.out).expanduser().resolve()
    positive_dir = root / POSITIVE_LABEL
    background_dir = root / BACKGROUND_LABEL
    for path in (positive_dir, background_dir):
        path.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.jsonl"
    if not manifest_path.exists():
        manifest_path.write_text("", encoding="utf-8")
    seed_from_disk(root)

    stayed = {POSITIVE_LABEL: len(list(positive_dir.glob("*.jpg"))),
              BACKGROUND_LABEL: len(list(background_dir.glob("*.jpg")))}
    print(f"already there: {POSITIVE_LABEL} {stayed[POSITIVE_LABEL]}, "
          f"{BACKGROUND_LABEL} {stayed[BACKGROUND_LABEL]}")

    want_pos = max(0, args.positives - stayed[POSITIVE_LABEL])
    if want_pos == 0 and args.negatives <= 0 and args.hard_negatives <= 0:
        print("nothing to do")
        return 0

    with manifest_path.open("a", encoding="utf-8") as manifest:
        counts: dict[str, int] = {}

        if want_pos:
            print(f"\npositives: memes ({POSITIVE_LABEL}) ...")
            urls = collect([_MEME_TAG], want=want_pos, max_pages=args.max_pages,
                           label="meme")
            started = time.time()
            counts["meme"] = fetch_group(
                urls, positive_dir, POSITIVE_NAME, "meme", manifest,
                start=next_index(positive_dir, POSITIVE_NAME))
            print(f"  meme: {counts['meme']} kept in {time.time() - started:.0f}s")

        plain = max(0, args.negatives)
        if plain:
            print(f"\nnegatives: plain illustrations ({BACKGROUND_LABEL}) ...")
            urls = collect(ILLUSTRATION_TAGS, exclude_tags=(_MEME_TAG,), want=plain,
                           max_pages=args.max_pages, label="illustration")
            started = time.time()
            counts["illustration"] = fetch_group(
                urls, background_dir, BACKGROUND_NAME, "illustration", manifest,
                start=next_index(background_dir, BACKGROUND_NAME))
            print(f"  illustration: {counts['illustration']} kept "
                  f"in {time.time() - started:.0f}s")

        if args.hard_negatives > 0:
            print(f"\nnegatives: illustrations with text ({BACKGROUND_LABEL}) ...")
            urls = collect(TEXT_NEGATIVE_TAGS, exclude_tags=(_MEME_TAG,),
                           want=args.hard_negatives, max_pages=args.max_pages,
                           label="texted")
            started = time.time()
            counts["texted"] = fetch_group(
                urls, background_dir, "texted", "texted", manifest,
                start=next_index(background_dir, "texted"))
            print(f"  texted: {counts['texted']} kept in {time.time() - started:.0f}s")

    total = sum(counts.values())
    print(f"\n{total} new images in {root}")
    for name, directory, prefix in (
        (POSITIVE_LABEL, positive_dir, ""),
        (BACKGROUND_LABEL, background_dir, ""),
    ):
        have = len(list(directory.glob("*.jpg")))
        print(f"  {name:8s} total {have:5d}")
    print(f"\nnext:\n  python tools/train_feature_plugin.py --data {root} "
          f"--loss bce --binary-label {POSITIVE_LABEL} --out plugins/meme "
          f"--id meme --name \"梗图判定\" --target tag --threshold 0.5 "
          f"--cache {root / 'embeddings.npz'} --force")
    return 0 if total >= 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
