"""Package attribution for discovered settings fields.

Determines which installed package owns each settings name using five
complementary strategies, applied in priority order:

1. **Django core** — ``django.conf.global_settings`` is imported directly;
   every UPPERCASE name it defines is attributed to ``"django"``.

2. **Imported-reference attribution** — a setting whose value is a reference
   pulled from a package (a callable, class, or constant, e.g.
   ``WIKI_CAN_ASSIGN = wiki.core.perms.can_assign``) is captured by static
   discovery as an ``EXPR`` default carrying that import; the setting is
   attributed to the package the reference comes from, via the expression's
   imports mapped to their PyPI distribution names. This is the static
   replacement for reflecting a *live* callable's module.

3. **Known package APIs** — packages that expose their settings as structured
   Python objects are queried directly:

   * **Celery** — ``celery.app.defaults._OLD_SETTING_KEYS`` is the canonical
     list of all 194 Django-style (``CELERY_*`` / ``BROKER_*``) setting names
     that Celery maintained for backwards compatibility.
   * **Django REST Framework** — ``rest_framework.settings.DEFAULTS`` gives
     the sub-keys; the top-level ``REST_FRAMEWORK`` key is added explicitly.

4. **AST scan** — each installed application's package is scanned for Python
   files; any ``getattr(settings, 'SETTING_NAME', ...)`` or
   ``settings.SETTING_NAME`` expression in those files reveals which settings
   the package reads.  Files named ``settings.py``, ``conf.py``, ``apps.py``
   etc. are prioritised.

5. **Static prefix/name table** — a built-in list of well-known prefix
   patterns (e.g. ``"CORS_"`` → ``"django-cors-headers"``) covers packages
   that do not expose their settings in any of the above ways.

Any setting not matched by strategies 1–5 is attributed to ``"project"``.

Usage::

    from django_aqueduct.discovery.package_attributor import PackageAttributor

    attributor = PackageAttributor(installed_apps=installed)
    attribution = attributor.attribute(fields)
    for f in fields:
        f.owning_package = attribution[f.name]
"""

from __future__ import annotations

import ast
import importlib
import os
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django_aqueduct.discovery.ir import SettingField  # pragma: no cover

# ---------------------------------------------------------------------------
# Public sentinel labels
# ---------------------------------------------------------------------------

LABEL_PROJECT = "project"
LABEL_DJANGO = "django"

# ---------------------------------------------------------------------------
# Distribution-name lookup
# ---------------------------------------------------------------------------


def _build_dist_map() -> dict[str, str]:
    """Return ``{python_pkg_root: pypi_dist_name}`` from ``importlib.metadata``.

    Returns an empty dict if ``importlib.metadata`` is unavailable or raises.
    """
    try:
        from importlib.metadata import packages_distributions  # noqa: PLC0415

        raw = packages_distributions()
        return {pkg: dists[0] for pkg, dists in raw.items() if dists}
    except Exception:  # noqa: BLE001
        return {}


# Module-level cache — built once per process.
_DIST_MAP: dict[str, str] = _build_dist_map()


def _dist_label(pkg_root: str) -> str:
    """Return the PyPI distribution name for a Python package root.

    Falls back to *pkg_root* itself when the distribution cannot be found
    (e.g. for editable installs not yet registered with importlib.metadata).

    Args:
        pkg_root: Top-level Python package name, e.g. ``"rest_framework"``.

    Returns:
        PyPI distribution name, e.g. ``"djangorestframework"``.
    """
    return _DIST_MAP.get(pkg_root, pkg_root)


def _module_to_dist_label(module_name: str) -> str | None:
    """Return the PyPI dist label for a dotted module name.

    Returns ``None`` when the top-level package is not found in the
    distribution map, which typically means it is project code rather
    than a third-party package.

    Args:
        module_name: A dotted Python module name, e.g. ``"rest_framework.views"``.

    Returns:
        Distribution label or ``None``.
    """
    pkg_root = module_name.split(".")[0]
    return _DIST_MAP.get(pkg_root)


# ---------------------------------------------------------------------------
# Strategy 1: Django core names
# ---------------------------------------------------------------------------


def _django_core_names() -> dict[str, str]:
    """Return ``{name: 'django'}`` for every setting in ``global_settings``.

    Delegates the ``global_settings`` import to
    :func:`~django_aqueduct.discovery.dependency_surface.django_core_names` so
    attribution and the dependency-surface report share one extraction pass.

    Returns:
        Mapping of setting name → ``"django"``.
    """
    from django_aqueduct.discovery.dependency_surface import (  # noqa: PLC0415
        django_core_names,
    )

    return dict.fromkeys(django_core_names(), LABEL_DJANGO)


