"""Tests for codegen.schema_generator (v2 IR)."""

from __future__ import annotations

import json

import pytest

from django_aqueduct.codegen.schema_generator import SchemaGenerator
from django_aqueduct.discovery.ir import (
    Default,
    Provenance,
    SettingField,
    TypeRef,
)


def _field(
    name: str,
    *,
    type_base: str = "str",
    default: Default | None = None,
    required: bool = False,
    description: str = "",
    owning_package: str = "",
    needs_refinement: bool = False,
    optional: bool = False,
) -> SettingField:
    return SettingField(
        name=name,
        type=TypeRef(type_base, optional=optional, needs_refinement=needs_refinement),
        default=default if default is not None else Default.literal_("x"),
        required=required,
        description=description,
        owning_package=owning_package,
        provenance=Provenance(source_module="myapp.settings"),
    )


@pytest.fixture
def basic_fields() -> list[SettingField]:
    return [
        _field("SITE_NAME", default=Default.literal_("app"), description="the name"),
        _field("MAX_CONN", type_base="int", default=Default.literal_(5)),
        _field("DEBUG", type_base="bool", default=Default.literal_(False)),
        _field("SECRET_KEY", default=Default.required(), required=True),
    ]


def test_generate_returns_valid_json_schema(basic_fields: list[SettingField]) -> None:
    schema = SchemaGenerator(basic_fields).generate()
    json.dumps(schema)  # serialisable
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is True
    assert schema["$schema"].startswith("http://json-schema.org/draft-07")


def test_type_mapping(basic_fields: list[SettingField]) -> None:
    props = SchemaGenerator(basic_fields).generate()["properties"]
    assert props["MAX_CONN"]["type"] == "integer"
    assert props["DEBUG"]["type"] == "boolean"
    assert props["SITE_NAME"]["type"] == "string"


def test_literal_default_and_description_included(
    basic_fields: list[SettingField],
) -> None:
    props = SchemaGenerator(basic_fields).generate()["properties"]
    assert props["SITE_NAME"]["default"] == "app"
    assert props["SITE_NAME"]["description"] == "the name"


def test_required_collected(basic_fields: list[SettingField]) -> None:
    schema = SchemaGenerator(basic_fields).generate()
    assert schema["required"] == ["SECRET_KEY"]


def test_derived_is_unconstrained() -> None:
    f = _field("CACHES", type_base="Any", default=Default.derived(), optional=True)
    prop = SchemaGenerator([f]).generate()["properties"]["CACHES"]
    assert prop.get("type") is None
    assert prop["x-aqueduct-default-strategy"] == "derived"


def test_package_extension_present_only_when_attributed() -> None:
    fields = [
        _field(
            "DATABASES",
            type_base="Any",
            default=Default.derived(),
            optional=True,
            owning_package="django",
        ),
        _field("MY_CUSTOM", default=Default.literal_("x")),
    ]
    props = SchemaGenerator(fields).generate()["properties"]
    assert props["DATABASES"]["x-aqueduct-package"] == "django"
    assert "x-aqueduct-package" not in props["MY_CUSTOM"]


def test_needs_refinement_flagged() -> None:
    f = _field("MYSTERY", type_base="Any", needs_refinement=True)
    prop = SchemaGenerator([f]).generate()["properties"]["MYSTERY"]
    assert prop["x-aqueduct-needs-refinement"] is True
