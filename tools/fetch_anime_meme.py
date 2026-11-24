#!/usr/bin/env python3
"""抓二次元梗图：用「图上有字」的标签，而不是 safebooru 的 ``meme``。

为什么另写一个：``fetch_meme_dataset.py`` 用的是 ``meme`` 标签，而 safebooru 上
这个标签以欧美照片模板为主，二次元梗图（动漫人物 + 字幕/对话框）几乎抓不到。
于是四类头学到的「梗图」和用户相册里的梗图完全不是一回事 —— 这是插画精确率
卡在 0.695 的根。

这里换三个标签，都是「画面里出现文字」的意思：

    chinese_text    中文文本（汉化组、中文梗）
    english_text    英文文本（英文梗、翻译漫画）
    speech_bubble   对话框（漫画分格、对白）

产出的文件名带标签来源，方便事后按来源抽查。

用法::

    python tools/fetch_anime_meme.py --out data/eval/梗图 --count 120
"""

from __future__ import annotations

import argparse
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

API = "https://safebooru.org/index.php?page=dapi&s=post&q=index"
UA = "albumcat-fetch/1.0 (dataset building for a local classifier)"

# 三个标签都表示「图上有字」。加 !meme 排除欧美梗图模板。
# 注意：不要写 "!meme" 这种排除语法 —— safebooru 的 dapi 不认，会把结果清成 0。
# 实测三个标签的总量：chinese_text 2.1 万、english_text 14.8 万、speech_bubble 12.7 万。
TAGS = [
    "chinese_text",
    "english_text",
    "speech_bubble",
]


def safebooru_posts(tags: str, page: int, limit: int = 100) -> list[dict]:
    url = f"{API}&limit={limit}&pid={page}&tags={urllib.parse.quote(tags)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            blob = r.read()
    except Exception as exc:  # noqa: BLE001
        print(f"  请求失败 {tags!r} p{page}: {type(exc).__name__}")
        return []
    if not blob.strip():
        return []
    try:
        root = ET.fromstring(blob)
    except ET.ParseError:
        print(f"  XML 解析失败 {tags!r} p{page}")
        return []
    out = []
    for p in root.findall("post"):
        # sample_url 是 850px 版本，原图常有 3-10 MB
        url = p.get("sample_url") or p.get("file_url") or ""
        if url.startswith("//"):
            url = "https:" + url
        if url:
            out.append({"url": url, "tags": p.get("tags", ""), "id": p.get("id", "")})
    return out


def download(item: dict, dest: Path) -> tuple[bool, int]:
    if dest.exists() and dest.stat().st_size > 2048:
        return True, dest.stat().st_size
    req = urllib.request.Request(item["url"], headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            blob = r.read()
        if len(blob) < 2048:
            return False, 0
        dest.write_bytes(blob)
        return True, len(blob)
    except Exception:  # noqa: BLE001
        return False, 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/eval/梗图")
    ap.add_argument("--count", type=int, default=120)
    ap.add_argument("--pages", type=int, default=4, help="每个标签翻几页（每页 100 条）")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    pool_items: list[dict] = []
    for tags in TAGS:
        got = 0
        for page in range(args.pages):
            posts = safebooru_posts(tags, page)
            print(f"  {tags!r} p{page}: {len(posts)} 条")
            if not posts:
                break
            for p in posts:
                p["source_tag"] = tags.split()[0]
                pool_items.append(p)
                got += 1
            if len(pool_items) >= args.count * 3:
                break

    if not pool_items:
        print("一条都没拿到，检查网络或标签是否还有效")
        return 1

    # 打散后取前 N，避免全是同一个标签
    import random
    random.seed(0)
    random.shuffle(pool_items)
    picks = pool_items[: args.count]

    print(f"\n下载 {len(picks)} 张 → {out}（{args.workers} 线程）")
    width = max(4, len(str(len(picks))))
    tasks = []
    for i, item in enumerate(picks, 1):
        name = f"meme-{item['source_tag']}-{i:0{width}d}.jpg"
        tasks.append((item, out / name))

    ok = fail = 0
    total = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(download, it, dest): dest for it, dest in tasks}
        for done, fut in enumerate(as_completed(futures), 1):
            success, size = fut.result()
            if success:
                ok += 1
                total += size
            else:
                fail += 1
            if done % 20 == 0 or done == len(tasks):
                print(f"  进度 {done}/{len(tasks)}  成功 {ok}  失败 {fail}  {total / 1048576:.0f} MB")

    print(f"\n完成：{ok} 张，{total / 1048576:.1f} MB → {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
