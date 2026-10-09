"""Tests for locating a module's source without importing it.

Every case writes a real package tree on disk and puts it on ``sys.path``, so
resolution runs through the same meta-path finders a real generation run uses.
The cases that matter here are the ones where a package in the chain has no
``__init__.py``: PEP 420 makes it a namespace package, which imports perfectly
well at runtime -- the Open edX plugin framework loads plugin settings with a
plain ``importlib.import_module`` -- but resolving one *without* importing the
parent is what used to raise a bare ``KeyError`` and abort generation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from django_aqueduct.discovery.static import resolve_module_source


@pytest.fixture()
def tree(tmp_path, monkeypatch):
    """Return a factory writing a package tree, with explicit ``__init__`` control.

    Only the directories named in *packages* get an ``__init__.py``; everything
    else on the path is left as a namespace package. That is the whole point of
    these tests, so it is spelled out per case rather than inferred.
    """
    monkeypatch.syspath_prepend(str(tmp_path))

    def _write(files: dict[str, str], packages: tuple[str, ...]) -> Path:
        for relative in packages:
            directory = tmp_path / relative
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "__init__.py").write_text("", encoding="utf-8")
        for relative, body in files.items():
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        import importlib  # noqa: PLC0415

        importlib.invalidate_caches()
        return tmp_path

    return _write


def test_resolves_through_a_namespace_sub_package(tree):
    """``plugin/settings/`` without an ``__init__.py`` still resolves.

    The shape edx-sysadmin, ol-openedx-chat and rapid-response-xblock ship.
    """
    root = tree(
        {"nsplugin/settings/common.py": "SETTING = 1\n"},
        packages=("nsplugin",),
    )

    resolved = resolve_module_source("nsplugin.settings.common")

    assert resolved == root / "nsplugin" / "settings" / "common.py"


def test_resolves_through_several_stacked_namespace_packages(tree):
    """Each namespace level anchors on the level above, so depth must work."""
    root = tree(
        {"deepplugin/conf/envs/common.py": "SETTING = 1\n"},
        packages=("deepplugin",),
    )

    resolved = resolve_module_source("deepplugin.conf.envs.common")

    assert resolved == root / "deepplugin" / "conf" / "envs" / "common.py"


def test_resolves_when_the_top_level_is_also_a_namespace_package(tree):
    """No ``__init__.py`` anywhere in the chain."""
    root = tree(
        {"allns/settings/common.py": "SETTING = 1\n"},
        packages=(),
    )

    resolved = resolve_module_source("allns.settings.common")

    assert resolved == root / "allns" / "settings" / "common.py"


def test_regular_packages_still_resolve(tree):
    """The ordinary layout is unaffected by the namespace handling."""
    root = tree(
        {"regplugin/settings/common.py": "SETTING = 1\n"},
        packages=("regplugin", "regplugin/settings"),
    )

    resolved = resolve_module_source("regplugin.settings.common")

    assert resolved == root / "regplugin" / "settings" / "common.py"


def test_nothing_in_the_chain_is_executed(tree):
    """The no-execution contract: resolution must not run plugin code.

    Making the parent's ``__path__`` visible is a hair's breadth from simply
    importing the parent, and importing is precisely what codegen v2 forbids --
    a plugin's ``__init__.py`` can touch the database, read env, or register
    signal handlers. Prove the distinction with a side effect that would be
    impossible to miss.
    """
    marker = (
        tree(
            {
                "sideplugin/__init__.py": (
                    "from pathlib import Path\n"
                    "Path(__file__).parent.joinpath('EXECUTED').write_text('yes')\n"
                ),
                "sideplugin/settings/common.py": "SETTING = 1\n",
            },
            packages=(),
        )
        / "sideplugin"
        / "EXECUTED"
    )

    resolve_module_source("sideplugin.settings.common")

    assert not marker.exists(), "resolution imported the parent package"


def test_sys_modules_is_left_untouched(tree):
    """The stubs are scaffolding, not state: nothing survives the call."""
    tree({"cleanplugin/settings/common.py": "SETTING = 1\n"}, packages=())
    before = set(sys.modules)

    resolve_module_source("cleanplugin.settings.common")

    assert set(sys.modules) == before


def test_sys_modules_is_left_untouched_when_resolution_fails(tree):
    """Including when the walk aborts partway down."""
    tree({"failplugin/settings/common.py": "SETTING = 1\n"}, packages=())
    before = set(sys.modules)

    with pytest.raises(ImportError):
        resolve_module_source("failplugin.settings.nosuchmodule")

    assert set(sys.modules) == before


def test_a_genuinely_imported_parent_is_not_replaced(tree):
    """An already-imported module keeps its own identity and ``__path__``."""
    tree({"liveplugin/settings/common.py": "SETTING = 1\n"}, packages=("liveplugin",))
    import importlib  # noqa: PLC0415

    real = importlib.import_module("liveplugin")
    try:
        resolve_module_source("liveplugin.settings.common")
        assert sys.modules["liveplugin"] is real
    finally:
        for name in [m for m in sys.modules if m.split(".")[0] == "liveplugin"]:
            del sys.modules[name]


def test_a_namespace_package_named_as_the_target_is_still_an_error(tree):
    """Resolving *to* a namespace package has no source, and must say so."""
    tree({"leafns/settings/common.py": "SETTING = 1\n"}, packages=("leafns",))

    with pytest.raises(ImportError, match="has no Python source"):
        resolve_module_source("leafns.settings")


def test_a_missing_module_reports_the_segment_that_was_not_found(tree):
    tree({"realplugin/settings/common.py": "SETTING = 1\n"}, packages=("realplugin",))

    with pytest.raises(ImportError, match="no parent package 'nosuchplugin'"):
        resolve_module_source("nosuchplugin.settings.common")
