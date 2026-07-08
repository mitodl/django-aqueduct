"""Tests for the report_settings_surface management command."""

from __future__ import annotations

import io
import json

import pytest
from django.core.management import call_command

from django_aqueduct import config as config_mod


def _run(**kwargs: object) -> str:
    out = io.StringIO()
    call_command("report_settings_surface", stdout=out, **kwargs)
    return out.getvalue()


def test_table_reconciles_project_settings() -> None:
    out = _run(modules="testapp.settings")
    # testapp sets DATABASES (Django default {} -> overridden) and USE_TZ.
    assert "DATABASES" in out
    assert "overridden" in out
    # SECRET_KEY is redacted, never leaking the testapp secret.
    assert "SECRET" in out
    assert "testapp-insecure-secret-key" not in out


def test_json_format_is_valid_and_deterministic() -> None:
    first = _run(modules="testapp.settings", format="json")
    second = _run(modules="testapp.settings", format="json")
    assert first == second
    data = json.loads(first)
    assert isinstance(data, list)
    keys = [(r["package"], r["setting"]) for r in data]
    assert keys == sorted(keys)


def test_markdown_format_has_table_header() -> None:
    out = _run(modules="testapp.settings", format="markdown")
    assert out.startswith("| PACKAGE | SETTING | TYPE |")


def test_packages_flag_restricts(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run(modules="testapp.settings", format="json", packages="does-not-exist")
    assert json.loads(out) == []


def test_config_defaults_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda *a, **k: config_mod.AqueductConfig(
            modules=["testapp.settings"],
            dependency_surface_report_format="json",
        ),
    )
    out = _run()
    json.loads(out)  # config-selected json format applied without --format


def test_secret_setting_value_never_emitted() -> None:
    out = _run(modules="testapp.settings", format="json")
    data = json.loads(out)
    secret = next(r for r in data if r["setting"] == "SECRET_KEY")
    assert secret["hint"] == "SECRET"
    assert secret["project_value"] == "(redacted)"
