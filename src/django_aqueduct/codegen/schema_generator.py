"""JSON Schema generator for discovered settings fields (codegen v2 IR).

The generated schema validates Kubernetes ConfigMaps, environment variable
sets, or any external settings source against the expected structure of a
Django project's settings.

Example::

    from django_aqueduct.codegen.schema_generator import SchemaGenerator
    from django_aqueduct.discovery.static import StaticModuleInspector
    import json

    fields = StaticModuleInspector("myapp.settings").discover()
    print(json.dumps(SchemaGenerator(fields).generate(), indent=2))

The schema uses JSON Schema draft-07 and includes ``x-aqueduct-*`` extension
properties for tooling:

``x-aqueduct-source``
    The dotted module path that declared the setting.
``x-aqueduct-package``
    The owning package label, when attribution has been run.
``x-aqueduct-default-strategy``
    Present for non-literal defaults; one of ``"expr"``, ``"derived"``,
    ``"required"``, ``"redacted"``.
``x-aqueduct-needs-refinement``
    ``true`` when the type annotation is best-effort.
"""

from __future__ import annotations

import json
from typing import Any

from django_aqueduct.discovery.ir import DefaultStrategy, SettingField

# ---------------------------------------------------------------------------
# Annotation → JSON Schema type mapping
# ---------------------------------------------------------------------------

_ANNOTATION_TO_JSON_TYPE: dict[str, dict[str, Any]] = {
    "str": {"type": "string"},
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "bool": {"type": "boolean"},
    "list[Any]": {"type": "array"},
    "list[str]": {"type": "array", "items": {"type": "string"}},
    "list[int]": {"type": "array", "items": {"type": "integer"}},
    "list[float]": {"type": "array", "items": {"type": "number"}},
    "list[bool]": {"type": "array", "items": {"type": "boolean"}},
    "dict[str, Any]": {"type": "object"},
    "dict[str, str]": {"type": "object", "additionalProperties": {"type": "string"}},
    "dict[str, int]": {
        "type": "object",
        "additionalProperties": {"type": "integer"},
    },
    "tuple[Any, ...]": {"type": "array"},
    "set[Any]": {"type": "array", "uniqueItems": True},
}


def _sorted_or_list(value: set[Any] | frozenset[Any]) -> list[Any]:
    """Return a set as a sorted list, falling back to insertion order if unsortable.

    Sorting gives deterministic schema output; mixed-type sets (which cannot be
    ordered) fall back to a plain list so we still emit *something* stable-ish.
    """
    try:
        return sorted(value)
    except TypeError:
        return list(value)


def _annotation_to_json_schema(annotation: str) -> dict[str, Any]:
    """Convert a type annotation base string to a JSON Schema type fragment.

    Returns an empty dict (unconstrained) for ``Any`` and unknown
    annotations.
    """
    return dict(_ANNOTATION_TO_JSON_TYPE.get(annotation, {}))


def _literal_or_url_schema(annotation: str) -> dict[str, Any] | None:
    """Return a schema fragment for an enrichment-produced ``Literal[...]``/``AnyUrl``.

    Returns ``None`` for any other annotation (falls through to the static
    lookup table).
    """
    if annotation == "AnyUrl":
        return {"type": "string", "format": "uri"}
    if annotation.startswith("Literal[") and annotation.endswith("]"):
        import ast  # noqa: PLC0415

        try:
            values = ast.literal_eval("[" + annotation[len("Literal[") : -1] + "]")
        except (ValueError, SyntaxError):
            return None
        return {"enum": values}
    return None


def _genson_schema_for(value: Any) -> dict[str, Any] | None:
    """Run genson on *value* and return a schema fragment, or ``None``.

    Returns ``None`` when genson is not installed, the value is not
    JSON-serialisable, or genson raises.
    """
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return None

    try:
        from genson import SchemaBuilder  # noqa: PLC0415

        builder = SchemaBuilder()
        builder.add_object(value)
        schema: dict[str, Any] = builder.to_schema()
        schema.pop("$schema", None)
        return schema
    except ImportError:
        return None
    except Exception:  # noqa: BLE001
        return None


