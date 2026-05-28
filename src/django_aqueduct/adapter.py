"""Adapter functions for injecting Pydantic settings into Django."""

import inspect
from typing import Any

from pydantic_settings import BaseSettings


def configure_django_settings(
    model_class: type[BaseSettings],
    scope: dict[str, Any] | None = None,
) -> None:
    """Instantiate *model_class* and inject its values into a Django settings module.

    This is the recommended (Option A) adapter. The host settings file becomes a
    thin shim::

        # myproject/settings/production.py
        from django_aqueduct import configure_django_settings
        from myproject.settings_model import ProductionSettings

        configure_django_settings(ProductionSettings)

    All ``django.conf.settings.FOO`` access in application code continues to
    work unchanged.

    Args:
        model_class: A :class:`pydantic_settings.BaseSettings` subclass.
        scope: The globals dict to update.  Defaults to the caller's
            ``f_globals`` so that the shim pattern above requires no explicit
            argument.
    """
    if scope is None:
        frame = inspect.currentframe()
        if frame is None or frame.f_back is None:  # pragma: no cover
            raise RuntimeError("Cannot determine caller frame")
        scope = frame.f_back.f_globals

    instance = model_class()
    scope.update(instance.model_dump())


def configure_django_programmatic(
    model_class: type[BaseSettings],
) -> None:
    """Instantiate *model_class* and configure Django programmatically.

    This is the Option B adapter for greenfield or container-native projects
    that do not use ``DJANGO_SETTINGS_MODULE``.  Call this **before**
    :func:`django.setup`::

        from django_aqueduct import configure_django_programmatic
        from myproject.settings_model import AppSettings

        configure_django_programmatic(AppSettings)

        import django
        django.setup()

    Args:
        model_class: A :class:`pydantic_settings.BaseSettings` subclass.
    """
    from django.conf import settings as django_settings  # noqa: PLC0415

    instance = model_class()
    django_settings.configure(**instance.model_dump())
