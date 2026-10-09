"""Tests for Open edX plugin-app settings discovery.

Every case writes a real package tree on disk and puts it on ``sys.path``, so
the module resolution under test (meta-path finders → source file → AST) is
the same code path a real generation run takes. Entry points are faked at the
``entry_points`` call site because manufacturing installed distribution
metadata would test setuptools, not this module.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from textwrap import dedent
from unittest.mock import patch

import pytest

from django_aqueduct.discovery.ir import DefaultStrategy, DiscoveryMethod
from django_aqueduct.discovery.openedx_plugins import (
    PluginDiscoveryError,
    PluginSettingsInspector,
    discover_openedx_plugin_settings,
    iter_plugin_settings_modules,
)


@dataclass
class _FakeDist:
    name: str


@dataclass
class _FakeEntryPoint:
    name: str
    value: str
    dist: _FakeDist | None = None


@pytest.fixture()
def plugin_tree(tmp_path, monkeypatch):
    """Return a factory that writes a plugin package and makes it importable."""
    monkeypatch.syspath_prepend(str(tmp_path))

    def _write(package: str, files: dict[str, str]) -> None:
        root = tmp_path / package
        root.mkdir(parents=True, exist_ok=True)
        (root / "__init__.py").write_text("", encoding="utf-8")
        for relative, body in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.parent != root:
                init = path.parent / "__init__.py"
                if not init.exists():
                    init.write_text("", encoding="utf-8")
            path.write_text(dedent(body).lstrip(), encoding="utf-8")
        # A package written after an earlier failed import would otherwise be
        # masked by the negative entry in the finder's cache.
        import importlib  # noqa: PLC0415

        importlib.invalidate_caches()
        for module in [m for m in sys.modules if m.split(".")[0] == package]:
            del sys.modules[module]

    return _write


def _entry_points(*eps):
    """Patch ``entry_points(group=...)`` to return *eps* for any group."""
    return patch(
        "django_aqueduct.discovery.openedx_plugins.entry_points",
        lambda group: list(eps),
    )


# ---------------------------------------------------------------------------
# PluginSettingsInspector
# ---------------------------------------------------------------------------


def test_collects_literal_assignments(plugin_tree):
    """A plugin_settings() literal assignment becomes a typed field."""
    plugin_tree(
        "litplugin",
        {
            "settings/common.py": """
                def plugin_settings(settings):
                    settings.MITX_REDIRECT_ENABLED = True
                    settings.MITX_REDIRECT_LOGIN_URL = "/auth/login/"
                    settings.MITX_REDIRECT_DENY_RE_LIST = []
            """,
        },
    )
    fields = {
        f.name: f
        for f in PluginSettingsInspector("litplugin.settings.common").discover()
    }

    assert set(fields) == {
        "MITX_REDIRECT_ENABLED",
        "MITX_REDIRECT_LOGIN_URL",
        "MITX_REDIRECT_DENY_RE_LIST",
    }
    enabled = fields["MITX_REDIRECT_ENABLED"]
    assert enabled.type.render() == "bool"
    assert enabled.default.strategy is DefaultStrategy.LITERAL
    assert enabled.default.literal is True
    assert fields["MITX_REDIRECT_LOGIN_URL"].type.render() == "str"
    # A mutable literal must render via default_factory, not a shared instance.
    assert fields["MITX_REDIRECT_DENY_RE_LIST"].default.strategy is (
        DefaultStrategy.FACTORY
    )


def test_records_openedx_plugin_provenance(plugin_tree):
    """Fields are tagged with the plugin discovery method and their module."""
    plugin_tree(
        "provplugin",
        {
            "settings/common.py": """
                def plugin_settings(settings):
                    settings.PROV_SETTING = 1
            """,
        },
    )
    (field,) = PluginSettingsInspector(
        "provplugin.settings.common", owning_package="prov-plugin"
    ).discover()

    assert field.provenance.method is DiscoveryMethod.OPENEDX_PLUGIN
    assert field.provenance.source_module == "provplugin.settings.common"
    assert field.owning_package == "prov-plugin"


def test_settings_read_becomes_derived(plugin_tree):
    """The ENV_TOKENS passthrough idiom has no static value, so it is DERIVED."""
    plugin_tree(
        "prodplugin",
        {
            "settings/production.py": """
                def plugin_settings(settings):
                    settings.MITX_REDIRECT_ENABLED = getattr(
                        settings, "ENV_TOKENS", {}
                    ).get("MITX_REDIRECT_ENABLED", settings.MITX_REDIRECT_ENABLED)
            """,
        },
    )
    (field,) = PluginSettingsInspector("prodplugin.settings.production").discover()

    assert field.default.strategy is DefaultStrategy.DERIVED


def test_ignores_mutation_of_host_owned_settings(plugin_tree):
    """`.extend()` / subscript writes mutate a host setting; they declare nothing."""
    plugin_tree(
        "mutplugin",
        {
            "settings/common.py": """
                def plugin_settings(settings):
                    settings.MIDDLEWARE.extend(["mutplugin.middleware.Thing"])
                    settings.FEATURES["ENABLE_MUT"] = True
                    settings.MUT_SETTING = "declared"
            """,
        },
    )
    names = [
        f.name for f in PluginSettingsInspector("mutplugin.settings.common").discover()
    ]

    assert names == ["MUT_SETTING"]


def test_ignores_locals_and_lowercase(plugin_tree):
    """Only UPPERCASE attributes of the settings parameter are settings."""
    plugin_tree(
        "scopeplugin",
        {
            "settings/common.py": """
                OTHER = object()

                def plugin_settings(settings):
                    helper = 1
                    settings.lowercase_thing = helper
                    OTHER.UPPER_ON_SOMETHING_ELSE = 2

                    def nested():
                        settings.NOT_A_SETTING = 3

                    settings.REAL_SETTING = 4
            """,
        },
    )
    names = [
        f.name
        for f in PluginSettingsInspector("scopeplugin.settings.common").discover()
    ]

    assert names == ["REAL_SETTING"]


def test_conditional_assignment_is_derived(plugin_tree):
    """A branch-dependent value must not be frozen to one side of the branch."""
    plugin_tree(
        "condplugin",
        {
            "settings/common.py": """
                import os

                def plugin_settings(settings):
                    if os.environ.get("TOGGLE"):
                        settings.COND_SETTING = "on"
                    else:
                        settings.COND_SETTING = "off"
            """,
        },
    )
    (field,) = PluginSettingsInspector("condplugin.settings.common").discover()

    assert field.default.strategy is DefaultStrategy.DERIVED
    assert field.provenance.conditional is True


def test_renamed_settings_parameter_is_followed(plugin_tree):
    """The framework calls plugin_settings positionally; the name is arbitrary."""
    plugin_tree(
        "renameplugin",
        {
            "settings/common.py": """
                def plugin_settings(conf):
                    conf.RENAMED_SETTING = "value"
            """,
        },
    )
    (field,) = PluginSettingsInspector("renameplugin.settings.common").discover()

    assert field.name == "RENAMED_SETTING"


def test_module_without_plugin_settings_yields_nothing(plugin_tree):
    """A settings module with no hook contributes no fields (and does not raise)."""
    plugin_tree(
        "emptyplugin",
        {"settings/common.py": "CONSTANT = 1\n"},
    )

    assert PluginSettingsInspector("emptyplugin.settings.common").discover() == []


# ---------------------------------------------------------------------------
# AppConfig / entry-point resolution
# ---------------------------------------------------------------------------


_LITERAL_APP = """
    from django.apps import AppConfig


    class LiteralConfig(AppConfig):
        name = "literalplugin"

        plugin_app = {
            "settings_config": {
                "lms.djangoapp": {
                    "common": {"relative_path": "settings.common"},
                    "production": {"relative_path": "settings.production"},
                }
            }
        }
