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


def _overlay(instance: BaseSettings, base_values: dict[str, Any]) -> dict[str, Any]:
    """Overlay *instance* onto *base_values*, keeping base for un-overridden fields.

    Only the settings the model *meaningfully carries* override the base:

    * **Source- or validator-set fields win.**  A field provided by a settings
      source (env var, YAML, ``.env``, ``__init__``) or assigned inside a
      ``@model_validator`` is a real override — pydantic records both in
      ``model_fields_set`` (including ``extra`` fields), which is exactly
      "everything the model meaningfully set."
    * **A field left at its default defers to the base** *when the base carries
      it*.  This is the important case for a codegen model: the generated
      default is a static snapshot of the base at generation time, so a setting
      the model never overrides — ``INSTALLED_APPS``, ``MIDDLEWARE`` and the
      other structural settings a plugin framework augments at *runtime*, or an
      opaque ``None``-rendered field like ``XBLOCK_MIXINS`` — resolves to the
      *live* base value, not the frozen snapshot.
    * **A model-only field contributes its default.**  A field the model
      declares that the base does not carry (a hand-added setting) still lands in
      the result at its default, so nothing the model adds silently vanishes.

    This replaces the earlier "model wins unless it is ``None``" heuristic, which
    let a non-``None`` snapshot default clobber a live base value.
    """
    full = instance.model_dump()
    overridden = instance.model_fields_set
    merged = dict(base_values)
    for key, value in full.items():
        if key in overridden or key not in base_values:
            merged[key] = value
        # else: field is at its default AND the base carries it → keep base.
    return merged


def configure_django_settings(
    model_class: type[BaseSettings],
    scope: dict[str, Any] | None = None,
    base: str | ModuleType | dict[str, Any] | None = None,
    *,
    pre_configure: Callable[[BaseSettings], None] | None = None,
    post_configure: Callable[[dict[str, Any], BaseSettings], None] | None = None,
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
    onto the base settings rather than replacing them wholesale.  Only the
    settings the model *meaningfully carries* override the base:

    * a field set by a settings source (env/YAML/``.env``/``__init__``) or by a
      ``@model_validator`` **wins**;
    * a field left at its **default** defers to the base **when the base carries
      it** — so a codegen model's static snapshot never clobbers the live base,
      and settings a runtime plugin framework injects into the base (extra
      ``INSTALLED_APPS`` entries, ``MIDDLEWARE`` …) survive;
    * a field the base does **not** carry contributes its default, so a
      hand-added model setting is not lost.

    See :func:`_overlay` for the full rule.  This supersedes the earlier "model
    wins unless it is ``None``" heuristic, which let a non-``None`` snapshot
    default overwrite a live base value.  When *base* is ``None`` the model
    fully replaces the scope (unchanged).

    A field that a validator *extends from the base* (e.g. appending a plugin's
    app to the base's plugin-complete ``INSTALLED_APPS``) cannot be expressed as
    a validator — the model is built before the overlay and has no access to the
    base.  Use *post_configure* for that: it runs after the overlay, on the merged
    settings.

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
        post_configure: Optional callback invoked with ``(merged, instance)``
            *after* the base overlay and *before* the scope is written — the
            supported place to adjust final, base-resolved settings (e.g. extend
            the base's plugin-complete ``INSTALLED_APPS``/``AUTHENTICATION_BACKENDS``).
            Mutate *merged* in place; *instance* gives typed access to the model.
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

    base_values = _collect_base_settings(base)
    if base_values:
        merged = _overlay(instance, base_values)
    else:
        merged = instance.model_dump()

    if post_configure is not None:
        post_configure(merged, instance)

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
