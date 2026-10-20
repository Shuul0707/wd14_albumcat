"""The query parser: what a Chinese search box actually asked for.

These tests pin the behaviour the phone relies on:

  * a named character lands on the WD14 key, not on the Chinese string,
  * the leftover words (the mood) survive for the vector half,
  * a series name expands to its cast instead of one arbitrary girl,
  * English, Chinese and underscored spellings all reach the same tag.
"""

from __future__ import annotations

import pytest

from albumcat.tagdict import load_dictionary, parse_query
from albumcat.types import CHARACTER, GENERAL


@pytest.fixture(scope="module")
def dictionary():
    return load_dictionary()


def test_character_and_mood_are_split(dictionary):
    parsed = parse_query("黑暗的初音未来", dictionary)

    keys = {m.key: m for m in parsed.mentions}
    assert "hatsune_miku" in keys
    assert keys["hatsune_miku"].category == CHARACTER
    assert "dark" in keys

    assert parsed.has_character
    # The vector must see the mood, not the character name: embedding "黑暗的初音
    # 未来" pulls towards Miku's usual bright look.
    assert parsed.vector_text == "黑暗"


def test_a_bare_character_still_gets_an_embedding(dictionary):
    parsed = parse_query("初音未来", dictionary)

    assert [m.key for m in parsed.characters] == ["hatsune_miku"]
    assert parsed.residual == ""
    assert parsed.vector_text == "初音未来"


def test_series_expands_to_its_cast(dictionary):
    parsed = parse_query("东方的角色", dictionary)

    series = [m for m in parsed.mentions if m.is_series]
    assert series, "东方Project 应该被认成一个系列"
    assert parsed.has_character
    cast = parsed.character_keys
    assert len(cast) > 10
    assert "hakurei_reimu" in cast
    # A series mention is not itself a character key.
    assert all(dictionary.category(key) == CHARACTER for key in cast)


def test_english_chinese_and_underscores_agree(dictionary):
    for text in ("初音未来", "hatsune_miku", "Hatsune Miku"):
        parsed = parse_query(text, dictionary)
        assert [m.key for m in parsed.characters] == ["hatsune_miku"], text


def test_longest_match_wins(dictionary):
    # "双马尾" alone is twintails; inside "金发双马尾" it must not swallow "金发".
    parsed = parse_query("金发双马尾的夜晚", dictionary)

    keys = [m.key for m in parsed.mentions]
    assert "blonde_hair" in keys
    assert "twintails" in keys
    assert "night" in keys


def test_unknown_words_stay_residual(dictionary):
    # 喵呜 is not a WD14 tag, so the whole phrase stays for the vector half.
    # (黄昏 *is* one -- sunset -- which is why this test needs real nonsense.)
    parsed = parse_query("像梦一样的喵呜", dictionary)

    assert parsed.mentions == ()
    # Stop words like 的 are dropped: they carry no visual meaning for CLIP.
    assert parsed.residual == "像梦一样喵呜"
    assert parsed.vector_text == "像梦一样喵呜"
    assert not parsed.has_character


def test_empty_query_is_empty(dictionary):
    parsed = parse_query("   ", dictionary)

    assert parsed.mentions == ()
    assert parsed.residual == ""
    assert parsed.vector_text == ""
    assert not parsed.has_character


def test_to_dict_is_json_shaped(dictionary):
    parsed = parse_query("黑暗的初音未来", dictionary).to_dict()

    assert parsed["text"] == "黑暗的初音未来"
    assert parsed["has_character"] is True
    mention = next(m for m in parsed["mentions"] if m["key"] == "hatsune_miku")
    assert mention["category"] == CHARACTER
    assert mention["series"] == "VOCALOID"
    assert mention["matched"] == "初音未来"
    general = next(m for m in parsed["mentions"] if m["key"] == "dark")
    assert general["category"] == GENERAL


def test_weights_rank_the_character_above_the_mood(dictionary):
    parsed = parse_query("黑暗的初音未来", dictionary)

    found = {m.key: m.weight for m in parsed.mentions}
    assert found["hatsune_miku"] > found["dark"]