"""

# The constant-reference spelling, as edx-platform's own plugins write it.
_CONSTANT_APP = """
    from django.apps import AppConfig
    from edx_django_utils.plugins import PluginSettings, PluginURLs
    from openedx.core.djangoapps.plugins.constants import ProjectType, SettingsType


    class ConstantConfig(AppConfig):
        name = "constantplugin"

        plugin_app = {
            PluginURLs.CONFIG: {ProjectType.LMS: {PluginURLs.RELATIVE_PATH: "urls"}},
            PluginSettings.CONFIG: {
                ProjectType.CMS: {
                    SettingsType.COMMON: {
                        PluginSettings.RELATIVE_PATH: "settings.common"
                    },
                },
            },
        }
"""


def test_resolves_literal_plugin_app(plugin_tree):
    """A plugin_app written with plain string keys resolves both settings types."""
    plugin_tree(
        "literalplugin",
        {
            "app.py": _LITERAL_APP,
            "settings/common.py": "def plugin_settings(settings):\n    pass\n",
            "settings/production.py": "def plugin_settings(settings):\n    pass\n",
        },
    )
    entry = _FakeEntryPoint("literalplugin", "literalplugin.app:LiteralConfig")
    with _entry_points(entry):
        found = list(iter_plugin_settings_modules("lms.djangoapp"))

    assert [(m.module_path, m.settings_type) for m in found] == [
        ("literalplugin.settings.common", "common"),
        ("literalplugin.settings.production", "production"),
    ]


def test_resolves_constant_reference_plugin_app(plugin_tree):
    """PluginSettings.CONFIG / ProjectType.CMS keys resolve without importing."""
    plugin_tree(
        "constantplugin",
        {
            "app.py": _CONSTANT_APP,
            "settings/common.py": "def plugin_settings(settings):\n    pass\n",
        },
    )
    entry = _FakeEntryPoint("constantplugin", "constantplugin.app:ConstantConfig")
    with _entry_points(entry):
        cms = list(iter_plugin_settings_modules("cms.djangoapp"))
        lms = list(iter_plugin_settings_modules("lms.djangoapp"))

    assert [m.module_path for m in cms] == ["constantplugin.settings.common"]
    # Its only LMS declaration is url_config, which contributes no settings.
    assert lms == []


def test_relative_path_defaults_to_settings(plugin_tree):
    """An omitted relative_path falls back to the framework's "settings" default."""
    plugin_tree(
        "defaultplugin",
        {
            "app.py": """
                from django.apps import AppConfig


                class DefaultConfig(AppConfig):
                    name = "defaultplugin"
                    plugin_app = {
                        "settings_config": {"lms.djangoapp": {"common": {}}}
                    }
            """,
            "settings.py": "def plugin_settings(settings):\n    pass\n",
        },
    )
    entry = _FakeEntryPoint("defaultplugin", "defaultplugin.app:DefaultConfig")
    with _entry_points(entry):
        (found,) = list(iter_plugin_settings_modules("lms.djangoapp"))

    assert found.module_path == "defaultplugin.settings"


