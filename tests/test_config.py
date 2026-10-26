"""Config loading: defaults, the unknown-key guard, env overlay, TOML round trip.

The unknown-key guard is the one users hit: a typo in ``config.toml`` must be an
error rather than a silently ignored setting that makes the tool "not work" for
reasons nobody can see.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from albumcat.config import Config, ModelConfig, PublishConfig


class TestDefaults:
    def test_shipped_defaults(self) -> None:
        cfg = Config()
        assert cfg.model.character_threshold == 0.5
        assert cfg.model.threshold == 0.35
        assert cfg.publish.character_root == "角色"
        assert cfg.publish.review_low == 0.5
        assert cfg.publish.review_high == 0.7
        assert cfg.publish.tree_in_subject is True
        assert cfg.publish.max_characters_per_image == 3

    def test_empty_file_keeps_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("", encoding="utf-8")
        cfg = Config.load(path)
        assert cfg.model == ModelConfig()
        assert cfg.publish == PublishConfig()
        assert cfg.root == tmp_path

    def test_values_from_the_file_win(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            '[model]\nthreshold = 0.4\n\n[publish]\ncharacter_root = "char"\n',
            encoding="utf-8",
        )
        cfg = Config.load(path)
        assert cfg.model.threshold == pytest.approx(0.4)
        assert cfg.publish.character_root == "char"
        # Untouched keys keep their defaults.
        assert cfg.model.character_threshold == 0.5


class TestValidation:
    def test_unknown_key_is_an_error(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("[model]\nnope = 1\n", encoding="utf-8")
        with pytest.raises(ValueError, match="unknown config keys"):
            Config.load(path)

    def test_missing_file_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            Config.load(tmp_path / "nope.toml")


class TestEnvOverlay:
    def test_values_are_coerced_to_the_field_type(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("", encoding="utf-8")
        cfg = Config.load(path)
        cfg.apply_env({
            "ALBUMCAT_MODEL_DEVICE": "cpu",
            "ALBUMCAT_MODEL_THRESHOLD": "0.6",
            "ALBUMCAT_PUBLISH_MAX_CHARACTERS_PER_IMAGE": "7",
            "ALBUMCAT_PUBLISH_TREE_IN_SUBJECT": "false",
            "ALBUMCAT_PUBLISH_GENERAL_TAG_DENYLIST": "1girl, watermark",
        })
        assert cfg.model.device == "cpu"
        assert cfg.model.threshold == pytest.approx(0.6)
        assert cfg.publish.max_characters_per_image == 7
        assert cfg.publish.tree_in_subject is False
        assert cfg.publish.general_tag_denylist == ["1girl", "watermark"]

    def test_unknown_env_vars_are_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("", encoding="utf-8")
        cfg = Config.load(path)
        cfg.apply_env({"ALBUMCAT_MODEL_NOT_A_FIELD": "1", "PATH": "/nowhere"})
        assert cfg.model.device == "auto"


class TestSave:
    def test_round_trip(self, tmp_path: Path) -> None:
        cfg = Config()
        cfg.model.threshold = 0.42
        cfg.publish.character_root = "角色X"
        cfg.publish.general_tag_denylist = ["watermark"]
        cfg.plugins.disabled = ["some-plugin"]
        dest = cfg.save(tmp_path / "out.toml")

        again = Config.load(dest)
        assert again.model.threshold == pytest.approx(0.42)
        assert again.publish.character_root == "角色X"
        assert again.publish.general_tag_denylist == ["watermark"]
        assert again.plugins.disabled == ["some-plugin"]

    def test_saved_file_is_valid_toml_with_sections(self, tmp_path: Path) -> None:
        import tomllib

        dest = Config().save(tmp_path / "out.toml")
        raw = tomllib.loads(dest.read_text(encoding="utf-8"))
        assert set(raw) == {"model", "publish", "plugins", "semantic", "ocr"}
        assert raw["publish"]["character_root"] == "角色"
        # OCR is its own section: the engine ships inside its package, so there
        # is no model path to configure, only behaviour.
        assert raw["ocr"]["max_side"] == 1600
        assert raw["ocr"]["min_score"] == pytest.approx(0.5)
