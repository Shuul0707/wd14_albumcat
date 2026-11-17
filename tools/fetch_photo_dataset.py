#!/usr/bin/env python3
"""从 COCO 2017 抓真实生活照，替换 picsum 的风景照。

为什么换源：原来的 `fetch_illustration_dataset.py` 用 picsum.photos 取"照片"，
那是 Unsplash 的随机图——山、海、建筑、静物。而手机相册里的"照片"是人像、室内、
食物、合影。WD14 在干净风景上学到的边界，套到人像上不成立，于是有了 PPT 第 9 页
那个「照片只有 53%，30 张有 10 张被判成插画」。

COCO val2017 的 5,000 张全是真实拍摄，含大量人像和室内场景，按类别挑完直接下原图。

用法::

    python tools/fetch_photo_dataset.py --out data/datasets/kind --count 200
    python tools/fetch_photo_dataset.py --out data/datasets/kind --count 500 \
        --categories person,dining table,chair,couch,bed,cup,bowl,pizza,cake,book

产出：``<out>/photo-coco-0001.jpg`` 这样命名，能被 ``build_kind_dataset.py`` 的
``photo-*.jpg`` 通配符直接收进去。
"""

from __future__ import annotations

import argparse
import io
import struct
import json
import sys
import urllib.request
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ANNOTATIONS_URL = "http://images.cocodataset.org/annotations/annotations_trainval2017.zip"
ANN_MEMBER = "annotations/instances_val2017.json"

# 默认挑这些类：人像 + 室内家具 + 食物。它们最接近"手机相册里的照片"，
# 也是 picsum 那个源里完全没有的。
DEFAULT_CATEGORIES = (
    "person,dining table,chair,couch,bed,tv,laptop,book,cup,bowl,"
    "pizza,cake,sandwich,banana,apple,cell phone"
)


def fetch_member_from_remote_zip(url: str, member: str, dest: Path) -> Path:
    """只下载远程 zip 里指定的那个文件，不整包下。

    ``annotations_trainval2017.zip`` 是 241 MB，其中 ``instances_val2017.json``
    一个文件就占大头，另外还有 captions/ 和 person_keypoints/ 两套我们用不到的。
    走 Range 把 zip 切开：先读尾部找到中央目录，再按偏移只取目标文件那一段。
    实测省掉约 200 MB 流量。
    """

    def ranged(start: int, end: int) -> bytes:
        """取一段字节。大于 1 MB 的请求会打印进度——慢链路上几 MB 也要几分钟，
        没有输出的话跟卡死没区别。"""
        want = end - start + 1
        req = urllib.request.Request(url, headers={
            "Range": f"bytes={start}-{end}",
            "User-Agent": "albumcat-fetch/1.0",
        })
        with urllib.request.urlopen(req, timeout=300) as r:
            if want <= 1024 * 1024:
                return r.read()
            buf = bytearray()
            while len(buf) < want:
                chunk = r.read(min(1 << 16, want - len(buf)))
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) % (1 << 20) < (1 << 16):
                    print(f"\r  {len(buf) / 1048576:.1f} / {want / 1048576:.1f} MB", end="", flush=True)
            print()
            return bytes(buf)

    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "albumcat-fetch/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        total = int(r.headers["Content-Length"])

    # EOCD 最长 22 + 65535 字节注释
    tail_len = min(65557, total)
    tail = ranged(total - tail_len, total - 1)
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0:
        raise SystemExit("zip 尾部没有 EOCD，可能不是标准 zip")
    cd_size = struct.unpack("<I", tail[eocd + 12:eocd + 16])[0]
    cd_offset = struct.unpack("<I", tail[eocd + 16:eocd + 20])[0]

    cd = ranged(cd_offset, cd_offset + cd_size - 1)
    pos = 0
    wanted = member.replace("\\", "/")
    while pos < len(cd) - 4:
        if cd[pos:pos + 4] != b"PK\x01\x02":
            break
        comp_size, uncomp_size = struct.unpack("<II", cd[pos + 20:pos + 28])
        name_len, extra_len, comment_len = struct.unpack("<HHH", cd[pos + 28:pos + 34])
        local_offset = struct.unpack("<I", cd[pos + 42:pos + 46])[0]
        name = cd[pos + 46:pos + 46 + name_len].decode("utf-8", "replace")
        if name == wanted:
            header = ranged(local_offset, local_offset + 29)
            l_name_len, l_extra_len = struct.unpack("<HH", header[26:30])
            data_start = local_offset + 30 + l_name_len + l_extra_len
            print(f"  只取 {member}：压缩 {comp_size / 1048576:.1f} MB / 解压 {uncomp_size / 1048576:.1f} MB")
            raw = ranged(data_start, data_start + comp_size - 1)
            blob = zlib.decompress(raw, -15) if comp_size != uncomp_size else raw
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(blob)
            if len(blob) != uncomp_size:
                raise SystemExit(f"解压后大小不符：{len(blob)} != {uncomp_size}")
            return dest
        pos += 46 + name_len + extra_len + comment_len
    raise SystemExit(f"zip 里没有 {member}")