def test_appconfig_without_plugin_app_is_skipped_silently(plugin_tree):
    """Most registered AppConfigs extend only URLs or signals — not a warning."""
    plugin_tree(
        "plainplugin",
        {
            "app.py": """
                from django.apps import AppConfig


                class PlainConfig(AppConfig):
                    name = "plainplugin"
            """,
        },
    )
    entry = _FakeEntryPoint("plainplugin", "plainplugin.app:PlainConfig")
    with _entry_points(entry):
        assert list(iter_plugin_settings_modules("lms.djangoapp")) == []


def test_uninstallable_appconfig_warns_rather_than_raising():
    """A dangling entry point is reported, not fatal."""
    entry = _FakeEntryPoint("ghost", "ghost_package.app:GhostConfig")
    with _entry_points(entry):
        found = list(iter_plugin_settings_modules("lms.djangoapp"))

    assert len(found) == 1
    assert isinstance(found[0], str)
    assert "ghost" in found[0]


# ---------------------------------------------------------------------------
# discover_openedx_plugin_settings
# ---------------------------------------------------------------------------


def test_common_default_survives_the_production_passthrough(plugin_tree):
    """production.py's DERIVED re-assignment must not erase common.py's value."""
    plugin_tree(
        "mergeplugin",
        {
            "app.py": """
                from django.apps import AppConfig


                class MergeConfig(AppConfig):
                    name = "mergeplugin"
                    plugin_app = {
                        "settings_config": {
                            "lms.djangoapp": {
                                "common": {"relative_path": "settings.common"},
                                "production": {
                                    "relative_path": "settings.production"
                                },
                            }
                        }
                    }
            """,
            "settings/common.py": """
                def plugin_settings(settings):
                    settings.MERGED_SETTING = True
            """,
            "settings/production.py": """
                def plugin_settings(settings):
                    settings.MERGED_SETTING = settings.ENV_TOKENS.get(
                        "MERGED_SETTING", settings.MERGED_SETTING
                    )
                    settings.PRODUCTION_ONLY = settings.ENV_TOKENS.get("X")
            """,
        },
    )
    entry = _FakeEntryPoint(
        "mergeplugin", "mergeplugin.app:MergeConfig", _FakeDist("merge-plugin")
    )
    with _entry_points(entry):
        result = discover_openedx_plugin_settings("lms.djangoapp")

    fields = {f.name: f for f in result.fields}
    assert result.warnings == []
    assert fields["MERGED_SETTING"].default.strategy is DefaultStrategy.LITERAL
    assert fields["MERGED_SETTING"].default.literal is True
    assert fields["MERGED_SETTING"].owning_package == "merge-plugin"
    # A setting only production.py declares is still worth a field: under
    # aqueduct that module never runs, so nothing else would admit the value.
    assert fields["PRODUCTION_ONLY"].default.strategy is DefaultStrategy.DERIVED


