"""Public declaration API for a package's *settings surface*.

A dependency uses this module to advertise, in a cheap and import-light way,
the settings it introduces into a Django project's namespace — their names,
types, defaults, and whether the project must supply a value. django-aqueduct's
``report_settings_surface`` command collects every declared surface (plus
built-in knowledge of Django/DRF/Celery) and reconciles it against what the
project actually sets, so a team can see what each dependency contributes and
decide about it.

This is the generalized analogue of Django REST Framework's ``DEFAULTS`` dict:
a package publishes a callable returning :class:`Setting` objects and registers
it under the ``django_aqueduct.settings_surface`` entry-point group::

    # in the dependency's pyproject.toml
    [project.entry-points."django_aqueduct.settings_surface"]
    my-package = "my_package.aqueduct_surface:surface"

.. code-block:: python

    # my_package/aqueduct_surface.py
    from django_aqueduct.surface import UNSET, Setting


    def surface() -> list[Setting]:
        return [
            Setting(
                "MY_PACKAGE_FROM_EMAIL",
                type="str",
                default="",
                description="Envelope From for outbound mail.",
            ),
            Setting(
                "MY_PACKAGE_REPLY_TO",
                type="str | None",
                default=None,
                description="Optional Reply-To address.",
            ),
            Setting(
                "MY_PACKAGE_API_TOKEN",
                type="str",
                default=UNSET,
                required=True,
                description="Required API token; the project must supply it.",
            ),
        ]

This module imports nothing heavy (no Django, no pydantic) — mirroring the
discipline of :mod:`django_aqueduct.discovery.ir` — so declaring a surface adds
a negligible import cost to the dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


class _Unset:
    """Type of the :data:`UNSET` sentinel — a singleton, falsey, repr ``UNSET``."""

    _instance: _Unset | None = None

    def __new__(cls) -> _Unset:
        """Return the single shared instance."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        """Render as ``UNSET`` in reports and tracebacks."""
        return "UNSET"

    def __bool__(self) -> bool:
        """Be falsey so ``if setting.default:`` reads naturally."""
        return False


#: Sentinel for :attr:`Setting.default` meaning "this package declares no
#: default value at all" — distinct from ``default=None`` (the package's
#: default *is* ``None``). A setting with ``default is UNSET`` has no value to
#: fall back to; pair it with ``required=True`` when the project must supply
#: one.
UNSET: Final = _Unset()


@dataclass(frozen=True)
class Setting:
    """One setting a dependency introduces into the Django settings namespace.

    This is the public unit of a *settings surface* (see the module docstring).
    It is deliberately a plain, frozen, hashable dataclass carrying only strings
    and plain values so it is cheap to construct and safe to declare from any
    package.

    Expressing "required vs. a ``None`` default":

    * ``Setting("X", default=UNSET, required=True)`` — the package declares no
      default and the project **must** supply a value.
    * ``Setting("X", default=None)`` — the package's default *is* ``None`` (a
      valid, optional value); the project need not set it.
    * ``Setting("X", default="")`` — an ordinary non-``None`` default.

    Use :attr:`has_default` rather than comparing ``default`` to ``UNSET``
    directly.

    Attributes:
        name: The UPPERCASE settings name (e.g. ``"MY_PACKAGE_FROM_EMAIL"``).
            Nested keys of a container setting may be expressed with a dot, e.g.
            ``"REST_FRAMEWORK.DEFAULT_THROTTLE_RATES"``.
        type: A type-annotation *string* (e.g. ``"str"``, ``"int"``,
            ``"str | None"``, ``"list[str]"``). A string — not a live type — so
            declaring a surface never imports the annotated types.
        default: The package's own default value, or :data:`UNSET` when the
            package declares none. Keep it a simple, reprable value.
        required: ``True`` when the project must supply a value (typically
            paired with ``default=UNSET``).
        description: Human-readable, single-line explanation for the report.
        dev_only: ``True`` when the setting is only relevant in development.
    """

    name: str
    type: str = "Any"
    default: object = UNSET
    required: bool = False
    description: str = ""
    dev_only: bool = False

    @property
    def has_default(self) -> bool:
        """Return ``True`` when the package declares a default (even ``None``)."""
        return self.default is not UNSET