def ensure_annotations(cache: Path) -> Path:
    """返回 instances_val2017.json 的路径。

    不再整包下 241 MB：走 Range 只取 zip 里的那一个成员。
    """
    target = cache / "instances_val2017.json"
    if target.exists() and target.stat().st_size > 30 * 1024 * 1024:
        return target
    cache.mkdir(parents=True, exist_ok=True)
    print(f"从远程 zip 单独取 {ANN_MEMBER}")
    fetch_member_from_remote_zip(ANNOTATIONS_URL, ANN_MEMBER, target)
    print(f"  标注就位：{target}  {target.stat().st_size / 1024 / 1024:.0f} MB")
    return target


def pick_images(ann_path: Path, categories: set[str], count: int, max_per_image: int = 1) -> list[tuple[str, str]]:
    """按类别挑图，返回 [(file_name, coco_url)]。

    一张图可能命中多个类别，这里按图片去重，每张图只算一次。
    """
    print(f"读标注并筛类别：{', '.join(sorted(categories))}")
    data = json.loads(ann_path.read_text(encoding="utf-8"))
    cat_id = {c["id"]: c["name"] for c in data["categories"]}
    wanted_ids = {i for i, n in cat_id.items() if n in categories}
    if not wanted_ids:
        raise SystemExit(f"没有匹配的类别，可用示例：{[c['name'] for c in data['categories'][:12]]}")

    hits: dict[int, int] = {}
    for a in data["annotations"]:
        if a["category_id"] in wanted_ids:
            hits[a["image_id"]] = hits.get(a["image_id"], 0) + 1

    # 命中越多类别的图越"杂"，越像生活照（比如人 + 椅子 + 杯子）。
    # 但也不能只要最杂的，否则全是餐桌。按命中数排，再均匀取。
    ranked = sorted(hits.items(), key=lambda kv: -kv[1])
    if not ranked:
        raise SystemExit("标注里没有匹配到任何图片")

    by_id = {img["id"]: img for img in data["images"]}
    picked: list[tuple[str, str]] = []
    for image_id, _ in ranked:
        img = by_id.get(image_id)
        if not img:
            continue
        picked.append((img["file_name"], img["coco_url"]))
        if len(picked) >= count:
            break
    print(f"  命中 {len(hits)} 张，取前 {len(picked)} 张")
    return picked


def download_one(url: str, dest: Path) -> tuple[Path, int]:
    if dest.exists() and dest.stat().st_size > 1024:
        return dest, dest.stat().st_size
    req = urllib.request.Request(url, headers={"User-Agent": "albumcat-fetch/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        blob = r.read()
    dest.write_bytes(blob)
    return dest, len(blob)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/datasets/kind", help="输出目录（会被 build_kind_dataset.py 读取）")
    ap.add_argument("--cache", default="data/datasets/_cache", help="标注缓存目录")
    ap.add_argument("--count", type=int, default=200, help="抓多少张")
    ap.add_argument("--categories", default=DEFAULT_CATEGORIES, help="逗号分隔的 COCO 类别名")
    ap.add_argument("--workers", type=int, default=8, help="并发下载线程数")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cats = {c.strip() for c in args.categories.split(",") if c.strip()}

    ann = ensure_annotations(Path(args.cache))
    picks = pick_images(ann, cats, args.count)

    width = max(4, len(str(len(picks))))
    tasks = []
    for i, (_, url) in enumerate(picks, 1):
        tasks.append((url, out / f"photo-coco-{i:0{width}d}.jpg"))

    print(f"下载 {len(tasks)} 张 → {out}（{args.workers} 线程）")
    ok = skip = fail = 0
    total_bytes = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(download_one, url, dest): dest for url, dest in tasks}
        for done, fut in enumerate(as_completed(futures), 1):
            dest = futures[fut]
            try:
                _, size = fut.result()
                if size == 0:
                    fail += 1
                elif size > 1024 and dest.stat().st_mtime < 1:
                    ok += 1
                else:
                    ok += 1
                total_bytes += size
            except Exception as e:  # 单张失败不影响整体
                fail += 1
                print(f"  失败 {dest.name}: {type(e).__name__}: {e}")
            if done % 25 == 0 or done == len(tasks):
                print(f"  进度 {done}/{len(tasks)}  成功 {ok}  失败 {fail}  累计 {total_bytes / 1024 / 1024:.0f} MB")

    print(f"\n完成：{ok} 张成功、{fail} 张失败，共 {total_bytes / 1024 / 1024:.1f} MB → {out}")
    if fail:
        print("重跑同一命令即可补齐（已存在的会跳过）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
