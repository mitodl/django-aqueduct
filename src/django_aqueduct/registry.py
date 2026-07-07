"""Extension registry for discovery inspectors.

The management command hardcoded exactly two inspectors (static + mitol
envparser). This registry lets a third-party package contribute its own
inspector — for a django-environ / django-configurations / constance-style
idiom, or to declare its own settings surface — without editing aqueduct.

A plugin advertises an entry point in the ``django_aqueduct.inspectors``
group::

    # in the plugin's pyproject.toml
    [project.entry-points."django_aqueduct.inspectors"]
    myplugin = "myplugin.aqueduct:build_inspector"

The referenced object must be an **inspector** (any object with a
``discover() -> list[SettingField]`` method) or a **zero-arg factory** that
returns one. :func:`discover_from_plugins` loads every registered plugin and
concatenates their discovered fields.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from django_aqueduct.discovery.ir import SettingField

INSPECTOR_GROUP = "django_aqueduct.inspectors"


@runtime_checkable
class Inspector(Protocol):
    """Anything that can discover settings fields."""

    def discover(self) -> list[SettingField]:
        """Return discovered settings fields."""
        ...  # pragma: no cover


class RegistryError(Exception):
    """Raised when a registered inspector plugin cannot be loaded."""


def _resolve_inspector(loaded: object, name: str) -> Inspector:
    """Coerce a loaded entry-point object into an :class:`Inspector`.

    Accepts an inspector instance directly, or a zero-arg factory (class or
    function) returning one.
    """
    if isinstance(loaded, Inspector):
        return loaded
    if callable(loaded):
        candidate = loaded()
        if isinstance(candidate, Inspector):
            return candidate
        raise RegistryError(
            f"Inspector plugin {name!r} factory returned "
            f"{type(candidate).__name__}, which has no discover() method."
        )
    raise RegistryError(
        f"Inspector plugin {name!r} is neither an inspector nor a factory."
    )


def load_inspectors() -> list[tuple[str, Inspector]]:
    """Return ``(name, inspector)`` for every registered inspector plugin.

    Raises:
        RegistryError: If a plugin fails to load or resolve, naming the plugin.
    """
    out: list[tuple[str, Inspector]] = []
    for ep in entry_points(group=INSPECTOR_GROUP):
        try:
            loaded = ep.load()
        except Exception as exc:  # noqa: BLE001
            raise RegistryError(
                f"Failed to load inspector plugin {ep.name!r}: {exc}"
            ) from exc
        out.append((ep.name, _resolve_inspector(loaded, ep.name)))
    return out


def discover_from_plugins() -> list[SettingField]:
    """Return the concatenated fields discovered by all registered plugins."""
    fields: list[SettingField] = []
    for _name, inspector in load_inspectors():
        fields.extend(inspector.discover())
    return fields
