r"""Management command: generate_aqueduct_settings.

Statically discovers settings from one or more source modules (via AST — the
modules are never imported) and emits either a typed Pydantic ``BaseSettings``
model or a JSON Schema document for validating Kubernetes ConfigMaps, ``.env``
files, or any external settings source.

Usage examples::

    # Typed Pydantic settings model
    python manage.py generate_aqueduct_settings --modules myapp.settings.common

    # JSON Schema for ConfigMap validation
    python manage.py generate_aqueduct_settings \\
        --modules myapp.settings \\
        --format jsonschema \\
        --output settings.schema.json

    # Multiple modules (later modules override earlier ones), write to a file
    python manage.py generate_aqueduct_settings \\
        --modules myapp.settings.base,myapp.settings.production \\
        --output src/myapp/settings_model.py

    # Include the mitol EnvParser registry (auto-detected when installed)
    python manage.py generate_aqueduct_settings \\
        --modules myapp.settings --include-envparser
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from django_aqueduct.codegen.renderer import ModelRenderer
from django_aqueduct.codegen.schema_generator import SchemaGenerator
from django_aqueduct.discovery.ir import SettingField
from django_aqueduct.discovery.static import StaticModuleInspector


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
    """Generate a typed Pydantic BaseSettings model from existing settings."""

    help = (
        "Statically introspect Django settings modules and emit a typed "
        "Pydantic BaseSettings model (or a JSON Schema) to stdout or a file."
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
                "Output format. 'python' (default) emits a Pydantic "
                "BaseSettings model. 'jsonschema' emits a JSON Schema document "
                "for validating ConfigMaps or environment variables."
            ),
        )
        parser.add_argument(
            "--modules",
            type=str,
            default="",
            help=(
                "Comma-separated dotted module paths to inspect, e.g. "
                "'myapp.settings.base,myapp.settings.production'. Later modules "
                "override earlier ones."
            ),
        )
        parser.add_argument(
            "--output",
            type=str,
            default="-",
            help="Output file path. Use '-' (the default) to write to stdout.",
        )
        parser.add_argument(
            "--include-envparser",
            action="store_true",
            default=None,
            help=(
                "Include fields from the mitol EnvParser registry. "
                "Auto-enabled when mitol-django-common is installed and a "
                "mitol app is in INSTALLED_APPS."
            ),
        )
        parser.add_argument(
            "--attribute-packages",
            action="store_true",
            default=False,
            help=(
                "Attribute each setting to its owning Python package and group "
                "the output by package. Requires Django to be configured for "
                "the AST-scan step."
            ),
        )

    def handle(self, *args: object, **options: object) -> None:
        """Execute the command."""
        modules_str = str(options.get("modules", "") or "")
        output_path = str(options.get("output", "-") or "-")
        output_format = str(options.get("format", "python") or "python")
        include_envparser: bool | None = options.get("include_envparser")  # type: ignore[assignment]

        module_paths = [m.strip() for m in modules_str.split(",") if m.strip()]

        if include_envparser is None:
            include_envparser = _envparser_available() and _mitol_in_installed_apps()

        # Discover into a name-keyed dict so a later module (or the envparser)
        # overrides an earlier definition — one attribute per name, no dupes.
        by_name: dict[str, SettingField] = {}
        for module_path in module_paths:
            try:
                for f in StaticModuleInspector(module_path).discover():
                    by_name[f.name] = f
            except (ImportError, OSError, SyntaxError) as exc:
                raise CommandError(
                    f"Static discovery failed for {module_path!r}: {exc}"
                ) from exc

        if include_envparser:
            try:
                from django_aqueduct.discovery.envparser import (  # noqa: PLC0415
                    EnvParserInspector,
                )

                for f in EnvParserInspector().discover():
                    by_name.setdefault(f.name, f)
            except ImportError as exc:
                raise CommandError(str(exc)) from exc

        fields: list[SettingField] = [by_name[name] for name in sorted(by_name)]

        if not fields:
            self.stderr.write(
                self.style.WARNING(
                    "No settings fields discovered. "
                    "Specify --modules or --include-envparser."
                )
            )

        if options.get("attribute_packages"):
            self._attribute(fields)

        if output_format == "jsonschema":
            import json  # noqa: PLC0415

            output = json.dumps(SchemaGenerator(fields).generate(), indent=2)
        else:
            output = ModelRenderer(fields).render()

        self._write(output, output_path)

    @staticmethod
    def _attribute(fields: list[SettingField]) -> None:
        """Populate ``owning_package`` on every field (in place)."""
        from django_aqueduct.discovery.package_attributor import (  # noqa: PLC0415
            PackageAttributor,
        )

        try:
            from django.apps import apps as django_apps  # noqa: PLC0415

            installed_apps = [a.name for a in django_apps.get_app_configs()]
        except Exception:  # noqa: BLE001
            installed_apps = []

        attribution = PackageAttributor(installed_apps=installed_apps).attribute(fields)
        for f in fields:
            f.owning_package = attribution.get(f.name, "project")

    def _write(self, output: str, output_path: str) -> None:
        """Write *output* to *output_path* ('-' means stdout)."""
        if output_path == "-":
            self.stdout.write(output, ending="")
            return
        try:
            with open(output_path, "w", encoding="utf-8") as fh:  # noqa: PTH123
                fh.write(output)
        except OSError as exc:
            raise CommandError(f"Cannot write to {output_path!r}: {exc}") from exc
        self.stdout.write(
            self.style.SUCCESS(f"Settings model written to {output_path}")
        )
