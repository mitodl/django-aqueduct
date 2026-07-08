"""Tests for the public Setting declaration dataclass."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from django_aqueduct import UNSET, Setting
from django_aqueduct.surface import _Unset


def test_defaults() -> None:
    s = Setting("X")
    assert s.name == "X"
    assert s.type == "Any"
    assert s.default is UNSET
    assert s.required is False
    assert s.description == ""
    assert s.dev_only is False


def test_has_default_distinguishes_unset_from_none() -> None:
    assert Setting("X", default=None).has_default is True
    assert Setting("X").has_default is False
    assert Setting("X", default="").has_default is True


def test_unset_is_singleton_falsey_and_reprs() -> None:
    assert _Unset() is UNSET
    assert bool(UNSET) is False
    assert repr(UNSET) == "UNSET"


def test_frozen() -> None:
    s = Setting("X")
    with pytest.raises(FrozenInstanceError):
        s.name = "Y"  # type: ignore[misc]


def test_hashable() -> None:
    assert Setting("A", default=1) in {Setting("A", default=1)}


def test_exported_from_package_root() -> None:
    import django_aqueduct

    assert django_aqueduct.Setting is Setting
    assert django_aqueduct.UNSET is UNSET
