"""Renderer-side tests for the enrichment surface: dict_enrichment overlay,
Constraints rendering, and the RUNTIME_ONLY default strategy."""

from __future__ import annotations

import ast

from django_aqueduct.codegen.dict_schema import TypedDictDef, TypedDictField
from django_aqueduct.codegen.renderer import ModelRenderer
from django_aqueduct.discovery.ir import (
    Constraints,
    Default,
    ImportSpec,
    Provenance,
    SettingField,
    TypeRef,
)


def _field(name, base="Any", **kwargs) -> SettingField:
    return SettingField(
        name=name,
        type=TypeRef(base),
        default=kwargs.pop("default", Default.literal_(None)),
        provenance=Provenance(source_module="m"),
        **kwargs,
    )


def test_dict_enrichment_overlay_takes_precedence():
    """A field whose default was never a literal dict still gets its TypedDict."""
    f = _field("DATABASES", base="Any", default=Default.derived())
    td = TypedDictDef(
        class_name="DatabasesEntry",
        fields=[TypedDictField(name="ENGINE", annotation="str", required=True)],
    )
    src = ModelRenderer(
        [f],
        dict_enrichment={"DATABASES": ("dict[str, DatabasesEntry]", [td])},
    ).render()
    # dict-typed → also gets NoDecode + a container_decoders validator (the
    # renderer treats an overlay-provided dict type the same as any other).
    assert "DATABASES: Annotated[dict[str, DatabasesEntry], NoDecode]" in src
    assert "class DatabasesEntry(TypedDict, total=False):" in src
    ast.parse(src)


def test_dict_enrichment_overlay_works_without_genson(mocker):
    """The externally-computed overlay doesn't need genson at render time.

    The overlay was already computed upstream (e.g. by discovery.runtime),
    so genson reporting itself unavailable must not affect a field the
    overlay already covers — the renderer's own genson-dependent literal-
    default path never even runs for that field (see ``_enrich_dicts``:
    fields already in ``enriched`` are skipped before it's consulted).
    """
    import django_aqueduct.codegen.dict_schema as dict_schema_mod

    mocker.patch.object(dict_schema_mod, "_genson_available", return_value=False)

    f = _field("DATABASES", base="Any")
    td = TypedDictDef(class_name="DatabasesEntry", fields=[])
    src = ModelRenderer(
        [f], dict_enrichment={"DATABASES": ("dict[str, DatabasesEntry]", [td])}
    ).render()
    assert "DATABASES: Annotated[dict[str, DatabasesEntry], NoDecode]" in src


def test_literal_type_renders_with_import():
    f = SettingField(
        name="ENVIRONMENT",
        type=TypeRef(
            "Literal['dev', 'staging']",
            imports=frozenset({ImportSpec("typing", "Literal")}),
            needs_refinement=True,
        ),
        default=Default.literal_(None),
        provenance=Provenance(source_module="m"),
    )
    src = ModelRenderer([f]).render()
    assert (
        "from typing import" in src
        and "Literal" in src.split("from typing import")[1].splitlines()[0]
    )
    assert "ENVIRONMENT: Literal['dev', 'staging']" in src
    assert "# refine type" in src
    # The tag word is what ruff's TD002/TD003/FIX002 fire on.
    assert "TODO" not in src
    ast.parse(src)


def test_anyurl_type_renders_with_import():
    f = SettingField(
        name="API_BASE_URL",
        type=TypeRef("AnyUrl", imports=frozenset({ImportSpec("pydantic", "AnyUrl")})),
        default=Default.literal_(None),
        provenance=Provenance(source_module="m"),
    )
    src = ModelRenderer([f]).render()
    assert "from pydantic import AnyUrl" in src
    assert "API_BASE_URL: AnyUrl" in src
    ast.parse(src)


def test_anyurl_field_gets_str_serializer():
    """An AnyUrl-typed field is paired with a field_serializer dumping str.

    Without this, model_dump() would emit a pydantic.Url object instead of
    str for a field promoted by apply_url_type_hints, breaking parity with
    the legacy str-typed setting.
    """
    f = SettingField(
        name="API_BASE_URL",
        type=TypeRef("AnyUrl", imports=frozenset({ImportSpec("pydantic", "AnyUrl")})),
        default=Default.literal_(None),
        provenance=Provenance(source_module="m"),
    )
    src = ModelRenderer([f]).render()
    assert "from pydantic import AnyUrl, Field, field_serializer" in src
    assert '@field_serializer("API_BASE_URL", when_used="always")' in src
    assert "def _aqueduct_serialize_url_fields(self, value: object) -> object:" in src
    assert "# >>> aqueduct:generated:url_serializers" in src
    ast.parse(src)


def test_non_url_field_has_no_serializer_region():
    f = _field("SITE_NAME", base="str", default=Default.literal_("My App"))
    src = ModelRenderer([f]).render()
    assert "field_serializer" not in src
    assert "url_serializers" not in src


def test_constraints_render_as_field_kwargs():
    f = _field(
        "TIMEOUT",
        base="int",
        default=Default.literal_(30),
        constraints=Constraints(gt=0, le=3600),
    )
    src = ModelRenderer([f]).render()
    assert "gt=0" in src
    assert "le=3600" in src
    assert "usage-mined bound(s)" in src
    ast.parse(src)


def test_constraints_absent_by_default():
    f = _field("TIMEOUT", base="int", default=Default.literal_(30))
    src = ModelRenderer([f]).render()
    assert "gt=" not in src
    assert "usage-mined" not in src


def test_runtime_only_default_renders_none_with_comment():
    f = _field("MYSTERY", base="Any", default=Default.runtime_only())
    src = ModelRenderer([f]).render()
    assert "MYSTERY: Any | None = Field(default=None)" not in src  # Any stays Any
    assert "MYSTERY:" in src
    assert "RUNTIME-ONLY" in src
    ast.parse(src)


def test_runtime_only_widens_concrete_type_to_optional():
    f = _field("MYSTERY", base="str", default=Default.runtime_only())
    src = ModelRenderer([f]).render()
    assert "MYSTERY: str | None = Field(\n        default=None\n    )" in src
