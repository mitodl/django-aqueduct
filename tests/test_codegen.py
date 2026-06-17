"""Tests for SettingsModelGenerator."""

from __future__ import annotations

import ast

import pytest

from django_aqueduct.codegen.generator import SettingsModelGenerator
from django_aqueduct.discovery.base import DiscoveredField, ValueKind


def _make_field(
    name: str,
    type_annotation: str = "str",
    default: object = "value",
    description: str = "",
    required: bool = False,
    source_module: str = "myapp.settings",
    dev_only: bool = False,
    needs_refinement: bool = False,
    value_kind: ValueKind = ValueKind.STATIC,
) -> DiscoveredField:
    return DiscoveredField(
        name=name,
        type_annotation=type_annotation,
        default=default,
        description=description,
        required=required,
        source_module=source_module,
        dev_only=dev_only,
        needs_refinement=needs_refinement,
        value_kind=value_kind,
    )


@pytest.fixture()
def sample_fields() -> list[DiscoveredField]:
    return [
        _make_field("SITE_NAME", "str", "My App", description="The site name"),
        _make_field("DEBUG", "bool", False),
        _make_field("MAX_CONN", "int", 10, source_module="myapp.settings"),
        _make_field(
            "OPTIONAL",
            "Any",
            None,
            needs_refinement=True,
            source_module="third_party.settings",
        ),
        _make_field(
            "DEV_TOOL",
            "str",
            "tool",
            dev_only=True,
            source_module="third_party.settings",
        ),
    ]


# ------------------------------------------------------------------ #
# Basic structure                                                      #
# ------------------------------------------------------------------ #


def test_output_is_valid_python(sample_fields: list[DiscoveredField]) -> None:
    """Generated output is parseable by ast.parse."""
    output = SettingsModelGenerator(sample_fields).render()
    ast.parse(output)  # raises SyntaxError on invalid Python


def test_contains_class_name(sample_fields: list[DiscoveredField]) -> None:
    """Output contains the AqueductSettings class definition."""
    output = SettingsModelGenerator(sample_fields).render()
    assert "class AqueductSettings(BaseSettings):" in output


def test_contains_extra_allow(sample_fields: list[DiscoveredField]) -> None:
    """model_config uses extra='allow'."""
    output = SettingsModelGenerator(sample_fields).render()
    assert 'extra="allow"' in output


def test_contains_field_import(sample_fields: list[DiscoveredField]) -> None:
    """Output imports Field from pydantic (critical bug regression test)."""
    output = SettingsModelGenerator(sample_fields).render()
    assert "from pydantic import Field" in output


def test_contains_field_names(sample_fields: list[DiscoveredField]) -> None:
    """Every field name appears in the output."""
    output = SettingsModelGenerator(sample_fields).render()
    for field in sample_fields:
        assert field.name in output


def test_contains_field_calls(sample_fields: list[DiscoveredField]) -> None:
    """Each field uses Field(...)."""
    output = SettingsModelGenerator(sample_fields).render()
    assert output.count("Field(") == len(sample_fields)


def test_section_headers(sample_fields: list[DiscoveredField]) -> None:
    """Section headers appear for each source module."""
    output = SettingsModelGenerator(sample_fields).render()
    assert "# ===== myapp.settings =====" in output
    assert "# ===== third_party.settings =====" in output


def test_needs_refinement_comment(sample_fields: list[DiscoveredField]) -> None:
    """Fields with needs_refinement get a TODO comment."""
    output = SettingsModelGenerator(sample_fields).render()
    assert "# TODO: refine type" in output
    assert "OPTIONAL" in output


def test_dev_only_comment(sample_fields: list[DiscoveredField]) -> None:
    """Dev-only fields get a # dev-only comment."""
    output = SettingsModelGenerator(sample_fields).render()
    assert "# dev-only" in output


def test_validator_stub(sample_fields: list[DiscoveredField]) -> None:
    """Output contains the model_validators TODO stub."""
    output = SettingsModelGenerator(sample_fields).render()
    assert "# TODO: add model_validators here" in output


