r"""Management command: generate_aqueduct_settings.

Introspects one or more settings sources and emits either a typed Pydantic
``BaseSettings`` scaffold or a JSON Schema document that can be used to
validate Kubernetes ConfigMaps, ``.env`` files, or any external settings
source.

Usage examples::

    # From a Python settings module (Pydantic model)
    python manage.py generate_aqueduct_settings --modules myapp.settings.common

    # JSON Schema for ConfigMap validation
    python manage.py generate_aqueduct_settings \\
        --modules myapp.settings \\
        --format jsonschema \\
        --output settings.schema.json

    # Multiple modules, write to a file
    python manage.py generate_aqueduct_settings \\
        --modules myapp.settings.base,third_party.settings \\
        --output src/myapp/settings_model.py

    # Include mitol EnvParser registry (auto-detected if mitol-django-common installed)
    python manage.py generate_aqueduct_settings \\
        --modules myapp.settings \\
        --include-envparser
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from django_aqueduct.codegen.generator import SettingsModelGenerator
from django_aqueduct.discovery.base import DiscoveredField
from django_aqueduct.discovery.module import ModuleInspector


def _envparser_available() -> bool:
    """Return True if mitol-django-common is installed and loadable."""
    try:
        import mitol.common.envs  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False


def _mitol_in_installed_apps() -> bool:
    """Return True if any mitol app is in INSTALLED_APPS."""
    try:
        from django.apps import apps  # noqa: PLC0415

        return any(app.name.startswith("mitol.") for app in apps.get_app_configs())
    except Exception:  # noqa: BLE001
        return False


class Command(BaseCommand):
    """Generate a typed Pydantic BaseSettings scaffold from existing settings."""

    help = (
        "Introspect Django settings modules and emit a typed Pydantic "
        "BaseSettings scaffold to stdout or a file."
    )

    def add_arguments(self, parser: object) -> None:  # noqa: ANN001
        """Declare CLI arguments."""
        from argparse import ArgumentParser  # noqa: PLC0415

        assert isinstance(parser, ArgumentParser)  # noqa: S101
        parser.add_argument(
            "--format",
            choices=["python", "jsonschema"],
            default="python",
            help=(
                "Output format. "
                "'python' (default) emits a Pydantic BaseSettings scaffold. "
                "'jsonschema' emits a JSON Schema document suitable for "
                "validating Kubernetes ConfigMaps or environment variables."
            ),
        )
        parser.add_argument(
            "--modules",
            type=str,
            default="",
            help=(
                "Comma-separated list of dotted Python module paths to inspect, "
                "e.g. 'myapp.settings.common,myapp.settings.production'."
            ),
        )
        parser.add_argument(
            "--output",
            type=str,
            default="-",
            help=("Output file path. Use '-' (the default) to write to stdout."),
        )
        parser.add_argument(
            "--include-envparser",
            action="store_true",
            default=None,
            help=(
                "Include fields from the mitol EnvParser registry. "
                "Auto-enabled when mitol-django-common is installed and "
                "a mitol app is in INSTALLED_APPS."
            ),
        )

    def handle(self, *args: object, **options: object) -> None:
        """Execute the command."""
        modules_str = str(options.get("modules", "") or "")
        output_path = str(options.get("output", "-") or "-")
        output_format = str(options.get("format", "python") or "python")
        include_envparser: bool | None = options.get("include_envparser")  # type: ignore[assignment]

        # Resolve --include-envparser auto-detection
        if include_envparser is None:
            include_envparser = _envparser_available() and _mitol_in_installed_apps()

        fields: list[DiscoveredField] = []

        # Module inspector(s)
        module_paths = [m.strip() for m in modules_str.split(",") if m.strip()]
        for module_path in module_paths:
            try:
                inspector = ModuleInspector(module_path)
                fields.extend(inspector.discover())
            except ImportError as exc:
                raise CommandError(str(exc)) from exc

        # EnvParser inspector
        if include_envparser:
            try:
                from django_aqueduct.discovery.envparser import (
                    EnvParserInspector,  # noqa: PLC0415
                )

                fields.extend(EnvParserInspector().discover())
            except ImportError as exc:
                raise CommandError(str(exc)) from exc

        if not fields:
            self.stderr.write(
                self.style.WARNING(
                    "No settings fields discovered. "
                    "Specify --modules or --include-envparser."
                )
            )

        if output_format == "jsonschema":
            import json  # noqa: PLC0415

            from django_aqueduct.codegen.schema_generator import (  # noqa: PLC0415
                SchemaGenerator,
            )

            output = json.dumps(SchemaGenerator(fields).generate(), indent=2)
        else:
            output = SettingsModelGenerator(fields).render()

        if output_path == "-":
            self.stdout.write(output, ending="")
        else:
            try:
                with open(output_path, "w", encoding="utf-8") as fh:  # noqa: PTH123
                    fh.write(output)
                self.stdout.write(
                    self.style.SUCCESS(f"Settings model written to {output_path}")
                )
            except OSError as exc:
                raise CommandError(f"Cannot write to {output_path!r}: {exc}") from exc
