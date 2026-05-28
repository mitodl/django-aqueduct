"""Base types for settings discovery."""

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class DiscoveredField:
    """A single settings field discovered from an inspection source.

    Attributes:
        name: The UPPERCASE settings name (e.g. ``DATABASE_URL``).
        type_annotation: A string representation of the inferred Python type
            annotation (e.g. ``"str"``, ``"dict[str, Any]"``).
        default: The default value observed at inspection time.
        description: Human-readable description sourced from the declaring
            code, or an empty string when unavailable.
        required: Whether the setting must be supplied at runtime.
        source_module: Dotted module path where the setting was declared.
        dev_only: Whether the setting is only relevant in development
            environments.
        needs_refinement: Set to ``True`` when the type annotation is a
            best-effort guess that the developer should review.
    """

    name: str
    type_annotation: str
    default: Any
    description: str
    required: bool
    source_module: str
    dev_only: bool
    needs_refinement: bool = field(default=False)


@runtime_checkable
class BaseInspector(Protocol):
    """Protocol that all settings inspectors must satisfy."""

    def discover(self) -> list[DiscoveredField]:
        """Return a list of fields discovered from this source.

        Returns:
            Ordered list of :class:`DiscoveredField` instances.
        """
        ...  # pragma: no cover
