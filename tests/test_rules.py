"""The rule engine: default policy plus user rules.

The default policy is where the project's opinion lives -- characters under
``角色/<name>``, a review band that is flagged but *not* published, at most N
characters per image -- so those are exactly the things worth pinning down.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from albumcat.config import PublishConfig
from albumcat.rules import Action, Condition, Rule, RuleEngine, load_rules
from albumcat.types import CHARACTER, GENERAL, RATING, Prediction, TagSet


def plan_for(predictions, cfg=None, rules=None):
    engine = RuleEngine(cfg or PublishConfig(), rules or [])
    return engine.evaluate(TagSet(predictions))


def character_paths(plan):
    return [tag.path for tag in plan.tags if tag.path[0] == "角色"]


class TestCharacters:
    def test_confident_character_goes_under_the_root_with_its_series(self) -> None:
        plan = plan_for([Prediction("hakurei_reimu_(touhou)", 0.95, CHARACTER)])
        assert plan.has_path(("角色", "Hakurei Reimu"))
        assert plan.has_path(("作品", "Touhou"))
        assert plan.needs_review is False

    def test_review_band_is_flagged_but_not_published(self) -> None:
        plan = plan_for([Prediction("kasane_teto", 0.6, CHARACTER)])
        assert plan.needs_review is True
        assert plan.tags == []
        assert any("review band" in note for note in plan.notes)

    def test_review_band_publishes_when_asked(self) -> None:
        plan = plan_for(
            [Prediction("kasane_teto", 0.6, CHARACTER)],
            PublishConfig(publish_review_band=True),
        )
        assert plan.has_path(("角色", "Kasane Teto"))
        assert plan.needs_review is True

    def test_below_review_low_is_dropped_silently(self) -> None:
        plan = plan_for([Prediction("kasane_teto", 0.3, CHARACTER)])
        assert plan.tags == []
        assert plan.needs_review is False

    def test_only_the_top_characters_survive(self) -> None:
        predictions = [
            Prediction("alpha", 0.95, CHARACTER),
            Prediction("beta", 0.94, CHARACTER),
            Prediction("gamma", 0.93, CHARACTER),
        ]
        plan = plan_for(predictions, PublishConfig(max_characters_per_image=2))
        assert character_paths(plan) == [("角色", "Alpha"), ("角色", "Beta")]

    def test_empty_root_means_flat_character_tags(self) -> None:
        plan = plan_for(
            [Prediction("hakurei_reimu_(touhou)", 0.95, CHARACTER)],
            PublishConfig(character_root="", series_root=""),
        )
        assert plan.has_path(("Hakurei Reimu",))
        assert plan.has_path(("Touhou",))


class TestRatings:
    @pytest.mark.parametrize(
        "bucket,label,star",
        [("explicit", "R18", 1), ("questionable", "敏感", 2), ("sensitive", "轻微", 4)],
    )
    def test_buckets(self, bucket: str, label: str, star: int) -> None:
        plan = plan_for(
            [Prediction(bucket, 0.9, RATING)],
            PublishConfig(write_star_rating=True),
        )
        assert plan.has_path(("分级", label))
        assert plan.star_rating == star

    def test_general_bucket_emits_nothing(self) -> None:
        assert plan_for([Prediction("general", 0.99, RATING)]).tags == []

    def test_below_threshold_emits_nothing(self) -> None:
        assert plan_for([Prediction("explicit", 0.2, RATING)]).tags == []

    def test_rating_disabled(self) -> None:
        plan = plan_for(
            [Prediction("explicit", 0.99, RATING)],
            PublishConfig(rating_enabled=False),
        )
        assert plan.tags == []

    def test_star_is_not_written_by_default(self) -> None:
        plan = plan_for([Prediction("explicit", 0.9, RATING)])
        assert plan.has_path(("分级", "R18"))
        assert plan.star_rating is None


class TestGeneralTags:
    def test_capped_and_denylisted(self) -> None:
        predictions = [
            Prediction("watermark", 0.99),
            Prediction("twintails", 0.9),
            Prediction("smile", 0.8),
        ]
        cfg = PublishConfig(max_general_tags_per_image=2, general_tag_denylist=["watermark"])
        plan = plan_for(predictions, cfg)
        assert [tag.path for tag in plan.tags] == [("Twintails",), ("Smile",)]

    def test_zero_means_no_general_tags(self) -> None:
        plan = plan_for([Prediction("twintails", 0.9)], PublishConfig(max_general_tags_per_image=0))
        assert plan.tags == []


class TestUserRules:
    def test_drop_suppresses_the_character(self) -> None:
        rules = [
            Rule(
                id="drop.reimu",
                when=Condition(key_regex=r"^hakurei_reimu"),
                then=Action(kind="drop"),
            )
        ]
        plan = plan_for([Prediction("hakurei_reimu_(touhou)", 0.99, CHARACTER)], rules=rules)
        assert character_paths(plan) == []

    def test_tag_rule_templates_key_display_and_series(self) -> None:
        rules = [
            Rule(
                id="album.touhou",
                when=Condition(key_regex="touhou"),
                then=Action(kind="tag", path=["收藏", "{series}", "{display}"]),
            )
        ]
        plan = plan_for([Prediction("hakurei_reimu_(touhou)", 0.99, CHARACTER)], rules=rules)
        assert plan.has_path(("收藏", "touhou", "Hakurei Reimu"))

    def test_star_and_review_actions(self) -> None:
        rules = [
            Rule(id="star.teto", when=Condition(key="kasane_teto"), then=Action(kind="star", value=5)),
            Rule(id="review.teto", when=Condition(key="kasane_teto"), then=Action(kind="review")),
        ]
        plan = plan_for([Prediction("kasane_teto", 0.99, CHARACTER)], rules=rules)
        assert plan.star_rating == 5
        assert plan.needs_review is True

    def test_disabled_rule_is_ignored(self) -> None:
        rules = [
            Rule(
                id="off",
                when=Condition(key="twintails"),
                then=Action(kind="tag", path=["A"]),
                enabled=False,
            )
        ]
        plan = plan_for([Prediction("twintails", 0.9)], rules=rules)
        assert not plan.has_path(("A",))

    def test_explain_names_the_rule_that_made_the_tag(self) -> None:
        rules = [
            Rule(
                id="album.touhou",
                when=Condition(key_regex="touhou"),
                then=Action(kind="tag", path=["收藏"]),
                description="all Touhou art goes to one album",
            )
        ]
        engine = RuleEngine(PublishConfig(), rules)
        assert engine.explain(("收藏",)) == "rule album.touhou: all Touhou art goes to one album"
        assert engine.explain(("角色", "Hakurei Reimu")) == "default policy"


class TestRuleIO:
    def test_load_accepts_a_bare_list_and_assigns_user_priority(self) -> None:
        def write(tmp_path: Path) -> Path:
            path = tmp_path / "rules.json"
            path.write_text(
                '[{"id": "r1", "when": {"key": "a"}, "then": {"kind": "drop"}}]',
                encoding="utf-8",
            )
            return path

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            rules = load_rules(write(Path(tmp)))
        assert [r.id for r in rules] == ["r1"]
        assert rules[0].priority == 10
        assert rules[0].then.kind == "drop"

    def test_load_accepts_the_wrapper_object(self, tmp_path: Path) -> None:
        path = tmp_path / "rules.json"
        path.write_text('{"rules": [{"id": "r2", "priority": 5}]}', encoding="utf-8")
        rules = load_rules(path)
        assert [r.id for r in rules] == ["r2"]
        assert rules[0].priority == 5

    def test_round_trip_through_to_dict(self, tmp_path: Path) -> None:
        from albumcat.rules import save_rules

        rules = [
            Rule(
                id="r1",
                when=Condition(category=CHARACTER, score_gte=0.8),
                then=Action(kind="tag", path=["角色", "{display}"], reason="why"),
                description="说明",
            )
        ]
        dest = save_rules(rules, tmp_path / "out.json")
        again = load_rules(dest)[0]
        assert again.when == rules[0].when
        assert again.then.kind == "tag"
        assert again.then.path == ["角色", "{display}"]
        assert again.description == "说明"