def test_rejects_unknown_project_type():
    """A typo'd project type is a caller error, not a silent empty result."""
    with pytest.raises(PluginDiscoveryError, match="unknown project type"):
        discover_openedx_plugin_settings("worker.djangoapp")


# ---------------------------------------------------------------------------
# No-execution contract
# ---------------------------------------------------------------------------


def test_no_plugin_code_is_executed(plugin_tree):
    """Resolving a module must not run the package __init__ files above it.

    ``importlib.util.find_spec`` imports a module's parent packages to find it,
    so a plugin's ``__init__.py`` would run during generation. These two blow
    up if anything imports them.
    """
    boom = 'raise RuntimeError("plugin code executed during discovery")\n'
    plugin_tree(
        "explodeplugin",
        {
            "__init__.py": boom,
            "settings/__init__.py": boom,
            "app.py": """
                from django.apps import AppConfig


                class ExplodeConfig(AppConfig):
                    name = "explodeplugin"
                    plugin_app = {
                        "settings_config": {
                            "lms.djangoapp": {
                                "common": {"relative_path": "settings.common"}
                            }
                        }
                    }
            """,
            "settings/common.py": """
                def plugin_settings(settings):
                    settings.EXPLODE_SETTING = "safe"
            """,
        },
    )
    entry = _FakeEntryPoint("explodeplugin", "explodeplugin.app:ExplodeConfig")
    with _entry_points(entry):
        result = discover_openedx_plugin_settings("lms.djangoapp")

    assert result.warnings == []
    assert [f.name for f in result.fields] == ["EXPLODE_SETTING"]
    assert "explodeplugin" not in sys.modules


# ---------------------------------------------------------------------------
# Unreadable declarations warn rather than vanishing
# ---------------------------------------------------------------------------


def test_computed_plugin_app_warns(plugin_tree):
    """A plugin_app built by a call cannot be read — say so, don't drop it."""
    plugin_tree(
        "computedplugin",
        {
            "app.py": """
                from django.apps import AppConfig

                from computedplugin.helpers import build_config


                class ComputedConfig(AppConfig):
                    name = "computedplugin"
                    plugin_app = build_config()
            """,
            "helpers.py": "def build_config():\n    return {}\n",
        },
    )
    entry = _FakeEntryPoint("computedplugin", "computedplugin.app:ComputedConfig")
    with _entry_points(entry):
        found = list(iter_plugin_settings_modules("lms.djangoapp"))

    assert len(found) == 1
    assert isinstance(found[0], str)
    assert "not a dict literal" in found[0]
    assert "not discoverable" in found[0]


def test_unresolvable_plugin_app_key_warns(plugin_tree):
    """A key outside the known-constant table is unreadable, not "absent"."""
    plugin_tree(
        "opaqueplugin",
        {
            "app.py": """
                from django.apps import AppConfig

                from opaqueplugin.constants import MyKeys


                class OpaqueConfig(AppConfig):
                    name = "opaqueplugin"
                    plugin_app = {
                        MyKeys.SETTINGS: {
                            "lms.djangoapp": {"common": {}},
                        },
                    }
            """,
            "constants.py": "class MyKeys:\n    SETTINGS = 'settings_config'\n",
        },
    )
    entry = _FakeEntryPoint("opaqueplugin", "opaqueplugin.app:OpaqueConfig")
    with _entry_points(entry):
        found = list(iter_plugin_settings_modules("lms.djangoapp"))

    assert len(found) == 1
    assert isinstance(found[0], str)
    assert "MyKeys.SETTINGS" in found[0]
    assert "could not be resolved statically" in found[0]