# ---------------------------------------------------------------------------
# Strategy 3a: Celery compat table
# ---------------------------------------------------------------------------


def _celery_old_setting_names() -> dict[str, str]:
    """Return ``{name: 'celery'}`` using Celery's own Django-compat key table.

    ``celery.app.defaults._OLD_SETTING_KEYS`` is the authoritative list of
    all Django-style (``CELERY_*`` / ``BROKER_*``) setting names that Celery
    maintained for the old Django integration. The import is delegated to
    :func:`~django_aqueduct.discovery.dependency_surface.celery_old_setting_names`
    so attribution and the surface report share one extraction pass.

    Returns:
        Mapping of old-style Celery setting name → ``"celery"``.
    """
    from django_aqueduct.discovery.dependency_surface import (  # noqa: PLC0415
        celery_old_setting_names,
    )

    names = celery_old_setting_names()
    if not names:
        return {}
    return dict.fromkeys(names, _dist_label("celery"))


# ---------------------------------------------------------------------------
# Strategy 3b: DRF settings
# ---------------------------------------------------------------------------


def _drf_names() -> dict[str, str]:
    """Return ``{'REST_FRAMEWORK': 'djangorestframework'}``.

    DRF's ``DEFAULTS`` dict holds the sub-keys consumed *within* the
    ``REST_FRAMEWORK`` dict; the only top-level Django setting is
    ``REST_FRAMEWORK`` itself.

    Returns:
        Mapping with a single entry for the top-level DRF setting.
    """
    from django_aqueduct.discovery.dependency_surface import (  # noqa: PLC0415
        drf_available,
    )

    if drf_available():
        return {"REST_FRAMEWORK": _dist_label("rest_framework")}
    return {}


# ---------------------------------------------------------------------------
# Strategy 4: AST scan
# ---------------------------------------------------------------------------

_PRIORITY_FILENAMES: frozenset[str] = frozenset(
    {
        "settings.py",
        "defaults.py",
        "conf.py",
        "apps.py",
        "middleware.py",
        "checks.py",
        "config.py",
    }
)

# Attribute names on local variables that act as the Django settings proxy
_SETTINGS_VAR_NAMES: frozenset[str] = frozenset({"settings", "django_settings", "conf"})


