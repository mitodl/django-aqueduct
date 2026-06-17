"""Tests for ModuleInspector."""

import pytest

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