def test_description_in_field(sample_fields: list[DiscoveredField]) -> None:
    """Field descriptions appear in the generated Field() call."""
    output = SettingsModelGenerator(sample_fields).render()
    assert 'description="The site name"' in output


def test_empty_fields() -> None:
    """Generator handles an empty field list without crashing."""
    output = SettingsModelGenerator([]).render()
    ast.parse(output)
    assert "class AqueductSettings(BaseSettings):" in output


def test_mutable_defaults_use_factory() -> None:
    """list and dict defaults use default_factory=lambda: ... syntax."""
    fields = [
        _make_field("MY_LIST", "list[Any]", ["a", "b"]),
        _make_field("MY_DICT", "dict[str, Any]", {"k": "v"}),
    ]
    output = SettingsModelGenerator(fields).render()
    assert "default_factory=lambda:" in output
    ast.parse(output)


# ------------------------------------------------------------------ #
# ValueKind-specific rendering                                         #
# ------------------------------------------------------------------ #


def test_derived_field_emits_none_default() -> None:
    """DERIVED fields render as default=None with a @model_validator comment."""
    fields = [
        _make_field(
            "COMPUTED_URL",
            "Any",
            object(),  # simulates a Derived proxy instance
            needs_refinement=True,
            value_kind=ValueKind.DERIVED,
        )
    ]
    output = SettingsModelGenerator(fields).render()
    assert "default=None" in output
    assert "DERIVED" in output
    assert "model_validator" in output
    ast.parse(output)


def test_callable_field_emits_none_default() -> None:
    """CALLABLE fields render as default=None with a CALLABLE comment."""

    def _hook() -> None:
        pass

    fields = [
        _make_field(
            "WIKI_CAN_ASSIGN",
            "Any",
            _hook,
            needs_refinement=True,
            value_kind=ValueKind.CALLABLE,
        )
    ]
    output = SettingsModelGenerator(fields).render()
    assert "default=None" in output
    assert "CALLABLE DEFAULT" in output
    ast.parse(output)


def test_opaque_tuple_without_class_refs() -> None:
    """OPAQUE tuple with a safe repr uses default=(...) directly."""
    fields = [
        _make_field(
            "PROXY_HEADER",
            "tuple[Any, ...]",
            ("HTTP_X_FORWARDED_PROTO", "https"),
            needs_refinement=True,
            value_kind=ValueKind.OPAQUE,
        )
    ]
    output = SettingsModelGenerator(fields).render()
    # Should render the tuple literal, not fall back to None
    assert "HTTP_X_FORWARDED_PROTO" in output
    ast.parse(output)


def test_opaque_tuple_with_class_refs_falls_back_to_none() -> None:
    """OPAQUE tuple containing class objects falls back to default=None."""
    fields = [
        _make_field(
            "XBLOCK_MIXINS",
            "tuple[Any, ...]",
            (int, str),  # repr → (<class 'int'>, <class 'str'>) → has '<'
            needs_refinement=True,
            value_kind=ValueKind.OPAQUE,
        )
    ]
    output = SettingsModelGenerator(fields).render()
    assert "default=None" in output
    assert "OPAQUE" in output
    ast.parse(output)


def test_path_default_renders_as_pathlib() -> None:
    """pathlib.Path defaults are emitted as pathlib.Path(...) so / operator works."""
    import pathlib

    fields = [
        _make_field(
            "DATA_DIR",
            "pathlib.Path",
            pathlib.Path("/var/data"),
        )
    ]
    output = SettingsModelGenerator(fields).render()
    assert "pathlib.Path('/var/data')" in output
    assert "import pathlib" in output
    ast.parse(output)


def test_set_default_uses_factory() -> None:
    """set defaults use default_factory=lambda: {...} syntax."""
    fields = [
        _make_field(
            "EXCLUDED_FIELDS",
            "set[Any]",
            {"email", "username"},
            needs_refinement=True,
            value_kind=ValueKind.OPAQUE,
        )
    ]
    output = SettingsModelGenerator(fields).render()
    assert "default_factory=lambda:" in output
    ast.parse(output)


# ------------------------------------------------------------------ #
# None-default annotations must be Optional                            #
# ------------------------------------------------------------------ #


def test_opaque_dict_with_class_refs_is_optional() -> None:
    """An OPAQUE dict that falls back to default=None gets a `| None` annotation.

    Regression test for the JWT_AUTH / CELERYBEAT_SCHEDULE boot crash: the
    dict default contained a non-serialisable element, so the generator fell
    back to default=None but previously kept the non-nullable dict[str, Any]
    annotation, which rejected a None value at instantiation.
    """
    fields = [
        _make_field(
            "JWT_AUTH",
            "dict[str, Any]",
            {"JWT_ISSUER": "x", "JWT_AUTH_COOKIE": str},  # class ref → repr has '<'
            value_kind=ValueKind.STATIC,
        )
    ]
    output = SettingsModelGenerator(fields).render()
    assert "default=None" in output
    assert "dict[str, Any] | None" in output
    ast.parse(output)


def test_optional_dict_annotation_not_double_wrapped() -> None:
    """An annotation that already permits None is not widened again."""
    from django_aqueduct.codegen.generator import _nullable_annotation

    assert _nullable_annotation("dict[str, Any]") == "dict[str, Any] | None"
    assert _nullable_annotation("dict[str, Any] | None") == "dict[str, Any] | None"
    assert _nullable_annotation("Any") == "Any"
    assert _nullable_annotation("Optional[str]") == "Optional[str]"


def test_derived_and_callable_annotations_accept_none() -> None:
    """DERIVED/CALLABLE fields (Any) keep an annotation that accepts None."""
    fields = [
        _make_field(
            "COMPUTED",
            "Any",
            object(),
            value_kind=ValueKind.DERIVED,
        ),
    ]
    output = SettingsModelGenerator(fields).render()
    # `Any` already accepts None — must not become the invalid `Any | None`
    # is acceptable too, but Any is preferred; assert it parses and is nullable.
    assert "COMPUTED: Any = Field(default=None)" in output
    ast.parse(output)


# ------------------------------------------------------------------ #
# genson TypedDict enrichment                                          #
# ------------------------------------------------------------------ #


def test_homogeneous_dict_gets_typeddict() -> None:
    """A homogeneous dict-valued field generates a TypedDict definition."""
    fields = [
        _make_field(
            "DATABASES",
            "dict[str, Any]",
            {
                "default": {
                    "ENGINE": "django.db.backends.mysql",
                    "HOST": "127.0.0.1",
                    "NAME": "mydb",
                    "PORT": "3306",
                },
                "replica": {
                    "ENGINE": "django.db.backends.mysql",
                    "HOST": "127.0.0.1",
                    "NAME": "mydb",
                    "PORT": "3307",
                },
            },
        )
    ]
    output = SettingsModelGenerator(fields).render()
    ast.parse(output)
    # TypedDict class should be emitted
    assert "TypedDict" in output
    assert "DatabasesEntry" in output
    # The field annotation should reference the TypedDict
    assert "dict[str, DatabasesEntry]" in output


def test_primitive_dict_gets_typed_annotation() -> None:
    """A dict with uniform primitive values gets dict[str, <type>] annotation."""
    fields = [
        _make_field(
            "CERT_LANGUAGES",
            "dict[str, Any]",
            {"en": "English", "es": "Español"},
        )
    ]
    output = SettingsModelGenerator(fields).render()
    ast.parse(output)
    # Should produce dict[str, str] annotation (no TypedDict needed)
    assert "dict[str, str]" in output


def test_no_typeddict_when_genson_unavailable(mocker: object) -> None:
    """Falls back to dict[str, Any] when genson is not available."""
    import django_aqueduct.codegen.dict_schema as ds

    mocker.patch.object(ds, "_genson_available", return_value=False)  # type: ignore[attr-defined]
    fields = [
        _make_field(
            "DATABASES",
            "dict[str, Any]",
            {"default": {"ENGINE": "django.db.backends.mysql"}},
        )
    ]
    output = SettingsModelGenerator(fields).render()
    ast.parse(output)
    assert "TypedDict" not in output
    assert "DatabasesEntry" not in output
