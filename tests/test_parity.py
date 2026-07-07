"""Tests for the parity comparison and check_aqueduct_settings command."""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from django_aqueduct.parity import compare


def test_in_sync() -> None:
    report = compare({"A": 1, "B": "x"}, {"A": 1, "B": "x"})
    assert report.in_sync is True
    assert report.divergences == []


def test_missing_in_each_direction() -> None:
    report = compare({"A": 1, "ONLY_MODEL": 1}, {"A": 1, "ONLY_LEGACY": 2})
    kinds = {(d.name, d.kind) for d in report.divergences}
    assert ("ONLY_MODEL", "missing_in_legacy") in kinds
    assert ("ONLY_LEGACY", "missing_in_model") in kinds


def test_value_divergence() -> None:
    report = compare({"A": 1}, {"A": 2})
    assert [(d.name, d.kind) for d in report.divergences] == [("A", "value")]


def test_type_divergence_bool_vs_int() -> None:
    # bool is a subclass of int; True == 1 must still be flagged as a type diff.
    report = compare({"FLAG": True}, {"FLAG": 1})
    assert [(d.name, d.kind) for d in report.divergences] == [("FLAG", "type")]


def test_ignore_list() -> None:
    report = compare({"A": 1}, {"A": 2}, ignore={"A"})
    assert report.in_sync is True
    assert report.ignored == ["A"]


def test_render_in_sync_and_diverged() -> None:
    assert "in parity" in compare({"A": 1}, {"A": 1}).render()
    assert "value differs" in compare({"A": 1}, {"A": 2}).render()


# ---- management command ----


def test_command_reports_drift() -> None:
    with pytest.raises(CommandError, match="diverge"):
        call_command(
            "check_aqueduct_settings",
            model="parity_model:AqueductSettings",
            legacy="parity_legacy",
        )


def test_command_passes_with_ignore(capsys: pytest.CaptureFixture[str]) -> None:
    call_command(
        "check_aqueduct_settings",
        model="parity_model:AqueductSettings",
        legacy="parity_legacy",
        ignore="ONLY_IN_MODEL,ONLY_IN_LEGACY",
    )
    assert "in parity" in capsys.readouterr().out


def test_command_requires_model_and_legacy() -> None:
    with pytest.raises(CommandError, match="required"):
        call_command("check_aqueduct_settings")


def test_command_bad_model_ref() -> None:
    with pytest.raises(CommandError, match="module.path:ClassName"):
        call_command(
            "check_aqueduct_settings", model="no_colon", legacy="parity_legacy"
        )


def test_list_vs_tuple_not_flagged() -> None:
    # Django uses list/tuple interchangeably; model_dump gives lists.
    report = compare({"HOSTS": ["a", "b"]}, {"HOSTS": ("a", "b")})
    assert report.in_sync is True


def test_nested_list_tuple_normalized() -> None:
    report = compare({"X": {"k": ["a", ("b", "c")]}}, {"X": {"k": ("a", ["b", "c"])}})
    assert report.in_sync is True


def test_list_tuple_value_difference_still_caught() -> None:
    report = compare({"HOSTS": ["a"]}, {"HOSTS": ("b",)})
    assert [(d.name, d.kind) for d in report.divergences] == [("HOSTS", "value")]


def test_command_empty_model_class_rejected() -> None:
    for bad in ("mod:", ":Class"):
        with pytest.raises(CommandError, match="module.path:ClassName"):
            call_command("check_aqueduct_settings", model=bad, legacy="parity_legacy")
