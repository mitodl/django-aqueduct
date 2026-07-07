"""Tests for the inspector plugin registry."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from django_aqueduct import registry
from django_aqueduct.discovery.ir import Default, Provenance, SettingField, TypeRef


def _field(name: str) -> SettingField:
    return SettingField(
        name=name,
        type=TypeRef("str"),
        default=Default.literal_("x"),
        provenance=Provenance(source_module="plugin"),
    )


class _Inspector:
    def __init__(self, *names: str) -> None:
        self._names = names

    def discover(self) -> list[SettingField]:
        return [_field(n) for n in self._names]


def _fake_entry_points(*eps):
    def _loader(group: str):
        assert group == registry.INSPECTOR_GROUP
        return list(eps)

    return _loader


def test_resolves_inspector_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    ep = SimpleNamespace(name="p", load=lambda: _Inspector("A"))
    monkeypatch.setattr(registry, "entry_points", _fake_entry_points(ep))
    result = registry.load_inspectors()
    assert result[0][0] == "p"
    assert [f.name for f in result[0][1].discover()] == ["A"]


def test_resolves_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    ep = SimpleNamespace(name="p", load=lambda: (lambda: _Inspector("B")))
    monkeypatch.setattr(registry, "entry_points", _fake_entry_points(ep))
    fields = registry.discover_from_plugins()
    assert [f.name for f in fields] == ["B"]


def test_multiple_plugins_concatenate(monkeypatch: pytest.MonkeyPatch) -> None:
    eps = [
        SimpleNamespace(name="a", load=lambda: _Inspector("A")),
        SimpleNamespace(name="b", load=lambda: _Inspector("B", "C")),
    ]
    monkeypatch.setattr(registry, "entry_points", _fake_entry_points(*eps))
    assert [f.name for f in registry.discover_from_plugins()] == ["A", "B", "C"]


def test_load_failure_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> None:
        raise RuntimeError("kaboom")

    ep = SimpleNamespace(name="bad", load=_boom)
    monkeypatch.setattr(registry, "entry_points", _fake_entry_points(ep))
    with pytest.raises(registry.RegistryError, match="bad"):
        registry.load_inspectors()


def test_resolves_inspector_class_by_instantiating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An entry point that loads the inspector *class* (a zero-arg factory) must
    # be instantiated, not returned as the class — even though the class
    # structurally satisfies the Inspector protocol.
    ep = SimpleNamespace(name="p", load=lambda: _Inspector)
    monkeypatch.setattr(registry, "entry_points", _fake_entry_points(ep))
    (_, inspector) = registry.load_inspectors()[0]
    assert not isinstance(inspector, type)
    assert inspector.discover() == []  # _Inspector() with no names


def test_factory_that_raises_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    def _bad_factory() -> None:
        raise ValueError("wrong signature")

    ep = SimpleNamespace(name="p", load=lambda: _bad_factory)
    monkeypatch.setattr(registry, "entry_points", _fake_entry_points(ep))
    with pytest.raises(registry.RegistryError, match="factory raised"):
        registry.load_inspectors()


def test_non_inspector_factory_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    ep = SimpleNamespace(name="p", load=lambda: (lambda: object()))
    monkeypatch.setattr(registry, "entry_points", _fake_entry_points(ep))
    with pytest.raises(registry.RegistryError, match="not an inspector instance"):
        registry.load_inspectors()
