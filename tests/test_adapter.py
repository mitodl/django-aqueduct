"""Tests for adapter functions."""

import subprocess
import sys
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings

# Repo root — works both locally and in CI where cwd is the checkout directory
_REPO_ROOT = Path(__file__).parent.parent


class _SimpleSettings(BaseSettings):
    SECRET_KEY: str = "test-secret"  # noqa: S105
    DEBUG: bool = False
    MAX_CONN: int = 5


def test_configure_django_settings_explicit_scope():
    """configure_django_settings injects model fields into an explicit scope dict."""
    from django_aqueduct.adapter import configure_django_settings  # noqa: PLC0415

    scope: dict = {}
    configure_django_settings(_SimpleSettings, scope=scope)

    assert scope["SECRET_KEY"] == "test-secret"
    assert scope["DEBUG"] is False
    assert scope["MAX_CONN"] == 5


def test_configure_django_settings_all_fields_present():
    """Every field from the model appears as a key in the scope."""
    from django_aqueduct.adapter import configure_django_settings  # noqa: PLC0415

    scope: dict = {}
    configure_django_settings(_SimpleSettings, scope=scope)

    for field_name in _SimpleSettings.model_fields:
        assert field_name in scope


def test_configure_django_settings_respects_env(monkeypatch):
    """configure_django_settings respects environment variable overrides."""
    from django_aqueduct.adapter import configure_django_settings  # noqa: PLC0415

    monkeypatch.setenv("MAX_CONN", "99")

    scope: dict = {}
    configure_django_settings(_SimpleSettings, scope=scope)
    assert scope["MAX_CONN"] == 99


def test_configure_django_settings_caller_globals():
    """configure_django_settings with no scope argument writes to caller globals."""
    # We test this by executing in a subprocess to avoid polluting the test process.
    code = f"""
import sys
sys.path.insert(0, {str(_REPO_ROOT / "src")!r})
from pydantic_settings import BaseSettings

class S(BaseSettings):
    MY_KEY: str = "hello"

from django_aqueduct.adapter import configure_django_settings
configure_django_settings(S)
assert MY_KEY == "hello", f"Expected 'hello', got {{MY_KEY!r}}"
print("OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


class _OverlaySettings(BaseSettings):
    # Carries a real value — overrides the base.
    DEBUG: bool = True
    # Opaque/derived field the generator could not serialise.
    XBLOCK_MIXINS: Any = None
    # Absent-from-base field — model contributes it.
    EXTRA: str = "from-model"


def test_overlay_keeps_base_value_when_model_field_absent():
    """A setting absent from the model falls back to the base value."""
    from django_aqueduct.adapter import configure_django_settings  # noqa: PLC0415

    base = {"INSTALLED_APPS": ["django.contrib.auth"], "DEBUG": False}
    scope: dict = {}
    configure_django_settings(_OverlaySettings, scope=scope, base=base)

    # INSTALLED_APPS is not a model field → base value survives.
    assert scope["INSTALLED_APPS"] == ["django.contrib.auth"]


def test_overlay_none_does_not_clobber_base():
    """A model value of None does not overwrite a non-None base value."""
    from django_aqueduct.adapter import configure_django_settings  # noqa: PLC0415

    base = {"XBLOCK_MIXINS": ("a", "b"), "DEBUG": False}
    scope: dict = {}
    configure_django_settings(_OverlaySettings, scope=scope, base=base)

    # Model's XBLOCK_MIXINS is None → real base tuple is preserved.
    assert scope["XBLOCK_MIXINS"] == ("a", "b")


def test_overlay_model_value_wins_over_base():
    """A non-None model value overrides the base value."""
    from django_aqueduct.adapter import configure_django_settings  # noqa: PLC0415

    base = {"DEBUG": False, "EXTRA": "from-base"}
    scope: dict = {}
    configure_django_settings(_OverlaySettings, scope=scope, base=base)

    assert scope["DEBUG"] is True
    assert scope["EXTRA"] == "from-model"


def test_overlay_ignores_lowercase_base_names():
    """Only UPPERCASE names from the base are carried into the scope."""
    from django_aqueduct.adapter import configure_django_settings  # noqa: PLC0415

    base = {"INSTALLED_APPS": ["x"], "helper": "ignored"}
    scope: dict = {}
    configure_django_settings(_OverlaySettings, scope=scope, base=base)

    assert "helper" not in scope
    assert scope["INSTALLED_APPS"] == ["x"]


def test_no_base_preserves_replace_behaviour():
    """Without base=, the model fully replaces the scope (legacy behaviour)."""
    from django_aqueduct.adapter import configure_django_settings  # noqa: PLC0415

    scope: dict = {"INSTALLED_APPS": ["pre-existing"]}
    configure_django_settings(_SimpleSettings, scope=scope)

    # No base → the model dump is applied; unrelated pre-existing keys remain
    # but no base merge occurs.
    assert scope["SECRET_KEY"] == "test-secret"
    assert scope["INSTALLED_APPS"] == ["pre-existing"]


def test_configure_django_programmatic():
    """configure_django_programmatic calls django.conf.settings.configure."""
    # Run in subprocess — Django settings can only be configured once per process.
    # Unset DJANGO_SETTINGS_MODULE to prevent pytest-django auto-configuration.
    code = """
import sys
sys.path.insert(0, "src")
from pydantic_settings import BaseSettings

class S(BaseSettings):
    SECRET_KEY: str = "prog-secret"
    INSTALLED_APPS: list = []
    DATABASES: dict = {}
    USE_TZ: bool = True

from django_aqueduct.adapter import configure_django_programmatic
configure_django_programmatic(S)

from django.conf import settings
assert settings.SECRET_KEY == "prog-secret", settings.SECRET_KEY
print("OK")
"""
    env = {"PYTHONPATH": str(_REPO_ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


# ---- bootstrap hook + model-instance exposure ----


def test_pre_configure_runs_before_injection() -> None:
    from pydantic_settings import BaseSettings

    from django_aqueduct import get_configured_model
    from django_aqueduct.adapter import configure_django_settings

    class _S(BaseSettings):
        DEBUG: bool = True
        SENTRY_DSN: str = "https://sentry.example/1"

    seen = {}

    def _pre(instance):
        # Runs before scope is populated; has typed access to the model.
        seen["dsn"] = instance.SENTRY_DSN
        seen["scope_has_debug"] = "DEBUG" in scope

    scope: dict = {}
    configure_django_settings(_S, scope=scope, pre_configure=_pre)

    assert seen["dsn"] == "https://sentry.example/1"
    assert seen["scope_has_debug"] is False  # ran before injection
    assert scope["DEBUG"] is True
    assert scope["AQUEDUCT_MODEL"].SENTRY_DSN == "https://sentry.example/1"
    assert get_configured_model().SENTRY_DSN == "https://sentry.example/1"
