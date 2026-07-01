"""Tests for ModuleInspector."""

import sys
import types

import pytest

from django_aqueduct.discovery.base import ValueKind
from django_aqueduct.discovery.module import ModuleInspector

# The known UPPERCASE names and their expected types from testapp/fixture_settings.py
EXPECTED_FIELDS = {
    "SITE_NAME": ("str", False),
    "API_KEY": ("str", False),
    "DEBUG": ("bool", False),
    "ENABLE_FEATURE_X": ("bool", False),
    "MAX_CONNECTIONS": ("int", False),
    "RATE_LIMIT": ("float", False),
    "ALLOWED_HOSTS": ("list[Any]", False),
    "CACHES": ("dict[str, Any]", False),
    "OPTIONAL_SETTING": ("Any", True),
    # Added in extended fixture
    "SECURE_PROXY_HEADER": ("tuple[Any, ...]", True),
    "EXCLUDED_FIELDS": ("set[Any]", True),
    "DATA_DIR": (
        "pathlib.Path",
        False,
    ),  # pathlib.Path → pathlib.Path (preserves / operator)
}


def test_discovers_all_uppercase_names() -> None:
    """ModuleInspector finds every UPPERCASE name in the fixture module."""
    inspector = ModuleInspector("fixture_settings")
    fields = inspector.discover()
    discovered_names = {f.name for f in fields}
    assert discovered_names == set(EXPECTED_FIELDS)


def test_correct_type_annotations() -> None:
    """Each field has the expected type annotation."""
    inspector = ModuleInspector("fixture_settings")
    fields = {f.name: f for f in inspector.discover()}
    for name, (expected_annotation, _) in EXPECTED_FIELDS.items():
        assert fields[name].type_annotation == expected_annotation, (
            f"{name}: expected {expected_annotation!r}, "
            f"got {fields[name].type_annotation!r}"
        )


def test_correct_needs_refinement() -> None:
    """needs_refinement is True only for None-valued names."""
    inspector = ModuleInspector("fixture_settings")
    fields = {f.name: f for f in inspector.discover()}
    for name, (_, expected_refinement) in EXPECTED_FIELDS.items():
        assert fields[name].needs_refinement is expected_refinement, (
            f"{name}: expected needs_refinement={expected_refinement}"
        )


def test_source_module_is_set() -> None:
    """source_module is the module's __name__."""
    inspector = ModuleInspector("fixture_settings")
    fields = inspector.discover()
    for f in fields:
        assert f.source_module == "fixture_settings"


def test_excludes_non_uppercase_names() -> None:
    """Private and mixed-case names are not included."""
    inspector = ModuleInspector("fixture_settings")
    fields = inspector.discover()
    names = {f.name for f in fields}
    assert "_private" not in names
    assert "not_uppercase" not in names


def test_import_error_on_bad_module() -> None:
    """ImportError is raised with an actionable message for missing modules."""
    inspector = ModuleInspector("this.module.does.not.exist")
    with pytest.raises(ImportError, match="this.module.does.not.exist"):
        inspector.discover()


def test_sorted_output() -> None:
    """Fields are returned in sorted order by name."""
    inspector = ModuleInspector("fixture_settings")
    fields = inspector.discover()
    names = [f.name for f in fields]
    assert names == sorted(names)


def test_secret_like_name_is_redacted() -> None:
    """API_KEY's live value is never captured — its name looks secret-like."""
    inspector = ModuleInspector("fixture_settings")
    fields = {f.name: f for f in inspector.discover()}
    assert fields["API_KEY"].value_kind == ValueKind.REDACTED
    assert fields["API_KEY"].default is None


class TestRedactionCoversRealSecretValues:
    """A live secret value present in the environment is never written out."""

    @staticmethod
    def _install_fixture_module(**names: object) -> str:
        module_name = "_aqueduct_redaction_fixture"
        module = types.ModuleType(module_name)
        for name, value in names.items():
            setattr(module, name, value)
        sys.modules[module_name] = module
        return module_name

    def teardown_method(self) -> None:
        sys.modules.pop("_aqueduct_redaction_fixture", None)

    @pytest.mark.parametrize(
        "name",
        [
            "SECRET_KEY",
            "DB_PASSWORD",
            "AUTH_TOKEN",
            "STRIPE_PRIVATE_KEY",
            "MAILGUN_API_KEY",
            "AWS_ACCESS_KEY",
            "OAUTH_CREDENTIAL",
            "JWT_SIGNING_KEY",
            "FIELD_ENCRYPTION_KEY",
            "SENTRY_DSN",
        ],
    )
    def test_redacts_secret_shaped_names(self, name: str) -> None:
        """Every marker-matching name is redacted regardless of its live value."""
        module_name = self._install_fixture_module(
            **{name: "sk_live_super_secret_value_12345"}
        )
        fields = {f.name: f for f in ModuleInspector(module_name).discover()}

        assert fields[name].value_kind == ValueKind.REDACTED
        assert fields[name].default is None

    def test_non_secret_name_is_not_redacted(self) -> None:
        """A benign name keeps its live value as the STATIC default."""
        module_name = self._install_fixture_module(SITE_NAME="My App")
        fields = {f.name: f for f in ModuleInspector(module_name).discover()}

        assert fields["SITE_NAME"].value_kind == ValueKind.STATIC
        assert fields["SITE_NAME"].default == "My App"
