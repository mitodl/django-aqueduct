"""Tests for dependency-surface extraction, declaration, and gathering."""

from __future__ import annotations

import sys
import types

import pytest

from django_aqueduct.discovery import dependency_surface as ds
from django_aqueduct.surface import UNSET, Setting

# ---------------------------------------------------------------------------
# Built-in extractor: Django (installed in the test environment)
# ---------------------------------------------------------------------------


def test_extract_django_has_known_settings() -> None:
    by_name = {s.name: s for s in ds.extract_django()}
    assert "DEBUG" in by_name
    assert by_name["DEBUG"].type == "bool"
    assert by_name["DEBUG"].default is False
    assert by_name["ALLOWED_HOSTS"].type == "list"
    # Every name is UPPERCASE and carries a (possibly None) default.
    assert all(s.name.isupper() for s in by_name.values())
    assert all(s.has_default for s in by_name.values())


def test_django_core_names_matches_extract() -> None:
    assert ds.django_core_names() == {s.name for s in ds.extract_django()}


# ---------------------------------------------------------------------------
# Built-in extractor: DRF (stubbed — DRF is not a test dependency)
# ---------------------------------------------------------------------------


def _install_fake_drf(monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = types.ModuleType("rest_framework")
    settings_mod = types.ModuleType("rest_framework.settings")
    settings_mod.DEFAULTS = {  # type: ignore[attr-defined]
        "PAGE_SIZE": None,
        "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.Basic"],
    }
    settings_mod.IMPORT_STRINGS = ["DEFAULT_AUTHENTICATION_CLASSES"]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rest_framework", pkg)
    monkeypatch.setitem(sys.modules, "rest_framework.settings", settings_mod)


def test_extract_drf_absent_returns_empty() -> None:
    if ds.drf_available():
        pytest.skip("DRF is installed; the absent-path test does not apply.")
    assert ds.extract_drf() == []


def test_extract_drf_stubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_drf(monkeypatch)
    by_name = {s.name: s for s in ds.extract_drf()}
    assert set(by_name) == {
        "REST_FRAMEWORK.PAGE_SIZE",
        "REST_FRAMEWORK.DEFAULT_AUTHENTICATION_CLASSES",
    }
    assert by_name["REST_FRAMEWORK.PAGE_SIZE"].default is None
    import_key = by_name["REST_FRAMEWORK.DEFAULT_AUTHENTICATION_CLASSES"]
    # An IMPORT_STRINGS key holds dotted paths, but often a *list* of them —
    # the reported type must match the default actually emitted alongside it.
    assert import_key.type == "list"
    assert import_key.default == ["rest_framework.authentication.Basic"]
    assert "IMPORT_STRINGS" in import_key.description