def test_computed_relative_path_warns_and_falls_back(plugin_tree):
    """A non-literal relative_path still yields a module, but not silently."""
    plugin_tree(
        "relpathplugin",
        {
            "app.py": """
                from django.apps import AppConfig

                SUFFIX = "common"


                class RelPathConfig(AppConfig):
                    name = "relpathplugin"
                    plugin_app = {
                        "settings_config": {
                            "lms.djangoapp": {
                                "common": {"relative_path": "settings." + SUFFIX}
                            }
                        }
                    }
            """,
            "settings.py": "def plugin_settings(settings):\n    pass\n",
        },
    )
    entry = _FakeEntryPoint("relpathplugin", "relpathplugin.app:RelPathConfig")
    with _entry_points(entry):
        found = list(iter_plugin_settings_modules("lms.djangoapp"))

    warnings = [item for item in found if isinstance(item, str)]
    modules = [item for item in found if not isinstance(item, str)]
    assert len(warnings) == 1
    assert "relative_path is not a string literal" in warnings[0]
    assert [m.module_path for m in modules] == ["relpathplugin.settings"]


# ---------------------------------------------------------------------------
# Merge policy
# ---------------------------------------------------------------------------


def _merge_app(package: str, name: str) -> str:
    """Return an apps.py declaring both settings modules for *package*."""
    return f"""
        from django.apps import AppConfig


        class {name}(AppConfig):
            name = "{package}"
            plugin_app = {{
                "settings_config": {{
                    "lms.djangoapp": {{
                        "common": {{"relative_path": "settings.common"}},
                        "production": {{"relative_path": "settings.production"}},
                    }}
                }}
            }}
    """


def test_production_literal_does_not_override_common_literal(plugin_tree):
    """Only common runs under aqueduct, so its value is the live default."""
    plugin_tree(
        "overrideplugin",
        {
            "app.py": _merge_app("overrideplugin", "OverrideConfig"),
            "settings/common.py": """
                def plugin_settings(settings):
                    settings.FLAG = False
            """,
            "settings/production.py": """
                def plugin_settings(settings):
                    settings.FLAG = True
            """,
        },
    )
    entry = _FakeEntryPoint("overrideplugin", "overrideplugin.app:OverrideConfig")
    with _entry_points(entry):
        result = discover_openedx_plugin_settings("lms.djangoapp")

    (flag,) = result.fields
    assert result.warnings == []
    assert flag.default.literal is False


def test_production_literal_fills_a_derived_common(plugin_tree):
    """A common module with no static value still gets a usable default."""
    plugin_tree(
        "fillplugin",
        {
            "app.py": _merge_app("fillplugin", "FillConfig"),
            "settings/common.py": """
                def plugin_settings(settings):
                    settings.FILLED = settings.SOMETHING_ELSE
            """,
            "settings/production.py": """
                def plugin_settings(settings):
                    settings.FILLED = "concrete"
            """,
        },
    )
    entry = _FakeEntryPoint("fillplugin", "fillplugin.app:FillConfig")
    with _entry_points(entry):
        result = discover_openedx_plugin_settings("lms.djangoapp")

    (filled,) = result.fields
    assert filled.default.strategy is DefaultStrategy.LITERAL
    assert filled.default.literal == "concrete"


def test_one_plugins_production_cannot_override_anothers_common(plugin_tree):
    """Cross-plugin collisions are resolved per plugin first, and reported."""
    plugin_tree(
        "acollide",
        {
            "app.py": """
                from django.apps import AppConfig


                class ACollideConfig(AppConfig):
                    name = "acollide"
                    plugin_app = {
                        "settings_config": {
                            "lms.djangoapp": {
                                "common": {"relative_path": "settings.common"}
                            }
                        }
                    }
            """,
            "settings/common.py": """
                def plugin_settings(settings):
                    settings.SHARED_FLAG = False
            """,
        },
    )
    plugin_tree(
        "zcollide",
        {
            "app.py": """
                from django.apps import AppConfig


                class ZCollideConfig(AppConfig):
                    name = "zcollide"
                    plugin_app = {
                        "settings_config": {
                            "lms.djangoapp": {
                                "production": {"relative_path": "settings.production"}
                            }
                        }
                    }
            """,
            "settings/production.py": """
                def plugin_settings(settings):
                    settings.SHARED_FLAG = True
            """,
        },
    )
    entries = (
        _FakeEntryPoint("acollide", "acollide.app:ACollideConfig"),
        _FakeEntryPoint("zcollide", "zcollide.app:ZCollideConfig"),
    )
    with _entry_points(*entries):
        result = discover_openedx_plugin_settings("lms.djangoapp")

    (flag,) = result.fields
    assert flag.default.literal is False
    assert len(result.warnings) == 1
    assert "SHARED_FLAG" in result.warnings[0]
    assert "'acollide'" in result.warnings[0]
    assert "'zcollide'" in result.warnings[0]
