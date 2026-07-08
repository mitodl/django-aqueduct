"""Enumerate the settings each installed dependency introduces (its *surface*).

Source-driven discovery (see :mod:`~django_aqueduct.discovery.static`) only sees
settings the *project* writes. A setting a dependency reads with its own
internal default, that the project never sets, is invisible — no field, nothing
to decide about. This module makes that surface visible by asking each
dependency what it introduces, from three providers (highest fidelity first):

1. **Declared surface (authoritative).** A package advertises a callable under
   the ``django_aqueduct.settings_surface`` entry-point group returning
   :class:`~django_aqueduct.surface.Setting` objects. See
   :mod:`django_aqueduct.surface`.

2. **Built-in extractors for the big three.** Django core
   (``django.conf.global_settings``), DRF (``rest_framework.settings.DEFAULTS``,
   with ``IMPORT_STRINGS`` awareness), and Celery (``celery.app.defaults``) all
   publish their defaults as importable Python objects. These are the *same*
   modules :mod:`~django_aqueduct.discovery.package_attributor` imports for
   attribution, so both consumers share one extraction pass here rather than
   duplicating the imports.

3. **INSTALLED_APPS scoping.** Built-in extractors are only included for
   packages actually present in the project's ``INSTALLED_APPS`` — the surface
   reflects *this* project's dependency set.

Safety: every provider only imports a package's *own* defaults module. Nothing
here executes project settings or takes a runtime snapshot, and output is
deterministic (stable sort by ``(dist, name)``).

The generic AST-default fallback and opt-in model emission described in the RFC
are deferred follow-ups; this module implements the report providers only.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from importlib.metadata import entry_points

from django_aqueduct.surface import UNSET, Setting

#: Entry-point group a dependency uses to declare its settings surface.
SURFACE_GROUP = "django_aqueduct.settings_surface"


class SurfaceError(Exception):
    """Raised when a declared-surface entry point cannot be loaded or coerced."""


@dataclass(frozen=True)
class SurfaceEntry:
    """One resolved surface setting, tagged with its owning distribution.

    Attributes:
        dist: The PyPI distribution label that introduces the setting.
        setting: The :class:`~django_aqueduct.surface.Setting` declaration.
        provider: How the entry was obtained — ``"declared"`` (entry point) or
            ``"builtin"`` (Django/DRF/Celery extractor).
    """

    dist: str
    setting: Setting
    provider: str


# ---------------------------------------------------------------------------
# value -> type-annotation string
# ---------------------------------------------------------------------------


def _type_str(value: object) -> str:
    """Return a best-effort type-annotation string for a default *value*."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if value is None:
        return "Any"
    if isinstance(value, list):
        return "list"
    if isinstance(value, tuple):
        return "tuple"
    if isinstance(value, set | frozenset):
        return "set"
    if isinstance(value, dict):
        return "dict"
    return "Any"


# ---------------------------------------------------------------------------
# Built-in extractor: Django core
# ---------------------------------------------------------------------------


def extract_django() -> list[Setting]:
    """Return a :class:`Setting` for every name in ``django.conf.global_settings``.

    Returns an empty list when Django is not importable.
    """
    try:
        import django.conf.global_settings as gs  # noqa: PLC0415
    except ImportError:
        return []
    out: list[Setting] = []
    for name in sorted(vars(gs)):
        if not name.isupper():
            continue
        value = getattr(gs, name)
        out.append(Setting(name=name, type=_type_str(value), default=value))
    return out


def django_core_names() -> set[str]:
    """Return the set of UPPERCASE names Django core defines (for attribution)."""
    return {s.name for s in extract_django()}


# ---------------------------------------------------------------------------
# Built-in extractor: Django REST Framework
# ---------------------------------------------------------------------------