def test_extract_drf_scalar_import_string_is_str(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_drf(monkeypatch)
    mod = sys.modules["rest_framework.settings"]
    mod.DEFAULTS = {"DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.Page"}  # type: ignore[attr-defined]
    mod.IMPORT_STRINGS = ["DEFAULT_PAGINATION_CLASS"]  # type: ignore[attr-defined]
    (setting,) = ds.extract_drf()
    assert setting.type == "str"


# ---------------------------------------------------------------------------
# Built-in extractor: Celery (stubbed — Celery is not a test dependency)
# ---------------------------------------------------------------------------


def _install_fake_celery(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Opt:
        def __init__(self, default: object, type_: str) -> None:
            self.default = default
            self.type = type_

    defaults_mod = types.ModuleType("celery.app.defaults")
    defaults_mod._OLD_SETTING_KEYS = ["CELERY_TASK_SERIALIZER", "BROKER_URL"]  # type: ignore[attr-defined]
    defaults_mod._TO_NEW_KEY = {  # type: ignore[attr-defined]
        "CELERY_TASK_SERIALIZER": "task_serializer",
        "BROKER_URL": "broker_url",
    }
    defaults_mod.NAMESPACES = {}  # type: ignore[attr-defined]
    defaults_mod.flatten = lambda _ns: [  # type: ignore[attr-defined]
        ("task_serializer", _Opt("json", "string")),
        ("broker_url", _Opt(None, "string")),
    ]
    app_pkg = types.ModuleType("celery.app")
    app_pkg.defaults = defaults_mod  # type: ignore[attr-defined]
    celery_pkg = types.ModuleType("celery")
    monkeypatch.setitem(sys.modules, "celery", celery_pkg)
    monkeypatch.setitem(sys.modules, "celery.app", app_pkg)
    monkeypatch.setitem(sys.modules, "celery.app.defaults", defaults_mod)


def test_extract_celery_absent_returns_empty() -> None:
    try:
        import celery.app.defaults  # noqa: F401
    except Exception:
        assert ds.extract_celery() == []
    else:
        pytest.skip("Celery is installed; the absent-path test does not apply.")


def test_extract_celery_stubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_celery(monkeypatch)
    by_name = {s.name: s for s in ds.extract_celery()}
    assert set(by_name) == {"CELERY_TASK_SERIALIZER", "BROKER_URL"}
    assert by_name["CELERY_TASK_SERIALIZER"].default == "json"
    assert by_name["CELERY_TASK_SERIALIZER"].type == "str"
    assert by_name["BROKER_URL"].default is None
    assert ds.celery_old_setting_names() == {"CELERY_TASK_SERIALIZER", "BROKER_URL"}


def test_extract_celery_tolerates_non_dict_to_new_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # _TO_NEW_KEY is a Celery private; if a release changes its shape we degrade
    # to name-only settings rather than raising out of the command.
    _install_fake_celery(monkeypatch)
    sys.modules["celery.app.defaults"]._TO_NEW_KEY = ["not", "a", "dict"]  # type: ignore[attr-defined]
    by_name = {s.name: s for s in ds.extract_celery()}
    assert set(by_name) == {"CELERY_TASK_SERIALIZER", "BROKER_URL"}
    assert all(not s.has_default for s in by_name.values())


# ---------------------------------------------------------------------------
# Declared-surface entry points
# ---------------------------------------------------------------------------


def _ep(name: str, loaded: object, dist_name: str | None = None) -> object:
    dist = types.SimpleNamespace(name=dist_name) if dist_name else None
    return types.SimpleNamespace(
        name=name, value=f"{name}:surface", load=lambda: loaded, dist=dist
    )


def _fake_entry_points(*eps: object):
    def _loader(group: str):
        assert group == ds.SURFACE_GROUP
        return list(eps)

    return _loader


def test_load_declared_surfaces_callable(monkeypatch: pytest.MonkeyPatch) -> None:
    surface = [Setting("MYPKG_FLAG", type="bool", default=False)]
    ep = _ep("mypkg", lambda: surface, dist_name="my-pkg")
    monkeypatch.setattr(ds, "entry_points", _fake_entry_points(ep))
    result = ds.load_declared_surfaces()
    assert result == {"my-pkg": surface}


def test_load_declared_surfaces_iterable_not_callable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    surface = [Setting("A"), Setting("B")]
    ep = _ep("p", surface, dist_name="p-dist")
    monkeypatch.setattr(ds, "entry_points", _fake_entry_points(ep))
    assert ds.load_declared_surfaces() == {"p-dist": surface}


def test_load_declared_surfaces_sorts_entry_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # entry_points() ordering is unspecified; the loader must not inherit it,
    # since load order decides the winner of a name collision in gather_surface.
    zeta = _ep("zeta", [Setting("SHARED", default="zeta")], dist_name="zeta-dist")
    alpha = _ep("alpha", [Setting("SHARED", default="alpha")], dist_name="alpha-dist")

    monkeypatch.setattr(ds, "entry_points", _fake_entry_points(zeta, alpha))
    reversed_order = ds.load_declared_surfaces()
    monkeypatch.setattr(ds, "entry_points", _fake_entry_points(alpha, zeta))
    natural_order = ds.load_declared_surfaces()

    assert list(reversed_order) == ["alpha-dist", "zeta-dist"]
    assert list(natural_order) == list(reversed_order)

    monkeypatch.setattr(ds, "entry_points", _fake_entry_points(zeta, alpha))
    (winner,) = [
        e
        for e in ds.gather_surface(["django.contrib.auth"])
        if e.setting.name == "SHARED"
    ]
    assert winner.dist == "alpha-dist"


def test_load_declared_surfaces_bad_type_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ep = _ep("p", lambda: [object()], dist_name="p")
    monkeypatch.setattr(ds, "entry_points", _fake_entry_points(ep))
    with pytest.raises(ds.SurfaceError, match="expected"):
        ds.load_declared_surfaces()


def test_load_declared_surfaces_callable_raises_is_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> list[Setting]:
        raise RuntimeError("nope")

    ep = _ep("p", _boom, dist_name="p")
    monkeypatch.setattr(ds, "entry_points", _fake_entry_points(ep))
    with pytest.raises(ds.SurfaceError, match="raised"):
        ds.load_declared_surfaces()


# ---------------------------------------------------------------------------
# gather_surface: precedence, scoping, restrict
# ---------------------------------------------------------------------------


def test_gather_declared_wins_over_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    # A declared surface for a name Django also defines must win.
    declared = [Setting("DEBUG", type="bool", default=True, description="declared")]
    ep = _ep("p", lambda: declared, dist_name="my-dist")
    monkeypatch.setattr(ds, "entry_points", _fake_entry_points(ep))

    entries = ds.gather_surface(["django.contrib.auth"])
    debug = [e for e in entries if e.setting.name == "DEBUG"]
    assert len(debug) == 1
    assert debug[0].provider == "declared"
    assert debug[0].dist == "my-dist"


def test_gather_builtin_scoped_to_installed_apps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_drf(monkeypatch)
    monkeypatch.setattr(ds, "entry_points", _fake_entry_points())

    # rest_framework not in INSTALLED_APPS -> no DRF rows.
    no_drf = ds.gather_surface(["django.contrib.auth"])
    assert not any(e.dist == "djangorestframework" for e in no_drf)

    with_drf = ds.gather_surface(["django.contrib.auth", "rest_framework"])
    assert any(e.setting.name.startswith("REST_FRAMEWORK.") for e in with_drf)


def test_gather_restrict_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ds, "entry_points", _fake_entry_points())
    entries = ds.gather_surface(["django.contrib.auth"], restrict=["nonexistent"])
    assert entries == []


def test_gather_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ds, "entry_points", _fake_entry_points())
    first = ds.gather_surface(["django.contrib.auth"])
    second = ds.gather_surface(["django.contrib.auth"])
    assert [(e.dist, e.setting.name) for e in first] == [
        (e.dist, e.setting.name) for e in second
    ]
    # Sorted by (dist, name).
    keys = [(e.dist, e.setting.name) for e in first]
    assert keys == sorted(keys)


def test_extract_django_default_can_be_none() -> None:
    # Guard the UNSET/None distinction survives extraction end-to-end.
    none_defaults = [s for s in ds.extract_django() if s.default is None]
    assert none_defaults, "expected at least one Django default of None"
    assert all(s.has_default for s in none_defaults)
    assert all(s.default is not UNSET for s in none_defaults)
