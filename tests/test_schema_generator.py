"""Tests for codegen.schema_generator — JSON Schema generation."""

from __future__ import annotations

import json

import pytest

from django_aqueduct.codegen.schema_generator import SchemaGenerator
from django_aqueduct.discovery.base import DiscoveredField, ValueKind


def _make_field(
    name: str,
    type_annotation: str = "str",
    default: object = "value",
    description: str = "",
    required: bool = False,
    source_module: str = "myapp.settings",
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
        dev_only=False,
        needs_refinement=needs_refinement,
        value_kind=value_kind,
    )


@pytest.fixture()
def basic_fields() -> list[DiscoveredField]:
    return [
        _make_field("DEBUG", "bool", False, description="Enable debug mode"),
        _make_field("SECRET_KEY", "str", "dev-key", required=True),
        _make_field("ALLOWED_HOSTS", "list[Any]", ["*"]),
        _make_field("SITE_ID", "int", 1),
        _make_field("TIME_ZONE", "str", "UTC"),
    ]


# ------------------------------------------------------------------ #
# Schema structure                                                     #
# ------------------------------------------------------------------ #


def test_generate_returns_dict(basic_fields: list[DiscoveredField]) -> None:
    schema = SchemaGenerator(basic_fields).generate()
    assert isinstance(schema, dict)


def test_schema_version(basic_fields: list[DiscoveredField]) -> None:
    schema = SchemaGenerator(basic_fields).generate()
    assert schema["$schema"] == "http://json-schema.org/draft-07/schema#"


def test_top_level_type_is_object(basic_fields: list[DiscoveredField]) -> None:
    schema = SchemaGenerator(basic_fields).generate()
    assert schema["type"] == "object"


def test_additional_properties_allowed(basic_fields: list[DiscoveredField]) -> None:
    """additionalProperties: true mirrors extra='allow' in BaseSettings."""
    schema = SchemaGenerator(basic_fields).generate()
    assert schema["additionalProperties"] is True


def test_all_fields_in_properties(basic_fields: list[DiscoveredField]) -> None:
    schema = SchemaGenerator(basic_fields).generate()
    for f in basic_fields:
        assert f.name in schema["properties"]


def test_schema_is_json_serialisable(basic_fields: list[DiscoveredField]) -> None:
    """The generated schema must round-trip through json.dumps."""
    schema = SchemaGenerator(basic_fields).generate()
    json.dumps(schema)  # raises if not serialisable


# ------------------------------------------------------------------ #
# Per-field type mapping                                               #
# ------------------------------------------------------------------ #


def test_bool_field_type(basic_fields: list[DiscoveredField]) -> None:
    schema = SchemaGenerator(basic_fields).generate()
    assert schema["properties"]["DEBUG"]["type"] == "boolean"


def test_str_field_type(basic_fields: list[DiscoveredField]) -> None:
    schema = SchemaGenerator(basic_fields).generate()
    assert schema["properties"]["TIME_ZONE"]["type"] == "string"


def test_int_field_type(basic_fields: list[DiscoveredField]) -> None:
    schema = SchemaGenerator(basic_fields).generate()
    assert schema["properties"]["SITE_ID"]["type"] == "integer"


def test_list_field_type(basic_fields: list[DiscoveredField]) -> None:
    schema = SchemaGenerator(basic_fields).generate()
    assert schema["properties"]["ALLOWED_HOSTS"]["type"] == "array"


# ------------------------------------------------------------------ #
# Description and defaults                                             #
# ------------------------------------------------------------------ #


def test_description_included(basic_fields: list[DiscoveredField]) -> None:
    schema = SchemaGenerator(basic_fields).generate()
    assert schema["properties"]["DEBUG"]["description"] == "Enable debug mode"


def test_default_included_for_serialisable(basic_fields: list[DiscoveredField]) -> None:
    schema = SchemaGenerator(basic_fields).generate()
    assert schema["properties"]["DEBUG"]["default"] is False
    assert schema["properties"]["TIME_ZONE"]["default"] == "UTC"


def test_no_description_key_when_empty() -> None:
    fields = [_make_field("FOO", "str", "x", description="")]
    schema = SchemaGenerator(fields).generate()
    assert "description" not in schema["properties"]["FOO"]


# ------------------------------------------------------------------ #
# required                                                             #
# ------------------------------------------------------------------ #


