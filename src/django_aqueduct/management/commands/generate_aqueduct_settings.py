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

    # One-time: adopt managed regions in a hand-refined v1-era model, with
    # zero change to its code (comment-only insertion)
    python manage.py generate_aqueduct_settings \\
        --wrap-existing src/myapp/settings_model.py

    # Optional enrichment: refine dict shapes / closed-value sets / URL
    # fields by importing the settings module under several env snapshots,
    # plus mining app code for value/range checks against settings.X
    python manage.py generate_aqueduct_settings \\
        --modules myapp.settings \\
        --enrich-runtime --runtime-env-file .env.dev --runtime-env-file .env.prod \\
        --enrich-usage src/myapp

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
    extra = "forbid"          # reject un-modeled keys (typo detection)

**A note on ``--enrich-runtime``:** unlike every other flag, it imports the
target settings module — once per ``--runtime-env-file`` snapshot — which
executes arbitrary project code. It never authors a field's default,
required-ness, or env aliases from what it observes (only type shape/
closed-value-set/URL refinements, which the renderer marks
``needs_refinement`` for human review); still, only point it at modules and
env files you trust. ``--enrich-usage`` never executes anything — it's a
plain AST scan of the given source paths.
"""

from __future__ import annotations

from typing import Any

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
            "--extra",
            choices=["allow", "ignore", "forbid"],
            default=None,
            help=(
                "Pydantic model 'extra' policy for un-modeled keys: 'allow' "
                "(keep), 'ignore' (drop), or 'forbid' (reject — enables typo "
                "detection)."
            ),
        )
        parser.add_argument(
            "--use-plugins",
            action="store_true",
            default=None,
            help=(
                "Include fields from inspector plugins registered under the "
                "'django_aqueduct.inspectors' entry-point group."
            ),
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
        parser.add_argument(
            "--wrap-existing",
            type=str,
            default=None,
            metavar="PATH",
            help=(
                "One-time migration helper: insert aqueduct:generated/preserved "
                "region markers into an existing hand-refined model file "
                "(e.g. one produced by the removed v1 engine) in place, without "
                "changing a single line of code. Ignores every other flag. "
                "Run this once per app before adopting --check/regeneration."
            ),
        )
        parser.add_argument(
            "--enrich-runtime",
            action="store_true",
            default=False,
            help=(
                "Import --modules once per --runtime-env-file snapshot to "
                "refine dict shapes (genson), closed-value-set fields "
                "(Literal[...]), and URL-shaped strings (AnyUrl) — never "
                "field defaults/required-ness/aliases. Executes project code; "
                "only point it at trusted modules and env files."
            ),
        )
        parser.add_argument(
            "--runtime-env-file",
            action="append",
            default=[],
            metavar="PATH",
            help=(
                "A .env-style KEY=VALUE file overlaid onto os.environ for one "
                "--enrich-runtime sample. Repeatable; each occurrence is one "
                "sample. Ignored without --enrich-runtime."
            ),
        )
        parser.add_argument(
            "--enrich-usage",
            action="append",
            default=[],
            metavar="PATH",
            help=(
                "A file or directory to scan (recursively) for settings.X "
                "comparisons/range checks, promoting closed-value-set fields "
                "to Literal[...] and range-checked numeric fields to "
                "Field(gt=/ge=/lt=/le=). Repeatable. Static-only — never "
                "executes anything."
            ),
        )
        parser.add_argument(
            "--literal-max-values",
            type=int,
            default=None,
            help=(
                "Max distinct observed/compared values for a scalar field to "
                "be promoted to Literal[...] (default: 8)."
            ),
        )

    def handle(self, *args: object, **options: object) -> None:
        """Execute the command."""
        wrap_existing_path = options.get("wrap_existing")
        if wrap_existing_path:
            self._wrap_existing(str(wrap_existing_path), options.get("class_name"))
            return

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
        extra = str(options.get("extra") or cfg.extra)
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

        use_plugins = options.get("use_plugins")
        if use_plugins is None:
            use_plugins = cfg.use_plugins
        if use_plugins:
            from django_aqueduct.registry import (  # noqa: PLC0415
                RegistryError,
                discover_from_plugins,
            )

            try:
                for f in discover_from_plugins():
                    by_name.setdefault(f.name, f)
            except RegistryError as exc:
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
            self._attribute(fields, cfg.attribution_rules)

        literal_max_values: int | None = options.get("literal_max_values")  # type: ignore[assignment]

        from django_aqueduct.discovery.enrich import (  # noqa: PLC0415
            apply_url_type_hints,
        )

        apply_url_type_hints(fields)

        dict_enrichment: dict[str, tuple[str, list[Any]]] = {}
        if options.get("enrich_runtime"):
            env_file_paths: list[str] = list(options.get("runtime_env_file") or [])  # type: ignore[call-overload]
            dict_enrichment |= self._enrich_runtime(
                fields, module_paths, env_file_paths, literal_max_values
            )

        usage_paths: list[str] = list(options.get("enrich_usage") or [])  # type: ignore[call-overload]
        if usage_paths:
            self._enrich_usage(fields, usage_paths, literal_max_values)

        if output_format == "jsonschema":
            import json  # noqa: PLC0415

            output = json.dumps(SchemaGenerator(fields).generate(), indent=2)
        else:
            output = ModelRenderer(
                fields,
                class_name=class_name,
                extra=extra,
                dict_enrichment=dict_enrichment or None,
            ).render()

        if check:
            self._check(output, output_path, output_format)
        else:
            self._emit(output, output_path, output_format, reset=reset)

    @staticmethod
    def _attribute(
        fields: list[SettingField], extra_rules: list[tuple[str, str]]
    ) -> None:
        """Populate ``owning_package`` on every field (in place).

        ``extra_rules`` are ``[tool.aqueduct] attribution_rules`` (prefix/exact
        pattern → package), prepended ahead of the built-in rules.
        """
        from django_aqueduct.discovery.package_attributor import (  # noqa: PLC0415
            PackageAttributor,
        )

        try:
            from django.apps import apps as django_apps  # noqa: PLC0415

            installed_apps = [a.name for a in django_apps.get_app_configs()]
        except Exception:  # noqa: BLE001
            installed_apps = []

        attributor = PackageAttributor(
            installed_apps=installed_apps, extra_rules=extra_rules or None
        )
        attribution = attributor.attribute(fields)
        for f in fields:
            f.owning_package = attribution.get(f.name, "project")

    @staticmethod
    def _enrich_runtime(
        fields: list[SettingField],
        module_paths: list[str],
        env_file_paths: list[str],
        literal_max_values: int | None,
    ) -> dict[str, tuple[str, list[Any]]]:
        """Sample *module_paths* under each ``--runtime-env-file`` and merge in.

        Returns the ``dict_enrichment`` overlay for :class:`ModelRenderer`.
        """
        from pathlib import Path  # noqa: PLC0415

        from django_aqueduct.discovery.enrich import (  # noqa: PLC0415
            apply_runtime_enrichment,
        )
        from django_aqueduct.discovery.runtime import (  # noqa: PLC0415
            RuntimeSamplingError,
            parse_env_file,
            sample_module_values,
        )

        if not module_paths:
            raise CommandError("--enrich-runtime requires --modules.")

        snapshots = []
        for path_str in env_file_paths:
            path = Path(path_str)
            try:
                snapshots.append(parse_env_file(path.read_text(encoding="utf-8")))
            except OSError as exc:
                raise CommandError(
                    f"--runtime-env-file: cannot read {path_str!r}: {exc}"
                ) from exc
        if not snapshots:
            # No env files given: sample once under the current process env,
            # exactly the flags/environment this invocation is already using.
            snapshots = [{}]

        try:
            samples = sample_module_values(module_paths, snapshots)
        except RuntimeSamplingError as exc:
            raise CommandError(f"--enrich-runtime: {exc}") from exc

        kwargs = (
            {"literal_max_values": literal_max_values}
            if literal_max_values is not None
            else {}
        )
        return apply_runtime_enrichment(fields, samples, **kwargs)

    @staticmethod
    def _enrich_usage(
        fields: list[SettingField],
        scan_paths: list[str],
        literal_max_values: int | None,
    ) -> None:
        """Scan *scan_paths* for settings.X comparisons/range checks and merge in."""
        from django_aqueduct.discovery.enrich import (  # noqa: PLC0415
            apply_usage_enrichment,
            apply_usage_range_enrichment,
        )
        from django_aqueduct.discovery.usage import (  # noqa: PLC0415
            find_usage_candidates,
        )

        names = [f.name for f in fields]
        literals, ranges = find_usage_candidates(scan_paths, names)
        kwargs = (
            {"literal_max_values": literal_max_values}
            if literal_max_values is not None
            else {}
        )
        apply_usage_enrichment(fields, literals, **kwargs)
        apply_usage_range_enrichment(fields, ranges)

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

    def _wrap_existing(self, path_str: str, class_name: object) -> None:
        """Insert region markers into an existing hand-refined model in place."""
        from pathlib import Path  # noqa: PLC0415

        from django_aqueduct.codegen.wrap_existing import (  # noqa: PLC0415
            WrapExistingError,
            wrap_existing,
        )

        path = Path(path_str)
        if not path.is_file():
            raise CommandError(f"--wrap-existing: {path_str!r} does not exist.")

        source = path.read_text(encoding="utf-8")
        try:
            wrapped = wrap_existing(
                source, class_name=class_name if isinstance(class_name, str) else None
            )
        except (WrapExistingError, SyntaxError) as exc:
            raise CommandError(f"--wrap-existing: {exc}") from exc

        path.write_text(wrapped, encoding="utf-8")
        self.stdout.write(
            self.style.SUCCESS(
                f"Inserted aqueduct region markers into {path_str}. "
                "Run generate_aqueduct_settings --output "
                f"{path_str} normally to regenerate going forward."
            )
        )
