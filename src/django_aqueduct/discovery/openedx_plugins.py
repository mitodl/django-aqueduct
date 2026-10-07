"""Static discovery of settings contributed by Open edX plugin apps.

An Open edX plugin app does not write its settings into the host project's
settings module.  It registers an ``AppConfig`` under the ``lms.djangoapp`` /
``cms.djangoapp`` entry-point group, and the host's ``common.py`` calls
``edx_django_utils.plugins.add_plugins()``, which imports each plugin's
settings module and calls its ``plugin_settings(settings)`` function to mutate
the live settings object:

.. code-block:: python

    # openedx_companion_auth/settings/common.py
    def plugin_settings(settings):
        settings.MITX_REDIRECT_ENABLED = True
        settings.MITX_REDIRECT_LOGIN_URL = "/auth/login/ol-oauth2/?auth_entry=login"

:mod:`~django_aqueduct.discovery.static` cannot see any of that: the names are
never assigned at module scope in a module the project names, so the generated
model has no field for them.  Under an env-var settings source a missing field
is not a cosmetic gap — pydantic-settings collects values *per declared field*,
so an undeclared ``MITX_REDIRECT_ENABLED`` in the environment is dropped
outright rather than parsed, and the setting cannot be overridden at all.

This module closes that gap, staying inside codegen v2's contract that no
project code is executed:

1. **Which plugins?**  ``importlib.metadata.entry_points(group=project_type)``
   reads installed distribution metadata — no import of plugin code.
2. **Which settings modules?**  Each entry point names an ``AppConfig`` class;
   its ``plugin_app`` dict is read by **parsing the source** of the module that
   defines it, never importing it.  The dict keys are routinely written as
   constant references (``PluginSettings.CONFIG``, ``ProjectType.LMS``) rather
   than literals, so a small table of the plugin framework's own constant
   values resolves them (see :data:`_CONSTANT_VALUES`).
3. **Which settings?**  :class:`PluginSettingsInspector` parses the resolved
   settings module and collects every ``settings.UPPERCASE = <expr>``
   assignment inside ``plugin_settings()``, reusing
   :class:`~django_aqueduct.discovery.static.StaticModuleInspector`'s default
   capture — so a literal becomes ``LITERAL``/``FACTORY``, a reproducible
   expression becomes ``EXPR``, and anything reading another setting (the
   ``settings.ENV_TOKENS.get(...)`` idiom that fills most ``production.py``
   plugin settings) becomes ``DERIVED``.

Merge order
-----------
A plugin may ship both a ``common`` and a ``production`` settings module. Under
django-aqueduct only the ``common`` one ever runs — the overlay base is
``<svc>.envs.common``, and that is the module whose ``add_plugins()`` call
executes — so ``common`` is the authority for a field's default. A
``production`` module still matters: its existence is what tells us the plugin
*intends* the setting to be operator-overridable, which under aqueduct means
"declare a field so the env/YAML source can carry it". Accordingly a concrete
default is never replaced by a non-concrete one (see :func:`_merge_field`).
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path

from django_aqueduct.discovery.ir import (
    DefaultStrategy,
    DiscoveryMethod,
    Provenance,
    SettingField,
)
from django_aqueduct.discovery.static import (
    StaticModuleInspector,
    _ImportTable,
    _iter_scoped_statements,
)

#: Entry-point groups the Open edX plugin framework registers AppConfigs under.
LMS_PROJECT_TYPE = "lms.djangoapp"
CMS_PROJECT_TYPE = "cms.djangoapp"
PROJECT_TYPES = (LMS_PROJECT_TYPE, CMS_PROJECT_TYPE)

#: Settings types to read, in the order they are merged (see module docstring).
_SETTINGS_TYPES = ("common", "production")

#: Name of the AppConfig attribute holding the plugin declaration, and of the
#: function inside a plugin settings module. Mirrors
#: ``edx_django_utils.plugins.constants``.
_PLUGIN_APP_ATTR = "plugin_app"
_PLUGIN_SETTINGS_FUNC = "plugin_settings"

#: Relative path used when a plugin's settings config omits ``relative_path``
#: (``PluginSettings.DEFAULT_RELATIVE_PATH``).
_DEFAULT_RELATIVE_PATH = "settings"

# Values of the plugin framework's constant classes, keyed by the
# ``Class.ATTR`` form they are written as in a plugin's apps.py. Resolving
# these statically is what lets the ``plugin_app`` dict be read without
# importing anything: plugins write
# ``PluginSettings.CONFIG: {ProjectType.LMS: {SettingsType.COMMON: ...}}``
# about as often as they write the plain strings.
#
# Mirrors ``edx_django_utils.plugins.constants`` and
# ``openedx.core.djangoapps.plugins.constants``. The non-settings CONFIG keys
# are included so an unresolved ``PluginURLs.CONFIG`` can never be mistaken
# for the settings config.
_CONSTANT_VALUES: dict[str, str] = {
    "PluginSettings.CONFIG": "settings_config",
    "PluginSettings.RELATIVE_PATH": "relative_path",
    "PluginSettings.DEFAULT_RELATIVE_PATH": _DEFAULT_RELATIVE_PATH,
    "PluginURLs.CONFIG": "url_config",
    "PluginSignals.CONFIG": "signals_config",
    "PluginContexts.CONFIG": "view_context_config",
    "ProjectType.LMS": LMS_PROJECT_TYPE,
    "ProjectType.CMS": CMS_PROJECT_TYPE,
    "SettingsType.PRODUCTION": "production",
    "SettingsType.COMMON": "common",
    "SettingsType.DEVSTACK": "devstack",
    "SettingsType.TEST": "test",
}


class PluginDiscoveryError(Exception):
    """Raised when a plugin's declaration is present but cannot be read."""


