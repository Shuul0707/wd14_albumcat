"""Publishers: turn :class:`~albumcat.types.TagPlan` objects into something visible.

Two routes, and the difference is only in where the tags end up:

* **Route A --** :class:`JpegXmpSink` writes standard XMP into the JPEG's own
  APP1 segment. The photo does not move and is not re-encoded; Aves (and
  Lightroom, digiKam, ...) read the tag tree straight out of the file.
* **Route B --** :class:`ImmichPublisher` makes API calls instead, so it never
  touches a byte of the photo. Two properties shape that design:

  * **Idempotence.** Applying a tag that is already on an asset is a no-op on
    Immich's side, so re-running the pipeline is always safe.
  * **Call economy.** ``PUT /tags/assets`` applies *every* listed tag to *every*
    listed asset, so it is only correct when those assets share a tag set. The
    publisher therefore inverts the problem: it collects, per tag, the assets
    that want it, and issues one ``PUT /tags/{id}/assets`` per tag. For a library
    with hundreds of characters that is a couple of hundred calls instead of one
    per photo.

Everything published is appended to a JSONL journal, so a bad run can be
reversed with ``albumcat undo``.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from . import xmp
from .config import Config, PublishConfig
from .immich import ImmichClient, ImmichError
from .types import TagPlan

__all__ = [
    "PublishReport",
    "Publisher",
    "ImmichPublisher",
    "JsonPublisher",
    "JpegXmpSink",
    "Journal",
    "plan_to_xmp_fields",
]

log = logging.getLogger("albumcat.publish")


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
@dataclass
class PublishReport:
    """What one publish pass did (or would have done)."""

    assets: int = 0
    tags_total: int = 0
    tags_reused: int = 0
    tags_created: int = 0
    links_applied: int = 0
    ratings_written: int = 0
    files_written: int = 0
    needs_review: int = 0
    dry_run: bool = False
    errors: list[tuple[str, str]] = field(default_factory=list)
    new_tags: list[str] = field(default_factory=list)

    def summary(self) -> str:
        mode = "DRY RUN" if self.dry_run else "applied"
        bits = [
            f"{self.assets} items ({mode})",
            f"{self.tags_total} distinct tags",
        ]
        if self.files_written:
            bits.append(f"{self.files_written} files")
        if self.tags_created:
            bits.append(f"{self.tags_created} new")
        bits.append(f"{self.links_applied} links")
        if self.ratings_written:
            bits.append(f"{self.ratings_written} ratings")
        if self.needs_review:
            bits.append(f"{self.needs_review} need review")
        if self.errors:
            bits.append(f"{len(self.errors)} errors")
        return ", ".join(bits)


# --------------------------------------------------------------------------- #
# journal
# --------------------------------------------------------------------------- #
class Journal:
    """Append-only record of published links, for undo."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def record(self, tag_id: str, tag_key: str, asset_ids: Sequence[str], run_id: str) -> None:
        if not asset_ids:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": time.time(),
                        "run": run_id,
                        "tag_id": tag_id,
                        "tag": tag_key,
                        "asset_ids": list(asset_ids),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    def read(self, run_id: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        rows = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if run_id is None or row.get("run") == run_id:
                    rows.append(row)
        return rows

    def runs(self) -> list[str]:
        seen: list[str] = []
        for row in self.read():
            run = row.get("run")
            if run and run not in seen:
                seen.append(run)
        return seen

    def write_raw(self, row: dict) -> None:
        """Append an arbitrary row.

        Immich undo works on (tag id, asset ids) tuples; the XMP sink's undo
        needs the *previous packet* of each file instead, so it writes its own
        row shape into the same journal.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# publishers
# --------------------------------------------------------------------------- #
class Publisher:
    """Interface every publish target implements."""

    def publish(self, plans: Iterable[TagPlan]) -> PublishReport:
        raise NotImplementedError


class JsonPublisher(Publisher):
    """Writes plans to a JSONL file. The offline/preview target."""

    def __init__(self, path: Path | str, indent: int | None = None) -> None:
        self.path = Path(path)
        self.indent = indent

    def publish(self, plans: Iterable[TagPlan]) -> PublishReport:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        report = PublishReport(dry_run=True)
        seen: set[str] = set()
        with self.path.open("w", encoding="utf-8") as fh:
            for plan in plans:
                report.assets += 1
                report.needs_review += int(plan.needs_review)
                for tag in plan.tags:
                    if tag.key not in seen:
                        seen.add(tag.key)
                fh.write(
                    json.dumps(plan.to_dict(), ensure_ascii=False, indent=self.indent) + "\n"
                )
        report.tags_total = len(seen)
        return report


def plan_to_xmp_fields(
    plan: TagPlan,
    publish_cfg: PublishConfig,
    separator: str = "|",
) -> tuple[list[str], list[str]]:
    """``(flat keywords, hierarchical paths)`` for one plan.

    ``dc:subject`` is the flat keyword bag and gets every tag's leaf name
    ("Kasane Teto"), so booru tags stay searchable. With ``tree_in_subject``
    (the default) it *also* gets the hierarchical paths, because Aves builds an
    entry's tags from this one property and nothing else -- see the config
    docstring.

    ``lr:HierarchicalSubject`` / ``digiKam:TagsList`` is the *tree* a
    gallery groups by, so by default it carries only the tags under the
    configured roots -- ``角色`` / ``作品`` / ``分级``. Dumping twenty-odd
    general tags into it as well would bury the character tags that are the
    point of the whole exercise. ``general_tags_in_tree = true`` restores
    the everything-in-the-tree behaviour.

    Lives outside the sink because ``albumcat serve`` needs the same answer
    for a phone that is going to do the writing itself.
    """
    if publish_cfg.general_tags_in_tree:
        tree = [t.path for t in plan.tags]
    else:
        roots = {
            publish_cfg.character_root,
            publish_cfg.series_root,
            publish_cfg.rating_root,
            publish_cfg.copyright_root,
        }
        tree = [t.path for t in plan.tags if t.path[0] in roots]

    flat = list(dict.fromkeys(t.path[-1] for t in plan.tags))
    hierarchical = list(dict.fromkeys(separator.join(p) for p in tree))

    if publish_cfg.tree_in_subject:
        # Aves derives a photo's tags from dc:subject alone -- its native reader
        # looks up XMP.DC_SUBJECT_PROP_NAME, splits on ";" and never touches
        # lr:HierarchicalSubject or digiKam:TagsList. Without this the phone
        # would show "Kasane Teto" but no "角色|Kasane Teto" to tap, and the
        # whole point of route A is tapping a character on the phone.
        flat = list(dict.fromkeys([*flat, *hierarchical]))

    return flat, hierarchical


class JpegXmpSink(Publisher):
    """Route A: writes tags into the photo's own XMP block.

    This is what makes a *third-party gallery* do the grouping without moving a
    single file. The photo keeps its name and its folder; only a metadata
    segment is replaced, so the pixels stay bit-identical.

    JPEG and PNG are both supported, and the format is decided by the file's
    own header rather than by its suffix -- the suffix is only used to decide
    which files are worth opening. PNG matters more than it sounds: an Android
    screenshot is a PNG, and screenshots are exactly the images people want
    sorted by character.

    Everything needed to undo a run is journaled *before* the first write to a
    file -- namely that file's previous XMP block, or ``None`` if it had none.
    The blob's meaning is format-specific (a JPEG stores the packet; a PNG
    stores the whole iTXt payload), so only ``xmp.find_block`` and
    ``xmp.restore_photo`` understand it. Never decode it here.
    """

    SUFFIXES = frozenset({".jpg", ".jpeg", ".jpe", ".png"})

    def __init__(
        self,
        config: Config | PublishConfig,
        dry_run: bool | None = None,
        journal: Journal | None = None,
        pad: int = xmp.DEFAULT_PAD,
        separator: str = "|",
    ) -> None:
        self.publish_cfg = config.publish if isinstance(config, Config) else config
        self.dry_run = self.publish_cfg.dry_run if dry_run is None else dry_run
        self.journal = journal
        self.pad = pad
        self.separator = separator
        self.run_id = time.strftime("%Y%m%d-%H%M%S")

    def _paths(self, plan: TagPlan) -> tuple[list[str], list[str]]:
        return plan_to_xmp_fields(plan, self.publish_cfg, self.separator)

    def publish(self, plans: Iterable[TagPlan]) -> PublishReport:
        plans = list(plans)
        report = PublishReport(dry_run=self.dry_run, assets=len(plans))
        seen: set[str] = set()

        for plan in plans:
            report.needs_review += int(plan.needs_review)
            if not plan.source_path or not plan.tags:
                continue
            path = Path(plan.source_path)
            flat, hierarchical = self._paths(plan)
            seen.update(hierarchical)

            if path.suffix.lower() not in self.SUFFIXES:
                report.errors.append(
                    (str(path), f"not a JPEG or PNG ({path.suffix or 'no suffix'})")
                )
                continue
            if not path.is_file():
                report.errors.append((str(path), "not a file"))
                continue

            if self.dry_run:
                report.files_written += 1
                report.links_applied += len(flat)
                continue

            rating = plan.star_rating if self.publish_cfg.write_star_rating else None
            try:
                before = xmp.find_block(path)
                xmp.write_photo(
                    path,
                    flat=flat,
                    hierarchical=hierarchical,
                    rating=rating,
                    merge=True,
                    pad=self.pad,
                )
            except (OSError, xmp.XmpError) as exc:
                report.errors.append((str(path), f"{type(exc).__name__}: {exc}"))
                continue

            report.files_written += 1
            report.links_applied += len(flat)
            if rating is not None:
                report.ratings_written += 1
            if self.journal:
                self.journal.write_raw(
                    {
                        "ts": time.time(),
                        "run": self.run_id,
                        "target": "xmp",
                        "path": str(path),
                        "prev_packet": (
                            base64.b64encode(before).decode("ascii") if before else None
                        ),
                        "flat": flat,
                        "hierarchical": hierarchical,
                        "rating": rating,
                    }
                )

        report.tags_total = len(seen)
        return report

    def undo(self, run_id: str | None = None) -> int:
        """Restore every touched file's XMP to how it looked before the run."""
        if self.journal is None:
            return 0
        # The first row per file holds the pre-run state; later rows are no-ops.
        first: dict[str, str | None] = {}
        for row in self.journal.read(run_id):
            if row.get("target") != "xmp":
                continue
            path = row.get("path")
            if not path or path in first:
                continue
            first[path] = row.get("prev_packet")

        restored = 0
        for path, blob in first.items():
            packet = base64.b64decode(blob) if blob else None
            try:
                xmp.restore_photo(path, packet)
                restored += 1
            except OSError as exc:
                log.warning("undo failed for %s: %s", path, exc)
        return restored


