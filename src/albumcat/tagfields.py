"""Turn a :class:`~albumcat.types.TagPlan` into the tag fields the phone gets.

Two lists come out of one plan, and the difference is only in how much
structure each one keeps:

* ``flat`` -- the flat tag table the phone stores. One entry per tag, no
  parents, so a booru tag stays a single searchable word.
* ``hierarchical`` -- the same tags written as level-separated paths, which is
  what makes grouped/clustered views possible at all.

Both are plain data: nothing here writes to a photo or to any service.
"""

from __future__ import annotations

from .config import PublishConfig
from .types import TagPlan

__all__ = ["plan_to_tag_fields"]


def plan_to_tag_fields(
    plan: TagPlan,
    publish_cfg: PublishConfig,
    separator: str = "|",
) -> tuple[list[str], list[str]]:
    """``(flat tags, hierarchical paths)`` for one plan.

    ``flat`` is the flat tag table the phone ends up storing: it gets every
    tag's leaf name ("Kasane Teto"), so booru tags stay searchable one word at
    a time. With ``tree_in_subject`` (the default) it *also* gets the
    hierarchical paths, because the phone builds its tag entries out of this
    one list and nothing else -- see the config docstring.

    ``hierarchical`` is the list of level-separated paths, so by default it
    carries only the tags under the configured roots -- ``角色`` / ``作品`` /
    ``分级``. Dumping twenty-odd general tags into it as well would bury the
    character tags that are the point of the whole exercise.
    ``general_tags_in_tree = true`` restores the everything-in-the-tree
    behaviour.

    Lives on its own so that the HTTP layer and any offline preview agree on
    the answer by construction rather than by copying the code.
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
        # The phone builds a photo's tags from the flat list alone -- it splits
        # that one field on the separator and re-parses the paths, and never
        # looks at the hierarchical list. Without this the phone would show
        # "Kasane Teto" but no "角色|Kasane Teto" to tap, and tapping a
        # character on the phone is the whole point.
        flat = list(dict.fromkeys([*flat, *hierarchical]))

    return flat, hierarchical