@dataclass
class PluginDiscoveryResult:
    """Fields discovered from plugin apps, plus anything that had to be skipped.

    Attributes:
        fields: One :class:`SettingField` per setting assigned by some plugin's
            ``plugin_settings()``, sorted by name.
        warnings: Human-readable notes about plugins that were skipped — an
            unparseable apps.py, a settings module that is declared but not
            installed, a ``plugin_app`` whose keys could not be resolved. These
            are reported rather than raised so one malformed plugin cannot fail
            a whole generation run.
    """

    fields: list[SettingField] = dataclass_field(default_factory=list)
    warnings: list[str] = dataclass_field(default_factory=list)


def _const(node: ast.expr) -> str | None:
    """Resolve *node* to a string key, from a literal or a known constant.

    Handles the two forms a ``plugin_app`` key is ever written in: a plain
    string (``"settings_config"``) and an attribute reference to one of the
    plugin framework's constant classes (``PluginSettings.CONFIG``). Anything
    else — a computed key, a constant this table does not know — returns
    ``None`` so the caller can skip it rather than guess.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return _CONSTANT_VALUES.get(f"{node.value.id}.{node.attr}")
    return None


def _dict_lookup(node: ast.expr | None, key: str) -> ast.expr | None:
    """Return the value node for *key* in an ``ast.Dict``, else ``None``."""
    if not isinstance(node, ast.Dict):
        return None
    for key_node, value_node in zip(node.keys, node.values, strict=True):
        if key_node is not None and _const(key_node) == key:
            return value_node
    return None


def _string_value(node: ast.expr | None) -> str | None:
    """Return *node*'s value when it is a plain string literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _module_source(module_path: str) -> Path:
    """Locate a module's source file without executing the module itself.

    ``find_spec`` imports the *parent packages* of ``module_path`` (that is how
    the import system locates a submodule) but never the module itself, which
    is where all plugin settings code lives.
    """
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.find_spec(module_path)
    if spec is None or spec.origin is None:
        raise PluginDiscoveryError(
            f"could not locate source for {module_path!r}; is it installed?"
        )
    return Path(spec.origin)


def _find_class(tree: ast.Module, class_name: str) -> ast.ClassDef | None:
    """Return the module-level ``class <class_name>`` definition, if present."""
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef) and stmt.name == class_name:
            return stmt
    return None


def _class_attribute(node: ast.ClassDef, attr: str) -> ast.expr | None:
    """Return the value assigned to class attribute *attr* in *node*'s body."""
    for stmt in node.body:
        if not isinstance(stmt, ast.Assign | ast.AnnAssign):
            continue
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id == attr:
                return stmt.value
    return None


@dataclass(frozen=True)
class PluginSettingsModule:
    """One plugin settings module to inspect.

    Attributes:
        module_path: Dotted path of the settings module, e.g.
            ``"openedx_companion_auth.settings.common"``.
        settings_type: ``"common"`` or ``"production"`` — which hook the
            module is registered for.
        app_name: The plugin AppConfig's ``name`` (its Python package).
        distribution: The installed distribution that provides the plugin, for
            attribution. Empty when the entry point carries no distribution.
    """

    module_path: str
    settings_type: str
    app_name: str
    distribution: str = ""


def _entry_point_distribution(entry_point: EntryPoint) -> str:
    """Return the distribution name behind *entry_point*, or an empty string."""
    dist = getattr(entry_point, "dist", None)
    return getattr(dist, "name", "") or ""


