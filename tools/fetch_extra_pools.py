"""Turn the raw extra pools into one JPEG folder per source.

    tools\\fetch_extra_pools.py [parquet|coco|bqb|desktop ...]

Why this exists: the first four-class head was trained on a single source per
class (safebooru for 插画/梗图, a HuggingFace meme set, two Wikimedia dumps).
A head that only ever saw one source learns that source's quirks, not the
class.  This tool adds deliberately different sources:

    梗图  MET-Meme (Chinese memes, mostly captioned) + ChineseBQB (Chinese
          sticker packs: anime and non-anime, captioned and clean)
    照片  COCO val2017 (people, streets, indoor scenes) + an earth-landscape
          photo set (no people, pure scenery)
    截图  RICO Screen2Words (Android UI) + locally rendered desktop/mobile web
          screenshots

Everything is decoded, re-encoded to JPEG and average-hash deduplicated, so the
build step never has to trust the raw download layout.  Output:
``data/datasets/extra/<source>/<name>.jpg`` plus ``extra/manifest.jsonl``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Iterable, Iterator

from PIL import Image

RAW = Path("data/datasets/raw")
OUT = Path("data/datasets/extra")

CLASS_OF_SOURCE = {
    "met-meme": "梗图",
    "chinesebqb": "梗图",
    "coco": "照片",
    "coco-hf": "照片",
    "celeba": "照片",
    "commons": "照片",
    "picsum": "照片",
    "landscape": "照片",
    "rico": "截图",
    "desktop": "截图",
}

# dataset parquet shards reachable from huggingface.co (see take_hf_parquet)
HF_SHARDS = {
    # COCO val2017 with annotations: the everyday-photo class -- people, food,
    # street scenes, pets -- which is what a phone camera roll actually holds
    "coco-hf": "https://huggingface.co/api/datasets/detection-datasets/coco/parquet/default/val/0.parquet",
    # aligned face crops: portrait-heavy, the weakest slice of the photo class
    "celeba": "https://huggingface.co/api/datasets/nielsr/CelebA-faces/parquet/default/train/0.parquet",
}

TARGETS = {
    "met-meme": 700,
    "chinesebqb": 1000,
    "coco": 900,
    "commons": 120,
    "picsum": 700,
    "landscape": 450,
    "rico": 600,
    "coco-hf": 800,
    # 178x218 aligned crops -- below MIN_SIDE, and upscaling them would teach
    # "a face filling the frame" instead of a photo; kept at 0 on purpose
    "celeba": 0,
    "desktop": 250,
}

MAX_EDGE = 1600
MIN_SIDE = 200
MIN_BYTES = 3_000

# Commons categories for the photo class: people, streets, scenery, food, pets,
# interiors -- roughly what a phone camera actually ends up pointed at.
COMMONS_CATEGORIES = (
    "Category:Portrait photographs",
    "Category:Street photography",
    "Category:Landscape photographs",
    "Category:Food photography",
    "Category:Domestic cats",
    "Category:Living rooms",
)

# A spread of desktop and mobile UIs; the mix matters more than the list.
SITES = [
    "https://www.bilibili.com", "https://www.zhihu.com", "https://weibo.com",
    "https://www.douban.com", "https://www.taobao.com", "https://www.jd.com",
    "https://github.com", "https://stackoverflow.com", "https://developer.mozilla.org",
    "https://news.ycombinator.com", "https://www.bbc.com/news", "https://www.nytimes.com",
    "https://www.wikipedia.org", "https://www.youtube.com", "https://www.reddit.com",
    "https://www.amazon.com", "https://www.apple.com", "https://www.microsoft.com",
    "https://www.google.com", "https://www.baidu.com", "https://dict.youdao.com",
    "https://mail.qq.com", "https://www.12306.cn", "https://www.gov.cn",
    "https://www.douyin.com", "https://www.xiaohongshu.com", "https://www.zhihu.com/explore",
    "https://segmentfault.com", "https://juejin.cn", "https://www.cnblogs.com",
    "https://pypi.org", "https://huggingface.co", "https://www.kaggle.com",
    "https://www.notion.so", "https://www.figma.com", "https://trello.com",
    "https://www.office.com", "https://outlook.live.com", "https://calendar.google.com",
    "https://www.dropbox.com", "https://www.evernote.com", "https://www.slack.com",
    "https://discord.com", "https://web.telegram.org", "https://www.instagram.com",
    "https://www.netflix.com", "https://www.spotify.com", "https://www.twitch.tv",
    "https://www.youtube.com/feed/trending", "https://www.baidu.com/s?wd=%E7%8C%AB",
]


def average_hash(image: Image.Image) -> int:
    small = image.convert("L").resize((8, 8), Image.BILINEAR)
    pixels = small.tobytes()
    average = sum(pixels) / 64.0
    bits = 0
    for value in pixels:
        bits = (bits << 1) | (1 if value >= average else 0)
    return bits


class Saver:
    """Decodes candidates, skips junk and near-duplicates, writes JPEGs."""

    def __init__(self, out: Path) -> None:
        self.out = out
        self.seen: set[int] = set()
        self.rows: list[dict] = []
        self.skipped = {"small": 0, "dupe": 0, "broken": 0}

    def save(self, image: Image.Image | None, source: str, name: str) -> bool:
        if image is None:
            self.skipped["broken"] += 1
            return False
        if min(image.size) < MIN_SIDE:
            self.skipped["small"] += 1
            return False
        try:
            digest = average_hash(image)
        except Exception:  # noqa: BLE001
            self.skipped["broken"] += 1
            return False
        if digest in self.seen:
            self.skipped["dupe"] += 1
            return False
        self.seen.add(digest)
        dest = self.out / source / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        rgb = image.convert("RGB")
        if max(rgb.size) > MAX_EDGE:
            scale = MAX_EDGE / max(rgb.size)
            rgb = rgb.resize((max(1, int(rgb.width * scale)), max(1, int(rgb.height * scale))),
                             Image.LANCZOS)
        rgb.save(dest, "JPEG", quality=90, optimize=True)
        self.rows.append({
            "path": str(dest).replace("\\", "/"),
            "source": source,
            "class": CLASS_OF_SOURCE[source],
            "bytes": dest.stat().st_size,
        })
        return True


def _open_bytes(blob: bytes) -> Image.Image | None:
    try:
        from io import BytesIO

        image = Image.open(BytesIO(blob))
        image.load()
        return image
    except Exception:  # noqa: BLE001
        return None


def iter_parquet_images(path: Path) -> Iterator[Image.Image | None]:
    """Yield decoded images from a parquet whose rows carry an image column.

    The schemas differ per dataset (HF Image struct, plain binary, base64), so
    the image column is discovered by looking at the first batch instead of
    hard-coding a column name.
    """
    import pyarrow.parquet as pq  # noqa: PLC0415 - optional heavy import

    handle = pq.ParquetFile(path)
    column = None
    for batch in handle.iter_batches(batch_size=32):
        if column is None:
            names = batch.schema.names
            for row in batch.to_pylist():
                for key, value in row.items():
                    if isinstance(value, (bytes, bytearray)) or (
                        isinstance(value, dict) and "bytes" in value
                    ):
                        column = key
                        break
                if column:
                    break
            if column is None:
                raise SystemExit(f"{path}: no image column found in {names}")
            print(f"  {path.name}: image column = {column!r}", flush=True)
        for row in batch.to_pylist():
            value = row.get(column)
            if isinstance(value, dict):
                value = value.get("bytes") or value.get("path")
            if isinstance(value, (bytes, bytearray)):
                yield _open_bytes(bytes(value))
            elif isinstance(value, str) and value.startswith("data:"):
                yield _open_bytes(__import__("base64").b64decode(value.split(",", 1)[1]))
            else:
                yield None


def take_parquet(saver: Saver, source: str, files: Iterable[Path], target: int) -> int:
    """Spread the sample across the whole file instead of taking the first rows.

    A parquet is not shuffled: MET-Meme keeps its rows grouped by source domain, so
    "first 700" would be one narrow slice of the meme world.
    """
    import pyarrow.parquet as pq  # noqa: PLC0415 - optional heavy import

    produced = 0
    for path in files:
        if produced >= target:
            break
        if not path.exists():
            print(f"  ! missing {path}", flush=True)
            continue
        total = pq.ParquetFile(path).metadata.num_rows
        want = target - produced
        stride = max(1, total // max(1, want))
        for index, image in enumerate(iter_parquet_images(path)):
            if produced >= target:
                break
            if index % stride:
                continue
            if saver.save(image, source, f"{source}-{produced + 1:05d}.jpg"):
                produced += 1
        print(f"  {source}: {produced}/{target} kept from {path.name} "
              f"(every {stride} row of {total}; skipped {saver.skipped})", flush=True)
    return produced


def download_parallel(url: str, dest: Path, *, workers: int = 8,
                      shard: int = 4 << 20) -> bool:
    """Fetch one large file in parallel byte ranges.

    The HuggingFace CDN gives about 180 kB/s per connection and supports ``Range``,
    so a single-stream 400 MB shard takes half an hour while eight ranges take a few
    minutes.  Writes go straight to their offsets; a partial file is discarded.
    """
    import requests  # noqa: PLC0415 - only the networked sources need HTTP
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    headers = {"User-Agent": "albumcat-dataset/0.6"}
    try:
        head = requests.head(url, headers=headers, timeout=30, allow_redirects=True)
        head.raise_for_status()
        total = int(head.headers.get("content-length") or 0)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! {dest.name}: HEAD failed {type(exc).__name__}: {exc}", flush=True)
        return False
    if total <= 0:
        print(f"  ! {dest.name}: no content-length", flush=True)
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.touch()
    with dest.open("r+b") as handle:
        handle.truncate(total)
    done = 0

    def grab(start: int) -> int:
        nonlocal done
        end = min(start + shard, total) - 1
        for _ in range(3):
            try:
                response = requests.get(
                    url, headers={**headers, "Range": f"bytes={start}-{end}"}, timeout=90)
                if response.status_code in (200, 206):
                    blob = response.content
                    with dest.open("r+b") as handle:
                        handle.seek(start)
                        handle.write(blob)
                    done += len(blob)
                    if done % (32 << 20) < shard:
                        print(f"    {dest.name}: {done / 1e6:.0f}/{total / 1e6:.0f} MB",
                              flush=True)
                    return len(blob)
            except Exception:  # noqa: BLE001 - retried
                time.sleep(1)
        return 0

    starts = list(range(0, total, shard))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        written = sum(pool.map(grab, starts))
    if written < total * 0.99:
        print(f"  ! {dest.name}: incomplete {written}/{total}", flush=True)
        dest.unlink(missing_ok=True)
        return False
    print(f"  {dest.name}: {total / 1e6:.0f} MB -> {dest}", flush=True)
    return True


def take_hf_parquet(saver: Saver, source: str, url: str, target: int) -> int:
    """Photos from a HuggingFace dataset's parquet shard, downloaded once.

    ``images.cocodataset.org`` was unreachable while this was written, but the same
    COCO images are on huggingface.co as a parquet shard, and so are aligned face
    crops (CelebA).  One shard is 400-500 MB, so it is cached under ``RAW`` and
    reused; the sample is spread over the shard by :func:`take_parquet`.
    """
    name = url.rstrip("/").split("/")[-3] if "/parquet/" in url else "shard.parquet"
    if "/parquet/" in url:
        parts = url.split("/")
        tail = [part for part in parts[parts.index("parquet") + 1:] if part]
        name = "-".join(tail[:2]) + "-" + parts[-1]
    dest = RAW / source / f"{name}"

    if not dest.exists() or dest.stat().st_size < 1024:
        print(f"  {source}: downloading {url.split('/datasets/')[-1]}", flush=True)
        if not download_parallel(url, dest):
            return 0
    return take_parquet(saver, source, [dest], target)


def take_coco(saver: Saver, target: int) -> int:
    archive = RAW / "coco/val2017.zip"
    if not archive.exists():
        print(f"  ! missing {archive}", flush=True)
        return 0
    stride = 5  # val2017 has 5,000 images; every 5th is plenty
    kept = 0
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(archive) as zf:
            names = sorted(n for n in zf.namelist() if n.lower().endswith(".jpg"))
            picks = names[::stride]
            print(f"  coco: {len(names)} image(s), taking {len(picks)} candidates", flush=True)
            for i, name in enumerate(picks, 1):
                if kept >= target:
                    break
                dest = Path(tmp) / Path(name).name
                with zf.open(name) as src, dest.open("wb") as fh:
                    shutil.copyfileobj(src, fh)
                if saver.save(_open_bytes(dest.read_bytes()), "coco", f"coco-{kept + 1:05d}.jpg"):
                    kept += 1
                dest.unlink(missing_ok=True)
                if i % 200 == 0:
                    print(f"    coco {i}/{len(picks)} scanned, {kept} kept", flush=True)
    return kept


def take_bqb(saver: Saver, target: int) -> int:
    root = RAW / "chinesebqb"
    if not root.is_dir():
        print(f"  ! missing {root}", flush=True)
        return 0
    index: list[dict] = []
    index_file = root / "index.jsonl"
    if index_file.exists():
        index = [json.loads(line) for line in index_file.read_text(encoding="utf-8").splitlines() if line]
    else:
        index = [{"file": p.name, "dir": p.parent.name} for p in sorted(root.glob("*")) if p.is_file()]
    by_dir: dict[str, list[dict]] = {}
    for row in index:
        by_dir.setdefault(row["dir"], []).append(row)
    # Round-robin over packs so no single sticker series dominates the pool.
    order: list[dict] = []
    round_no = 0
    while len(order) < len(index):
        for name in sorted(by_dir):
            rows = by_dir[name]
            if round_no < len(rows):
                order.append(rows[round_no])
        round_no += 1
        if round_no > 200:
            break
    kept = 0
    for row in order:
        if kept >= target:
            break
        src = root / row["file"]
        if not src.exists() or src.stat().st_size < MIN_BYTES:
            continue
        if saver.save(_open_bytes(src.read_bytes()), "chinesebqb", f"bqb-{kept + 1:05d}.jpg"):
            kept += 1
    return kept


def find_edge() -> str | None:
    for candidate in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ):
        if Path(candidate).exists():
            return candidate
    return None


def take_desktop(saver: Saver, target: int) -> int:
    edge = find_edge()
    if not edge:
        print("  ! Edge not found; skipping desktop screenshots", flush=True)
        return 0
    kept = 0
    cache = Path(tempfile.mkdtemp(prefix="albumcat-ui-"))
    try:
        for index, url in enumerate(SITES):
            if kept >= target:
                break
            mobile = index % 3 == 0
            size = "390,844" if mobile else "1280,800"
            shot = cache / f"shot-{index:03d}.png"
            profile = cache / f"profile-{index:03d}"
            cmd = [edge, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                   "--no-first-run", "--disable-extensions", "--disable-background-networking",
                   f"--user-data-dir={profile}", f"--window-size={size}",
                   f"--screenshot={shot}", url]
            try:
                subprocess.run(cmd, capture_output=True, timeout=45, check=False)
            except subprocess.TimeoutExpired:
                continue
            if not shot.exists() or shot.stat().st_size < MIN_BYTES:
                continue
            if saver.save(_open_bytes(shot.read_bytes()), "desktop",
                          f"desktop-{kept + 1:05d}.jpg"):
                kept += 1
                print(f"    desktop {kept}/{target} {url}", flush=True)
            shot.unlink(missing_ok=True)
            shutil.rmtree(profile, ignore_errors=True)
    finally:
        shutil.rmtree(cache, ignore_errors=True)
    return kept


def take_picsum(saver: Saver, target: int) -> int:
    """Random real photos from picsum.photos (Unsplash-sourced).

    Cheap breadth for the photo class: COCO gives people and street scenes, this
    adds whatever else Unsplash happens to hold.  Seeds are fixed, so a rebuild
    fetches the same set.
    """
    import requests  # noqa: PLC0415 - only this source needs HTTP
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    headers = {"User-Agent": "albumcat-dataset/0.6 (https://github.com/bilebee/wd14-API-onnx)"}
    kept = len(list((saver.out / "picsum").glob("*.jpg"))) if (saver.out / "picsum").exists() else 0
    if kept >= target:
        print(f"  picsum: already {kept} file(s), nothing to do", flush=True)
        return kept

    def one(index: int) -> tuple[str, bytes] | None:
        name = f"picsum-{index:05d}.jpg"
        if (saver.out / "picsum" / name).exists():
            return None
        url = f"https://picsum.photos/seed/albumcat-{index:04d}/1200/800"
        for _ in range(2):
            try:
                response = requests.get(url, timeout=30, headers=headers)
                if response.status_code == 200 and len(response.content) >= MIN_BYTES:
                    return name, response.content
            except Exception:  # noqa: BLE001 - a flaky seed is not fatal
                continue
        return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        for result in pool.map(one, range(1, target * 4)):
            if result is None:
                continue
            if saver.save(_open_bytes(result[1]), "picsum", result[0]):
                kept += 1
                if kept % 50 == 0:
                    print(f"    picsum {kept}/{target}", flush=True)
            if kept >= target:
                break
    return kept


def take_commons(saver: Saver, target: int) -> int:
    """Real photos from Wikimedia Commons, spread over a few useful categories.

    COCO's download host was unreachable while this was written (it stalled at
    0 MB), and Commons covers the same ground: people, streets, interiors, food,
    pets.  Category members are mostly photos but not all files are -- anything
    whose name smells like a diagram, poster or map is dropped.

    Downloads run in a thread pool (Commons throttles single sequential clients to
    about one image every 1.5 s); the Saver still sees them one at a time so the
    duplicate filter stays reliable.
    """
    import requests  # noqa: PLC0415 - only this source needs HTTP
    from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: PLC0415

    headers = {"User-Agent": "albumcat-dataset/0.6 (https://github.com/bilebee/wd14-API-onnx)"}
    skip_words = ("map", "diagram", "logo", "poster", "chart", "graph", "seal",
                  "coat_of_arms", "flag", "scan", "drawing", "engraving", "painting",
                  "illustration", "cartoon", "stamp", "coin", "screenshot")
    per_category = max(1, target // len(COMMONS_CATEGORIES))

    def candidates(category: str, want: int) -> list[str]:
        """Thumb URLs for one category, in listing order."""
        urls: list[str] = []
        offset = None
        while len(urls) < want:
            params = {
                "action": "query", "format": "json", "generator": "categorymembers",
                "gcmtitle": category, "gcmtype": "file", "gcmlimit": "50",
                "prop": "imageinfo", "iiprop": "url|size|mime", "iiurlwidth": "1400",
            }
            if offset:
                params["gcmcontinue"] = offset
            try:
                payload = requests.get("https://commons.wikimedia.org/w/api.php",
                                       params=params, timeout=40, headers=headers).json()
            except Exception as exc:  # noqa: BLE001
                print(f"    commons {category}: {type(exc).__name__}", flush=True)
                break
            offset = payload.get("continue", {}).get("gcmcontinue")
            pages = payload.get("query", {}).get("pages", {})
            if not pages:
                break
            for page in pages.values():
                title = page.get("title", "").lower()
                info = (page.get("imageinfo") or [{}])[0]
                if any(word in title for word in skip_words):
                    continue
                if info.get("mime") not in ("image/jpeg", "image/png"):
                    continue
                url = info.get("thumburl") or info.get("url")
                if url:
                    urls.append(url)
            if not offset:
                break
        return urls

    kept = 0

    def download(url: str) -> bytes | None:
        """Fetch one thumb, retrying: Commons resets the connection when pushed."""
        for attempt in range(2):
            try:
                response = requests.get(url, timeout=20, headers=headers)
                if response.status_code == 200:
                    return response.content
            except Exception:  # noqa: BLE001 - a throttled thumb is not fatal
                time.sleep(0.5 + attempt)
        return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        for category in COMMONS_CATEGORIES:
            if kept >= target:
                break
            want = min(per_category, target - kept)
            urls = candidates(category, want * 3)
            if not urls:
                print(f"  commons {category}: nothing usable", flush=True)
                continue
            # Save the moment a blob lands: waiting for the whole batch means one
            # hanging thumb holds back every good image behind it.
            got = 0
            futures = [pool.submit(download, url) for url in urls]
            for future in as_completed(futures):
                if got >= want or kept >= target:
                    for pending in futures:
                        pending.cancel()
                    break
                blob = future.result()
                if blob is None:
                    continue
                if saver.save(_open_bytes(blob), "commons", f"commons-{kept + 1:05d}.jpg"):
                    kept += 1
                    got += 1
                    if kept % 25 == 0:
                        print(f"    commons {kept}/{target}", flush=True)
            print(f"  commons {category}: +{got} ({kept}/{target} total)", flush=True)
    return kept


def write_manifest(rows: list[dict]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = OUT / "manifest.jsonl"
    merged: dict[str, dict] = {}
    if manifest.exists():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                merged[Path(row["path"]).as_posix()] = {**row,
                                                        "path": Path(row["path"]).as_posix()}
    for row in rows:
        key = Path(row["path"]).as_posix()
        merged[key] = {**row, "path": key}
    ordered = [merged[key] for key in sorted(merged)]
    manifest.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in ordered) + "\n",
        encoding="utf-8",
    )
    counts: dict[str, int] = {}
    for row in ordered:
        counts[row["source"]] = counts.get(row["source"], 0) + 1
    print(f"\n  manifest: {len(ordered)} image(s) -> {manifest}", flush=True)
    for source in sorted(counts):
        print(f"    {source:<12} {counts[source]:5d}  ({CLASS_OF_SOURCE[source]})", flush=True)


def rescan_rows() -> list[dict]:
    """Rebuild manifest rows from the files already on disk.

    Two downloader runs finishing at the same time can each write the manifest and
    the later write wins, so a crashed or interrupted run leaves files that no
    manifest knows about.  The folder layout holds the answer, so it can be
    re-derived instead of re-downloading.
    """
    rows: list[dict] = []
    for folder in sorted(OUT.iterdir() if OUT.exists() else []):
        if not folder.is_dir():
            continue
        source = folder.name
        if source not in CLASS_OF_SOURCE:
            print(f"  ! unknown pool folder {source}, ignored", flush=True)
            continue
        for path in sorted(folder.glob("*.jpg")):
            rows.append({"path": path.as_posix(), "source": source,
                         "class": CLASS_OF_SOURCE[source], "bytes": path.stat().st_size})
    return rows


def main(argv: list[str] | None = None) -> int:
    global RAW, OUT  # noqa: PLW0603 - two paths, set once from the CLI
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("which", nargs="*", default=[],
                        help="subset of: parquet coco picsum bqb desktop (default: all)")
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--raw", type=Path, default=RAW)
    args = parser.parse_args(argv)

    RAW, OUT = args.raw, args.out
    which = set(args.which) or {"parquet", "hf", "commons", "picsum", "bqb", "desktop"}

    saver = Saver(OUT)
    if "rescan" in which:
        # no downloads: re-derive the manifest from whatever is already on disk
        write_manifest(rescan_rows())
        return 0
    if "parquet" in which:
        take_parquet(saver, "met-meme",
                     [RAW / "met-meme/train.parquet", RAW / "met-meme/val.parquet"],
                     TARGETS["met-meme"])
        take_parquet(saver, "landscape", [RAW / "landscape/train.parquet"], TARGETS["landscape"])
        take_parquet(saver, "rico", [RAW / "rico/test.parquet"], TARGETS["rico"])
    if "coco" in which:
        take_coco(saver, TARGETS["coco"])
    if "hf" in which:
        for source, url in HF_SHARDS.items():
            take_hf_parquet(saver, source, url, TARGETS.get(source, 600))
    if "commons" in which:
        take_commons(saver, TARGETS["commons"])
    if "picsum" in which:
        take_picsum(saver, TARGETS["picsum"])
    if "bqb" in which:
        take_bqb(saver, TARGETS["chinesebqb"])
    if "desktop" in which:
        take_desktop(saver, TARGETS["desktop"])

    write_manifest(saver.rows + rescan_rows())
    print(f"  skipped: {saver.skipped}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
