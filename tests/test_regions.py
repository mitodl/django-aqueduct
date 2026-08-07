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


def _exec_module(source: str, name: str):
    """Import *source* as a module so pydantic actually builds the model class."""
    import sys
    import types

    module = types.ModuleType(name)
    module.__dict__["__name__"] = name
    exec(compile(source, f"<{name}>", "exec"), module.__dict__)  # noqa: S102
    sys.modules[name] = module
    return module


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


def test_plain_assignment_is_not_treated_as_an_override() -> None:
    """A bare `A = 5` borrows its annotation from the generated declaration.

    Suppressing that declaration would leave an unannotated class attribute,
    which pydantic v2 refuses to build a model from — a hard import failure,
    strictly worse than the duplicate-field lint finding being removed.
    """
    doc = _doc("    A: int = Field(default=1)", preserved_body="    A = 5")
    assert overridden_field_names(doc) == set()


def test_model_with_a_plain_assignment_override_still_builds() -> None:
    """The above, proven against pydantic rather than asserted about it."""
    from django_aqueduct.codegen.renderer import ModelRenderer
    from django_aqueduct.discovery.ir import (
        Default,
        Provenance,
        SettingField,
        TypeRef,
    )

    fields = [
        SettingField(
            name="POOL_SIZE",
            type=TypeRef("int"),
            default=Default.literal_(1),
            provenance=Provenance(source_module="m"),
        )
    ]
    rendered = ModelRenderer(fields).render()
    existing = rendered.replace(
        "    # Add @model_validator / @field_validator methods here.",
        "    POOL_SIZE = 5",
    )

    names = overridden_field_names(existing)
    assert names == set(), "a plain assignment must not suppress the declaration"

    merged = merge(existing, ModelRenderer(fields, overridden=names).render())
    assert "POOL_SIZE: int = Field(default=1)" in merged
    assert "POOL_SIZE = 5" in merged

    # The real assertion: the merged module imports and the override wins.
    module = _exec_module(merged, "plain_assign_model")
    assert module.AqueductSettings().POOL_SIZE == 5


def test_annotated_override_suppresses_and_the_model_still_builds() -> None:
    """The documented pattern: one annotated declaration, model builds, value wins."""
    from django_aqueduct.codegen.renderer import ModelRenderer
    from django_aqueduct.discovery.ir import (
        Default,
        Provenance,
        SettingField,
        TypeRef,
    )

    fields = [
        SettingField(
            name="POOL_SIZE",
            type=TypeRef("int"),
            default=Default.literal_(1),
            provenance=Provenance(source_module="m"),
        )
    ]
    rendered = ModelRenderer(fields).render()
    existing = rendered.replace(
        "    # Add @model_validator / @field_validator methods here.",
        "    POOL_SIZE: int = Field(default=5)",
    )

    names = overridden_field_names(existing)
    assert names == {"POOL_SIZE"}

    merged = merge(existing, ModelRenderer(fields, overridden=names).render())
    assert merged.count("POOL_SIZE:") == 1

    module = _exec_module(merged, "annotated_override_model")
    assert module.AqueductSettings().POOL_SIZE == 5


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


def test_override_before_the_fields_region_with_no_trailing_statements() -> None:
    """The class is found from the region's opening marker, not its closing one.

    `ClassDef.end_lineno` stops at the last *statement*, and every region marker
    is a comment. With the override above the fields region and nothing after it
    but comment-only regions — which is what the renderer emits for a model with
    no container decoders or URL serializers — `end_lineno` lands on the last
    generated field, before the closing marker. Keying off the closing marker
    rejected the settings class here and let regeneration re-emit the duplicate.
    """
    doc = (
        "class S(BaseSettings):\n"
        '    model_config = SettingsConfigDict(env_prefix="")\n'
        "\n"
        "    POOL_SIZE: int = Field(default=10)\n"
        "\n"
        "    # >>> aqueduct:generated:fields\n"
        "    POOL_SIZE: Any = Field(default=None)\n"
        '    SITE_NAME: str = Field(default="x")\n'
        "    # <<< aqueduct:generated:fields\n"
        "\n"
        "    # >>> aqueduct:preserved:validators\n"
        "    # Add @model_validator / @field_validator methods here.\n"
        "    # <<< aqueduct:preserved:validators\n"
    )
    assert "POOL_SIZE" in overridden_field_names(doc)
    # The generated declaration itself is still not an override.
    assert "SITE_NAME" not in overridden_field_names(doc)


def test_override_before_fields_region_is_dropped_end_to_end() -> None:
    """The above, through merge(): exactly one class-level POOL_SIZE survives."""
    from django_aqueduct.codegen.renderer import ModelRenderer
    from django_aqueduct.discovery.ir import (
        Default,
        Provenance,
        SettingField,
        TypeRef,
    )

    fields = [
        SettingField(
            name=name,
            type=TypeRef("int"),
            default=Default.literal_(1),
            provenance=Provenance(source_module="m"),
        )
        for name in ("POOL_SIZE", "SITE_NAME")
    ]
    first = ModelRenderer(fields).render()
    # Hand-place the override above the fields region.
    existing = first.replace(
        "    # >>> aqueduct:generated:fields",
        "    POOL_SIZE: int = Field(default=10)\n\n    # >>> aqueduct:generated:fields",
    )

    names = overridden_field_names(existing)
    assert "POOL_SIZE" in names

    merged = merge(existing, ModelRenderer(fields, overridden=names).render())
    assert merged.count("POOL_SIZE:") == 1
    assert "POOL_SIZE: int = Field(default=10)" in merged
    assert "SITE_NAME: int = Field(default=1)" in merged


def test_helper_class_above_the_region_is_not_mistaken_for_the_model() -> None:
    """The innermost qualifying class wins, not merely the nearest-preceding one."""
    doc = (
        "class S(BaseSettings):\n"
        "    POOL_SIZE: int = Field(default=10)\n"
        "\n"
        "    class Helper:\n"
        "        SITE_NAME: str = 'nested'\n"
        "\n"
        "    # >>> aqueduct:generated:fields\n"
        "    POOL_SIZE: Any = Field(default=None)\n"
        "    # <<< aqueduct:generated:fields\n"
    )
    names = overridden_field_names(doc)
    assert "POOL_SIZE" in names
    # Helper closes before the region, so its members are not overrides.
    assert "SITE_NAME" not in names
