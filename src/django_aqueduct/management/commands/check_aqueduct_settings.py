r"""Management command: check_aqueduct_settings.

Instantiate the generated ``AqueductSettings`` model and compare its resolved
values against the legacy settings module under the same environment, failing
on unexplained drift. Run it in CI so the model cannot silently desync from
``settings.py`` while both are maintained in parallel.

Usage::

    python manage.py check_aqueduct_settings \\
        --model myapp.settings_model:AqueductSettings \\
        --legacy myapp.settings.production \\
        --ignore SECRET_KEY,ENVIRONMENT

``--ignore`` (or ``[tool.aqueduct] parity_ignore``) lists keys with a
deliberate, documented divergence (e.g. a setting the migration intentionally
made required).
"""

from __future__ import annotations

import importlib
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from django_aqueduct.parity import compare, uppercase_settings


class Command(BaseCommand):
    """Check generated-model settings against the legacy settings module."""

    help = (
        "Compare the AqueductSettings model's resolved values against the "
        "legacy settings module and report drift."
    )

    def add_arguments(self, parser: object) -> None:  # noqa: ANN001
        """Declare CLI arguments."""
        from argparse import ArgumentParser  # noqa: PLC0415

        assert isinstance(parser, ArgumentParser)  # noqa: S101
        parser.add_argument(
            "--model",
            type=str,
            default=None,
            help="'module.path:ClassName' of the generated BaseSettings model.",
        )
        parser.add_argument(
            "--legacy",
            type=str,
            default=None,
            help="Dotted path to the legacy settings module to compare against.",
        )
        parser.add_argument(
            "--ignore",
            type=str,
            default="",
            help="Comma-separated setting names with a deliberate divergence.",
        )

    def handle(self, *args: object, **options: object) -> None:
        """Execute the command."""
        from django_aqueduct.config import ConfigError, load_config  # noqa: PLC0415

        try:
            cfg = load_config()
        except ConfigError as exc:
            raise CommandError(str(exc)) from exc

        model_ref = str(options.get("model") or cfg.parity_model or "")
        legacy_ref = str(options.get("legacy") or cfg.parity_legacy or "")
        if not model_ref or not legacy_ref:
            raise CommandError(
                "Both --model ('module:Class') and --legacy ('module') are "
                "required (or set them in [tool.aqueduct])."
            )

        ignore = {
            name.strip()
            for name in str(options.get("ignore") or "").split(",")
            if name.strip()
        } | set(cfg.parity_ignore)

        model_values = self._model_values(model_ref)
        legacy_values = uppercase_settings(self._import(legacy_ref))

        report = compare(model_values, legacy_values, ignore=ignore)
        if report.in_sync:
            self.stdout.write(self.style.SUCCESS(report.render()))
            return
        self.stderr.write(report.render())
        raise CommandError(
            f"{len(report.divergences)} setting(s) diverge between the model "
            f"and legacy settings."
        )

    def _model_values(self, model_ref: str) -> dict[str, Any]:
        """Instantiate the model from ``module:Class`` and return model_dump()."""
        if ":" not in model_ref:
            raise CommandError(
                f"--model must be 'module.path:ClassName', got {model_ref!r}."
            )
        module_path, _, class_name = model_ref.partition(":")
        module = self._import(module_path)
        try:
            model_cls = getattr(module, class_name)
        except AttributeError as exc:
            raise CommandError(f"{class_name!r} not found in {module_path!r}.") from exc
        try:
            instance = model_cls()
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f"Could not instantiate {model_ref}: {exc}") from exc
        return dict(instance.model_dump())

    @staticmethod
    def _import(module_path: str) -> Any:
        try:
            return importlib.import_module(module_path)
        except ImportError as exc:
            raise CommandError(f"Could not import {module_path!r}: {exc}") from exc
