"""Renderer-side tests for the enrichment surface: dict_enrichment overlay,
Constraints rendering, and the RUNTIME_ONLY default strategy."""

from __future__ import annotations

import ast

from django_aqueduct.adapter import PLUGIN_MARKER, UNRESOLVED_MARKER
from django_aqueduct.codegen.dict_schema import TypedDictDef, TypedDictField
from django_aqueduct.codegen.renderer import ModelRenderer
from django_aqueduct.discovery.ir import (
    Constraints,
    Default,
    DiscoveryMethod,
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


def _url_field(name: str = "API_BASE_URL") -> SettingField:
    return SettingField(
        name=name,
        type=TypeRef(
            "UrlStr", imports=frozenset({ImportSpec("django_aqueduct", "UrlStr")})
        ),
        default=Default.literal_(None),
        provenance=Provenance(source_module="m"),
    )


def test_url_str_type_renders_with_import():
    src = ModelRenderer([_url_field()]).render()
    assert "from django_aqueduct import UrlStr" in src
    assert "API_BASE_URL: UrlStr" in src
    ast.parse(src)


def test_url_str_field_needs_no_serializer():
    """UrlStr is a str, so nothing has to convert it back on dump.

    The field_serializer the AnyUrl promotion needed only ever intercepted
    serialization; it did nothing for a validator reading the attribute, for a
    URL nested in a dict setting, or for AnyUrl's trailing-slash rewrite. A str
    subtype removes the need for it entirely.
    """
    src = ModelRenderer([_url_field()]).render()
    assert "field_serializer" not in src
    assert "url_serializers" not in src
    assert "AnyUrl" not in src
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
    # The subject is the widened annotation: `default=None` is only valid if the
    # annotation admits None. Asserted apart from the Field body so that adding
    # a kwarg (e.g. the unresolved marker) does not masquerade as a regression
    # in type widening.
    assert "MYSTERY: str | None = Field(" in src
    assert "default=None" in src


def test_only_unresolved_strategies_carry_the_marker():
    """The marker distinguishes a placeholder None from a real None default.

    All four strategies below render ``default=None``, so the rendered *value*
    cannot tell the overlay which names may be left absent. Only DERIVED and
    RUNTIME_ONLY mean "no value was determined"; REDACTED has a static
    assignment whose value is merely withheld, and a literal ``None`` is a
    genuine default.
    """
    marked = {
        name: UNRESOLVED_MARKER
        in ModelRenderer([_field(name, base="Any", default=default)]).render()
        for name, default in (
            ("A_DERIVED", Default.derived()),
            ("A_RUNTIME_ONLY", Default.runtime_only()),
            ("A_REDACTED", Default.redacted()),
            ("A_LITERAL_NONE", Default.literal_(None)),
        )
    }
    assert marked == {
        "A_DERIVED": True,
        "A_RUNTIME_ONLY": True,
        "A_REDACTED": False,
        "A_LITERAL_NONE": False,
    }


def _plugin_field(name, package, **kwargs):
    return SettingField(
        name=name,
        type=TypeRef("Any"),
        default=kwargs.pop("default", Default.literal_(True)),
        provenance=Provenance(
            source_module="m",
            method=DiscoveryMethod.OPENEDX_PLUGIN,
            plugin_distribution=package,
        ),
        owning_package=package,
        **kwargs,
    )


def test_plugin_field_records_the_distribution_that_supplies_it():
    src = ModelRenderer(
        [_plugin_field("MITX_REDIRECT_ENABLED", "openedx-companion-auth")]
    ).render()
    # Quoting is asserted exactly: the key and the value are produced by
    # different code paths, and a mixed pair would ship into generated files.
    assert f'json_schema_extra={{"{PLUGIN_MARKER}": "openedx-companion-auth"}}' in src
    ast.parse(src)


def test_non_plugin_field_records_no_distribution():
    # Attribution alone must not mark a field: owning_package is populated for
    # project settings too, and only a plugin_settings() default is withheld
    # when its supplier is missing.
    f = _field("SITE_NAME", base="str", default=Default.literal_("x"))
    f.owning_package = "some-distribution"
    assert PLUGIN_MARKER not in ModelRenderer([f]).render()


def test_a_derived_plugin_field_carries_both_markers_in_one_dict():
    """Two kwargs would be a TypeError; they must merge into one mapping."""
    src = ModelRenderer(
        [
            _plugin_field(
                "FORUM_SEARCH_BACKEND", "openedx-forum", default=Default.derived()
            )
        ]
    ).render()
    assert src.count("json_schema_extra=") == 1
    assert PLUGIN_MARKER in src
    assert UNRESOLVED_MARKER in src
    ast.parse(src)


def test_marker_survives_package_attribution():
    """The marker must not be read off a field attribution rewrites.

    `--attribute-packages` reassigns every field's ``owning_package`` in
    place, usually to the sentinel ``"project"`` for a plugin setting no rule
    matches. Sourcing the marker from there would name a distribution that can
    never be installed, so the adapter would withhold the default on *every*
    deployment -- including the ones that do install the plugin. A silent
    false negative is the worst failure mode this feature has.
    """
    f = _plugin_field("MITX_REDIRECT_ENABLED", "openedx-companion-auth")
    f.owning_package = "project"  # what attribution does to it

    src = ModelRenderer([f]).render()
    assert f'json_schema_extra={{"{PLUGIN_MARKER}": "openedx-companion-auth"}}' in src
    assert '"project"' not in src
