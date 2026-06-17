"""Tests for type_inference.infer_annotation."""

from __future__ import annotations

import pathlib

import pytest

from django_aqueduct.discovery.base import ValueKind
from django_aqueduct.discovery.type_inference import InferenceResult, infer_annotation


class _CustomType:
    """Unrecognised type for testing the fallback branch."""


def _fn() -> None:
    """Named function for testing CALLABLE detection."""


@pytest.mark.parametrize(
    ("value", "expected_annotation", "expected_refinement", "expected_kind"),
    [
        # ---- primitives ----
        (True, "bool", False, ValueKind.STATIC),
        (False, "bool", False, ValueKind.STATIC),
        (42, "int", False, ValueKind.STATIC),
        (0, "int", False, ValueKind.STATIC),
        (3.14, "float", False, ValueKind.STATIC),
        (0.0, "float", False, ValueKind.STATIC),
        ("hello", "str", False, ValueKind.STATIC),
        ("", "str", False, ValueKind.STATIC),
        # ---- os.PathLike → pathlib.Path (preserves / operator for path joining) ----
        (pathlib.Path("/var/data"), "pathlib.Path", False, ValueKind.STATIC),
        (pathlib.PurePosixPath("/etc/config"), "pathlib.Path", False, ValueKind.STATIC),
        # ---- None → optional sentinel ----
        (None, "Any", True, ValueKind.STATIC),
        # ---- JSON-serialisable containers ----
        ([], "list[Any]", False, ValueKind.STATIC),
        ([1, 2, 3], "list[Any]", False, ValueKind.STATIC),
        ({}, "dict[str, Any]", False, ValueKind.STATIC),
        ({"a": 1}, "dict[str, Any]", False, ValueKind.STATIC),
        # ---- OPAQUE containers ----
        ((1, 2), "tuple[Any, ...]", True, ValueKind.OPAQUE),
        ((), "tuple[Any, ...]", True, ValueKind.OPAQUE),
        (("a", "b"), "tuple[Any, ...]", True, ValueKind.OPAQUE),
        ({1, 2, 3}, "set[Any]", True, ValueKind.OPAQUE),
        (frozenset({1}), "set[Any]", True, ValueKind.OPAQUE),
        # ---- CALLABLE values ----
        (_fn, "Any", True, ValueKind.CALLABLE),
        (lambda: None, "Any", True, ValueKind.CALLABLE),
        (int, "Any", True, ValueKind.CALLABLE),  # class object
        (list, "Any", True, ValueKind.CALLABLE),  # built-in class
        # ---- Unrecognised custom class → OPAQUE ----
        (_CustomType(), "Any", True, ValueKind.OPAQUE),
    ],
)
def test_infer_annotation(
    value: object,
    expected_annotation: str,
    expected_refinement: bool,
    expected_kind: ValueKind,
) -> None:
    """infer_annotation returns correct annotation, refinement flag, and kind."""
    result = infer_annotation(value)
    assert result.annotation == expected_annotation
    assert result.needs_refinement is expected_refinement
    assert result.value_kind is expected_kind


def test_result_is_named_tuple() -> None:
    """InferenceResult is a NamedTuple with the expected fields."""
    result = infer_annotation("hello")
    assert isinstance(result, InferenceResult)
    assert result.annotation == "str"
    assert result.needs_refinement is False
    assert result.value_kind is ValueKind.STATIC


def test_bool_before_int() -> None:
    """bool is correctly distinguished from int (bool is a subclass of int)."""
    result_true = infer_annotation(True)
    assert result_true.annotation == "bool"
    assert result_true.value_kind is ValueKind.STATIC

    result_int = infer_annotation(1)
    assert result_int.annotation == "int"
    assert result_int.value_kind is ValueKind.STATIC


def test_derived_proxy_detection() -> None:
    """Objects whose repr matches '<ClassName object at 0x...>' are DERIVED."""

    class _FakeDerived:
        def __repr__(self) -> str:
            return f"<FakeDerived object at 0x{id(self):x}>"

    result = infer_annotation(_FakeDerived())
    assert result.value_kind is ValueKind.DERIVED
    assert result.annotation == "Any"
    assert result.needs_refinement is True


def test_path_subclass() -> None:
    """os.PathLike values (pathlib.Path, PurePosixPath, etc.) are STATIC."""
    result = infer_annotation(pathlib.PurePosixPath("/etc"))
    assert result.annotation == "pathlib.Path"
    assert result.value_kind is ValueKind.STATIC
