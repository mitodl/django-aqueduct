"""JSON Schema generator for discovered settings fields.

The generated schema can be used to validate Kubernetes ConfigMaps,
environment variable sets, or any external settings source against the
expected structure of a Django project's settings.

Example::

    from django_aqueduct.codegen.schema_generator import SchemaGenerator
    from django_aqueduct.discovery.module import ModuleInspector
    import json

    fields = ModuleInspector("myapp.settings").discover()
    schema = SchemaGenerator(fields).generate()
    with open("settings.schema.json", "w") as fh:
        json.dump(schema, fh, indent=2)

The schema uses JSON Schema draft-07 and includes ``x-aqueduct-*``
extension properties for tooling:

``x-aqueduct-source``
    The dotted module path that declared the setting.
``x-aqueduct-value-kind``
    Present only for non-``STATIC`` values; one of ``"derived"``,
    ``"callable"``, or ``"opaque"``.
``x-aqueduct-needs-refinement``
    ``true`` when the type annotation is best-effort.
"""

from __future__ import annotations

import json
from typing import Any

from django_aqueduct.discovery.base import DiscoveredField, ValueKind

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


def _annotation_to_json_schema(annotation: str) -> dict[str, Any]:
    """Convert a Pydantic annotation string to a JSON Schema type fragment.

    Returns an empty dict (unconstrained) for ``Any`` and unknown
    annotations.

    Args:
        annotation: A Pydantic-compatible type annotation string.

    Returns:
        A JSON Schema type fragment (no ``$schema`` key).
    """
    return dict(_ANNOTATION_TO_JSON_TYPE.get(annotation, {}))


def _genson_schema_for(value: Any) -> dict[str, Any] | None:
    """Run genson on *value* and return a schema fragment.

    Used for both dict and list values.  Returns ``None`` when genson is
    not installed, the value is not JSON-serialisable, or genson raises.

    Args:
        value: Any Python object to infer a schema for.

    Returns:
        A JSON Schema fragment without the ``$schema`` key, or ``None``.
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


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class SchemaGenerator:
    """Generate a JSON Schema document from discovered settings fields.

    The resulting schema targets draft-07 and is suitable for:

    * Validating Kubernetes ConfigMap data keys.
    * Validating ``values.yaml`` / Helm template outputs.
    * IDE autocompletion for ``.env`` files via JSON Schema plugins.
    * CI checks that catch misconfigured settings before deployment.

    When ``genson`` is available (``pip install django-aqueduct[codegen]``)
    the schema for ``dict``- and ``list``-valued settings is inferred
    directly from the runtime default value, producing richer schemas than
    the annotation alone can provide.

    Args:
        fields: Discovered settings fields, typically from one or more
            :class:`~django_aqueduct.discovery.base.BaseInspector`
            implementations.

    Example::

        from django_aqueduct.codegen.schema_generator import SchemaGenerator
        from django_aqueduct.discovery.module import ModuleInspector
        import json

        fields = ModuleInspector("myapp.settings").discover()
        print(json.dumps(SchemaGenerator(fields).generate(), indent=2))
    """

    def __init__(self, fields: list[DiscoveredField]) -> None:
        """Store the fields to render."""
        self._fields = fields

    def generate(self) -> dict[str, Any]:
        """Return a JSON Schema ``dict`` for the settings collection.

        The schema has ``type: object``, ``additionalProperties: true``
        (matching ``extra="allow"`` in the Pydantic model), and one
        property per discovered field.

        Returns:
            A complete JSON Schema document as a Python ``dict``.
        """
        properties: dict[str, Any] = {}
        required: list[str] = []

        for f in self._fields:
            prop = self._field_to_schema(f)

            # Standard JSON Schema annotations
            if f.description:
                prop["description"] = f.description

            # Include JSON-serialisable defaults in the schema so validators
            # know the expected value when the key is absent.
            if f.value_kind == ValueKind.STATIC:
                try:
                    json.dumps(f.default)
                    prop["default"] = f.default
                except (TypeError, ValueError):
                    pass

            # x-aqueduct extensions for tooling
            prop["x-aqueduct-source"] = f.source_module
            if f.value_kind != ValueKind.STATIC:
                prop["x-aqueduct-value-kind"] = f.value_kind.value
            if f.needs_refinement:
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
            # Mirrors extra="allow" in the Pydantic BaseSettings model.
            "additionalProperties": True,
        }
        if required:
            schema["required"] = sorted(required)

        return schema

    def _field_to_schema(self, f: DiscoveredField) -> dict[str, Any]:
        """Convert a single field to a JSON Schema property definition.

        DERIVED and CALLABLE fields produce an unconstrained ``{}``
        schema because their values are not meaningfully configurable
        from outside the Python process.

        For dict- and list-valued STATIC fields, genson is used when
        available to produce a richer schema than the annotation alone.

        Args:
            f: The field to convert.

        Returns:
            A JSON Schema property fragment (no ``$schema`` key).
        """
        if f.value_kind in (ValueKind.DERIVED, ValueKind.CALLABLE):
            return {}

        # Try genson for dict and list defaults
        if f.value_kind == ValueKind.STATIC and isinstance(f.default, dict | list):
            schema = _genson_schema_for(f.default)
            if schema is not None:
                return schema

        return _annotation_to_json_schema(f.type_annotation)