class SchemaGenerator:
    """Generate a JSON Schema document from typed IR settings fields.

    When ``genson`` is available (``pip install django-aqueduct[codegen]``)
    the schema for ``dict``- and ``list``-valued literal defaults is inferred
    directly from the value, producing richer schemas than the annotation
    alone.

    Args:
        fields: Discovered :class:`~django_aqueduct.discovery.ir.SettingField`
            IR objects.
    """

    def __init__(self, fields: list[SettingField]) -> None:
        """Store the fields to render."""
        self._fields = fields

    def generate(self) -> dict[str, Any]:
        """Return a JSON Schema ``dict`` for the settings collection."""
        properties: dict[str, Any] = {}
        required: list[str] = []

        for f in self._fields:
            prop = self._field_to_schema(f)

            if f.description:
                prop["description"] = f.description

            # Include a literal default so validators know the expected value
            # when the key is absent. Sets/frozensets are not JSON-serialisable
            # but map to an ``array`` type, so emit them as a sorted list.
            if f.default.strategy in (DefaultStrategy.LITERAL, DefaultStrategy.FACTORY):
                value = f.default.literal
                if isinstance(value, set | frozenset):
                    value = _sorted_or_list(value)
                try:
                    json.dumps(value)
                    prop["default"] = value
                except (TypeError, ValueError):
                    pass

            prop["x-aqueduct-source"] = f.provenance.source_module
            if f.owning_package:
                prop["x-aqueduct-package"] = f.owning_package
            if f.default.strategy not in (
                DefaultStrategy.LITERAL,
                DefaultStrategy.FACTORY,
            ):
                prop["x-aqueduct-default-strategy"] = f.default.strategy.value
            if f.type.needs_refinement:
                prop["x-aqueduct-needs-refinement"] = True

            properties[f.name] = prop

            if f.required:
                required.append(f.name)

        schema: dict[str, Any] = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "title": "AqueductSettings",
            "description": (
                "Generated by django-aqueduct. "
                "Validate ConfigMaps or environment variables against this schema."
            ),
            "type": "object",
            "properties": properties,
            "additionalProperties": True,
        }
        if required:
            schema["required"] = sorted(required)

        return schema

    def _field_to_schema(self, f: SettingField) -> dict[str, Any]:
        """Convert a single field to a JSON Schema property fragment.

        DERIVED fields produce an unconstrained ``{}`` schema because their
        values are computed at runtime, not supplied from an external
        ConfigMap. REDACTED fields (secrets) *are* configurable, so they keep
        their type constraint (usually ``string``) and merely omit ``default``.
        Dict/list literal defaults use genson when available for a richer
        schema than the annotation alone. ``Literal[...]``/``AnyUrl`` (from
        ``--enrich-runtime``/``--enrich-usage``) map to ``enum``/``format:
        uri``; usage-mined numeric bounds map to draft-07's
        minimum/maximum/exclusiveMinimum/exclusiveMaximum.
        """
        if f.default.strategy is DefaultStrategy.DERIVED:
            return {}

        if f.default.strategy in (
            DefaultStrategy.LITERAL,
            DefaultStrategy.FACTORY,
        ) and isinstance(f.default.literal, dict | list):
            schema = _genson_schema_for(f.default.literal)
            if schema is not None:
                return schema

        schema = _literal_or_url_schema(f.type.base) or _annotation_to_json_schema(
            f.type.base
        )
        if f.constraints.gt is not None:
            schema["exclusiveMinimum"] = f.constraints.gt
        if f.constraints.ge is not None:
            schema["minimum"] = f.constraints.ge
        if f.constraints.lt is not None:
            schema["exclusiveMaximum"] = f.constraints.lt
        if f.constraints.le is not None:
            schema["maximum"] = f.constraints.le
        return schema
