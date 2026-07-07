"""Shared helpers for external settings sources (Vault, SSM, YAML).

Each source fetches a flat ``{key: value}`` mapping from somewhere and hands it
to pydantic-settings. Two things every source must get right, and which the
first implementations got wrong:

* **Coherent caching** — fetch the backing store once, not once per field *and*
  again in ``__call__``.
* **Complex values** — a value stored as a JSON string (``'{"a": 1}'`` in
  Vault) must be JSON-decoded when the target field is complex, or validation
  fails. :func:`build_from_mapping` routes every value through
  ``prepare_field_value`` with the field's complexity, exactly like
  pydantic-settings' own ``EnvSettingsSource``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping

    from pydantic_settings import PydanticBaseSettingsSource


class SourceError(Exception):
    """Base error for external settings sources (connection/auth/read failures)."""


def build_from_mapping(
    source: PydanticBaseSettingsSource,
    secrets: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a settings dict from a fetched *secrets* mapping.

    Declared fields are routed through ``prepare_field_value`` so complex
    (dict/list/model) fields whose stored value is a JSON string are decoded
    rather than rejected. Keys that match no declared field are passed through
    unchanged, preserving ``extra="allow"`` behaviour for secrets stores that
    hold more than the model declares.

    Args:
        source: The calling settings source (for ``settings_cls`` and the
            ``field_is_complex`` / ``prepare_field_value`` hooks).
        secrets: The flat mapping fetched from the backing store.

    Returns:
        ``{key: value}`` for every key in *secrets* (declared fields decoded,
        extras verbatim).
    """
    declared = source.settings_cls.model_fields
    data: dict[str, Any] = {}
    for key, value in secrets.items():
        field = declared.get(key)
        # JSON-decode only a *string* value for a complex field (JSON-in-Vault).
        # A value already parsed as a dict/list (e.g. from YAML) is passed
        # through untouched, and extra="allow" keys have no field to decode
        # against.
        if (
            field is not None
            and isinstance(value, str)
            and source.field_is_complex(field)
        ):
            data[key] = source.decode_complex_value(key, field, value)
        else:
            data[key] = value
    return data
