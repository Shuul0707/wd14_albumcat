"""Turn a free-form search box into tag mentions + the words left over.

A phone-side semantic search has two signals: the CLIP vector of the query, and
the WD14 tags the query *names*. "黑暗的初音未来" is the interesting case -- the
user means "pictures of Hatsune Miku that are dark", so the query has to be
split into a character filter (``hatsune_miku``) and a leftover mood phrase
("黑暗") that the vector should explain.

Splitting needs a Chinese name table, because WD14's own labels are English.
``albumcat/data/tag_zh.json`` (built by ``tools/build_tag_zh.py``) supplies one
for 95.9% of the 9083 labels; this module loads that table and does
longest-match segmentation over a squashed view of the query, so ``Long Hair``,
``long_hair`` and ``longhair`` all land on the same tag while Chinese text keeps
its original character offsets.

Series names get their own mention kind: WD14 has no ``touhou`` tag, the series
only rides along in each character's parenthetical (``hakurei_reimu_(touhou)``),
so "东方的图" has to expand to the cast list rather than to one arbitrary girl.
"""

from __future__ import annotations

import json
import re
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .types import CHARACTER, GENERAL, RATING, prettify

__all__ = [
    "SERIES",
    "KIND_CATEGORY",
    "TagMention",
    "ParsedQuery",
    "TagDictionary",
    "load_dictionary",
    "parse_query",
    "squeeze",
]

DATA_FILE = Path(__file__).resolve().parent / "data" / "tag_zh.json"

#: A mention that named a source work instead of a tag ("东方Project").
SERIES = "series"

#: short code in the shipped table -> ``types.CATEGORIES`` value
KIND_CATEGORY = {"c": CHARACTER, "g": GENERAL, "r": RATING}

#: How much a mention counts towards the tag half of the fused score. A named
#: character is the strongest statement a user can make ("show me Reimu"), a
#: series is one step removed, a mood/outfit tag is a hint, a rating is nearly
#: always incidental.
KIND_WEIGHT = {CHARACTER: 1.0, SERIES: 0.7, GENERAL: 0.55, RATING: 0.35}

#: Shortest surface form we accept. Chinese is dense (雷姆 = 2 chars is a full
#: character name), Latin text is not, and short Latin fragments match inside
#: unrelated words.
_MIN_CHARS_CJK = 2
_MIN_CHARS_LATIN = 3

#: Guard against a query that is nothing but tags.
_MAX_MENTIONS = 12

#: Dropped from the leftover phrase, because they only glue tag words together
#: ("黑暗的" -> "黑暗"). Deliberately conservative: verbs like 看/想 survive.
_STOP = {
    "的", "地", "得", "了", "着", "和", "与", "及", "吧", "呢", "啊", "呀", "吗", "是", "在", "些", "个",
    "a", "an", "the", "of", "and", "with", "in", "on", "at", "to", "for", "is", "are",
    "some", "any", "very", "more", "most", "me", "my", "i", "please", "picture", "pictures",
}

_TOKEN_RE = re.compile(r"[A-Za-z0-9']+|[^\sA-Za-z0-9']")


def squeeze(text: str) -> str:
    """Lowercase + NFKC, keeping only letters and digits.

    Mirrors ``tools/build_tag_zh.py:squeeze`` -- the two must agree or the index
    silently stops matching. Spaces, underscores and punctuation are dropped so
    "Long Hair" / "long_hair" / "longhair" are one key.
    """
    return "".join(
        ch for ch in unicodedata.normalize("NFKC", text).lower() if ch.isalnum()
    )


def _squeeze_with_offsets(text: str) -> tuple[str, list[int]]:
    """``squeeze`` plus, for each kept char, its index in ``text``."""
    kept: list[str] = []
    offsets: list[int] = []
    for i, ch in enumerate(text):
        for norm in unicodedata.normalize("NFKC", ch).lower():
            if norm.isalnum():
                kept.append(norm)
                offsets.append(i)
    return "".join(kept), offsets