class ImmichPublisher(Publisher):
    """Applies plans to Immich as hierarchical tags (plus optional star rating)."""

    def __init__(
        self,
        client: ImmichClient,
        config: Config | PublishConfig,
        dry_run: bool | None = None,
        journal: Journal | None = None,
        tag_colors: dict[str, str] | None = None,
    ) -> None:
        self.client = client
        self.publish_cfg = config.publish if isinstance(config, Config) else config
        self.dry_run = self.publish_cfg.dry_run if dry_run is None else dry_run
        self.journal = journal
        self.tag_colors = tag_colors or {}
        self.run_id = time.strftime("%Y%m%d-%H%M%S")
        # (parent_id, lowercased name) -> tag dict, kept in sync with the server.
        self._tags: dict[tuple[str | None, str], dict] = {}

    # ------------------------------------------------------------------ #
    def _load_tags(self) -> None:
        self._tags = {
            (t.get("parentId"), t["name"].lower()): t for t in self.client.list_tags()
        }
        log.info("immich has %d tags", len(self._tags))

    def _resolve(self, path: Sequence[str]) -> tuple[dict | None, bool]:
        """Return ``(tag, created)`` for a tag path, creating parents as needed.

        In dry-run mode nothing is created and ``created`` means "would create".
        """
        parent_id: str | None = None
        tag: dict | None = None
        any_created = False
        for part in path:
            key = (parent_id, part.lower())
            found = self._tags.get(key)
            if found is None:
                any_created = True
                if self.dry_run:
                    found = {"id": "", "name": part, "parentId": parent_id}
                    self._tags[key] = found
                else:
                    color = self.tag_colors.get(part)
                    found = self.client.create_tag(part, parent_id=parent_id, color=color)
                    self._tags[key] = found
            parent_id = found.get("id") or ""
            tag = found
        return tag, any_created

    # ------------------------------------------------------------------ #
    def publish(self, plans: Iterable[TagPlan]) -> PublishReport:
        plans = list(plans)
        report = PublishReport(dry_run=self.dry_run, assets=len(plans))

        by_tag: dict[tuple[str, ...], set[str]] = {}
        ratings: dict[int, list[str]] = {}
        for plan in plans:
            if not plan.asset_id:
                continue
            report.needs_review += int(plan.needs_review)
            for tag in plan.tags:
                by_tag.setdefault(tag.path, set()).add(plan.asset_id)
            if plan.star_rating is not None:
                ratings.setdefault(plan.star_rating, []).append(plan.asset_id)

        if not by_tag and not ratings:
            return report

        self._load_tags()
        report.tags_total = len(by_tag)

        for path, asset_ids in sorted(by_tag.items(), key=lambda kv: kv[0]):
            try:
                tag, created = self._resolve(path)
            except ImmichError as exc:
                report.errors.append(("/".join(path), str(exc)))
                continue
            if created:
                report.tags_created += 1
                report.new_tags.append("/".join(path))
            else:
                report.tags_reused += 1

            if self.dry_run:
                report.links_applied += len(asset_ids)
                continue
            if not tag or not tag.get("id"):
                report.errors.append(("/".join(path), "no tag id after resolve"))
                continue
            try:
                self.client.tag_asset_ids(tag["id"], sorted(asset_ids))
            except ImmichError as exc:
                report.errors.append(("/".join(path), str(exc)))
                continue
            report.links_applied += len(asset_ids)
            if self.journal:
                self.journal.record(tag["id"], "/".join(path), sorted(asset_ids), self.run_id)

        for star, asset_ids in sorted(ratings.items()):
            if not asset_ids:
                continue
            if self.dry_run:
                report.ratings_written += len(asset_ids)
                continue
            try:
                self.client.update_assets(asset_ids, rating=int(star))
                report.ratings_written += len(asset_ids)
            except ImmichError as exc:
                report.errors.append((f"rating={star}", str(exc)))

        return report

    # ------------------------------------------------------------------ #
    def undo(self, run_id: str | None = None) -> int:
        """Remove every tag link recorded in the journal (all runs, or one)."""
        if self.journal is None:
            return 0
        rows = self.journal.read(run_id)
        removed = 0
        for row in rows:
            tag_id = row.get("tag_id")
            asset_ids = row.get("asset_ids") or []
            if not tag_id or not asset_ids:
                continue
            try:
                self.client.untag_assets(tag_id, asset_ids)
                removed += len(asset_ids)
            except ImmichError as exc:
                log.warning("undo failed for tag %s: %s", row.get("tag"), exc)
        return removed
