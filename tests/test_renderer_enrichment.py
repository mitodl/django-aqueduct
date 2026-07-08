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
    assert "DATABASES: dict[str, DatabasesEntry]" in src
    assert "class DatabasesEntry(TypedDict, total=False):" in src
    ast.parse(src)


def test_dict_enrichment_overlay_works_without_genson(mocker):
    """The externally-computed overlay doesn't need genson importable at render time."""
    import django_aqueduct.codegen.renderer as renderer_mod

    f = _field("DATABASES", base="Any")
    td = TypedDictDef(class_name="DatabasesEntry", fields=[])

    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "django_aqueduct.codegen.dict_schema":
            raise ImportError("simulated: genson not installed")
        return real_import(name, *args, **kwargs)

    mocker.patch("builtins.__import__", side_effect=fake_import)
    src = ModelRenderer(
        [f], dict_enrichment={"DATABASES": ("dict[str, DatabasesEntry]", [td])}
    ).render()
    assert "DATABASES: dict[str, DatabasesEntry]" in src
    del renderer_mod  # imported only to document the module under test


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
    assert "# TODO: refine type" in src
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
    assert "MYSTERY: str | None = Field(default=None)" in src
