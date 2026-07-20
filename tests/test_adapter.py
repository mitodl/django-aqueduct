"""Tests for adapter functions."""

import subprocess
import sys
from pathlib import Path
from typing import Any

from pydantic import model_validator
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


def test_overlay_default_defers_to_base():
    """A field left at its class default defers to the base's value.

    This is the codegen case: the model's default is a static snapshot of the
    base at generation time, so the live base value must win — a plugin app the
    base gained at runtime is not clobbered by the frozen snapshot.
    """
    from django_aqueduct.adapter import configure_django_settings  # noqa: PLC0415

    base = {"DEBUG": False, "EXTRA": "from-base"}
    scope: dict = {}
    configure_django_settings(_OverlaySettings, scope=scope, base=base)

    # Neither field was set by a source or a validator — both are class
    # defaults, so the (live) base value wins over the snapshot default.
    assert scope["DEBUG"] is False
    assert scope["EXTRA"] == "from-base"


def test_overlay_source_set_value_wins_over_base(monkeypatch):
    """A field set by a settings source overrides the base value."""
    from django_aqueduct.adapter import configure_django_settings  # noqa: PLC0415

    monkeypatch.setenv("DEBUG", "true")
    base = {"DEBUG": False}
    scope: dict = {}
    configure_django_settings(_OverlaySettings, scope=scope, base=base)

    # DEBUG is now in model_fields_set (env-provided) → the model value wins.
    assert scope["DEBUG"] is True


def test_overlay_model_only_field_contributes_default():
    """A model field the base does not carry lands at its default."""
    from django_aqueduct.adapter import configure_django_settings  # noqa: PLC0415

    base = {"DEBUG": False}  # no EXTRA in base
    scope: dict = {}
    configure_django_settings(_OverlaySettings, scope=scope, base=base)

    # EXTRA is model-only → its default is contributed, not dropped.
    assert scope["EXTRA"] == "from-model"


class _DerivedSettings(BaseSettings):
    BROKER_HOST: str | None = None
    BROKER_URL: str | None = None

    @model_validator(mode="after")
    def _derive(self):
        if self.BROKER_HOST and not self.BROKER_URL:
            self.BROKER_URL = f"amqp://{self.BROKER_HOST}/"
        return self


def test_overlay_validator_set_value_wins_over_base(monkeypatch):
    """A value assigned by a @model_validator overrides the base value."""
    from django_aqueduct.adapter import configure_django_settings  # noqa: PLC0415

    monkeypatch.setenv("BROKER_HOST", "rabbit")
    base = {"BROKER_URL": "amqp://base-default/"}
    scope: dict = {}
    configure_django_settings(_DerivedSettings, scope=scope, base=base)

    # The validator assigned BROKER_URL → it is in model_fields_set → wins.
    assert scope["BROKER_URL"] == "amqp://rabbit/"


def test_overlay_unfired_validator_field_defers_to_base():
    """A derived field the validator did not set defers to the base value."""
    from django_aqueduct.adapter import configure_django_settings  # noqa: PLC0415

    base = {"BROKER_URL": "amqp://base-default/"}
    scope: dict = {}
    configure_django_settings(_DerivedSettings, scope=scope, base=base)

    # BROKER_HOST unset → validator does not assign BROKER_URL → base wins.
    assert scope["BROKER_URL"] == "amqp://base-default/"


def test_post_configure_runs_after_overlay():
    """post_configure receives the merged settings and can extend base lists."""
    from django_aqueduct import get_configured_model  # noqa: PLC0415
    from django_aqueduct.adapter import configure_django_settings  # noqa: PLC0415

    seen: dict = {}

    def _post(merged, instance):
        # Runs after the base overlay: INSTALLED_APPS is the (plugin-complete)
        # base list, which we extend here rather than in a validator.
        seen["apps_before"] = list(merged["INSTALLED_APPS"])
        seen["is_model"] = instance is get_configured_model()
        merged["INSTALLED_APPS"] = [*merged["INSTALLED_APPS"], "myplugin.apps.Cfg"]

    base = {"INSTALLED_APPS": ["django.contrib.auth", "plugin.injected"]}
    scope: dict = {}
    configure_django_settings(
        _OverlaySettings, scope=scope, base=base, post_configure=_post
    )

    assert seen["apps_before"] == ["django.contrib.auth", "plugin.injected"]
    assert seen["is_model"] is True
    assert scope["INSTALLED_APPS"] == [
        "django.contrib.auth",
        "plugin.injected",
        "myplugin.apps.Cfg",
    ]


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