def drf_available() -> bool:
    """Return ``True`` when Django REST Framework is importable."""
    try:
        import rest_framework.settings  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def extract_drf() -> list[Setting]:
    """Return one :class:`Setting` per ``REST_FRAMEWORK`` sub-key.

    DRF's settings live *inside* the single top-level ``REST_FRAMEWORK`` dict,
    so each sub-key is reported as ``REST_FRAMEWORK.<KEY>``. Keys listed in
    DRF's ``IMPORT_STRINGS`` are dotted import paths, noted as such.

    Returns an empty list when DRF is not importable.
    """
    try:
        from rest_framework.settings import (  # noqa: PLC0415
            DEFAULTS,
            IMPORT_STRINGS,
        )
    except ImportError:
        return []
    import_strings = set(IMPORT_STRINGS)
    out: list[Setting] = []
    for key in sorted(DEFAULTS):
        value = DEFAULTS[key]
        if key in import_strings:
            out.append(
                Setting(
                    name=f"REST_FRAMEWORK.{key}",
                    type="str",
                    default=value,
                    description="Dotted import path (DRF IMPORT_STRINGS).",
                )
            )
        else:
            out.append(
                Setting(
                    name=f"REST_FRAMEWORK.{key}",
                    type=_type_str(value),
                    default=value,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Built-in extractor: Celery
# ---------------------------------------------------------------------------

#: Celery ``Option.type`` name -> annotation string.
_CELERY_TYPE_MAP: dict[str, str] = {
    "string": "str",
    "int": "int",
    "float": "float",
    "bool": "bool",
    "dict": "dict",
    "tuple": "tuple",
    "list": "list",
    "any": "Any",
}


def celery_old_setting_names() -> set[str]:
    """Return Celery's old Django-style setting names (for attribution).

    ``celery.app.defaults._OLD_SETTING_KEYS`` is Celery's own authoritative list
    of the ``CELERY_*`` / ``BROKER_*`` names it kept for the old Django
    integration. Returns an empty set when Celery is not importable.
    """
    try:
        from celery.app.defaults import _OLD_SETTING_KEYS  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return set()
    return set(_OLD_SETTING_KEYS)


def extract_celery() -> list[Setting]:
    """Return one :class:`Setting` per old-style ``CELERY_*`` / ``BROKER_*`` name.

    Names come from Celery's authoritative ``_OLD_SETTING_KEYS`` table; defaults
    and declared types are recovered from the flattened ``NAMESPACES`` options
    when the old->new key mapping is available. Best-effort and fully guarded:
    returns an empty list when Celery is absent or its internals differ.
    """
    try:
        from celery.app import defaults as cd  # noqa: PLC0415
    except ImportError:
        return []

    old_keys = getattr(cd, "_OLD_SETTING_KEYS", None)
    if not old_keys:
        return []

    flat: dict[str, object] = {}
    try:
        flat = dict(cd.flatten(cd.NAMESPACES))
    except Exception:  # noqa: BLE001
        flat = {}
    to_new: dict[str, str] = getattr(cd, "_TO_NEW_KEY", {})

    out: list[Setting] = []
    for old in sorted(old_keys):
        opt = flat.get(to_new.get(old, ""))
        if opt is not None:
            default = getattr(opt, "default", UNSET)
            type_str = _CELERY_TYPE_MAP.get(getattr(opt, "type", "any") or "any", "Any")
        else:
            default, type_str = UNSET, "Any"
        out.append(Setting(name=old, type=type_str, default=default))
    return out


# ---------------------------------------------------------------------------
# Built-in provider: assemble, scoped to INSTALLED_APPS
# ---------------------------------------------------------------------------

#: Built-in dist -> the import root that must appear in INSTALLED_APPS for the
#: extractor to be included. Celery is triggered by any ``django_celery_*`` app
#: or a bare ``celery`` app.
_BUILTIN_ROOTS: dict[str, frozenset[str]] = {
    "django": frozenset({"django"}),
    "rest_framework": frozenset({"rest_framework"}),
    "celery": frozenset({"celery", "django_celery_beat", "django_celery_results"}),
}


def _dist_label(pkg_root: str) -> str:
    """Return the PyPI distribution label for *pkg_root* (lazy, avoids a cycle)."""
    from django_aqueduct.discovery.package_attributor import (  # noqa: PLC0415
        _dist_label as label,
    )

    return label(pkg_root)


def builtin_surfaces(installed_roots: set[str]) -> dict[str, list[Setting]]:
    """Return ``{dist_label: [Setting, ...]}`` for each installed built-in.

    Each built-in is included only when its trigger root (see
    :data:`_BUILTIN_ROOTS`) is present in *installed_roots*.
    """
    out: dict[str, list[Setting]] = {}
    extractors = {
        "django": extract_django,
        "rest_framework": extract_drf,
        "celery": extract_celery,
    }
    for root, extractor in extractors.items():
        if not (_BUILTIN_ROOTS[root] & installed_roots):
            continue
        settings = extractor()
        if settings:
            out[_dist_label(root)] = settings
    return out


# ---------------------------------------------------------------------------
# Declared-surface provider (entry points)
# ---------------------------------------------------------------------------


def _coerce_settings(obj: object, ep_name: str) -> list[Setting]:
    """Coerce an entry point's return value into a list of :class:`Setting`."""
    if isinstance(obj, Setting):
        return [obj]
    if isinstance(obj, Iterable):
        items = list(obj)
        for item in items:
            if not isinstance(item, Setting):
                raise SurfaceError(
                    f"Surface plugin {ep_name!r} yielded {type(item).__name__}, "
                    f"expected django_aqueduct.surface.Setting."
                )
        return items
    raise SurfaceError(
        f"Surface plugin {ep_name!r} returned {type(obj).__name__}, expected a "
        f"Setting or an iterable of Setting."
    )


def _ep_dist_label(ep: object) -> str:
    """Return the distribution name backing an entry point, falling back to name."""
    dist = getattr(ep, "dist", None)
    name = getattr(dist, "name", None)
    if isinstance(name, str) and name:
        return name
    return str(getattr(ep, "name", "unknown"))


def load_declared_surfaces() -> dict[str, list[Setting]]:
    """Return ``{dist_label: [Setting, ...]}`` from every registered plugin.

    A plugin advertises a :class:`Setting` iterable, or a zero-arg callable
    returning one, under :data:`SURFACE_GROUP`.

    Raises:
        SurfaceError: If a plugin fails to load, call, or yields a non-Setting.
    """
    out: dict[str, list[Setting]] = {}
    for ep in entry_points(group=SURFACE_GROUP):
        try:
            loaded = ep.load()
        except Exception as exc:  # noqa: BLE001
            raise SurfaceError(
                f"Failed to load surface plugin {ep.name!r}: {exc}"
            ) from exc
        if callable(loaded):
            try:
                result = loaded()
            except Exception as exc:  # noqa: BLE001
                raise SurfaceError(
                    f"Surface plugin {ep.name!r} callable raised: {exc}"
                ) from exc
        else:
            result = loaded
        out.setdefault(_ep_dist_label(ep), []).extend(_coerce_settings(result, ep.name))
    return out


# ---------------------------------------------------------------------------
# Gather + precedence
# ---------------------------------------------------------------------------


def gather_surface(
    installed_apps: Sequence[str],
    restrict: Sequence[str] | None = None,
) -> list[SurfaceEntry]:
    """Return the reconciled surface for a project, deterministically ordered.

    Declared surfaces are authoritative: on a setting-name collision the
    declared entry wins over a built-in one. Built-in extractors are scoped to
    packages present in *installed_apps*; declared surfaces are always included
    (a package that advertises one is opting in). *restrict*, when given, limits
    the result to those distribution labels.

    Args:
        installed_apps: Dotted ``INSTALLED_APPS`` names.
        restrict: Optional list of distribution labels to keep.

    Returns:
        ``SurfaceEntry`` list sorted by ``(dist, setting name)``.
    """
    restrict_set = set(restrict) if restrict else None
    installed_roots = {app.split(".")[0] for app in installed_apps}

    by_name: dict[str, SurfaceEntry] = {}

    for dist, settings in load_declared_surfaces().items():
        if restrict_set is not None and dist not in restrict_set:
            continue
        for setting in settings:
            by_name.setdefault(setting.name, SurfaceEntry(dist, setting, "declared"))

    for dist, settings in builtin_surfaces(installed_roots).items():
        if restrict_set is not None and dist not in restrict_set:
            continue
        for setting in settings:
            by_name.setdefault(setting.name, SurfaceEntry(dist, setting, "builtin"))

    return sorted(by_name.values(), key=lambda e: (e.dist, e.setting.name))
