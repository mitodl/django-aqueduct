"""Tests for discovery.runtime — multi-snapshot live-import sampling."""

from __future__ import annotations

import sys

import pytest

from django_aqueduct.discovery.runtime import (
    RuntimeSamplingError,
    parse_env_file,
    sample_module_values,
)


def test_parse_env_file_basic():
    text = "FOO=bar\nBAZ=1\n"
    assert parse_env_file(text) == {"FOO": "bar", "BAZ": "1"}


def test_parse_env_file_ignores_blank_and_comment_lines():
    text = "\n# a comment\nFOO=bar\n\n"
    assert parse_env_file(text) == {"FOO": "bar"}


def test_parse_env_file_strips_matching_quotes():
    text = "FOO=\"bar baz\"\nBAR='x'\nBAZ=\"mismatched'\n"
    parsed = parse_env_file(text)
    assert parsed["FOO"] == "bar baz"
    assert parsed["BAR"] == "x"
    assert parsed["BAZ"] == "\"mismatched'"


def test_parse_env_file_ignores_lines_without_equals():
    assert parse_env_file("not-an-assignment\nFOO=1\n") == {"FOO": "1"}


@pytest.fixture
def env_sensitive_module(tmp_path, monkeypatch):
    """A settings-like module that reads os.environ at import time."""
    path = tmp_path / "env_sensitive_settings.py"
    path.write_text(
        "import os\n"
        "ENVIRONMENT = os.environ.get('APP_ENV', 'dev')\n"
        "TIMEOUT = int(os.environ.get('TIMEOUT', '30'))\n"
        "DATABASES = {'default': {'HOST': os.environ.get('DB_HOST', 'localhost')}}\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    yield "env_sensitive_settings"
    sys.modules.pop("env_sensitive_settings", None)


def test_sample_module_values_reflects_env_overrides(env_sensitive_module):
    samples = sample_module_values(
        [env_sensitive_module],
        [{"APP_ENV": "staging"}, {"APP_ENV": "production"}],
    )
    assert len(samples) == 2
    assert samples[0]["ENVIRONMENT"] == "staging"
    assert samples[1]["ENVIRONMENT"] == "production"


def test_sample_module_values_restores_environ(env_sensitive_module, monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    sample_module_values([env_sensitive_module], [{"APP_ENV": "staging"}])
    import os

    assert "APP_ENV" not in os.environ


def test_sample_module_values_empty_snapshots_samples_nothing(env_sensitive_module):
    assert sample_module_values([env_sensitive_module], []) == []


def test_sample_module_values_later_module_overrides_earlier(tmp_path, monkeypatch):
    (tmp_path / "base_settings.py").write_text("SITE_NAME = 'base'\n")
    (tmp_path / "override_settings.py").write_text("SITE_NAME = 'override'\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        samples = sample_module_values(["base_settings", "override_settings"], [{}])
        assert samples[0]["SITE_NAME"] == "override"
    finally:
        sys.modules.pop("base_settings", None)
        sys.modules.pop("override_settings", None)


def test_sample_module_values_raises_on_import_failure(tmp_path, monkeypatch):
    (tmp_path / "broken_settings.py").write_text("raise RuntimeError('boom')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        with pytest.raises(RuntimeSamplingError, match="boom"):
            sample_module_values(["broken_settings"], [{}])
    finally:
        sys.modules.pop("broken_settings", None)


def test_sample_module_values_forces_fresh_import(tmp_path, monkeypatch):
    """A module cached from a prior test run must not leak a stale value."""
    path = tmp_path / "reimport_settings.py"
    path.write_text("import os\nVALUE = os.environ.get('V', 'a')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        samples = sample_module_values(
            ["reimport_settings"], [{"V": "first"}, {"V": "second"}]
        )
        assert samples[0]["VALUE"] == "first"
        assert samples[1]["VALUE"] == "second"
    finally:
        sys.modules.pop("reimport_settings", None)
