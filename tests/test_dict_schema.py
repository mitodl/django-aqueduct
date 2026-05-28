"""Tests for codegen.dict_schema — genson-powered dict enrichment."""

from __future__ import annotations

import pytest

from django_aqueduct.codegen.dict_schema import (
    TypedDictDef,
    _is_homogeneous_dict,
    _is_json_serialisable,
    _json_schema_type_to_annotation,
    _to_pascal_case,
    enrich_dict_annotation,
)

# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def test_to_pascal_case_simple() -> None:
    assert _to_pascal_case("DATABASES") == "Databases"


def test_to_pascal_case_compound() -> None:
    assert _to_pascal_case("COURSE_ENROLLMENT_MODES") == "CourseEnrollmentModes"


def test_to_pascal_case_single_char() -> None:
    assert _to_pascal_case("A") == "A"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"a": 1}, True),
        ([1, 2, 3], True),
        ("hello", True),
        (None, True),
        (42, True),
        # non-serialisable
        ({1, 2}, False),
        # tuples ARE JSON-serialisable (as arrays) in Python's json module
        ((1, 2), True),
    ],
)
def test_is_json_serialisable(value: object, expected: bool) -> None:
    assert _is_json_serialisable(value) is expected


# ------------------------------------------------------------------ #
# JSON Schema → annotation conversion                                  #
# ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"type": "string"}, "str"),
        ({"type": "integer"}, "int"),
        ({"type": "number"}, "float"),
        ({"type": "boolean"}, "bool"),
        ({"type": "null"}, "None"),
        ({"type": "object"}, "dict[str, Any]"),
        ({"type": "array"}, "list[Any]"),
        ({"type": "array", "items": {"type": "string"}}, "list[str]"),
        ({"anyOf": [{"type": "string"}, {"type": "integer"}]}, "str | int"),
        (
            {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "str | None",
        ),
        ({}, "Any"),
    ],
)
def test_json_schema_type_to_annotation(
    schema: dict[str, object], expected: str
) -> None:
    assert _json_schema_type_to_annotation(schema) == expected


def test_anyof_deduplicates() -> None:
    """anyOf with repeated types produces a clean annotation."""
    schema = {"anyOf": [{"type": "string"}, {"type": "string"}]}
    assert _json_schema_type_to_annotation(schema) == "str"


# ------------------------------------------------------------------ #
# Homogeneous dict detection                                           #
# ------------------------------------------------------------------ #


def test_homogeneous_dict_same_keys() -> None:
    value = {
        "a": {"id": 1, "name": "foo"},
        "b": {"id": 2, "name": "bar"},
    }
    assert _is_homogeneous_dict(value) is True


def test_homogeneous_dict_different_keys() -> None:
    value = {
        "a": {"id": 1, "name": "foo"},
        "b": {"id": 2, "slug": "bar"},  # different key set
    }
    assert _is_homogeneous_dict(value) is False


def test_homogeneous_dict_non_dict_values() -> None:
    value = {"a": "string", "b": "string"}
    # Values are not dicts → not homogeneous-of-structs
    assert _is_homogeneous_dict(value) is False


def test_homogeneous_dict_empty() -> None:
    assert _is_homogeneous_dict({}) is False


def test_homogeneous_dict_single_entry() -> None:
    value = {"only": {"x": 1}}
    assert _is_homogeneous_dict(value) is True


# ------------------------------------------------------------------ #
# enrich_dict_annotation — end-to-end                                  #
# ------------------------------------------------------------------ #


def test_enrich_homogeneous_struct_dict() -> None:
    """Homogeneous dict of structs produces a TypedDictDef."""
    value = {
        "audit": {"id": 1, "slug": "audit", "min_price": 0},
        "verified": {"id": 2, "slug": "verified", "min_price": 1},
    }
    annotation, defs = enrich_dict_annotation("COURSE_MODES", value)
    assert annotation == "dict[str, CourseModesEntry]"
    assert len(defs) == 1
    td = defs[0]
    assert td.class_name == "CourseModesEntry"
    assert isinstance(td, TypedDictDef)
    field_names = {f.name for f in td.fields}
    assert field_names >= {"id", "slug", "min_price"}
    # id and min_price should be integers
    id_field = next(f for f in td.fields if f.name == "id")
    assert id_field.annotation == "int"


def test_enrich_homogeneous_primitive_dict() -> None:
    """Dict with uniform str values produces dict[str, str] (no TypedDict)."""
    value = {"en": "English", "es": "Español", "fr": "Français"}
    annotation, defs = enrich_dict_annotation("CERT_LANGUAGES", value)
    assert annotation == "dict[str, str]"
    assert defs == []


def test_enrich_empty_dict_falls_back() -> None:
    """Empty dict falls back to dict[str, Any]."""
    annotation, defs = enrich_dict_annotation("MY_DICT", {})
    assert annotation == "dict[str, Any]"
    assert defs == []


def test_enrich_non_serialisable_falls_back() -> None:
    """Dict with non-JSON-serialisable values falls back gracefully."""
    annotation, defs = enrich_dict_annotation("MY_DICT", {"key": {1, 2, 3}})  # type: ignore[dict-item]
    assert annotation == "dict[str, Any]"
    assert defs == []


def test_enrich_without_genson(mocker: object) -> None:
    """Falls back to dict[str, Any] when genson is not installed."""
    import django_aqueduct.codegen.dict_schema as ds

    mocker.patch.object(ds, "_genson_available", return_value=False)  # type: ignore[attr-defined]
    annotation, defs = enrich_dict_annotation("MY_DICT", {"a": {"x": 1}})
    assert annotation == "dict[str, Any]"
    assert defs == []


def test_typeddict_field_annotations() -> None:
    """TypedDictFields carry the correct annotations."""
    value = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "HOST": "127.0.0.1",
            "PORT": "3306",
            "ATOMIC_REQUESTS": True,
            "CONN_MAX_AGE": 0,
        },
        "replica": {
            "ENGINE": "django.db.backends.mysql",
            "HOST": "127.0.0.2",
            "PORT": "3306",
            "ATOMIC_REQUESTS": False,
            "CONN_MAX_AGE": 0,
        },
    }
    annotation, defs = enrich_dict_annotation("DATABASES", value)
    assert annotation == "dict[str, DatabasesEntry]"
    assert defs[0].class_name == "DatabasesEntry"
    by_name = {f.name: f for f in defs[0].fields}
    assert by_name["ENGINE"].annotation == "str"
    assert by_name["ATOMIC_REQUESTS"].annotation == "bool"
    assert by_name["CONN_MAX_AGE"].annotation == "int"
