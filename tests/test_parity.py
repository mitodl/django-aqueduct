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


def test_dict_extra_legacy_keys_not_flagged() -> None:
    # Django's ConnectionHandler.ensure_defaults injects ATOMIC_REQUESTS/
    # AUTOCOMMIT/TEST/TIME_ZONE into each DATABASES entry at runtime; the
    # model's raw model_dump() never had a chance to produce them.
    model = {"DATABASES": {"default": {"ENGINE": "postgres", "NAME": "db"}}}
    legacy = {
        "DATABASES": {
            "default": {
                "ENGINE": "postgres",
                "NAME": "db",
                "ATOMIC_REQUESTS": False,
                "AUTOCOMMIT": True,
                "TIME_ZONE": None,
                "TEST": {"NAME": None},
            }
        }
    }
    report = compare(model, legacy)
    assert report.in_sync is True


def test_dict_missing_model_key_still_flagged() -> None:
    # A model key absent from legacy (not runtime augmentation, an actual
    # gap) must still be reported.
    model = {"HEALTH_CHECK": {"SUBSETS": {"default": ["default"]}}}
    legacy = {"HEALTH_CHECK": {"DISK_USAGE_MAX": 90}}
    report = compare(model, legacy)
    assert [(d.name, d.kind) for d in report.divergences] == [("HEALTH_CHECK", "value")]


def test_dict_shared_key_value_mismatch_still_flagged() -> None:
    model = {"DATABASES": {"default": {"ENGINE": "postgres", "NAME": "db"}}}
    legacy = {
        "DATABASES": {"default": {"ENGINE": "postgres", "NAME": "other-db", "TEST": {}}}
    }
    report = compare(model, legacy)
    assert [(d.name, d.kind) for d in report.divergences] == [("DATABASES", "value")]


def test_command_empty_model_class_rejected() -> None:
    for bad in ("mod:", ":Class"):
        with pytest.raises(CommandError, match="module.path:ClassName"):
            call_command("check_aqueduct_settings", model=bad, legacy="parity_legacy")