def iter_plugin_settings_modules(
    project_type: str,
) -> Iterator[PluginSettingsModule | str]:
    """Yield each plugin settings module registered for *project_type*.

    Yields a :class:`PluginSettingsModule` per resolved module and a plain
    ``str`` for each warning — a plugin whose declaration could not be read is
    reported and skipped, never fatal.

    Only distribution metadata is read; the AppConfig's ``plugin_app`` is
    resolved by parsing its source. No plugin code is imported.
    """
    for entry_point in sorted(entry_points(group=project_type), key=lambda e: e.name):
        module_name, _, class_name = entry_point.value.partition(":")
        if not class_name:
            yield (
                f"{entry_point.name}: entry point {entry_point.value!r} names no "
                f"AppConfig class; skipped."
            )
            continue
        try:
            source_path = _module_source(module_name)
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
        except (PluginDiscoveryError, OSError, SyntaxError, ImportError) as exc:
            yield f"{entry_point.name}: cannot read {module_name!r}: {exc}"
            continue

        class_def = _find_class(tree, class_name)
        if class_def is None:
            yield (
                f"{entry_point.name}: {class_name!r} is not a module-level class "
                f"in {module_name!r}; skipped."
            )
            continue

        app_name = _string_value(_class_attribute(class_def, "name"))
        if app_name is None:
            yield (
                f"{entry_point.name}: {class_name}.name is not a string literal; "
                f"cannot resolve its settings module path."
            )
            continue

        plugin_app = _class_attribute(class_def, _PLUGIN_APP_ATTR)
        if plugin_app is None:
            # Not every registered AppConfig extends settings; most declare
            # only URLs or signals. Silent by design — not a warning.
            continue

        settings_config = _dict_lookup(plugin_app, "settings_config")
        project_config = _dict_lookup(settings_config, project_type)
        if project_config is None:
            continue

        distribution = _entry_point_distribution(entry_point)
        for settings_type in _SETTINGS_TYPES:
            type_config = _dict_lookup(project_config, settings_type)
            if type_config is None:
                continue
            relative_path = (
                _string_value(_dict_lookup(type_config, "relative_path"))
                or _DEFAULT_RELATIVE_PATH
            )
            yield PluginSettingsModule(
                module_path=f"{app_name}.{relative_path}",
                settings_type=settings_type,
                app_name=app_name,
                distribution=distribution,
            )


