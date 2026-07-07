"""Tests for YamlSettingsSource."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_settings import BaseSettings, SettingsConfigDict

from django_aqueduct.sources.yaml import YamlError, YamlSettingsSource


class _Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="allow")
    NAME: str = ""
    NESTED: dict = {}


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "settings.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_reads_scalar_and_nested(tmp_path: Path) -> None:
    path = _write(tmp_path, "NAME: myapp\nNESTED:\n  a: 1\n  b: two\n")
    data = YamlSettingsSource(_Settings, path)()
    assert data["NAME"] == "myapp"
    assert data["NESTED"] == {"a": 1, "b": "two"}


def test_extra_keys_pass_through(tmp_path: Path) -> None:
    path = _write(tmp_path, "UNDECLARED: keep\n")
    assert YamlSettingsSource(_Settings, path)()["UNDECLARED"] == "keep"


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(YamlError, match="not found"):
        YamlSettingsSource(_Settings, tmp_path / "nope.yaml")()


def test_missing_file_optional_returns_empty(tmp_path: Path) -> None:
    src = YamlSettingsSource(_Settings, tmp_path / "nope.yaml", optional=True)
    assert src() == {}


def test_empty_file_returns_empty(tmp_path: Path) -> None:
    path = _write(tmp_path, "")
    assert YamlSettingsSource(_Settings, path)() == {}


def test_non_mapping_top_level_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(YamlError, match="must contain a mapping"):
        YamlSettingsSource(_Settings, path)()


def test_malformed_yaml_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, "NAME: [unclosed\n")
    with pytest.raises(YamlError, match="Could not parse"):
        YamlSettingsSource(_Settings, path)()


def test_data_cached(tmp_path: Path) -> None:
    path = _write(tmp_path, "NAME: v1\n")
    src = YamlSettingsSource(_Settings, path)
    src()
    path.write_text("NAME: v2\n", encoding="utf-8")  # change on disk
    assert src()["NAME"] == "v1"  # cached, not re-read
