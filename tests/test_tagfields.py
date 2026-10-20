"""flat vs hierarchical tag fields.

This is the contract with the phone: it stores the ``flat`` list verbatim and
builds its tag entries from that one field, so ``tree_in_subject`` is what makes
``角色|Hakurei Reimu`` tappable at all. Breaking it is invisible on the server
and only shows up as "the Tags page has no character tags" on the device.
"""

from __future__ import annotations

from albumcat.config import PublishConfig
from albumcat.tagfields import plan_to_tag_fields
from albumcat.types import TagPlan


def make_plan(*paths: tuple[str, ...]) -> TagPlan:
    plan = TagPlan()
    for path in paths:
        plan.add(path, 0.9, "wd14", "test")
    return plan


def test_flat_holds_leaf_names_and_then_the_paths_by_default() -> None:
    plan = make_plan(("角色", "Hakurei Reimu"), ("作品", "Touhou"), ("Twintails",))
    flat, hierarchical = plan_to_tag_fields(plan, PublishConfig())
    assert hierarchical == ["角色|Hakurei Reimu", "作品|Touhou"]
    assert flat == [
        "Hakurei Reimu",
        "Touhou",
        "Twintails",
        "角色|Hakurei Reimu",
        "作品|Touhou",
    ]


def test_general_tags_stay_out_of_the_tree_by_default() -> None:
    plan = make_plan(("角色", "A"), ("Twintails",), ("Smile",))
    _, hierarchical = plan_to_tag_fields(plan, PublishConfig())
    assert hierarchical == ["角色|A"]


def test_general_tags_can_join_the_tree() -> None:
    plan = make_plan(("角色", "A"), ("Twintails",))
    _, hierarchical = plan_to_tag_fields(plan, PublishConfig(general_tags_in_tree=True))
    assert hierarchical == ["角色|A", "Twintails"]


def test_tree_in_subject_off_leaves_the_flat_list_plain() -> None:
    plan = make_plan(("角色", "A"))
    flat, hierarchical = plan_to_tag_fields(plan, PublishConfig(tree_in_subject=False))
    assert flat == ["A"]
    assert hierarchical == ["角色|A"]


def test_custom_separator() -> None:
    plan = make_plan(("角色", "A"))
    flat, hierarchical = plan_to_tag_fields(plan, PublishConfig(), separator=">")
    assert hierarchical == ["角色>A"]
    assert flat == ["A", "角色>A"]


def test_empty_plan_gives_empty_lists() -> None:
    assert plan_to_tag_fields(TagPlan(), PublishConfig()) == ([], [])


def test_duplicates_collapse_and_order_is_stable() -> None:
    plan = make_plan(("角色", "A"), ("角色", "A"), ("Twintails",), ("Twintails",))
    flat, hierarchical = plan_to_tag_fields(plan, PublishConfig())
    assert hierarchical == ["角色|A"]
    assert flat == ["A", "Twintails", "角色|A"]
