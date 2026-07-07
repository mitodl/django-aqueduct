"""Tests for codegen.regions — managed-region merge and drift check."""

from __future__ import annotations

import pytest

from django_aqueduct.codegen.regions import (
    RegionError,
    check_drift,
    generated_regions,
    merge,
)


def _doc(fields_body: str, preserved_body: str = "    # hand-written") -> str:
    return (
        "# header\n"
        "# >>> aqueduct:generated:imports\n"
        "from x import y\n"
        "# <<< aqueduct:generated:imports\n"
        "\n"
        "class S(BaseSettings):\n"
        "    # >>> aqueduct:generated:fields\n"
        f"{fields_body}\n"
        "    # <<< aqueduct:generated:fields\n"
        "\n"
        "    # >>> aqueduct:preserved:validators\n"
        f"{preserved_body}\n"
        "    # <<< aqueduct:preserved:validators\n"
    )


def test_generated_regions_extracts_bodies() -> None:
    regions = generated_regions(_doc("    A: int = 1"))
    assert regions == {"imports": "from x import y", "fields": "    A: int = 1"}


def test_merge_replaces_generated_preserves_hand_written() -> None:
    existing = _doc("    A: int = 1", preserved_body="    def custom(self): ...")
    regenerated = _doc("    A: int = 1\n    B: str = 'x'")
    merged = merge(existing, regenerated)
    # generated region updated
    assert "B: str = 'x'" in merged
    # preserved hand-written code survives
    assert "def custom(self): ..." in merged


def test_merge_preserves_free_form_outside_regions() -> None:
    existing = _doc("    A: int = 1") + "\n# a trailing hand comment\nEXTRA = 1\n"
    regenerated = _doc("    A: int = 2")
    merged = merge(existing, regenerated)
    assert "A: int = 2" in merged
    assert "# a trailing hand comment" in merged
    assert "EXTRA = 1" in merged


def test_merge_errors_when_region_marker_missing() -> None:
    # existing file had its fields markers deleted by hand
    broken = "# header\nclass S:\n    A: int = 1\n"
    regenerated = _doc("    A: int = 1")
    with pytest.raises(RegionError, match="missing"):
        merge(broken, regenerated)


def test_unbalanced_marker_raises() -> None:
    text = "# >>> aqueduct:generated:fields\nA = 1\n"  # never closed
    with pytest.raises(RegionError, match="Unclosed"):
        generated_regions(text)


def test_mismatched_marker_raises() -> None:
    text = "# >>> aqueduct:generated:fields\nA = 1\n# <<< aqueduct:generated:imports\n"
    with pytest.raises(RegionError, match="mismatch"):
        generated_regions(text)


def test_check_drift_in_sync() -> None:
    doc = _doc("    A: int = 1")
    result = check_drift(doc, doc)
    assert result.in_sync is True
    assert result.diff == ""


def test_check_drift_reports_diff() -> None:
    existing = _doc("    A: int = 1")
    regenerated = _doc("    A: int = 2")
    result = check_drift(existing, regenerated)
    assert result.in_sync is False
    assert "A: int = 1" in result.diff
    assert "A: int = 2" in result.diff


def test_check_drift_ignores_preserved_changes() -> None:
    existing = _doc("    A: int = 1", preserved_body="    # v1")
    regenerated = _doc("    A: int = 1", preserved_body="    # v2 IGNORED")
    # only preserved region differs → still in sync
    assert check_drift(existing, regenerated).in_sync is True
