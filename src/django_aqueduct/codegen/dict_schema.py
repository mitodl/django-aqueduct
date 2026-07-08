"""Optional genson-powered type enrichment for dict-valued settings.

When ``genson`` is available (installed via the ``[codegen]`` extra) and
a dict value is JSON-serialisable, this module infers richer type
annotations and generates companion ``TypedDict`` class definitions that
the code generator can embed in the output file.

Falls back silently to ``dict[str, Any]`` / empty TypedDef list when:

* ``genson`` is not installed.
* The value contains non-JSON-serialisable elements (``Path``, custom
  objects, etc.).
* The inferred schema does not contain useful structural information.

Install the extra::

    pip install django-aqueduct[codegen]
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------


@dataclass
class TypedDictField:
    """A single field in a generated ``TypedDict``."""

    name: str
    """Python attribute name (matches the dict key)."""

    annotation: str
    """Pydantic-compatible type annotation string."""

    required: bool
    """Whether this field was observed as required in every sample value."""


@dataclass
class TypedDictDef:
    """A ``TypedDict`` class definition to emit in the generated settings file.

    All generated TypedDicts use ``total=False`` so that partial configs
    (e.g. a database entry missing optional keys) remain valid.
    """

    class_name: str
    """PascalCase class name, e.g. ``DatabasesEntry``."""

    fields: list[TypedDictField] = field(default_factory=list)
    """Fields sorted alphabetically for deterministic output."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _genson_available() -> bool:
    """Return ``True`` if ``genson`` is importable."""
    try:
        import genson  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False


def _is_json_serialisable(value: Any) -> bool:
    """Return ``True`` if *value* can be round-tripped through ``json.dumps``."""
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


def _to_pascal_case(setting_name: str) -> str:
    """Convert ``SETTING_NAME`` to ``SettingName``.

    Example::

        >>> _to_pascal_case("COURSE_ENROLLMENT_MODES")
        'CourseEnrollmentModes'
    """
    return "".join(part.capitalize() for part in setting_name.lower().split("_"))


def _json_schema_type_to_annotation(schema: dict[str, Any]) -> str:
    """Convert a JSON Schema type fragment to a Python annotation string.

    Handles the scalar types produced by genson and simple ``anyOf``
    unions.  Complex nested objects fall back to ``dict[str, Any]``.

    Args:
        schema: A JSON Schema fragment (no ``$schema`` key required).

    Returns:
        A Pydantic-compatible annotation string.
    """
    t = schema.get("type")
    if t == "string":
        return "str"
    if t == "integer":
        return "int"
    if t == "number":
        return "float"
    if t == "boolean":
        return "bool"
    if t == "null":
        return "None"
    if t == "array":
        items = schema.get("items")
        if items:
            item_ann = _json_schema_type_to_annotation(items)
            return f"list[{item_ann}]"
        return "list[Any]"
    if t == "object":
        return "dict[str, Any]"
    # genson emits anyOf when it observes multiple types for the same key
    if "anyOf" in schema:
        parts = [_json_schema_type_to_annotation(s) for s in schema["anyOf"]]
        # deduplicate while preserving order
        seen: dict[str, None] = {}
        for p in parts:
            seen[p] = None
        unique = list(seen)
        if "None" in unique and len(unique) > 1:
            # represent as Optional
            non_null = [u for u in unique if u != "None"]
            base = " | ".join(non_null)
            return f"{base} | None"
        return " | ".join(unique) if len(unique) > 1 else unique[0]
    return "Any"


def _schema_to_typeddict(class_name: str, schema: dict[str, Any]) -> TypedDictDef:
    """Build a :class:`TypedDictDef` from an ``object``-type JSON Schema fragment.

    Args:
        class_name: The desired Python class name.
        schema: A JSON Schema fragment with ``type: object`` and ``properties``.

    Returns:
        A :class:`TypedDictDef` with fields sorted alphabetically.
    """
    properties: dict[str, Any] = schema.get("properties", {})
    required_set = set(schema.get("required", []))
    typed_fields = [
        TypedDictField(
            name=fname,
            annotation=_json_schema_type_to_annotation(fschema),
            required=fname in required_set,
        )
        for fname, fschema in sorted(properties.items())
    ]
    return TypedDictDef(class_name=class_name, fields=typed_fields)


def _is_homogeneous_dict(value: dict[str, Any]) -> bool:
    """Return ``True`` when all values in *value* are dicts with the same keys.

    A dict is considered homogeneous when:

    * It is non-empty.
    * Every value is itself a ``dict``.
    * All inner dicts share the exact same set of top-level keys.

    This heuristic correctly identifies settings such as
    ``COURSE_ENROLLMENT_MODES`` and ``DATABASES`` (where both entries
    happen to carry the same fields).

    Args:
        value: The outer dict to inspect.

    Returns:
        ``True`` if the dict is homogeneous, ``False`` otherwise.
    """
    if not value:
        return False
    inner_values = list(value.values())
    if not all(isinstance(v, dict) for v in inner_values):
        return False
    key_sets = [frozenset(v.keys()) for v in inner_values]
    return len(set(key_sets)) == 1


