"""Runtime type inference for settings values."""

from __future__ import annotations

import inspect
import os
from typing import Any, NamedTuple

from django_aqueduct.discovery.base import ValueKind


class InferenceResult(NamedTuple):
    """Result of type inference on a settings value.

    Attributes:
        annotation: Pydantic-compatible type annotation string, e.g.
            ``"str"``, ``"list[Any]"``, ``"dict[str, Any]"``.
        needs_refinement: ``True`` when the annotation is a best-effort
            guess that the developer should review (e.g. ``None``-valued
            or unrecognised type).
        value_kind: Semantic classification that controls how the code
            generator renders the default.
    """

    annotation: str
    needs_refinement: bool
    value_kind: ValueKind


def infer_annotation(value: Any) -> InferenceResult:
    """Infer a Pydantic-compatible type annotation from a runtime value.

    Evaluation order matters:

    1. ``bool`` is checked *before* ``int`` because ``bool`` is a
       subclass of ``int`` in Python.
    2. Primitive ``isinstance`` checks precede callable/class checks so
       that built-in types (e.g. ``int``, ``str``) are never mistakenly
       classified as ``CALLABLE``.
    3. ``None`` is recognised as ``STATIC`` (the field is optional) with
       ``needs_refinement=True`` because the actual type is unknown.
    4. ``tuple`` / ``set`` / ``frozenset`` are ``OPAQUE`` — they are
       Python-native but not JSON-serialisable; the generator may still
       attempt a ``repr()``-based default but will fall back to ``None``
       when the repr contains angle brackets (e.g. class references).
    5. Functions, lambdas, and class objects are ``CALLABLE`` — they
       cannot be stored as Pydantic defaults.
    6. Objects whose ``repr()`` matches the ``"<ClassName object at 0x...>"``
       pattern are treated as ``DERIVED`` lazy proxies.
    7. Everything else falls back to ``OPAQUE``.

    Args:
        value: The runtime settings value to inspect.

    Returns:
        An :class:`InferenceResult` with ``annotation``,
        ``needs_refinement``, and ``value_kind``.
    """
    # ------------------------------------------------------------------ #
    # 1 – Primitives (must precede callable checks)                       #
    # ------------------------------------------------------------------ #
    # bool before int: bool is a subclass of int
    if isinstance(value, bool):
        return InferenceResult("bool", False, ValueKind.STATIC)
    if isinstance(value, int):
        return InferenceResult("int", False, ValueKind.STATIC)
    if isinstance(value, float):
        return InferenceResult("float", False, ValueKind.STATIC)
    if isinstance(value, str):
        return InferenceResult("str", False, ValueKind.STATIC)

    # Any os.PathLike (pathlib.Path, path.Path, etc.) preserves its type so
    # callers can continue using the / operator for path concatenation.
    if isinstance(value, os.PathLike):
        return InferenceResult("pathlib.Path", False, ValueKind.STATIC)

    # None is a valid optional sentinel
    if value is None:
        return InferenceResult("Any", True, ValueKind.STATIC)

    # ------------------------------------------------------------------ #
    # 2 – JSON-serialisable containers                                    #
    # ------------------------------------------------------------------ #
    if isinstance(value, list):
        return InferenceResult("list[Any]", False, ValueKind.STATIC)
    if isinstance(value, dict):
        return InferenceResult("dict[str, Any]", False, ValueKind.STATIC)

    # ------------------------------------------------------------------ #
    # 3 – Python-native containers (not JSON-serialisable)                #
    # ------------------------------------------------------------------ #
    if isinstance(value, tuple):
        return InferenceResult("tuple[Any, ...]", True, ValueKind.OPAQUE)
    if isinstance(value, set | frozenset):
        return InferenceResult("set[Any]", True, ValueKind.OPAQUE)

    # ------------------------------------------------------------------ #
    # 4 – Callables (functions, lambdas, bound methods, class objects)    #
    # ------------------------------------------------------------------ #
    if inspect.isfunction(value) or inspect.isbuiltin(value) or inspect.ismethod(value):
        return InferenceResult("Any", True, ValueKind.CALLABLE)
    if inspect.isclass(value):
        return InferenceResult("Any", True, ValueKind.CALLABLE)

    # ------------------------------------------------------------------ #
    # 5 – Lazy proxy / derived objects                                    #
    # Prefer checking the type's module/qualname for known proxy patterns  #
    # before falling back to the repr heuristic, so that regular custom    #
    # class instances (which also have the '<ClassName object at 0x>' repr) #
    # are correctly classified as OPAQUE rather than DERIVED.              #
    # ------------------------------------------------------------------ #
    type_qualname = (
        f"{type(value).__module__ or ''}.{type(value).__qualname__ or ''}".lower()
    )
    if any(
        marker in type_qualname for marker in ("derived", "proxy", "lazy", "deferred")
    ):
        return InferenceResult("Any", True, ValueKind.DERIVED)

    # Fall back to the '<ClassName object at 0x...>' repr heuristic for any
    # remaining unrecognised non-repr-able instance.
    try:
        r = repr(value)
        if r.startswith("<") and " object at 0x" in r:
            return InferenceResult("Any", True, ValueKind.OPAQUE)
    except Exception:  # noqa: BLE001  # pragma: no cover
        return InferenceResult("Any", True, ValueKind.OPAQUE)

    # ------------------------------------------------------------------ #
    # 6 – Unrecognised                                                    #
    # ------------------------------------------------------------------ #
    return InferenceResult("Any", True, ValueKind.OPAQUE)
