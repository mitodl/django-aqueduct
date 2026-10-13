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
   defines it, never importing it.  Even locating that source goes through
   :func:`~django_aqueduct.discovery.static.resolve_module_source`, which walks
   the dotted path one segment at a time rather than through
   ``importlib.util.find_spec`` — the latter imports a module's parent
   packages, so a plugin's ``__init__.py`` would run.  The dict keys are
   routinely written as constant references (``PluginSettings.CONFIG``,
   ``ProjectType.LMS``) rather than literals, so a small table of the plugin
   framework's own constant values resolves them (see
   :data:`_CONSTANT_VALUES`).
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
"declare a field so the env/YAML source can carry it". So within a plugin
``common`` wins, and a declaration carrying a value is never displaced by one
that does not (see :func:`_merge_within_plugin`).

That merge happens strictly *per plugin*, before anything is merged across
plugins — otherwise one plugin's ``production`` module could override a
different plugin's ``common`` default. Two plugins declaring the same setting
is a genuine conflict with no static answer, so it is resolved deterministically
and reported (see :func:`_merge_across_plugins`).
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import NamedTuple

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
    resolve_module_source,
)

logger = logging.getLogger(__name__)

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
            installed, a ``plugin_app`` whose keys could not be resolved — plus
            one per setting that two different plugins both declare. These are
            reported rather than raised so one malformed plugin cannot fail a
            whole generation run.
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


@dataclass(frozen=True)
class _Lookup:
    """Outcome of looking one key up in a ``plugin_app`` sub-dict.

    A missing key and an unreadable declaration are different things, and
    conflating them is how a plugin's settings go missing in silence. ``value``
    set means the key was found; both fields empty means the dict was readable
    and genuinely does not declare the key; ``unreadable`` set means the static
    resolver could not tell, and the caller must warn rather than assume
    absence.

    Attributes:
        value: The value node for the key, when it was found.
        unreadable: Why the lookup could not be trusted, phrased to drop into
            a warning. Empty when the lookup was conclusive.
    """

    value: ast.expr | None = None
    unreadable: str = ""


def _dict_lookup(node: ast.expr | None, key: str) -> _Lookup:
    """Look *key* up in an ``ast.Dict``, distinguishing absent from unreadable.

    A ``plugin_app`` built by a call (``plugin_app = build_config()``), spliced
    together with ``**``, or keyed by a constant outside :data:`_CONSTANT_VALUES`
    cannot be read statically. Reporting that as "declares no settings" makes
    every one of the plugin's fields vanish with nothing on stderr, so it is
    reported as unreadable instead.
    """
    if node is None:
        return _Lookup()
    if not isinstance(node, ast.Dict):
        return _Lookup(
            unreadable=f"{ast.unparse(node)!r} is not a dict literal",
        )
    unresolved: list[str] = []
    for key_node, value_node in zip(node.keys, node.values, strict=True):
        if key_node is None:
            # ``{**other, ...}`` — the spliced-in keys are not visible here.
            unresolved.append("**-unpacking")
            continue
        resolved = _const(key_node)
        if resolved is None:
            unresolved.append(ast.unparse(key_node))
            continue
        if resolved == key:
            return _Lookup(value=value_node)
    if unresolved:
        return _Lookup(
            unreadable=(
                f"key {key!r} is absent but {', '.join(unresolved)} "
                f"could not be resolved statically"
            ),
        )
    return _Lookup()


def _string_value(node: ast.expr | None) -> str | None:
    """Return *node*'s value when it is a plain string literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


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
            source_path = resolve_module_source(module_name)
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ImportError, KeyError) as exc:
            # KeyError for the same reason as the settings-module read below:
            # resolution touches ``sys.modules`` for namespace parents, and a
            # plugin that cannot be read must cost only its own fields.  This
            # site resolves the AppConfig module itself, so a plugin whose
            # *top-level* package is a namespace one arrives here first.
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
        if settings_config.unreadable:
            yield (
                f"{entry_point.name}: cannot read {class_name}.{_PLUGIN_APP_ATTR} "
                f"in {module_name!r} ({settings_config.unreadable}); any settings "
                f"it contributes are not discoverable."
            )
            continue

        project_config = _dict_lookup(settings_config.value, project_type)
        if project_config.unreadable:
            yield (
                f"{entry_point.name}: cannot read the {project_type!r} entry of "
                f"{class_name}.{_PLUGIN_APP_ATTR} ({project_config.unreadable}); "
                f"any settings it contributes are not discoverable."
            )
            continue
        if project_config.value is None:
            continue

        distribution = _entry_point_distribution(entry_point)
        for settings_type in _SETTINGS_TYPES:
            type_config = _dict_lookup(project_config.value, settings_type)
            if type_config.unreadable:
                yield (
                    f"{entry_point.name}: cannot read the {settings_type!r} entry "
                    f"of {class_name}.{_PLUGIN_APP_ATTR} "
                    f"({type_config.unreadable}); any settings it contributes are "
                    f"not discoverable."
                )
                continue
            if type_config.value is None:
                continue

            path_lookup = _dict_lookup(type_config.value, "relative_path")
            relative_path = _string_value(path_lookup.value)
            if relative_path is None:
                if path_lookup.unreadable or path_lookup.value is not None:
                    yield (
                        f"{entry_point.name}: {settings_type} relative_path is not "
                        f"a string literal; assuming "
                        f"{_DEFAULT_RELATIVE_PATH!r}."
                    )
                relative_path = _DEFAULT_RELATIVE_PATH

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


def _is_concrete(field: SettingField) -> bool:
    """Whether *field*'s default carries a value a human can read and override."""
    return field.default.strategy in _CONCRETE_STRATEGIES