def _build_genson_schema_multi(values: Sequence[Any]) -> dict[str, Any] | None:
    """Run genson over every sample in *values* and merge into one schema.

    Feeding genson multiple observations (e.g. the same setting's value
    sampled under several env snapshots) lets it infer which keys are
    present in every sample (``required``) versus only some (optional) —
    strictly more accurate than any single snapshot can be.

    Returns ``None`` when genson is unavailable, *values* is empty, any
    element is not JSON-serialisable, or genson raises an exception.
    """
    if not _genson_available() or not values:
        return None
    if not all(_is_json_serialisable(v) for v in values):
        return None
    try:
        from genson import SchemaBuilder  # noqa: PLC0415

        builder = SchemaBuilder()
        for v in values:
            builder.add_object(v)
        schema = builder.to_schema()
        schema.pop("$schema", None)
        return schema  # type: ignore[no-any-return]
    except Exception:  # noqa: BLE001, S110
        return None


def _build_genson_schema(value: Any) -> dict[str, Any] | None:
    """Run genson on a single *value*. See :func:`_build_genson_schema_multi`."""
    return _build_genson_schema_multi([value])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enrich_dict_annotation_multi(
    setting_name: str,
    values: Sequence[dict[str, Any]],
) -> tuple[str, list[TypedDictDef]]:
    """Infer a richer type annotation for a dict-valued setting from N samples.

    Same two structural patterns as :func:`enrich_dict_annotation`, but
    merges the schema across every sample in *values* (e.g. the setting's
    value observed under several env snapshots via
    :mod:`~django_aqueduct.discovery.runtime`). A key present in every
    sample's inner entries is inferred ``required``; a key present in only
    some samples is inferred optional — strictly more accurate than any
    single sample can produce, and the reason this exists as its own
    function rather than a loop calling :func:`enrich_dict_annotation` once
    per sample (which would just produce N independent, possibly
    conflicting, TypedDicts).

    Args:
        setting_name: The UPPERCASE settings name used to derive class
            names (e.g. ``"DATABASES"`` → ``"DatabasesEntry"``).
        values: The runtime dict values observed for this setting, one per
            sample. Non-dict and empty-dict samples are ignored.

    Returns:
        A ``(annotation_string, typeddict_defs)`` tuple. When enrichment is
        not possible both members reflect the safe fallback:
        ``("dict[str, Any]", [])``.
    """
    samples = [v for v in values if isinstance(v, dict) and v]
    if not _genson_available() or not samples:
        return "dict[str, Any]", []

    top_schema = _build_genson_schema_multi(samples)
    if top_schema is None or top_schema.get("type") != "object":
        return "dict[str, Any]", []

    pascal = _to_pascal_case(setting_name)

    # ------------------------------------------------------------------
    # Case 1: homogeneous dict of structs → dict[str, NameEntry]
    # ------------------------------------------------------------------
    if all(_is_homogeneous_dict(v) for v in samples):
        inner_values = [inner for v in samples for inner in v.values()]
        inner_schema_obj = _build_genson_schema_multi(inner_values)
        if inner_schema_obj is None:
            return "dict[str, Any]", []

        if (
            inner_schema_obj.get("type") != "object"
            or "properties" not in inner_schema_obj
        ):
            # Homogeneous dict of primitives — extract the scalar type
            # from the values' common schema instead
            primitive_ann = _json_schema_type_to_annotation(inner_schema_obj)
            if primitive_ann not in ("Any", "dict[str, Any]", "list[Any]"):
                return f"dict[str, {primitive_ann}]", []
            return "dict[str, Any]", []

        entry_class = f"{pascal}Entry"
        td = _schema_to_typeddict(entry_class, inner_schema_obj)
        return f"dict[str, {entry_class}]", [td]

    # ------------------------------------------------------------------
    # Case 2: homogeneous dict of primitives (non-struct values)
    # ------------------------------------------------------------------
    inner_values = [inner for v in samples for inner in v.values()]
    if all(not isinstance(v, dict | list) for v in inner_values):
        val_schema = _build_genson_schema_multi(inner_values)
        if val_schema is not None:
            primitive_ann = _json_schema_type_to_annotation(val_schema)
            if primitive_ann not in ("Any", "dict[str, Any]", "list[Any]"):
                return f"dict[str, {primitive_ann}]", []

    return "dict[str, Any]", []


def enrich_dict_annotation(
    setting_name: str,
    value: dict[str, Any],
) -> tuple[str, list[TypedDictDef]]:
    """Infer a richer type annotation for a dict-valued setting.

    When ``genson`` is available and the value is JSON-serialisable, this
    function generates :class:`TypedDictDef` definitions for nested object
    types and returns a more precise annotation than the default
    ``dict[str, Any]``.

    Two structural patterns are handled:

    **Homogeneous dict of structs** — all values are dicts with identical
    key sets (e.g. ``DATABASES``, ``COURSE_ENROLLMENT_MODES``).  Returns
    ``dict[str, XxxEntry]`` and one :class:`TypedDictDef` for the inner
    type, built by merging all observed inner schemas.

    **Homogeneous dict of primitives** — all values share the same scalar
    type (e.g. ``CERTIFICATE_TEMPLATE_LANGUAGES = {'en': 'English', ...}``).
    Returns ``dict[str, str]`` (or the appropriate primitive annotation)
    with no TypedDict definitions.

    All other cases (heterogeneous dicts, deeply nested structures, or
    when genson is unavailable) fall back to ``dict[str, Any]`` with an
    empty definition list.

    Args:
        setting_name: The UPPERCASE settings name used to derive class
            names (e.g. ``"DATABASES"`` → ``"DatabasesEntry"``).
        value: The runtime dict value to inspect.

    Returns:
        A ``(annotation_string, typeddict_defs)`` tuple.  When enrichment
        is not possible both members reflect the safe fallback:
        ``("dict[str, Any]", [])``.
    """
    return enrich_dict_annotation_multi(setting_name, [value])
