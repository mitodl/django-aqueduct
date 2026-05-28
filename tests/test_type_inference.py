"""Tests for type_inference.infer_annotation."""

import pytest

from django_aqueduct.discovery.type_inference import infer_annotation


class _CustomType:
    """Unrecognised type for testing the fallback branch."""


@pytest.mark.parametrize(
    ("value", "expected_annotation", "expected_refinement"),
    [
        # Primitives
        (True, "bool", False),
        (False, "bool", False),
        (42, "int", False),
        (0, "int", False),
        (3.14, "float", False),
        (0.0, "float", False),
        ("hello", "str", False),
        ("", "str", False),
        # Containers
        ([], "list[Any]", False),
        ([1, 2, 3], "list[Any]", False),
        ({}, "dict[str, Any]", False),
        ({"a": 1}, "dict[str, Any]", False),
        # None → needs refinement
        (None, "Any", True),
        # Unrecognised types → needs refinement
        ({1, 2, 3}, "Any", True),
        (_CustomType(), "Any", True),
    ],
)
def test_infer_annotation(
    value: object,
    expected_annotation: str,
    expected_refinement: bool,
) -> None:
    """infer_annotation returns correct annotation and refinement flag."""
    annotation, needs_refinement = infer_annotation(value)
    assert annotation == expected_annotation
    assert needs_refinement is expected_refinement


def test_bool_before_int():
    """bool is correctly distinguished from int (bool is a subclass of int)."""
    annotation, needs_refinement = infer_annotation(True)
    assert annotation == "bool"
    assert needs_refinement is False

    annotation, needs_refinement = infer_annotation(1)
    assert annotation == "int"
    assert needs_refinement is False
