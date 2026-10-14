"""The exported version must match the one the project declares."""

from __future__ import annotations

import tomllib
from pathlib import Path

import django_aqueduct

_PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def test_version_matches_pyproject():
    """``__version__`` tracks ``pyproject.toml``.

    It used to be a hand-maintained literal and drifted two minor versions --
    still ``0.13.0`` while the project shipped 0.14.0 and 0.14.1. A consumer
    gating on "do I have the fix?" read the wrong answer, so this is a
    correctness guard, not housekeeping.
    """
    declared = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"][
        "version"
    ]
    assert django_aqueduct.__version__ == declared


def test_version_is_not_the_uninstalled_fallback():
    """Guard the fallback itself.

    ``importlib.metadata`` returning ``PackageNotFoundError`` is a legitimate
    state in a bare source tree, but under the test suite the package is always
    installed -- so the fallback showing up here means the distribution name
    stopped matching and every consumer now reads a meaningless version.
    """
    assert django_aqueduct.__version__ != "0.0.0+unknown"