@dataclass(frozen=True, slots=True)
class TagMention:
    """One tag (or series) the query named, with where it came from."""

    key: str
    """WD14 key, e.g. ``hatsune_miku``. For a series mention: the series label."""

    category: str
    """``character`` / ``tag`` / ``rating`` / ``series``."""

    zh: str = ""
    """Chinese name if the table has one, else empty."""

    matched: str = ""
    """The surface form the user actually typed (``初音``)."""

    start: int = 0
    """Span in the original query, so the UI can highlight it."""

    end: int = 0

    keys: tuple[str, ...] = ()
    """Series mentions only: the character keys this series expands to."""

    series: str = ""
    """Series a character belongs to, so the phone can give same-series photos
    a small credit below an exact character hit."""

    @property
    def label(self) -> str:
        return prettify(self.key) if self.category != SERIES else self.key

    @property
    def display(self) -> str:
        return self.zh or self.label

    @property
    def weight(self) -> float:
        return KIND_WEIGHT.get(self.category, 0.5)

    @property
    def is_character(self) -> bool:
        return self.category == CHARACTER

    @property
    def is_series(self) -> bool:
        return self.category == SERIES

    def to_dict(self) -> dict:
        out = {
            "key": self.key,
            "category": self.category,
            "zh": self.zh,
            "label": self.label,
            "matched": self.matched,
            "start": self.start,
            "end": self.end,
            "weight": round(self.weight, 3),
            "series": self.series,
        }
        if self.keys:
            out["keys"] = list(self.keys)
        return out


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    """A query split into named tags and everything else."""

    text: str
    mentions: tuple[TagMention, ...] = ()

    @property
    def residual(self) -> str:
        """The query with the matched words cut out, stop words and all.

        "黑暗的初音未来" -> "黑暗"; "雷姆 蓝发" -> "". Empty when the query was
        nothing but tag names, which is the common case and means the vector
        half of the score has to fall back to the query itself.
        """
        if not self.mentions:
            return _clean(self.text)
        pieces: list[str] = []
        cursor = 0
        for mention in sorted(self.mentions, key=lambda m: m.start):
            if mention.start > cursor:
                pieces.append(self.text[cursor:mention.start])
            cursor = max(cursor, mention.end)
        pieces.append(self.text[cursor:])
        return _clean(" ".join(pieces))

    @property
    def vector_text(self) -> str:
        """What to feed CLIP.

        Preference order: the leftover descriptive words, then the non-character
        tag names the query named, then the query untouched. A bare
        "初音未来" still gets an embedding rather than an empty vector, and
        "黑暗的初音未来" gets "黑暗" -- the mood, without the character pulling
        the embedding towards that character's usual bright look.
        """
        leftover = self.residual
        if leftover:
            return leftover
        words = [m.matched for m in self.mentions if not m.is_character and m.matched]
        if words:
            return " ".join(dict.fromkeys(words))
        return self.text.strip()

    @property
    def characters(self) -> tuple[TagMention, ...]:
        return tuple(m for m in self.mentions if m.is_character)

    @property
    def character_keys(self) -> tuple[str, ...]:
        out: dict[str, None] = {}
        for mention in self.mentions:
            if mention.is_character:
                out[mention.key] = None
            elif mention.is_series:
                for key in mention.keys:
                    out[key] = None
        return tuple(out)

    @property
    def tag_keys(self) -> tuple[str, ...]:
        return tuple(m.key for m in self.mentions if m.category == GENERAL)

    @property
    def has_character(self) -> bool:
        return any(m.is_character or m.is_series for m in self.mentions)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "residual": self.residual,
            "vector_text": self.vector_text,
            "has_character": self.has_character,
            "mentions": [m.to_dict() for m in self.mentions],
        }


def _clean(text: str) -> str:
    """Drop punctuation and glue words, keeping word order and spacing."""
    if not text:
        return ""
    out: list[str] = []
    for token in _TOKEN_RE.findall(text):
        lower = token.lower()
        if lower in _STOP:
            continue
        if not any(ch.isalnum() for ch in token):
            continue
        out.append(token)
    if not out:
        return ""
    joined = ""
    for token in out:
        if not joined:
            joined = token
        elif token.isascii() and token[:1].isalnum():
            joined += " " + token
        else:
            joined += token
    return joined.strip()


