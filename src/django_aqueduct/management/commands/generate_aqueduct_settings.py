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

Regenerating a file *merges* into its managed ``# >>> aqueduct:generated:*``
regions, leaving hand-written code in ``# >>> aqueduct:preserved:*`` regions
(and anywhere outside a generated region) untouched. ``--reset`` overwrites the
whole file instead. ``--check`` writes nothing and exits non-zero (with a diff)
when the on-disk file has drifted from a fresh render — use it in CI.

Flags default to any ``[tool.aqueduct]`` table in the nearest ``pyproject.toml``
so generation is reproducible::

    [tool.aqueduct]
    modules = ["myapp.settings.base", "myapp.settings.production"]
    output = "src/myapp/settings_model.py"
    include_envparser = true
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
            default=None,
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
            default=None,
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
            default=None,
            help=(
                "Attribute each setting to its owning Python package and group "
                "the output by package. Requires Django to be configured for "
                "the AST-scan step."
            ),
        )
        parser.add_argument(
            "--class-name",
            type=str,
            default=None,
            help="Name of the generated BaseSettings subclass.",
        )
        parser.add_argument(
            "--check",
            action="store_true",
            default=False,
            help=(
                "Do not write. Compare the freshly-generated managed regions "
                "against the on-disk --output file and exit non-zero (with a "
                "diff) if they have drifted. For CI."
            ),
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            default=False,
            help=(
                "Overwrite the whole --output file with a fresh skeleton "
                "instead of merging into its existing managed regions. Discards "
                "hand-written code in preserved regions."
            ),
        )

    def handle(self, *args: object, **options: object) -> None:
        """Execute the command."""
        from django_aqueduct.config import ConfigError, load_config  # noqa: PLC0415

        try:
            cfg = load_config()
        except ConfigError as exc:
            raise CommandError(str(exc)) from exc

        # CLI flags override [tool.aqueduct]; fall back to config, then default.
        modules_str = str(options.get("modules") or "")
        cli_modules = [m.strip() for m in modules_str.split(",") if m.strip()]
        module_paths = cli_modules or cfg.modules

        output_path = str(options.get("output") or cfg.output or "-")
        output_format = str(options.get("format") or cfg.output_format or "python")
        class_name = str(options.get("class_name") or cfg.class_name)
        check = bool(options.get("check"))
        reset = bool(options.get("reset"))

        include_envparser: bool | None = options.get("include_envparser")  # type: ignore[assignment]
        if include_envparser is None:
            include_envparser = cfg.include_envparser
        if include_envparser is None:
            include_envparser = _envparser_available() and _mitol_in_installed_apps()

        attribute_packages = options.get("attribute_packages")
        if attribute_packages is None:
            attribute_packages = cfg.attribute_packages

        # Discover into a name-keyed dict so a later module overrides an
        # earlier one (one attribute per name, no dupes); the envparser below
        # only fills in names not already found in a module.
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

        if attribute_packages:
            self._attribute(fields)

        if output_format == "jsonschema":
            import json  # noqa: PLC0415

            output = json.dumps(SchemaGenerator(fields).generate(), indent=2)
        else:
            output = ModelRenderer(fields, class_name=class_name).render()

        if check:
            self._check(output, output_path, output_format)
        else:
            self._emit(output, output_path, output_format, reset=reset)

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

    def _emit(
        self, output: str, output_path: str, output_format: str, *, reset: bool
    ) -> None:
        """Write *output*, merging into an existing file's managed regions.

        Python output merges into the existing file so hand-written code in
        preserved regions (and anywhere outside a generated region) survives;
        ``--reset`` bypasses the merge. JSON Schema and stdout always overwrite.
        """
        from pathlib import Path  # noqa: PLC0415

        if output_path == "-":
            self.stdout.write(output, ending="")
            return

        path = Path(output_path)
        final = output
        if output_format == "python" and not reset and path.is_file():
            from django_aqueduct.codegen.regions import (  # noqa: PLC0415
                RegionError,
                merge,
            )

            existing = path.read_text(encoding="utf-8")
            try:
                final = merge(existing, output)
            except RegionError as exc:
                raise CommandError(
                    f"Cannot merge into {output_path!r}: {exc} "
                    f"Pass --reset to overwrite it."
                ) from exc

        try:
            path.write_text(final, encoding="utf-8")
        except OSError as exc:
            raise CommandError(f"Cannot write to {output_path!r}: {exc}") from exc
        self.stdout.write(
            self.style.SUCCESS(f"Settings model written to {output_path}")
        )

    def _check(self, output: str, output_path: str, output_format: str) -> None:
        """Compare generated output against the on-disk file; fail on drift."""
        from pathlib import Path  # noqa: PLC0415

        if output_path == "-":
            raise CommandError("--check requires --output to point at a file.")
        path = Path(output_path)
        if not path.is_file():
            raise CommandError(
                f"--check: {output_path!r} does not exist; run without --check "
                f"to create it."
            )
        existing = path.read_text(encoding="utf-8")

        if output_format == "python":
            from django_aqueduct.codegen.regions import (  # noqa: PLC0415
                RegionError,
                check_drift,
            )

            try:
                result = check_drift(existing, output)
            except RegionError as exc:
                raise CommandError(f"--check: {exc}") from exc
            in_sync, diff = result.in_sync, result.diff
        else:
            in_sync = existing == output
            diff = "" if in_sync else "on-disk JSON Schema differs from generated."

        if in_sync:
            self.stdout.write(self.style.SUCCESS(f"{output_path} is up to date."))
            return
        if diff:
            self.stderr.write(diff)
        raise CommandError(
            f"{output_path} is out of date. Re-run without --check to update it."
        )
