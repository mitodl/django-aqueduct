"""Tests for SettingsModelGenerator."""

import ast

import pytest

from django_aqueduct.codegen.generator import SettingsModelGenerator
from django_aqueduct.discovery.base import DiscoveredField


def _make_field(
    name: str,
    type_annotation: str = "str",
    default: object = "value",
    description: str = "",
    required: bool = False,
    source_module: str = "myapp.settings",
    dev_only: bool = False,
    needs_refinement: bool = False,
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


def test_output_is_valid_python(sample_fields):
    """Generated output is parseable by ast.parse."""
    output = SettingsModelGenerator(sample_fields).render()
    ast.parse(output)  # raises SyntaxError on invalid Python


def test_contains_class_name(sample_fields):
    """Output contains the AqueductSettings class definition."""
    output = SettingsModelGenerator(sample_fields).render()
    assert "class AqueductSettings(BaseSettings):" in output


def test_contains_extra_allow(sample_fields):
    """model_config uses extra='allow'."""
    output = SettingsModelGenerator(sample_fields).render()
    assert 'extra="allow"' in output


def test_contains_field_names(sample_fields):
    """Every field name appears in the output."""
    output = SettingsModelGenerator(sample_fields).render()
    for field in sample_fields:
        assert field.name in output


def test_contains_field_calls(sample_fields):
    """Each field uses Field(...)."""
    output = SettingsModelGenerator(sample_fields).render()
    # Count Field( occurrences — should equal number of fields
    assert output.count("Field(") == len(sample_fields)


def test_section_headers(sample_fields):
    """Section headers appear for each source module."""
    output = SettingsModelGenerator(sample_fields).render()
    assert "# ===== myapp.settings =====" in output
    assert "# ===== third_party.settings =====" in output


def test_needs_refinement_comment(sample_fields):
    """Fields with needs_refinement get a TODO comment."""
    output = SettingsModelGenerator(sample_fields).render()
    assert "# TODO: refine type" in output
    # The OPTIONAL field specifically
    assert "OPTIONAL" in output


def test_dev_only_comment(sample_fields):
    """Dev-only fields get a # dev-only comment."""
    output = SettingsModelGenerator(sample_fields).render()
    assert "# dev-only" in output


def test_validator_stub(sample_fields):
    """Output contains the model_validators TODO stub."""
    output = SettingsModelGenerator(sample_fields).render()
    assert "# TODO: add model_validators here" in output


def test_description_in_field(sample_fields):
    """Field descriptions appear in the generated Field() call."""
    output = SettingsModelGenerator(sample_fields).render()
    assert 'description="The site name"' in output


def test_empty_fields():
    """Generator handles an empty field list without crashing."""
    output = SettingsModelGenerator([]).render()
    ast.parse(output)
    assert "class AqueductSettings(BaseSettings):" in output


def test_mutable_defaults_use_factory():
    """list and dict defaults use default_factory=lambda: ... syntax."""
    fields = [
        _make_field("MY_LIST", "list[Any]", ["a", "b"]),
        _make_field("MY_DICT", "dict[str, Any]", {"k": "v"}),
    ]
    output = SettingsModelGenerator(fields).render()
    # Both should use default_factory and produce valid Python
    assert "default_factory=lambda:" in output
    ast.parse(output)
