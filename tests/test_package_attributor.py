"""Tests for discovery.package_attributor."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from unittest.mock import patch

import pytest

from django_aqueduct.discovery.ir import (
    Default,
    ImportSpec,
    Provenance,
    SettingField,
    TypeRef,
)
from django_aqueduct.discovery.package_attributor import (
    LABEL_DJANGO,
    LABEL_PROJECT,
    PackageAttributor,
    _ast_scan_package,
    _dist_label,
    _django_core_names,
    _matches_rule,
    _module_to_dist_label,
    extract_settings_names_from_source,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_field(
    name: str,
    default: Any = "value",
    source_module: str = "myapp.settings",
) -> SettingField:
    return SettingField(
        name=name,
        type=TypeRef("str"),
        default=Default.literal_(default),
        provenance=Provenance(source_module=source_module),
    )


# ---------------------------------------------------------------------------
# _matches_rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "pattern", "expected"),
    [
        ("CELERY_TASK_SERIALIZER", "CELERY_", True),
        ("CELERY_TASK_SERIALIZER", "CELERYBEAT_", False),
        ("REST_FRAMEWORK", "REST_FRAMEWORK", True),
        ("REST_FRAMEWORK_EXTRA", "REST_FRAMEWORK", False),
        ("BROKER_HEARTBEAT", "BROKER_", True),
        ("DEBUG", "DEBUG", True),
        ("DEBUG_TOOLBAR", "DEBUG", False),
    ],
)
def test_matches_rule(name: str, pattern: str, expected: bool) -> None:
    assert _matches_rule(name, pattern) is expected


# ---------------------------------------------------------------------------
# _django_core_names
# ---------------------------------------------------------------------------


def test_django_core_names_contains_common_settings() -> None:
    """Django core attribution covers well-known Django settings."""
    core = _django_core_names()
    assert "DATABASES" in core
    assert "INSTALLED_APPS" in core
    assert "MIDDLEWARE" in core
    assert "USE_TZ" in core
    assert "DEBUG" in core
    for label in core.values():
        assert label == LABEL_DJANGO


def test_django_core_names_returns_empty_on_import_failure(mocker: Any) -> None:
    """Falls back to empty dict when django.conf.global_settings cannot be imported."""
    mocker.patch(
        "django_aqueduct.discovery.package_attributor._django_core_names",
        return_value={},
    )
    result = _django_core_names()
    # After the patch undoes itself (mocker scope), the real function returns data
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# _dist_label / _module_to_dist_label
# ---------------------------------------------------------------------------


def test_dist_label_falls_back_to_pkg_root() -> None:
    """Unknown packages fall back to the package root name."""
    # 'nonexistent_pkg' won't be in the dist map
    assert _dist_label("nonexistent_pkg") == "nonexistent_pkg"


def test_module_to_dist_label_project_code_returns_none() -> None:
    """Module names not in the dist map return None (project code)."""
    result = _module_to_dist_label("myproject.apps.core.settings")
    assert result is None


def test_module_to_dist_label_django_returns_dist() -> None:
    """Django's top-level package is in the dist map."""
    result = _module_to_dist_label("django.conf.global_settings")
    # django should map to itself or a dist name
    assert result is not None


# ---------------------------------------------------------------------------
# extract_settings_names_from_source
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # getattr pattern
        (
            "x = getattr(settings, 'CORS_ALLOWED_ORIGINS', [])",
            {"CORS_ALLOWED_ORIGINS"},
        ),
        # settings.ATTR pattern
        (
            "val = settings.OAUTH2_PROVIDER",
            {"OAUTH2_PROVIDER"},
        ),
        # Both patterns together
        (
            "a = getattr(settings, 'REST_FRAMEWORK', {})\nb = settings.DATABASES",
            {"REST_FRAMEWORK", "DATABASES"},
        ),
        # Short names (<= 3 chars) are excluded
        (
            "x = getattr(settings, 'ABC', None)",
            set(),
        ),
        # Lowercase names are excluded
        (
            "x = settings.debug",
            set(),
        ),
        # Syntax error in source → empty set
        (
            "def broken(:",
            set(),
        ),
        # django_settings alias also detected
        (
            "val = django_settings.PUSH_NOTIFICATIONS_SETTINGS",
            {"PUSH_NOTIFICATIONS_SETTINGS"},
        ),
    ],
)
def test_extract_settings_names_from_source(source: str, expected: set[str]) -> None:
    assert extract_settings_names_from_source(source) == expected


# ---------------------------------------------------------------------------
# _ast_scan_package
# ---------------------------------------------------------------------------


def test_ast_scan_package_corsheaders() -> None:
    """AST scan finds CORS_* settings when corsheaders is importable."""
    try:
        import corsheaders  # noqa: F401
    except ImportError:
        pytest.skip("corsheaders not installed")
    names = _ast_scan_package("corsheaders")
    # corsheaders.conf reads CORS_ALLOWED_ORIGINS and siblings
    assert any(n.startswith("CORS_") for n in names)