def test_required_field_in_required_array(basic_fields: list[DiscoveredField]) -> None:
    schema = SchemaGenerator(basic_fields).generate()
    assert "SECRET_KEY" in schema["required"]


def test_optional_fields_not_in_required(basic_fields: list[DiscoveredField]) -> None:
    schema = SchemaGenerator(basic_fields).generate()
    required = schema.get("required", [])
    assert "DEBUG" not in required


def test_no_required_key_when_none_required() -> None:
    fields = [_make_field("FOO", "str", "x", required=False)]
    schema = SchemaGenerator(fields).generate()
    assert "required" not in schema


# ------------------------------------------------------------------ #
# ValueKind handling                                                   #
# ------------------------------------------------------------------ #


def test_derived_field_is_unconstrained() -> None:
    """DERIVED fields produce an empty type constraint ({})."""
    fields = [
        _make_field(
            "COMPUTED_URL",
            "Any",
            None,
            value_kind=ValueKind.DERIVED,
        )
    ]
    schema = SchemaGenerator(fields).generate()
    prop = schema["properties"]["COMPUTED_URL"]
    assert "type" not in prop
    assert prop["x-aqueduct-value-kind"] == "derived"


def test_callable_field_is_unconstrained() -> None:
    fields = [
        _make_field(
            "WIKI_CAN_EDIT",
            "Any",
            None,
            value_kind=ValueKind.CALLABLE,
        )
    ]
    schema = SchemaGenerator(fields).generate()
    prop = schema["properties"]["WIKI_CAN_EDIT"]
    assert "type" not in prop
    assert prop["x-aqueduct-value-kind"] == "callable"


def test_derived_field_default_not_included() -> None:
    """DERIVED fields do not get a 'default' key in the schema."""
    fields = [_make_field("LAZY", "Any", None, value_kind=ValueKind.DERIVED)]
    schema = SchemaGenerator(fields).generate()
    assert "default" not in schema["properties"]["LAZY"]


# ------------------------------------------------------------------ #
# x-aqueduct extensions                                               #
# ------------------------------------------------------------------ #


def test_source_extension_present(basic_fields: list[DiscoveredField]) -> None:
    schema = SchemaGenerator(basic_fields).generate()
    for f in basic_fields:
        assert schema["properties"][f.name]["x-aqueduct-source"] == f.source_module


def test_needs_refinement_extension(basic_fields: list[DiscoveredField]) -> None:
    fields = [_make_field("FUZZY", "Any", None, needs_refinement=True)]
    schema = SchemaGenerator(fields).generate()
    assert schema["properties"]["FUZZY"].get("x-aqueduct-needs-refinement") is True


def test_no_needs_refinement_extension_when_false(
    basic_fields: list[DiscoveredField],
) -> None:
    schema = SchemaGenerator(basic_fields).generate()
    assert "x-aqueduct-needs-refinement" not in schema["properties"]["DEBUG"]


# ------------------------------------------------------------------ #
# genson enrichment of dict fields                                     #
# ------------------------------------------------------------------ #


def test_dict_field_gets_genson_schema() -> None:
    """Dict-valued settings receive richer schemas from genson."""
    fields = [
        _make_field(
            "DATABASES",
            "dict[str, Any]",
            {
                "default": {
                    "ENGINE": "django.db.backends.mysql",
                    "HOST": "127.0.0.1",
                    "NAME": "mydb",
                }
            },
        )
    ]
    schema = SchemaGenerator(fields).generate()
    prop = schema["properties"]["DATABASES"]
    # genson should produce a richer schema than just {"type": "object"}
    assert prop.get("type") == "object"
    # genson-derived schema should contain property-level info
    assert "properties" in prop


def test_list_field_gets_genson_schema() -> None:
    """List-valued settings receive richer schemas from genson."""
    fields = [_make_field("ALLOWED_HOSTS", "list[Any]", ["localhost", "127.0.0.1"])]
    schema = SchemaGenerator(fields).generate()
    prop = schema["properties"]["ALLOWED_HOSTS"]
    assert prop.get("type") == "array"
    assert prop.get("items", {}).get("type") == "string"


def test_empty_fields() -> None:
    """Empty field list generates a valid empty schema."""
    schema = SchemaGenerator([]).generate()
    json.dumps(schema)
    assert schema["properties"] == {}
    assert "required" not in schema
