"""Tests for DiscoveredField and BaseInspector."""

from django_aqueduct.discovery.base import BaseInspector, DiscoveredField


def test_discovered_field_construction():
    """DiscoveredField is constructable with all required fields."""
    f = DiscoveredField(
        name="MY_SETTING",
        type_annotation="str",
        default="hello",
        description="A test setting",
        required=False,
        source_module="myapp.settings",
        dev_only=False,
    )
    assert f.name == "MY_SETTING"
    assert f.type_annotation == "str"
    assert f.default == "hello"
    assert f.description == "A test setting"
    assert f.required is False
    assert f.source_module == "myapp.settings"
    assert f.dev_only is False
    assert f.needs_refinement is False


def test_discovered_field_needs_refinement_default():
    """needs_refinement defaults to False."""
    f = DiscoveredField(
        name="X",
        type_annotation="Any",
        default=None,
        description="",
        required=False,
        source_module="m",
        dev_only=False,
    )
    assert f.needs_refinement is False


def test_discovered_field_needs_refinement_explicit():
    """needs_refinement can be set explicitly."""
    f = DiscoveredField(
        name="X",
        type_annotation="Any",
        default=None,
        description="",
        required=False,
        source_module="m",
        dev_only=False,
        needs_refinement=True,
    )
    assert f.needs_refinement is True


def test_base_inspector_protocol():
    """A class with a discover() method satisfies BaseInspector."""

    class MyInspector:
        def discover(self) -> list[DiscoveredField]:
            return []

    assert isinstance(MyInspector(), BaseInspector)


def test_base_inspector_protocol_missing_method():
    """A class without discover() does not satisfy BaseInspector."""

    class NotAnInspector:
        pass

    assert not isinstance(NotAnInspector(), BaseInspector)
