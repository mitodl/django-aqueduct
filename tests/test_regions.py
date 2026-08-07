"""Tests for codegen.regions — managed-region merge and drift check."""

from __future__ import annotations

import pytest

from django_aqueduct.codegen.regions import (
    RegionError,
    check_drift,
    generated_regions,
    merge,
    overridden_field_names,
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


def test_merge_errors_on_obsolete_region() -> None:
    # existing file has a generated region the generator no longer produces
    existing = _doc("    A: int = 1").replace(
        "# <<< aqueduct:generated:fields\n",
        "# <<< aqueduct:generated:fields\n"
        "    # >>> aqueduct:generated:stale\n"
        "    OLD = 1\n"
        "    # <<< aqueduct:generated:stale\n",
    )
    regenerated = _doc("    A: int = 1")
    with pytest.raises(RegionError, match="no longer produced"):
        merge(existing, regenerated)


def test_duplicate_marker_raises() -> None:
    text = (
        "# >>> aqueduct:generated:fields\nA = 1\n# <<< aqueduct:generated:fields\n"
        "# >>> aqueduct:generated:fields\nB = 2\n# <<< aqueduct:generated:fields\n"
    )
    with pytest.raises(RegionError, match="Duplicate"):
        generated_regions(text)


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


# --- overridden_field_names (feeds ModelRenderer(overridden=...)) ------------


def test_override_in_preserved_region_is_detected() -> None:
    doc = _doc(
        "    A: int = Field(default=1)\n    B: str = Field(default='x')",
        preserved_body="    A: PositiveInt = Field(default=5)",
    )
    assert overridden_field_names(doc) == {"A"}


def test_generated_declarations_are_not_overrides() -> None:
    assert overridden_field_names(_doc("    A: int = Field(default=1)")) == set()


def test_free_form_class_body_declaration_counts_as_an_override() -> None:
    """An override need not sit in a preserved region — anywhere outside counts."""
    doc = (
        _doc("    A: int = Field(default=1)").rstrip("\n")
        + "\n    A: PositiveInt = Field(default=5)\n"
    )
    assert overridden_field_names(doc) == {"A"}


def test_plain_assignment_override_is_detected() -> None:
    doc = _doc("    A: int = Field(default=1)", preserved_body="    A = 5")
    assert overridden_field_names(doc) == {"A"}


def test_same_name_in_an_unrelated_class_is_not_an_override() -> None:
    doc = (
        _doc("    A: int = Field(default=1)").rstrip("\n")
        + "\n\n\nclass Helper:\n    A: int = 9\n"
    )
    assert overridden_field_names(doc) == set()


def test_unparseable_file_reports_no_overrides() -> None:
    doc = (
        _doc("    A: int = Field(default=1)", preserved_body="    A: int = 5")
        + "\ndef broken(:\n"
    )
    assert overridden_field_names(doc) == set()


def test_file_without_a_fields_region_reports_no_overrides() -> None:
    assert overridden_field_names("class S:\n    A: int = 1\n") == set()


def test_retired_url_serializers_region_gets_an_actionable_error() -> None:
    """The 0.13.0 migration path: UrlStr needs no serializer, so the region went.

    A file generated by 0.8.0-0.12.0 with --enrich-url-types still has the
    markers; CHANGELOG tells maintainers to delete them or pass --reset, so the
    error has to say exactly that.
    """
    existing = (
        _doc("    URL: str = Field(default='https://x.test')").rstrip("\n")
        + "\n\n    # >>> aqueduct:generated:url_serializers\n"
        "    def _aqueduct_serialize_url_fields(self, value):\n"
        "        return str(value)\n"
        "    # <<< aqueduct:generated:url_serializers\n"
    )
    regenerated = _doc("    URL: UrlStr = Field(default='https://x.test')")

    with pytest.raises(RegionError, match="url_serializers") as exc:
        merge(existing, regenerated)
    assert "--reset" in str(exc.value)