class _PluginDeclaration(NamedTuple):
    """One plugin's declaration of one setting, tagged with its source module.

    The ``settings_type`` is what makes the within-plugin merge order-free:
    ``common`` wins because it is the module that runs, not because it happened
    to be visited first.
    """

    settings_type: str
    field: SettingField


def _merge_within_plugin(
    existing: _PluginDeclaration, incoming: _PluginDeclaration
) -> _PluginDeclaration:
    """Combine two declarations of the same setting from *one* plugin.

    Under aqueduct the overlay base is ``<svc>.envs.common``, so a plugin's
    ``common`` module is the only one whose ``plugin_settings()`` ever runs —
    its value *is* the live default. A ``production`` declaration matters for a
    different reason: its existence says the plugin intends the setting to be
    operator-overridable, which here means "declare a field". So:

    * ``common`` wins whenever both declarations carry a value. Taking
      ``production``'s would emit a default the running system never has —
      common ``FLAG = False`` plus production ``FLAG = True`` is ``False``.
    * A concrete declaration still beats a non-concrete one either way. A
      ``production`` module re-reading ``settings.ENV_TOKENS`` is statically
      ``DERIVED`` with no value in it, and so is a ``common`` module that
      computes its value from another setting; dropping the side that has a
      value would leave the field with no default at all.
    """
    if _is_concrete(existing.field) != _is_concrete(incoming.field):
        return existing if _is_concrete(existing.field) else incoming
    return incoming if incoming.settings_type == "common" else existing


def _merge_across_plugins(
    existing: SettingField, incoming: SettingField
) -> SettingField:
    """Resolve two *different* plugins declaring the same setting.

    There is no correct answer here — at runtime whichever plugin
    ``add_plugins()`` reaches last wins, and that order is installation
    metadata we deliberately do not replay. The policy is therefore the
    defensible, deterministic one: the first plugin in entry-point-name order
    wins, except that a declaration carrying a value is never displaced by one
    that does not. Every collision is reported as a warning so the operator can
    pin the value explicitly rather than depend on this.
    """
    if _is_concrete(existing) or not _is_concrete(incoming):
        return existing
    return incoming


def discover_openedx_plugin_settings(project_type: str) -> PluginDiscoveryResult:
    """Discover every setting contributed by plugins for *project_type*.

    Declarations are merged per plugin first (``common`` is the authority, see
    :func:`_merge_within_plugin`) and only then across plugins, so one plugin's
    ``production`` module can never override another plugin's ``common``
    default.

    Args:
        project_type: The plugin entry-point group to read —
            ``"lms.djangoapp"`` or ``"cms.djangoapp"``.

    Returns:
        A :class:`PluginDiscoveryResult` holding the merged fields (sorted by
        name), a warning per plugin that had to be skipped, and a warning per
        setting two plugins both declare.
    """
    if project_type not in PROJECT_TYPES:
        raise PluginDiscoveryError(
            f"unknown project type {project_type!r}; "
            f"expected one of {', '.join(PROJECT_TYPES)}."
        )

    result = PluginDiscoveryResult()
    # app name -> setting name -> that plugin's winning declaration.
    # Insertion order is first-seen order, which the cross-plugin merge uses.
    per_plugin: dict[str, dict[str, _PluginDeclaration]] = {}

    for item in iter_plugin_settings_modules(project_type):
        if isinstance(item, str):
            result.warnings.append(item)
            continue
        inspector = PluginSettingsInspector(
            item.module_path, owning_package=item.distribution
        )
        try:
            discovered = inspector.discover()
        except (ImportError, OSError, SyntaxError, KeyError) as exc:
            # KeyError: resolution reads ``sys.modules[parent].__path__`` for a
            # namespace package, which the resolver keeps populated — but a
            # plugin that cannot be read must only cost its own fields, never
            # the whole run, so the escape hatch is here too rather than only
            # at the one site inside the resolver known to raise it.  Logged
            # because this also spans the AST walk, where a KeyError would be
            # an internal bug wearing a "cannot read settings module" label.
            logger.debug(
                "reading settings module %r for %s failed",
                item.module_path,
                item.app_name,
                exc_info=True,
            )
            result.warnings.append(
                f"{item.app_name}: cannot read settings module "
                f"{item.module_path!r}: {exc}"
            )
            continue
        declarations = per_plugin.setdefault(item.app_name, {})
        for found in discovered:
            incoming = _PluginDeclaration(item.settings_type, found)
            existing = declarations.get(found.name)
            declarations[found.name] = (
                incoming
                if existing is None
                else _merge_within_plugin(existing, incoming)
            )

    # app name of the plugin whose declaration currently wins, per setting.
    owners: dict[str, str] = {}
    by_name: dict[str, SettingField] = {}
    for app_name, plugin_declarations in per_plugin.items():
        for name, declaration in plugin_declarations.items():
            held = by_name.get(name)
            if held is None:
                by_name[name] = declaration.field
                owners[name] = app_name
                continue
            kept = _merge_across_plugins(held, declaration.field)
            winner = owners[name] if kept is held else app_name
            result.warnings.append(
                f"{name}: declared by both {owners[name]!r} and {app_name!r}; "
                f"kept {winner!r}'s declaration. Pin the value in your own "
                f"settings if that is not the one you want."
            )
            by_name[name] = kept
            owners[name] = winner

    result.fields = [by_name[name] for name in sorted(by_name)]
    return result
