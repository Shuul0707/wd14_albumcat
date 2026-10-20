"""Prettify / split_series / TagSet merge semantics.

These are the pure functions everything else leans on: if ``prettify`` starts
capitalising ``r18`` the phone shows the wrong tag name, and if ``TagSet``
starts keeping the *first* score instead of the best one a plugin can never
raise WD14's confidence.
"""

from __future__ import annotations

import pytest

from albumcat.types import (
    CHARACTER,
    GENERAL,
    RATING,
    Prediction,
    TagSet,
    prettify,
    split_series,
)


class TestPrettify:
    def test_underscores_become_spaces_and_words_are_capitalised(self) -> None:
        assert prettify("hatsune_miku") == "Hatsune Miku"

    def test_trailing_parenthetical_is_dropped(self) -> None:
        assert prettify("hakurei_reimu_(touhou)") == "Hakurei Reimu"

    def test_short_and_non_alpha_tokens_keep_their_case(self) -> None:
        # "3d" and "r18" are tag words, not proper nouns.
        assert prettify("3d") == "3d"
        assert prettify("r18") == "r18"

    def test_empty_input_falls_back_to_the_key(self) -> None:
        assert prettify("___") == "___"


class TestSplitSeries:
    def test_extracts_the_series(self) -> None:
        assert split_series("hakurei_reimu_(touhou)") == ("hakurei_reimu", "touhou")

    def test_without_parenthetical(self) -> None:
        assert split_series("hatsune_miku") == ("hatsune_miku", None)

    def test_empty_parenthetical_is_not_a_series(self) -> None:
        assert split_series("something_()") == ("something_()", None)

    def test_full_width_parentheses_work_too(self) -> None:
        assert split_series("kasane_teto（utau）") == ("kasane_teto", "utau")


class TestPrediction:
    def test_display_falls_back_to_prettify(self) -> None:
        assert Prediction("hakurei_reimu_(touhou)", 0.9, CHARACTER).display == "Hakurei Reimu"

    def test_explicit_label_wins(self) -> None:
        assert Prediction("x", 0.5, CHARACTER, label="自定义").display == "自定义"

    def test_series_property(self) -> None:
        assert Prediction("hakurei_reimu_(touhou)", 0.9, CHARACTER).series == "touhou"

    def test_ref_is_category_plus_key(self) -> None:
        pred = Prediction("touhou", 0.9, GENERAL)
        assert pred.ref == (GENERAL, "touhou")

    def test_unknown_category_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            Prediction("x", 0.5, "nonsense")


class TestTagSet:
    def test_merge_keeps_the_best_score_and_its_provenance(self) -> None:
        tagset = TagSet([
            Prediction("cat_girl", 0.4, GENERAL, source="wd14"),
            Prediction("cat_girl", 0.8, GENERAL, source="my-plugin"),
        ])
        assert len(tagset) == 1
        best = tagset.get("cat_girl", GENERAL)
        assert best is not None
        assert best.score == 0.8
        assert best.source == "my-plugin"
        # Both observations stay visible for logging.
        assert len(tagset.all_observations) == 2

    def test_same_key_in_different_categories_stays_separate(self) -> None:
        tagset = TagSet([Prediction("touhou", 0.9, GENERAL), Prediction("touhou", 0.8, CHARACTER)])
        assert len(tagset) == 2

    def test_by_category_sorts_by_score_desc(self) -> None:
        tagset = TagSet([Prediction("a", 0.2), Prediction("b", 0.9), Prediction("c", 0.5)])
        assert [p.key for p in tagset.general] == ["b", "c", "a"]

    def test_best_rating_defaults_to_general(self) -> None:
        assert TagSet().best_rating() == ("general", 0.0)

    def test_best_rating_picks_the_highest(self) -> None:
        tagset = TagSet([
            Prediction("sensitive", 0.6, RATING),
            Prediction("explicit", 0.9, RATING),
        ])
        assert tagset.best_rating() == ("explicit", 0.9)

    def test_ratings_mapping(self) -> None:
        tagset = TagSet([Prediction("explicit", 0.9, RATING), Prediction("general", 0.6, RATING)])
        assert tagset.ratings == {"explicit": 0.9, "general": 0.6}

    def test_contains_uses_the_key(self) -> None:
        tagset = TagSet([Prediction("twintails", 0.9)])
        assert "twintails" in tagset
        assert "smile" not in tagset
