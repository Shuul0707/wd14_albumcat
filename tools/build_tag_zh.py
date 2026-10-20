"""Build ``src/albumcat/data/tag_zh.json`` -- the Chinese name table for WD14 tags.

Two inputs, one output:

* ``models/vit/selected_tags.csv`` -- which tags exist at all (9083) and their
  category (character / general / rating);
* ``.tmp/ffdkj_tags.sqlite`` -- the Chinese translations, from
  https://github.com/ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table
  (MIT, ``tags(name, category, cn_name, post_count)``, ~334k rows, covers 95%
  of WD14);
* ``src/albumcat/data/tag_zh_curated.json`` -- hand-written aliases layered on
  top (short forms, Japanese names, romaji, synonyms) plus Chinese for the tags
  the dataset simply does not have.

The output is committed, so users never need the 24 MB sqlite. Rerun this only
when the upstream dataset changes::

    python tools/build_tag_zh.py            # write the table
    python tools/build_tag_zh.py --check    # fail if it is stale
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import subprocess
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELECTED = ROOT / "models" / "vit" / "selected_tags.csv"
SQLITE = ROOT / ".tmp" / "ffdkj_tags.sqlite"
CURATED = ROOT / "src" / "albumcat" / "data" / "tag_zh_curated.json"
OUT = ROOT / "src" / "albumcat" / "data" / "tag_zh.json"

SOURCE = (
    "ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table (MIT) "
    "+ albumcat curated aliases"
)
DOWNLOAD_URLS = (
    "https://raw.githubusercontent.com/ffdkj/"
    "ffdkj-Danbooru_Tag-Chinese-English-Translation-Table/main/tag.sqlite",
    "https://github.com/ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table"
    "/raw/main/tag.sqlite",
)

#: category column -> short code in the output (keeps the JSON small)
KIND = {4: "c", 0: "g", 9: "r"}

#: Parentheticals that are not a source work. WD14 uses them as qualifiers
#: ("kirisawa_juuzou_(character)"), and a series named "角色" would make every
#: query containing the word 角色 look like a series request.
_NOT_A_SERIES = {
    "角色", "人物", "原创", "原創", "未知", "其他", "其它", "无", "無", "不明",
    "游戏", "作品", "单位", "部隊", "部队",
    "character", "original", "unknown", "other", "none", "unit",
}

_PAREN = re.compile(r"[（(]([^（()）]*)[)）]")


# ---------------------------------------------------------------------- #
# normalization (must match src/albumcat/tagdict.py)
# ---------------------------------------------------------------------- #
def squeeze(text: str) -> str:
    """Lowercase, NFKC, and drop everything that is not a letter or digit.

    Chinese names, English labels and underscore keys all collapse to the same
    shape ("Long Hair" / "long_hair" / "longhair" -> ``longhair``), which is
    what makes one flat index enough.
    """
    out: list[str] = []
    for ch in unicodedata.normalize("NFKC", text).lower():
        if ch.isalnum():
            out.append(ch)
    return "".join(out)


def pretty_name(cn: str) -> tuple[str, str]:
    """``初音未来（VOCALOID）`` -> ``("初音未来", "VOCALOID")``.

    The last parenthetical of a Chinese tag name is the series ("（Fate）",
    "（舰队Collection）"); the ones before it are outfit/ascension variants and
    are simply dropped from the display name.
    """
    groups = _PAREN.findall(cn)
    name = _PAREN.sub("", cn).strip()
    series = groups[-1].strip() if groups else ""
    # A group that is only digits/punct is not a series.
    if series and not re.search(r"[A-Za-z\u4e00-\u9fff]", series):
        series = ""
    return name or cn.strip(), series


# ---------------------------------------------------------------------- #
# inputs
# ---------------------------------------------------------------------- #
def read_wd14() -> list[tuple[str, int]]:
    with SELECTED.open(encoding="utf-8", newline="") as fp:
        rows = list(csv.DictReader(fp))
    out = [(r["name"], int(r["category"])) for r in rows if r.get("name")]
    if not out:
        raise SystemExit(f"no tags in {SELECTED}")
    return out


def read_translations() -> dict[str, tuple[str, int]]:
    if not SQLITE.exists():
        fetch_sqlite()
    con = sqlite3.connect(str(SQLITE))
    try:
        rows = con.execute("select name, cn_name, post_count from tags").fetchall()
    finally:
        con.close()
    table: dict[str, tuple[str, int]] = {}
    for name, cn, count in rows:
        if not name:
            continue
        table[name] = ((cn or "").strip(), int(count or 0))
    if len(table) < 100_000:
        raise SystemExit(f"{SQLITE} looks truncated ({len(table)} rows)")
    return table


def fetch_sqlite() -> None:
    SQLITE.parent.mkdir(parents=True, exist_ok=True)
    print(f"{SQLITE} missing, downloading the source dataset ...", file=sys.stderr)
    for url in DOWNLOAD_URLS:
        cmd = ["curl.exe", "-fL", "--retry", "3", "--connect-timeout", "20",
               "-o", str(SQLITE), url]
        if subprocess.run(cmd, capture_output=True).returncode == 0 and SQLITE.exists():
            print(f"  saved {SQLITE.stat().st_size:,} bytes from {url}", file=sys.stderr)
            return
        # raw.githubusercontent is reachable only through the local proxy on
        # some machines; retry once through it before giving up.
        proxied = cmd[:1] + ["-x", "http://127.0.0.1:7891"] + cmd[1:]
        if subprocess.run(proxied, capture_output=True).returncode == 0 and SQLITE.exists():
            print(f"  saved {SQLITE.stat().st_size:,} bytes from {url} (via proxy)",
                  file=sys.stderr)
            return
        SQLITE.unlink(missing_ok=True)
    raise SystemExit(
        "could not download tag.sqlite; download it manually from\n"
        "  https://github.com/ffdkj/ffdkj-Danbooru_Tag-Chinese-English-Translation-Table\n"
        f"and save it as {SQLITE}"
    )


def read_curated() -> dict:
    with CURATED.open(encoding="utf-8") as fp:
        return json.load(fp)


# ---------------------------------------------------------------------- #
# build
# ---------------------------------------------------------------------- #
def build() -> dict:
    wd14 = read_wd14()
    known = {name: cat for name, cat in wd14}
    zh, curated = read_translations(), read_curated()

    extra = curated.get("extra_zh", {})
    alias_groups: dict[str, list[str]] = {}
    for section in ("characters", "general"):
        for key, aliases in curated.get(section, {}).items():
            alias_groups.setdefault(key, []).extend(aliases)

    tags: dict[str, dict[str, str]] = {}
    #: alias -> (index value, popularity) so a collision resolves to the busier tag
    best: dict[str, tuple[str, int]] = {}
    #: series label -> character keys carrying it ("东方Project" -> [hakurei_reimu, ...])
    series_members: dict[str, list[str]] = {}

    def offer(alias: str, value: str, popularity: int) -> None:
        squashed = squeeze(alias)
        if len(squashed) < 2:
            return
        current = best.get(squashed)
        if current is None or popularity > current[1]:
            best[squashed] = (value, popularity)

    stats = {"zh": 0, "from_dataset": 0, "from_curated": 0, "no_zh": 0}
    unknown_aliases: list[str] = []

    for key, category in wd14:
        kind = KIND[category]
        raw_zh, popularity = zh.get(key, ("", 0))
        name, series = pretty_name(raw_zh) if raw_zh else ("", "")
        if name:
            stats["from_dataset"] += 1
        else:
            fallback = extra.get(key, "")
            if fallback:
                name = fallback
                series = ""
                stats["from_curated"] += 1
        if not name:
            stats["no_zh"] += 1

        entry: dict[str, str] = {"k": kind}
        if name:
            entry["n"] = name
            stats["zh"] += 1
        if series:
            entry["s"] = series
        aliases = alias_groups.get(key, [])
        if aliases:
            entry["a"] = list(aliases)
        tags[key] = entry

        # Every spelling we are willing to match on, most specific first so a
        # collision (蕾米 for both Remilia and Rem) resolves to the busier tag.
        if name:
            offer(name, key, popularity)
        for alias in aliases:
            offer(alias, key, popularity)
        offer(key.replace("_", " "), key, popularity)
        if series and kind == "c" and series not in _NOT_A_SERIES:
            series_members.setdefault(series, []).append(key)

    # A series name ("东方Project") is not a tag of its own in WD14 -- it rides
    # along in the character tag's parenthetical. Offer it as a *series* match
    # that expands to its cast, instead of mapping it to one arbitrary member.
    series_aliases: dict[str, list[str]] = curated.get("series", {})
    for series_name in list(series_members):
        members = series_members[series_name]
        popularity = 1000 + len(members)  # beats a same-named character tag
        offer(series_name, "#" + series_name, popularity)
        for alias in series_aliases.get(series_name, []):
            offer(alias, "#" + series_name, popularity)
    for series_name in series_aliases:
        if series_name not in series_members:
            unknown_aliases.append(series_name)

    for key in list(alias_groups):
        if key not in known:
            unknown_aliases.append(key)

    index = {alias: value for alias, (value, _) in best.items()}
    series_table = {
        squeeze(label): {"n": label, "k": sorted(keys)}
        for label, keys in sorted(series_members.items())
        if len(squeeze(label)) >= 2
    }
    out = {
        "version": 1,
        "source": SOURCE,
        "counts": {
            "tags": len(tags),
            "index": len(index),
            "series": len(series_table),
            **stats,
        },
        "tags": tags,
        "series": series_table,
        "index": index,
    }
    if unknown_aliases:
        out["counts"]["unknown_aliases"] = len(unknown_aliases)
    return out


def dump(data: dict) -> str:
    # Compact: ~1 MB of JSON, read once at startup. Sorted keys keep the diff
    # stable when the dataset is refreshed.
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if the committed table differs from a fresh build")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    data = build()
    counts = data["counts"]
    payload = dump(data)
    print(f"tags {counts['tags']:,}  index {counts['index']:,}  "
          f"with zh {counts['zh']:,} ({counts['zh'] / counts['tags']:.1%})  "
          f"no zh {counts['no_zh']:,}  payload {len(payload.encode('utf-8')):,} B")

    out = build_index_report(data) if args.verbose else []
    for line in out:
        print(line)

    if args.check:
        if not OUT.exists():
            print(f"[FAIL] {OUT} is missing")
            return 1
        if OUT.read_text(encoding="utf-8") != payload:
            print(f"[FAIL] {OUT} is stale, rerun python tools/build_tag_zh.py")
            return 1
        print("[ OK ] tag_zh.json is up to date")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(payload, encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size:,} B)")
    return 0


def build_index_report(data: dict) -> list[str]:
    """A few sample lookups, so a human can eyeball the table in one screen."""
    lines = ["", "sample index:"]
    for alias in ("初音未来", "初音", "hatsunemiku", "灵梦", "黑暗", "longhair",
                  "1girl", "general", "explicit"):
        key = data["index"].get(squeeze(alias))
        if key is None:
            lines.append(f"  {alias:14s} -> (no match)")
            continue
        entry = data["tags"][key]
        zh = entry.get("n", "-")
        lines.append(f"  {alias:14s} -> {key:32s} {zh}  (kind={entry['k']})")
    return lines


if __name__ == "__main__":
    sys.exit(main())