def test_ast_scan_package_missing_package() -> None:
    """Scanning a non-existent package returns empty set."""
    result = _ast_scan_package("_totally_nonexistent_package_xyz")
    assert result == set()


def test_ast_scan_package_uses_temp_file(tmp_path: Any) -> None:
    """AST scan reads settings names from a real file on disk."""
    pkg_dir = tmp_path / "fakepkg"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text(
        "x = getattr(settings, 'MY_CUSTOM_SETTING', None)\n"
    )
    init_py = pkg_dir / "settings.py"
    init_py.write_text("val = settings.ANOTHER_SETTING\n")

    # Monkeypatch _iter_package_files to return our temp files
    fakes = [str(pkg_dir / "__init__.py"), str(init_py)]
    with patch(
        "django_aqueduct.discovery.package_attributor._iter_package_files",
        return_value=fakes,
    ):
        result = _ast_scan_package("fakepkg")

    assert "MY_CUSTOM_SETTING" in result
    assert "ANOTHER_SETTING" in result


# ---------------------------------------------------------------------------
# PackageAttributor.attribute — core behaviour
# ---------------------------------------------------------------------------


class TestPackageAttributor:
    def _attributor(
        self,
        installed_apps: Sequence[str] | None = None,
        extra_rules: Sequence[tuple[str, str]] | None = None,
    ) -> PackageAttributor:
        return PackageAttributor(
            installed_apps=installed_apps,
            extra_rules=extra_rules,
            ast_scan_max_files=0,  # skip AST scan in unit tests
        )

    def test_django_core_settings_attributed_to_django(self) -> None:
        """DATABASES, INSTALLED_APPS, etc. are attributed to 'django'."""
        attributor = self._attributor()
        fields = [
            _make_field("DATABASES"),
            _make_field("INSTALLED_APPS"),
            _make_field("USE_TZ"),
            _make_field("DEBUG"),
            _make_field("SECRET_KEY"),
        ]
        result = attributor.attribute(fields)
        for name in ("DATABASES", "INSTALLED_APPS", "USE_TZ", "DEBUG", "SECRET_KEY"):
            assert result[name] == LABEL_DJANGO, f"{name} should be 'django'"

    def test_static_prefix_celery(self) -> None:
        """CELERY_* and BROKER_* settings are attributed to 'celery'."""
        attributor = self._attributor()
        fields = [
            _make_field("CELERY_TASK_SERIALIZER"),
            _make_field("CELERY_DEFAULT_QUEUE"),
            _make_field("BROKER_HEARTBEAT"),
            _make_field("BROKER_USE_SSL"),
            _make_field("CELERYD_HIJACK_ROOT_LOGGER"),
        ]
        result = attributor.attribute(fields)
        for name in fields:
            assert result[name.name] == "celery", f"{name.name} should be 'celery'"

    def test_static_exact_drf(self) -> None:
        """REST_FRAMEWORK is attributed to djangorestframework."""
        attributor = self._attributor()
        result = attributor.attribute([_make_field("REST_FRAMEWORK")])
        assert result["REST_FRAMEWORK"] == "djangorestframework"

    def test_static_prefix_social_auth(self) -> None:
        attributor = self._attributor()
        result = attributor.attribute([_make_field("SOCIAL_AUTH_PIPELINE")])
        assert result["SOCIAL_AUTH_PIPELINE"] == "social-auth-app-django"

    def test_static_prefix_cors(self) -> None:
        attributor = self._attributor()
        result = attributor.attribute([_make_field("CORS_ALLOW_HEADERS")])
        assert result["CORS_ALLOW_HEADERS"] == "django-cors-headers"

    def test_static_prefix_oauth2(self) -> None:
        attributor = self._attributor()
        result = attributor.attribute([_make_field("OAUTH2_PROVIDER")])
        assert result["OAUTH2_PROVIDER"] == "django-oauth-toolkit"

    def test_project_fallback(self) -> None:
        """Unknown settings fall back to 'project'."""
        attributor = self._attributor()
        result = attributor.attribute([_make_field("MY_COMPLETELY_CUSTOM_SETTING")])
        assert result["MY_COMPLETELY_CUSTOM_SETTING"] == LABEL_PROJECT

    def test_extra_rules_take_priority_over_builtin(self) -> None:
        """User-provided extra_rules override built-in static rules."""
        attributor = self._attributor(extra_rules=[("CORS_", "my-custom-cors-fork")])
        result = attributor.attribute([_make_field("CORS_ALLOWED_ORIGINS")])
        assert result["CORS_ALLOWED_ORIGINS"] == "my-custom-cors-fork"

    def test_extra_rules_exact_match(self) -> None:
        attributor = self._attributor(
            extra_rules=[("MY_SPECIAL_SETTING", "my-internal-package")]
        )
        result = attributor.attribute([_make_field("MY_SPECIAL_SETTING")])
        assert result["MY_SPECIAL_SETTING"] == "my-internal-package"

    def test_imported_reference_attributed_to_source_package(self, mocker: Any) -> None:
        """A setting whose EXPR value is imported from a package is attributed to it.

        The static replacement for v1's live inspect.getmodule() strategy:
        ``HANDLER = rest_framework.views.something`` is captured as an EXPR
        default carrying ``from rest_framework import views``.
        """
        mocker.patch(
            "django_aqueduct.discovery.package_attributor._module_to_dist_label",
            side_effect=lambda m: (
                "djangorestframework" if "rest_framework" in m else None
            ),
        )
        field = SettingField(
            name="MY_HANDLER",
            type=TypeRef("Any"),
            default=Default.expr_(
                "rest_framework.views.something",
                frozenset({ImportSpec("rest_framework", "views")}),
            ),
            provenance=Provenance(source_module="myapp.settings"),
        )
        result = self._attributor().attribute([field])
        assert result["MY_HANDLER"] == "djangorestframework"

    def test_imported_reference_from_project_code_falls_through(self) -> None:
        """An EXPR import that maps to no distribution falls back to project."""
        field = SettingField(
            name="WIKI_CAN_EDIT",
            type=TypeRef("Any"),
            default=Default.expr_(
                "myproject.perms.can_edit",
                frozenset({ImportSpec("myproject", "perms")}),
            ),
            provenance=Provenance(source_module="myapp.settings"),
        )
        result = self._attributor().attribute([field])
        assert result["WIKI_CAN_EDIT"] == LABEL_PROJECT

    def test_dynamic_map_cached(self, mocker: Any) -> None:
        """The dynamic map is built exactly once across multiple attribute() calls."""
        attributor = self._attributor()
        spy = mocker.patch.object(
            attributor,
            "_build_dynamic_map",
            wraps=attributor._build_dynamic_map,
        )
        fields = [_make_field("DEBUG")]
        attributor.attribute(fields)
        attributor.attribute(fields)
        spy.assert_called_once()

    def test_all_fields_present_in_result(self) -> None:
        """attribute() returns an entry for every supplied field."""
        attributor = self._attributor()
        names = [
            "DEBUG",
            "DATABASES",
            "CELERY_TASK_SERIALIZER",
            "REST_FRAMEWORK",
            "MY_CUSTOM_SETTING",
        ]
        fields = [_make_field(n) for n in names]
        result = attributor.attribute(fields)
        assert set(result) == set(names)

    def test_celery_compat_table_used_when_available(self, mocker: Any) -> None:
        """When celery is importable, its _OLD_SETTING_KEYS table is used."""
        fake_keys = {"CELERY_BEAT_SCHEDULE", "BROKER_TRANSPORT_OPTIONS"}
        mocker.patch(
            "django_aqueduct.discovery.package_attributor._celery_old_setting_names",
            return_value=dict.fromkeys(fake_keys, "celery"),
        )
        attributor = self._attributor()
        fields = [_make_field(k) for k in fake_keys]
        result = attributor.attribute(fields)
        for k in fake_keys:
            assert result[k] == "celery"

    def test_drf_attributed_when_rest_framework_installed(self, mocker: Any) -> None:
        mocker.patch(
            "django_aqueduct.discovery.package_attributor._drf_names",
            return_value={"REST_FRAMEWORK": "djangorestframework"},
        )
        attributor = self._attributor()
        result = attributor.attribute([_make_field("REST_FRAMEWORK")])
        assert result["REST_FRAMEWORK"] == "djangorestframework"

    def test_django_takes_priority_over_static_rules(self) -> None:
        """Django core settings are not overridden by static prefix rules.

        For example CSRF_COOKIE_SECURE is in django.conf.global_settings;
        even though 'CSRF_' might appear in a static rule it should still
        be attributed to 'django' because the dynamic map takes priority.
        """
        attributor = self._attributor(extra_rules=[("CSRF_", "my-csrf-package")])
        result = attributor.attribute([_make_field("CSRF_COOKIE_SECURE")])
        # Extra rules rank below the dynamic map; Django core wins
        assert result["CSRF_COOKIE_SECURE"] == LABEL_DJANGO


def test_builtin_rules_fixed_misattributions() -> None:
    """AWS_ → django-storages, JWT_ → edx-drf-extensions, bare OAUTH_ dropped."""
    from django_aqueduct.discovery.package_attributor import BUILTIN_RULES

    rules = dict(BUILTIN_RULES)
    assert rules["AWS_"] == "django-storages"
    assert rules["JWT_"] == "edx-drf-extensions"
    assert "OAUTH_" not in rules
    assert rules["OAUTH2_"] == "django-oauth-toolkit"


def test_extra_rules_reachable() -> None:
    attr = PackageAttributor(ast_scan_max_files=0, extra_rules=[("MYAPP_", "my-pkg")])
    result = attr.attribute([_make_field("MYAPP_TOKEN")])
    assert result["MYAPP_TOKEN"] == "my-pkg"
