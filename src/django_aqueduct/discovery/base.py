"""Base types for settings discovery."""

from dataclasses import dataclass, field
from enum import Enum
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
    value_kind: "ValueKind" = field(default_factory=lambda: ValueKind.STATIC)
    owning_package: str = field(default="")
    """PyPI distribution name of the package that owns this setting.

    Populated by
    :class:`~django_aqueduct.discovery.package_attributor.PackageAttributor`
    when ``--attribute-packages`` is passed to the management command.  An
    empty string means the setting has not been attributed yet.
    """


class ValueKind(str, Enum):  # noqa: FURB189, UP042
    """Semantic classification of a settings value's runtime representation.

    The kind is used by the code generator to decide how to render the
    default and which comments or TODO markers to emit.

    Attributes:
        STATIC: A JSON-serializable primitive, list, dict, or ``None``.
            These values can be rendered as Python literals and used
            directly as Pydantic ``Field`` defaults.
        OPAQUE: A Python-native type that cannot be represented in JSON
            (``tuple``, ``set``, ``frozenset``).  Can usually be rendered
            via ``repr()``, but the generated type annotation requires
            review.
        CALLABLE: A function, lambda, class, or bound method.  Cannot
            be stored as a Pydantic field default; the generator emits
            ``default=None`` and a comment pointing to the original name.
        DERIVED: A lazy proxy that is computed at runtime from other
            settings (e.g. ``openedx.core.lib.derived.Derived``).  The
            generator emits ``default=None`` and a comment advising the
            developer to reproduce the logic in a ``@model_validator``.
        REDACTED: A value discovered by inspecting a *live* settings module
            whose name looks secret-like (``SECRET``, ``PASSWORD``,
            ``TOKEN``, etc.). Module inspection captures whatever value was
            resolved from the environment at generation time, so writing it
            verbatim into a generated (and likely committed) file risks
            leaking real secrets. The generator emits ``default=None`` and a
            comment instead of the observed value.
    """

    STATIC = "static"
    OPAQUE = "opaque"
    CALLABLE = "callable"
    DERIVED = "derived"
    REDACTED = "redacted"


@runtime_checkable
class BaseInspector(Protocol):
    """Protocol that all settings inspectors must satisfy."""

    def discover(self) -> list[DiscoveredField]:
        """Return a list of fields discovered from this source.

        Returns:
            Ordered list of :class:`DiscoveredField` instances.
        """
        ...  # pragma: no cover
