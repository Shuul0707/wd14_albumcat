"""Ground truth for the web page: which class each sample file really is.

The page is handed a flat folder (``all/``), so a file name says nothing about
its class.  The answer lives in the sibling folders -- one folder per class --
and this module turns those folder names into the four labels the classifier
emits.  A file inside ``all/`` is matched by name against the class folders,
which is what makes one ``all/`` upload scoreable.

Naming is forgiving on purpose: the sample folders people keep are called
``chahua``/``meme``/``photo``/``screenshoot`` as often as they are called
``插画``/``梗图``/``照片``/``截图``, so both spellings (and a few obvious
neighbours) resolve to the same label.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

__all__ = ["LABELS", "IMAGE_SUFFIXES", "label_for_dir", "build_truth_map"]

LABELS = ("截图", "插画", "梗图", "照片")

IMAGE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp", ".gif", ".bmp", ".avif", ".heic", ".heif"}
)

_ALIASES = {
    # illustration
    "插画": "插画",
    "插圖": "插画",
    "illu": "插画",
    "illustration": "插画",
    "illustrations": "插画",
    "chahua": "插画",
    "artwork": "插画",
    "drawing": "插画",
    # meme
    "梗图": "梗图",
    "梗圖": "梗图",
    "meme": "梗图",
    "memes": "梗图",
    "emoji": "梗图",
    "sticker": "梗图",
    "stickers": "梗图",
    # photo
    "照片": "照片",
    "相片": "照片",
    "photo": "照片",
    "photos": "照片",
    "photograph": "照片",
    "photographs": "照片",
    "real": "照片",
    # screenshot
    "截图": "截图",
    "截圖": "截图",
    "screenshot": "截图",
    "screenshots": "截图",
    "screenshoot": "截图",
    "screen": "截图",
}

_SPLIT = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


def label_for_dir(name: str) -> str | None:
    """Map a folder name to one of :data:`LABELS`, or None if it says nothing."""
    if not name:
        return None
    text = str(name).strip()
    if not text:
        return None
    # a whole-name hit first, so "screenshoot" does not have to be a token
    direct = _ALIASES.get(text.lower())
    if direct:
        return direct
    for token in _SPLIT.split(text):
        if not token:
            continue
        hit = _ALIASES.get(token.lower())
        if hit:
            return hit
    for label in LABELS:
        if label in text:
            return label
    return None


def build_truth_map(root: str | Path) -> dict:
    """Scan ``root`` and return ``{file name: class label}`` plus a summary.

    Files under a class folder carry their own answer; files anywhere else
    (typically the flat ``all/`` copy) are looked up by name, which is the only
    signal available once the folders have been flattened.
    """
    root = Path(root).expanduser()
    out: dict = {
        "dir": str(root),
        "exists": root.is_dir(),
        "classes": {},
        "counts": {label: 0 for label in LABELS},
        "labels": list(LABELS),
        "unlabeled": [],
        "conflicts": [],
    }
    if not root.is_dir():
        return out

    images = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
    classes: dict[str, str] = {}
    strays: list[Path] = []

    for path in images:
        label = None
        for parent in path.parents:
            if parent == root:
                break
            label = label_for_dir(parent.name)
            if label:
                break
        if label is None:
            strays.append(path)
            continue
        known = classes.get(path.name)
        if known is not None and known != label:
            out["conflicts"].append(path.name)
            continue
        classes[path.name] = label
        out["counts"][label] += 1

    for path in strays:
        label = classes.get(path.name)
        if label:
            continue
        out["unlabeled"].append(path.name)

    out["classes"] = classes
    seen = Counter(classes.values())
    # keep all four labels, zeros included: the page prints a row per class and a
    # missing key would silently drop a class nobody predicted
    out["counts"] = {label: seen[label] for label in LABELS}
    out["matched"] = len(classes)
    out["stray"] = len(out["unlabeled"])
    return out