def extract_settings_names_from_source(src: str) -> set[str]:
    """Extract setting names read from ``django.conf.settings`` via AST.

    Detects two patterns:

    * ``getattr(settings, 'SETTING_NAME', ...)``
    * ``settings.SETTING_NAME``

    Only names that are entirely uppercase and longer than three characters
    are returned to avoid false positives from short identifiers.

    Args:
        src: Python source code as a string.

    Returns:
        Set of discovered setting name strings.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()

    names: set[str] = set()

    for node in ast.walk(tree):
        # getattr(settings, 'SETTING_NAME', ...)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            val = node.args[1].value
            if val.isupper() and len(val) > 3:
                names.add(val)

        # settings.SETTING_NAME
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in _SETTINGS_VAR_NAMES
            and node.attr.isupper()
            and len(node.attr) > 3
        ):
            names.add(node.attr)

    return names


def _iter_package_files(pkg_root: str, max_files: int) -> list[str]:
    """Return up to *max_files* Python files from a package.

    Files in ``_PRIORITY_FILENAMES`` appear first; test directories are
    excluded.

    Args:
        pkg_root: Top-level Python package name.
        max_files: Maximum number of files to return.

    Returns:
        List of absolute file paths.
    """
    try:
        pkg = importlib.import_module(pkg_root)
        pkg_paths: list[str] = list(getattr(pkg, "__path__", []))
    except ImportError:
        return []

    priority: list[str] = []
    rest: list[str] = []

    for pkg_path in pkg_paths:
        for dirpath, dirnames, filenames in os.walk(pkg_path):
            # Prune test and migration directories in-place
            dirnames[:] = [
                d
                for d in dirnames
                if "test" not in d.lower() and d not in ("migrations", "__pycache__")
            ]
            for fname in filenames:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(dirpath, fname)
                if fname in _PRIORITY_FILENAMES:
                    priority.append(fpath)
                else:
                    rest.append(fpath)

    return (priority + rest)[:max_files]


def _ast_scan_package(pkg_root: str, max_files: int = 20) -> set[str]:
    """Scan a package's Python files and return settings names it reads.

    Args:
        pkg_root: Top-level Python package name (e.g. ``"corsheaders"``).
        max_files: Maximum number of files to scan per package.

    Returns:
        Set of UPPERCASE setting names found in the package source.
    """
    names: set[str] = set()
    for fpath in _iter_package_files(pkg_root, max_files):
        try:
            with open(fpath, encoding="utf-8", errors="ignore") as f:
                src = f.read()
        except OSError:
            continue
        names |= extract_settings_names_from_source(src)
    return names


# ---------------------------------------------------------------------------
# Strategy 5: Static prefix/name table
# ---------------------------------------------------------------------------

#: Built-in attribution rules.  Each entry is ``(pattern, package_label)``:
#:
#: * If *pattern* ends with ``"_"`` it is a **prefix** match.
#: * Otherwise it is an **exact** match.
#:
#: Rules are evaluated in order; the first match wins.
BUILTIN_RULES: list[tuple[str, str]] = [
    # ---- Celery (old Django-integration style) ----
    ("BROKER_", "celery"),
    ("CELERY_", "celery"),
    ("CELERYD_", "celery"),
    ("CELERYBEAT_", "celery"),
    # ---- Django REST Framework ----
    ("REST_FRAMEWORK", "djangorestframework"),
    # ---- drf-yasg ----
    ("SWAGGER_SETTINGS", "drf-yasg"),
    # ---- Python Social Auth ----
    ("SOCIAL_AUTH_", "social-auth-app-django"),
    # ---- django-cors-headers ----
    ("CORS_", "django-cors-headers"),
    # ---- django-oauth-toolkit ----
    # OAUTH2_ only; a bare OAUTH_ prefix is too broad (matches unrelated
    # project settings) and was dropped.
    ("OAUTH2_", "django-oauth-toolkit"),
    # ---- JWT auth (edx-drf-extensions; JWT_AUTH etc.) ----
    # Was mis-attributed to the abandoned djangorestframework-jwt package.
    ("JWT_", "edx-drf-extensions"),
    # ---- AWS storage ----
    # AWS_* settings are consumed by django-storages, not boto3 directly.
    ("AWS_", "django-storages"),
    # ---- django-push-notifications ----
    ("PUSH_NOTIFICATIONS_", "django-push-notifications"),
    ("FCM_", "django-push-notifications"),
    # ---- django-simple-history ----
    ("SIMPLE_HISTORY_", "django-simple-history"),
    # ---- django-waffle ----
    ("WAFFLE_", "django-waffle"),
    # ---- django-ratelimit ----
    ("RATELIMIT_", "django-ratelimit"),
]


def _matches_rule(name: str, pattern: str) -> bool:
    """Return True if *name* satisfies *pattern*.

    Args:
        name: An UPPERCASE settings name.
        pattern: A prefix pattern (ends with ``"_"``) or an exact name.

    Returns:
        ``True`` when *name* matches the pattern.
    """
    if pattern.endswith("_"):
        return name.startswith(pattern)
    return name == pattern


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------


class PackageAttributor:
    """Attribute settings fields to the Python package that owns them.

    Resolution order (first match wins):

    1. **Django core** — settings in ``django.conf.global_settings``.
    2. **Callable inspection** — ``inspect.getmodule()`` on function/class
       defaults; the result is mapped to its PyPI distribution name.
    3. **Celery compat table** — ``celery.app.defaults._OLD_SETTING_KEYS``.
    4. **DRF** — the ``REST_FRAMEWORK`` top-level key.
    5. **AST scan** — ``getattr(settings, 'X')`` / ``settings.X`` patterns
       found in each installed package's source files.
    6. **User-provided rules** — ``extra_rules`` passed to the constructor.
    7. **Built-in static rules** — :data:`BUILTIN_RULES`.
    8. ``"project"`` — settings that could not be attributed elsewhere.

    Args:
        installed_apps: List of dotted app names from ``INSTALLED_APPS``.
            Used to discover which packages to AST-scan.  When ``None`` or
            empty the AST scan step is skipped.
        extra_rules: Additional ``(pattern, label)`` pairs prepended to the
            built-in rules.  Prefix patterns end with ``"_"``; all others
            are exact-match.
        ast_scan_max_files: Maximum number of source files to scan per
            package.  Increase for thorough attribution; decrease for speed.

    Example::

        from django.apps import apps
        from django_aqueduct.discovery.package_attributor import PackageAttributor

        installed = [app.name for app in apps.get_app_configs()]
        attributor = PackageAttributor(installed_apps=installed)
        attribution = attributor.attribute(fields)
        for f in fields:
            f.owning_package = attribution[f.name]
    """

    def __init__(
        self,
        installed_apps: Sequence[str] | None = None,
        extra_rules: Sequence[tuple[str, str]] | None = None,
        ast_scan_max_files: int = 20,
    ) -> None:
        """Initialise the attributor.

        Args:
            installed_apps: Dotted app names for AST-based discovery.
            extra_rules: Additional prefix/exact rules (prepended to built-ins).
            ast_scan_max_files: Per-package AST scan file limit.
        """
        self._installed_apps: list[str] = list(installed_apps or [])
        self._extra_rules: list[tuple[str, str]] = list(extra_rules or [])
        self._ast_scan_max_files = ast_scan_max_files
        # Lazily built on first call to attribute()
        self._dynamic_map: dict[str, str] | None = None

    # ------------------------------------------------------------------
    # Internal: build the dynamic map (strategies 1, 3, 4)
    # ------------------------------------------------------------------

    def _build_dynamic_map(self) -> dict[str, str]:
        """Build the attribution map from all dynamic sources.

        Strategies 1 (Django core), 3a (Celery), 3b (DRF), and 4 (AST
        scan) all write into a shared dict using ``setdefault`` so that
        earlier strategies take precedence over later ones.

        Returns:
            Mapping of setting name → package label.
        """
        result: dict[str, str] = {}

        # Strategy 1: Django core
        result.update(_django_core_names())

        # Strategy 3a: Celery compat table
        for name, label in _celery_old_setting_names().items():
            result.setdefault(name, label)

        # Strategy 3b: DRF
        for name, label in _drf_names().items():
            result.setdefault(name, label)

        # Strategy 4: AST scan of installed packages
        seen_roots: set[str] = set()
        for app_name in self._installed_apps:
            pkg_root = app_name.split(".")[0]
            if pkg_root in ("django",) or pkg_root in seen_roots:
                continue
            seen_roots.add(pkg_root)
            label = _dist_label(pkg_root)
            for name in _ast_scan_package(pkg_root, self._ast_scan_max_files):
                result.setdefault(name, label)

        return result

    # ------------------------------------------------------------------
    # Internal: attribute a single field
    # ------------------------------------------------------------------

    def _attribute_one(self, f: SettingField) -> str:
        """Return the package label for a single field.

        Args:
            f: The field to attribute.

        Returns:
            A package label string (e.g. ``"django"``, ``"celery"``).
        """
        assert self._dynamic_map is not None  # noqa: S101

        # Django core / Celery / DRF / AST-scan dynamic map
        if f.name in self._dynamic_map:
            return self._dynamic_map[f.name]

        # Imported-reference attribution — the static replacement for v1's
        # live ``inspect.getmodule()`` strategy. A setting whose value is a
        # reference pulled from a package (a callable, class, or constant, e.g.
        # ``WIKI_CAN_ASSIGN = wiki.core.perms.can_assign``) is captured as an
        # EXPR default carrying that import; attribute the setting to the
        # package the reference comes from. Sorted for deterministic output.
        for spec in sorted(f.default.expr_imports, key=lambda s: s.sort_key()):
            label = _module_to_dist_label(spec.module)
            if label is not None:
                return label

        # User-provided rules
        for pattern, label in self._extra_rules:
            if _matches_rule(f.name, pattern):
                return label

        # Strategy 7 — built-in static rules
        for pattern, label in BUILTIN_RULES:
            if _matches_rule(f.name, pattern):
                return label

        # Strategy 8 — project fallback
        return LABEL_PROJECT

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def attribute(self, fields: Sequence[SettingField]) -> dict[str, str]:
        """Return ``{field_name: package_label}`` for every field.

        Builds the dynamic attribution map on the first call and caches
        it for subsequent calls on the same instance.

        Args:
            fields: The discovered settings fields to attribute.

        Returns:
            Mapping from field name to package label.
        """
        if self._dynamic_map is None:
            self._dynamic_map = self._build_dynamic_map()

        return {f.name: self._attribute_one(f) for f in fields}
