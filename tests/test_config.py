"""Tests for the [tool.aqueduct] pyproject config loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from django_aqueduct.config import ConfigError, find_pyproject, load_config


def _write_pyproject(tmp_path: Path, body: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(body, encoding="utf-8")
    return tmp_path


def test_load_full_config(tmp_path: Path) -> None:
    root = _write_pyproject(
        tmp_path,
        """
[tool.aqueduct]
modules = ["a.settings", "a.settings.prod"]
output = "src/a/settings_model.py"
include_envparser = true
attribute_packages = true
class_name = "Settings"
format = "jsonschema"
""",
    )
    cfg = load_config(root)
    assert cfg.modules == ["a.settings", "a.settings.prod"]
    assert cfg.output == "src/a/settings_model.py"
    assert cfg.include_envparser is True
    assert cfg.attribute_packages is True
    assert cfg.class_name == "Settings"
    assert cfg.output_format == "jsonschema"


def test_defaults_when_no_table(tmp_path: Path) -> None:
    root = _write_pyproject(tmp_path, "[project]\nname = 'x'\n")
    cfg = load_config(root)
    assert cfg.modules == []
    assert cfg.output is None
    assert cfg.include_envparser is None
    assert cfg.attribute_packages is False
    assert cfg.class_name == "AqueductSettings"


def test_missing_pyproject_returns_defaults(tmp_path: Path) -> None:
    # tmp_path has no pyproject.toml
    cfg = load_config(tmp_path / "nonexistent")
    assert cfg.modules == []


def test_invalid_format_raises(tmp_path: Path) -> None:
    root = _write_pyproject(tmp_path, "[tool.aqueduct]\nformat = 'yaml'\n")
    with pytest.raises(ConfigError, match="format='yaml' is invalid"):
        load_config(root)


def test_malformed_toml_raises_config_error(tmp_path: Path) -> None:
    root = _write_pyproject(tmp_path, "[tool.aqueduct]\nmodules = [oops\n")
    with pytest.raises(ConfigError, match="Could not read"):
        load_config(root)


def test_find_pyproject_walks_up(tmp_path: Path) -> None:
    _write_pyproject(tmp_path, "[tool.aqueduct]\nmodules = ['x']\n")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    found = find_pyproject(nested)
    assert found == tmp_path / "pyproject.toml"


def test_extra_config_loaded(tmp_path: Path) -> None:
    root = _write_pyproject(tmp_path, "[tool.aqueduct]\nextra = 'forbid'\n")
    assert load_config(root).extra == "forbid"


def test_invalid_extra_raises(tmp_path: Path) -> None:
    root = _write_pyproject(tmp_path, "[tool.aqueduct]\nextra = 'nope'\n")
    with pytest.raises(ConfigError, match="extra='nope' is invalid"):
        load_config(root)


def test_parity_config_loaded(tmp_path: Path) -> None:
    root = _write_pyproject(
        tmp_path,
        "[tool.aqueduct]\n"
        "parity_model = 'a.m:AqueductSettings'\n"
        "parity_legacy = 'a.settings'\n"
        "parity_ignore = ['SECRET_KEY', 'ENVIRONMENT']\n",
    )
    cfg = load_config(root)
    assert cfg.parity_model == "a.m:AqueductSettings"
    assert cfg.parity_legacy == "a.settings"
    assert cfg.parity_ignore == ["SECRET_KEY", "ENVIRONMENT"]
