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
            "--engine",
            choices=["v1", "v2"],
            default="v1",
            help=(
                "Discovery/render engine. 'v1' (default) imports the settings "
                "module and reads resolved runtime values. 'v2' uses static "
                "AST discovery (deterministic, secret-safe, recovers env "
                "aliases/required-ness/source expressions) via "
                "StaticModuleInspector + ModelRenderer. 'v2' currently "
                "supports --format python only."
            ),
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
        parser.add_argument(
            "--attribute-packages",
            action="store_true",
            default=False,
            help=(
                "Attribute each setting to its owning Python package and "
                "group the output by package instead of source module. "
                "Uses five strategies in priority order: Django core, "
                "callable inspection, Celery/DRF built-in APIs, AST scan "
                "of installed packages, and a static prefix table. "
                "Requires Django to be configured for the AST scan step."
            ),
        )

    def _handle_v2(
        self, module_paths: list[str], output_path: str, output_format: str
    ) -> None:
        """Run the v2 static-discovery + IR-renderer pipeline."""
        from django_aqueduct.codegen.renderer import ModelRenderer  # noqa: PLC0415
        from django_aqueduct.discovery.ir import SettingField  # noqa: PLC0415
        from django_aqueduct.discovery.static import (  # noqa: PLC0415
            StaticModuleInspector,
        )

        if output_format != "python":
            raise CommandError("--engine v2 supports --format python only.")
        if not module_paths:
            raise CommandError("--engine v2 requires --modules.")

        fields: list[SettingField] = []
        for module_path in module_paths:
            try:
                fields.extend(StaticModuleInspector(module_path).discover())
            except (ImportError, OSError, SyntaxError) as exc:
                raise CommandError(
                    f"Static discovery failed for {module_path!r}: {exc}"
                ) from exc

        output = ModelRenderer(fields).render()
        self._write(output, output_path)

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

    def handle(self, *args: object, **options: object) -> None:
        """Execute the command."""
        modules_str = str(options.get("modules", "") or "")
        output_path = str(options.get("output", "-") or "-")
        output_format = str(options.get("format", "python") or "python")
        engine = str(options.get("engine", "v1") or "v1")
        include_envparser: bool | None = options.get("include_envparser")  # type: ignore[assignment]

        module_paths = [m.strip() for m in modules_str.split(",") if m.strip()]

        if engine == "v2":
            self._handle_v2(module_paths, output_path, output_format)
            return

        # Resolve --include-envparser auto-detection
        if include_envparser is None:
            include_envparser = _envparser_available() and _mitol_in_installed_apps()

        fields: list[DiscoveredField] = []

        # Module inspector(s)
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

        # --attribute-packages: populate owning_package on every field
        if options.get("attribute_packages"):
            from django_aqueduct.discovery.package_attributor import (  # noqa: PLC0415
                PackageAttributor,
            )

            try:
                from django.apps import apps as django_apps  # noqa: PLC0415

                installed_apps = [a.name for a in django_apps.get_app_configs()]
            except Exception:  # noqa: BLE001
                installed_apps = []

            attributor = PackageAttributor(installed_apps=installed_apps)
            attribution = attributor.attribute(fields)
            for f in fields:
                f.owning_package = attribution.get(f.name, "project")

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