class TagDictionary:
    """The Chinese name table, plus longest-match segmentation over queries."""

    __slots__ = ("_tags", "_series", "_index", "_max_len", "source", "version")

    def __init__(self, tags: dict[str, dict], index: dict[str, str],
                 series: dict[str, dict] | None = None,
                 *, source: str = "", version: int = 0) -> None:
        self._tags = tags
        self._series = series or {}
        self._index = index
        self._max_len = max((len(k) for k in index), default=1)
        self.source = source
        self.version = version

    # -- loading -------------------------------------------------------- #
    @classmethod
    def from_file(cls, path: Path | str = DATA_FILE) -> "TagDictionary":
        path = Path(path)
        with path.open(encoding="utf-8") as fp:
            data = json.load(fp)
        return cls(
            data.get("tags", {}),
            data.get("index", {}),
            data.get("series", {}),
            source=data.get("source", ""),
            version=int(data.get("version", 0)),
        )

    # -- lookups -------------------------------------------------------- #
    def __contains__(self, key: str) -> bool:
        return key in self._tags

    def __len__(self) -> int:
        return len(self._tags)

    @property
    def alias_count(self) -> int:
        return len(self._index)

    @property
    def series_count(self) -> int:
        return len(self._series)

    def chinese(self, key: str) -> str:
        """Chinese name for a tag key, or ``""``."""
        return (self._tags.get(key) or {}).get("n", "")

    def series(self, key: str) -> str:
        """Source work a character tag belongs to, or ``""``."""
        return (self._tags.get(key) or {}).get("s", "")

    def category(self, key: str) -> str | None:
        entry = self._tags.get(key)
        if entry is None:
            return None
        return KIND_CATEGORY.get(entry.get("k", ""), GENERAL)

    def cast(self, series: str) -> tuple[str, ...]:
        """Character keys of a series, by its name ("东方Project")."""
        entry = self._series.get(squeeze(series)) or {}
        return tuple(entry.get("k") or ())

    def describe(self, key: str) -> dict:
        """Everything the UI needs about one tag."""
        return {
            "key": key,
            "zh": self.chinese(key),
            "series": self.series(key),
            "category": self.category(key) or GENERAL,
            "label": prettify(key),
        }

    def resolve(self, text: str) -> str | None:
        """Map one word/phrase to a key without scanning a longer query."""
        value = self._index.get(squeeze(text))
        return None if value is None or value.startswith("#") else value

    # -- parsing -------------------------------------------------------- #
    def parse(self, text: str, *, max_mentions: int = _MAX_MENTIONS) -> ParsedQuery:
        """Segment ``text`` into mentions, longest match first at each offset."""
        text = (text or "").strip()
        if not text:
            return ParsedQuery(text="")
        squashed, offsets = _squeeze_with_offsets(text)
        if not squashed:
            return ParsedQuery(text=text)

        mentions: list[TagMention] = []
        seen: set[str] = set()
        i = 0
        while i < len(squashed) and len(mentions) < max_mentions:
            match = self._longest_at(squashed, i, offsets, text)
            if match is None:
                i += 1
                continue
            value, end = match
            mention = self._mention(value, text, offsets, i, end)
            if mention is not None:
                dedupe_key = ("#" + mention.key) if mention.is_series else mention.key
                if dedupe_key not in seen:
                    seen.add(dedupe_key)
                    mentions.append(mention)
            i = end

        mentions.sort(key=lambda m: m.start)
        return ParsedQuery(text=text, mentions=tuple(mentions))

    def _mention(self, value: str, text: str, offsets: list[int],
                 start: int, end: int) -> TagMention | None:
        matched = text[offsets[start]: offsets[end - 1] + 1]
        if value.startswith("#"):
            label = value[1:]
            entry = self._series.get(squeeze(label)) or {}
            keys = tuple(entry.get("k") or ())
            if not keys:
                return None
            return TagMention(
                key=entry.get("n", label),
                category=SERIES,
                zh=entry.get("n", label),
                matched=matched,
                start=offsets[start],
                end=offsets[end - 1] + 1,
                keys=keys,
            )
        entry = self._tags.get(value)
        if entry is None:
            return None
        return TagMention(
            key=value,
            category=KIND_CATEGORY.get(entry.get("k", ""), GENERAL),
            zh=entry.get("n", ""),
            matched=matched,
            start=offsets[start],
            end=offsets[end - 1] + 1,
            series=self.series(value) or "",
        )

    def _longest_at(
        self, squashed: str, start: int, offsets: list[int], original: str
    ) -> tuple[str, int] | None:
        limit = min(self._max_len, len(squashed) - start)
        for length in range(limit, 0, -1):
            candidate = squashed[start:start + length]
            value = self._index.get(candidate)
            if value is None:
                continue
            if not _acceptable(candidate, offsets, start, length, original):
                continue
            return value, start + length
        return None


def _acceptable(
    candidate: str, offsets: list[int], start: int, length: int, original: str
) -> bool:
    """Reject matches that are too short to be meant."""
    cjk = any(not ch.isascii() for ch in candidate)
    if len(candidate) < (_MIN_CHARS_CJK if cjk else _MIN_CHARS_LATIN):
        return False
    if cjk:
        return True
    # A Latin match must not sit inside a longer Latin word: "ai" inside
    # "chair" is not a mention, and neither is "1girl" inside "1girls" unless
    # the table says so (it does not, so the boundary check keeps it honest).
    before = offsets[start] - 1
    if before >= 0 and original[before].isalnum() and original[before].isascii():
        return False
    after = offsets[start + length - 1] + 1
    if after < len(original) and original[after].isalnum() and original[after].isascii():
        return False
    return True


# ---------------------------------------------------------------------- #
# module-level singleton
# ---------------------------------------------------------------------- #
_LOCK = threading.Lock()
_CACHE: TagDictionary | None = None


def load_dictionary(path: Path | str | None = None) -> TagDictionary:
    """Load (once) the shipped Chinese table. Cheap: ~1 MB of JSON."""
    global _CACHE
    with _LOCK:
        if _CACHE is None:
            _CACHE = TagDictionary.from_file(path or DATA_FILE)
        return _CACHE


def parse_query(text: str, dictionary: TagDictionary | None = None) -> ParsedQuery:
    return (dictionary or load_dictionary()).parse(text)


def missing_chinese(mentions: Iterable[TagMention], dictionary: TagDictionary
                    ) -> list[str]:
    """Tag keys among ``mentions`` that have no Chinese name (for reporting)."""
    return [m.key for m in mentions
            if m.category != SERIES and m.key in dictionary and not dictionary.chinese(m.key)]
