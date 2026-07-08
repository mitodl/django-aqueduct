"""Tests for dependency-surface reconciliation and report rendering."""

from __future__ import annotations

import json

from django_aqueduct.discovery.dependency_surface import SurfaceEntry
from django_aqueduct.discovery.ir import (
    Default,
    Provenance,
    SettingField,
    TypeRef,
)
from django_aqueduct.discovery.surface_report import (
    reconcile,
    render,
    render_json,
    render_table,
)
from django_aqueduct.surface import Setting


def _entry(name: str, default: object, dist: str = "pkg") -> SurfaceEntry:
    return SurfaceEntry(dist, Setting(name, type="str", default=default), "builtin")


def _field(name: str, default: Default, type_: str = "str") -> SettingField:
    return SettingField(
        name=name,
        type=TypeRef(type_),
        default=default,
        provenance=Provenance(source_module="proj.settings"),
    )


def test_unset_with_non_none_default_is_review() -> None:
    (row,) = reconcile([_entry("A", "x")], {})
    assert row.project_status == "unset"
    assert row.project_value == "unset"
    assert row.hint == "REVIEW"
    assert row.package_default == "'x'"


def test_unset_with_none_default_is_dash() -> None:
    (row,) = reconcile([_entry("A", None)], {})
    assert row.project_status == "unset"
    assert row.hint == "-"


def test_required_package_default_renders_and_reviews() -> None:
    entry = SurfaceEntry("pkg", Setting("A", required=True), "builtin")
    (row,) = reconcile([entry], {})
    assert row.package_default == "(required)"
    assert row.hint == "REVIEW"


def test_set_matching_default_is_ok() -> None:
    (row,) = reconcile([_entry("A", "x")], {"A": _field("A", Default.literal_("x"))})
    assert row.project_status == "set"
    assert row.project_value == "'x'"
    assert row.hint == "OK"


def test_overridden_when_project_literal_differs() -> None:
    (row,) = reconcile([_entry("A", "x")], {"A": _field("A", Default.literal_("y"))})
    assert row.project_status == "overridden"
    assert row.project_value == "'y'"
    assert row.hint == "OK"


def test_non_literal_project_value_is_set_not_overridden() -> None:
    (row,) = reconcile([_entry("A", "x")], {"A": _field("A", Default.required())})
    assert row.project_status == "set"
    assert row.project_value == "(required)"


def test_secret_name_is_redacted_everywhere() -> None:
    entry = _entry("MY_SECRET_KEY", "hunter2")
    (row,) = reconcile(
        [entry], {"MY_SECRET_KEY": _field("MY_SECRET_KEY", Default.literal_("real"))}
    )
    assert row.package_default == "(redacted)"
    assert row.project_value == "(redacted)"
    assert row.hint == "SECRET"
    assert "hunter2" not in render_table([row])
    assert "real" not in render_json([row])


def _golden_entries() -> list[SurfaceEntry]:
    return [
        _entry("AWS_S3_FILE_OVERWRITE", True, dist="django-storages"),
        _entry("DEFAULT_THROTTLE_RATES", None, dist="djangorestframework"),
    ]


def _golden_fields() -> dict[str, SettingField]:
    return {
        "AWS_S3_FILE_OVERWRITE": _field(
            "AWS_S3_FILE_OVERWRITE", Default.literal_(False), "bool"
        )
    }


def test_golden_table() -> None:
    rows = reconcile(_golden_entries(), _golden_fields())
    expected = (
        "PACKAGE              SETTING                 TYPE  DEFAULT  PROJECT            HINT\n"  # noqa: E501
        "django-storages      AWS_S3_FILE_OVERWRITE   str   True     overridden: False  OK\n"  # noqa: E501
        "djangorestframework  DEFAULT_THROTTLE_RATES  str   None     unset              -\n"  # noqa: E501
    )
    assert render_table(rows) == expected


def test_golden_json() -> None:
    rows = reconcile(_golden_entries(), _golden_fields())
    data = json.loads(render_json(rows))
    assert data == [
        {
            "package": "django-storages",
            "setting": "AWS_S3_FILE_OVERWRITE",
            "type": "str",
            "package_default": "True",
            "project_status": "overridden",
            "project_value": "False",
            "hint": "OK",
        },
        {
            "package": "djangorestframework",
            "setting": "DEFAULT_THROTTLE_RATES",
            "type": "str",
            "package_default": "None",
            "project_status": "unset",
            "project_value": "unset",
            "hint": "-",
        },
    ]


def test_render_determinism() -> None:
    rows = reconcile(_golden_entries(), _golden_fields())
    for fmt in ("table", "json", "markdown"):
        assert render(rows, fmt) == render(rows, fmt)


def test_empty_table() -> None:
    assert "No dependency-surface settings" in render_table([])
