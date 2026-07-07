"""Adapter functions for injecting Pydantic settings into Django."""

from __future__ import annotations

import importlib
import inspect
from typing import TYPE_CHECKING, Any

from pydantic_settings import BaseSettings

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

# The most recently configured model instance, exposed for runtime code and the
# parity command to reach typed values without re-instantiating (also injected
# into the settings scope as ``AQUEDUCT_MODEL``).
_configured_model: BaseSettings | None = None


def get_configured_model() -> BaseSettings | None:
    """Return the model instance from the last ``configure_django_settings`` call."""
    return _configured_model


def _collect_base_settings(
    base: str | ModuleType | dict[str, Any] | None,
) -> dict[str, Any]:
    """Collect the UPPERCASE settings names from a base settings source.

    Args:
        base: A dotted module path (e.g. ``"lms.envs.common"``), an already
            imported module, a plain mapping, or ``None``.

    Returns:
        A dict of every ``UPPER_CASE`` name → value found in *base*, or an
        empty dict when *base* is ``None``.
    """
    if base is None:
        return {}
    if isinstance(base, dict):
        return {k: v for k, v in base.items() if k.isupper()}
    module = importlib.import_module(base) if isinstance(base, str) else base
    return {name: getattr(module, name) for name in dir(module) if name.isupper()}


def configure_django_settings(
    model_class: type[BaseSettings],
    scope: dict[str, Any] | None = None,
    base: str | ModuleType | dict[str, Any] | None = None,
    *,
    pre_configure: Callable[[BaseSettings], None] | None = None,
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

    **Overlay semantics.**  When *base* is supplied, the model is *overlaid*
    onto the base settings rather than replacing them wholesale.  The base
    (typically the upstream ``…envs.common`` module) provides a value for
    every setting the model does not meaningfully carry, so a setting that is
    absent from the model, or that the generator could not serialise (rendered
    as a ``None`` default — e.g. ``INSTALLED_APPS`` built from class
    references, ``XBLOCK_MIXINS``), degrades to the real base value instead of
    silently vanishing to Django's empty default.

    The merge rule is: **the model value wins, unless it is ``None`` and the
    base has a non-``None`` value** — in which case the base value is kept.
    This restores the unserialisable settings automatically and generalises
    the per-field ``@model_validator`` restore pattern.

    When *base* is ``None`` the historical replace behaviour is preserved.

    Args:
        model_class: A :class:`pydantic_settings.BaseSettings` subclass.
        scope: The globals dict to update.  Defaults to the caller's
            ``f_globals`` so that the shim pattern above requires no explicit
            argument.
        base: Optional base settings to overlay onto — a dotted module path,
            an imported module, or a mapping.  When omitted the model fully
            replaces the scope (legacy behaviour).
        pre_configure: Optional callback invoked with the validated model
            instance *before* its values are injected — the supported place to
            initialise Sentry (or anything else) with typed values before
            Django settings exist. The instance is also exposed as
            ``settings.AQUEDUCT_MODEL`` and via :func:`get_configured_model`.
    """
    if scope is None:
        frame = inspect.currentframe()
        if frame is None or frame.f_back is None:  # pragma: no cover
            raise RuntimeError("Cannot determine caller frame")
        scope = frame.f_back.f_globals

    # Two-phase bootstrap: build the validated model, hand it to *pre_configure*
    # (the place to initialise Sentry with typed values before Django settings
    # exist), then inject. Exposing the instance lets shims stop poking into
    # model_fields[...].default and re-reading env with legacy helpers.
    instance = model_class()
    if pre_configure is not None:
        pre_configure(instance)

    global _configured_model  # noqa: PLW0603
    _configured_model = instance
    scope["AQUEDUCT_MODEL"] = instance

    model_values = instance.model_dump()

    base_values = _collect_base_settings(base)
    if not base_values:
        scope.update(model_values)
        return

    merged = dict(base_values)
    for key, value in model_values.items():
        if value is None and base_values.get(key) is not None:
            # The model could not serialise this value (opaque/derived field
            # rendered as default=None) — keep the real base value.
            continue
        merged[key] = value
    scope.update(merged)


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