class PluginSettingsInspector(StaticModuleInspector):
    """Discover the settings a plugin's ``plugin_settings()`` assigns.

    Subclasses :class:`~django_aqueduct.discovery.static.StaticModuleInspector`
    to reuse its default capture (literal / reproducible expression / derived)
    and its import table, and replaces only *where it looks*: module-level
    ``NAME = ...`` assignments become ``settings.NAME = ...`` assignments
    inside the ``plugin_settings`` function.

    Args:
        module_path: Dotted path of the plugin settings module.
        source_file: Optional explicit source path (as on the base class).
        owning_package: Distribution to record on each field's
            ``owning_package``.
    """

    def __init__(
        self,
        module_path: str,
        source_file: str | Path | None = None,
        owning_package: str = "",
    ) -> None:
        """Store the module path, optional source file, and owning package."""
        super().__init__(module_path, source_file)
        self._owning_package = owning_package

    def discover(self) -> list[SettingField]:
        """Return one :class:`SettingField` per ``settings.NAME`` assignment."""
        path = self._resolve_source()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        source_lines = source.splitlines()

        imports = _ImportTable()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.add_import(node)
            elif isinstance(node, ast.ImportFrom):
                imports.add_importfrom(node)

        func = self._plugin_settings_func(tree)
        if func is None:
            return []
        settings_param = self._settings_param(func)
        if settings_param is None:
            return []

        fields: dict[str, SettingField] = {}
        # Direct body statements are unconditional; anything reached through a
        # control-flow statement is conditional, exactly as the base class
        # treats module-level assignments. Nested defs/classes are not
        # descended into (``_iter_scoped_statements``), so a local in a helper
        # closure is never mistaken for a setting.
        for stmt in func.body:
            self._collect(
                stmt,
                settings_param,
                imports,
                source,
                source_lines,
                fields,
                conditional=False,
            )
        for stmt in func.body:
            if isinstance(stmt, ast.If | ast.Try | ast.For | ast.While | ast.With):
                for inner in _iter_scoped_statements(stmt):
                    self._collect(
                        inner,
                        settings_param,
                        imports,
                        source,
                        source_lines,
                        fields,
                        conditional=True,
                    )

        return [fields[name] for name in sorted(fields)]

    @staticmethod
    def _plugin_settings_func(tree: ast.Module) -> ast.FunctionDef | None:
        """Return the module-level ``def plugin_settings(settings)``, if any."""
        for stmt in tree.body:
            if isinstance(stmt, ast.FunctionDef) and stmt.name == _PLUGIN_SETTINGS_FUNC:
                return stmt
        return None

    @staticmethod
    def _settings_param(func: ast.FunctionDef) -> str | None:
        """Return the name bound to the settings module inside *func*.

        The framework calls ``plugin_settings(settings_module)`` positionally,
        so the first positional parameter is the settings object whatever it is
        named.
        """
        positional = func.args.posonlyargs + func.args.args
        return positional[0].arg if positional else None

    def _collect(
        self,
        stmt: ast.AST,
        settings_param: str,
        imports: _ImportTable,
        source: str,
        source_lines: list[str],
        fields: dict[str, SettingField],
        *,
        conditional: bool,
    ) -> None:
        """Record *stmt* when it assigns ``<settings_param>.UPPERCASE = <expr>``.

        Augmenting calls (``settings.MIDDLEWARE.extend([...])``) and
        subscript writes (``settings.FEATURES["X"] = True``) are deliberately
        not settings *declarations* — they mutate a value the host already
        owns — so only plain attribute assignment is collected.
        """
        if not isinstance(stmt, ast.Assign | ast.AnnAssign):
            return
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        value = stmt.value
        if value is None:
            return
        for target in targets:
            if not isinstance(target, ast.Attribute):
                continue
            if not (
                isinstance(target.value, ast.Name) and target.value.id == settings_param
            ):
                continue
            name = target.attr
            if not name.isupper():
                continue
            if name in fields and conditional:
                continue
            discovered = self._build_field(
                name,
                value,
                stmt.lineno,
                imports,
                source,
                source_lines,
                conditional=conditional,
            )
            discovered.owning_package = self._owning_package
            discovered.provenance = Provenance(
                source_module=self._module_path,
                method=DiscoveryMethod.OPENEDX_PLUGIN,
                lineno=stmt.lineno,
                conditional=conditional,
            )
            fields[name] = discovered


#: Default strategies that carry a real value a human can read and override.
_CONCRETE_STRATEGIES = frozenset(
    {
        DefaultStrategy.LITERAL,
        DefaultStrategy.FACTORY,
        DefaultStrategy.EXPR,
        DefaultStrategy.REQUIRED,
    }
)


def _merge_field(existing: SettingField, incoming: SettingField) -> SettingField:
    """Combine two declarations of the same setting from one plugin.

    A plugin's ``production.py`` almost always re-assigns what its
    ``common.py`` declared, reading the operator's value out of
    ``settings.ENV_TOKENS`` and falling back to the common default. Statically
    that is a ``DERIVED`` default with no value in it, so letting the later
    module win outright would throw away the only real default the plugin has.
    Keep whichever declaration actually carries a value.
    """
    if (
        existing.default.strategy in _CONCRETE_STRATEGIES
        and incoming.default.strategy not in _CONCRETE_STRATEGIES
    ):
        return existing
    return incoming


def discover_openedx_plugin_settings(project_type: str) -> PluginDiscoveryResult:
    """Discover every setting contributed by plugins for *project_type*.

    Args:
        project_type: The plugin entry-point group to read —
            ``"lms.djangoapp"`` or ``"cms.djangoapp"``.

    Returns:
        A :class:`PluginDiscoveryResult` holding the merged fields (sorted by
        name) and a warning per plugin that had to be skipped.
    """
    if project_type not in PROJECT_TYPES:
        raise PluginDiscoveryError(
            f"unknown project type {project_type!r}; "
            f"expected one of {', '.join(PROJECT_TYPES)}."
        )

    result = PluginDiscoveryResult()
    by_name: dict[str, SettingField] = {}

    for item in iter_plugin_settings_modules(project_type):
        if isinstance(item, str):
            result.warnings.append(item)
            continue
        inspector = PluginSettingsInspector(
            item.module_path, owning_package=item.distribution
        )
        try:
            discovered = inspector.discover()
        except (ImportError, OSError, SyntaxError) as exc:
            result.warnings.append(
                f"{item.app_name}: cannot read settings module "
                f"{item.module_path!r}: {exc}"
            )
            continue
        for found in discovered:
            existing = by_name.get(found.name)
            by_name[found.name] = (
                found if existing is None else _merge_field(existing, found)
            )

    result.fields = [by_name[name] for name in sorted(by_name)]
    return result
