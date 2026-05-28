"""Tests for adapter functions."""

import subprocess
import sys

from pydantic_settings import BaseSettings


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
    code = """
import sys
sys.path.insert(0, "src")
from pydantic_settings import BaseSettings

class S(BaseSettings):
    MY_KEY: str = "hello"

from django_aqueduct.adapter import configure_django_settings
configure_django_settings(S)
assert MY_KEY == "hello", f"Expected 'hello', got {MY_KEY!r}"
print("OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd="/home/tmacey/code/mit/apps/maintained/django-aqueduct",
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


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
    env = {"PYTHONPATH": "src"}
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd="/home/tmacey/code/mit/apps/maintained/django-aqueduct",
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout
