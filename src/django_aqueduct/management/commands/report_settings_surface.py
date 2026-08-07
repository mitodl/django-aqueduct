r"""Management command: report_settings_surface.

Enumerate, per installed dependency, the settings it introduces into the Django
settings namespace — its name, type, and the package's own default — and
reconcile each against what *this* project actually sets: **set** (with the
project's value), **unset**, or **overridden**. It is a decision aid, printed to
stdout; unlike ``generate_aqueduct_settings`` it writes no model and adds
nothing to the generated file, keeping ``generate --check`` drift output clean.

Surface data comes from three providers (highest fidelity first):

* **Declared surfaces** — packages that advertise a callable under the
  ``django_aqueduct.settings_surface`` entry-point group (see
  :mod:`django_aqueduct.surface`). Authoritative.
* **Built-in extractors** for Django core, DRF, and Celery, scoped to the
  packages in ``INSTALLED_APPS`` — reusing the same imports package attribution
  already performs.

Reconciliation reuses the ordinary static discovery over
``[tool.aqueduct] modules`` (plus the mitol EnvParser when enabled), so no extra
project code is imported or executed.

Usage::

    python manage.py report_settings_surface
    python manage.py report_settings_surface --format markdown
    python manage.py report_settings_surface --format json > surface.json
    python manage.py report_settings_surface --packages djangorestframework,celery

Flags override the ``[tool.aqueduct]`` keys ``dependency_surface_packages`` and
``dependency_surface_report_format``.

Safety: every provider imports only a package's *own* defaults module; no
project settings module is imported beyond what static discovery already does;
output is deterministic (stable sort by distribution then name) and
secret-shaped names are redacted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.management.base import BaseCommand, CommandError

if TYPE_CHECKING:  # pragma: no cover
    from django_aqueduct.discovery.ir import SettingField


class Command(BaseCommand):
    """Report the settings each installed dependency introduces, reconciled."""

    help = (
        "Enumerate, per installed dependency, the settings it introduces (name, "
        "type, package default) and whether this project sets them."
    )

    def add_arguments(self, parser: object) -> None:  # noqa: ANN001
        """Declare CLI arguments."""
        from argparse import ArgumentParser  # noqa: PLC0415

        assert isinstance(parser, ArgumentParser)  # noqa: S101
        parser.add_argument(
            "--format",
            choices=["table", "json", "markdown"],
            default=None,
            help=(
                "Output format: 'table' (default), 'json', or 'markdown'. "
                "Overrides [tool.aqueduct] dependency_surface_report_format."
            ),
        )
        parser.add_argument(
            "--packages",
            type=str,
            default=None,
            help=(
                "Comma-separated distribution labels to restrict the report to "
                "(e.g. 'djangorestframework,celery'). Overrides "
                "[tool.aqueduct] dependency_surface_packages."
            ),
        )
        parser.add_argument(
            "--modules",
            type=str,
            default="",
            help=(
                "Comma-separated settings modules to reconcile against. "
                "Defaults to [tool.aqueduct] modules."
            ),
        )

    def handle(self, *args: object, **options: object) -> None:
        """Execute the command."""
        from django_aqueduct.config import ConfigError, load_config  # noqa: PLC0415
        from django_aqueduct.discovery.dependency_surface import (  # noqa: PLC0415
            SurfaceError,
            gather_surface,
        )
        from django_aqueduct.discovery.surface_report import (  # noqa: PLC0415
            reconcile,
            render,
        )

        try:
            cfg = load_config()
        except ConfigError as exc:
            raise CommandError(str(exc)) from exc

        report_format = str(
            options.get("format") or cfg.dependency_surface_report_format or "table"
        )

        packages_opt = options.get("packages")
        if isinstance(packages_opt, str):
            restrict = [p.strip() for p in packages_opt.split(",") if p.strip()]
        else:
            restrict = cfg.dependency_surface_packages

        installed_apps = self._installed_apps()

        try:
            entries = gather_surface(installed_apps, restrict=restrict or None)
        except SurfaceError as exc:
            raise CommandError(str(exc)) from exc

        modules_str = str(options.get("modules") or "")
        cli_modules = [m.strip() for m in modules_str.split(",") if m.strip()]
        module_paths = cli_modules or cfg.modules
        project_fields = self._project_fields(module_paths, cfg.include_envparser)

        rows = reconcile(entries, project_fields)
        self.stdout.write(render(rows, report_format), ending="")

    @staticmethod
    def _installed_apps() -> list[str]:
        """Return dotted ``INSTALLED_APPS`` names, or an empty list."""
        try:
            from django.apps import apps as django_apps  # noqa: PLC0415

            return [a.name for a in django_apps.get_app_configs()]
        except Exception:  # noqa: BLE001
            return []

    def _project_fields(
        self, module_paths: list[str], include_envparser: bool | None
    ) -> dict[str, SettingField]:
        """Discover the settings the project itself defines (static + EnvParser)."""
        from django_aqueduct.discovery.static import (  # noqa: PLC0415
            StaticModuleInspector,
        )

        by_name: dict[str, SettingField] = {}
        for module_path in module_paths:
            try:
                for field in StaticModuleInspector(module_path).discover():
                    by_name[field.name] = field
            except (ImportError, OSError, SyntaxError) as exc:
                raise CommandError(
                    f"Static discovery failed for {module_path!r}: {exc}"
                ) from exc

        want_envparser = include_envparser
        if want_envparser is None:
            want_envparser = self._envparser_available()
        if want_envparser:
            try:
                from django_aqueduct.discovery.envparser import (  # noqa: PLC0415
                    EnvParserInspector,
                )

                for field in EnvParserInspector().discover():
                    by_name.setdefault(field.name, field)
            except ImportError:
                pass
        return by_name

    @staticmethod
    def _envparser_available() -> bool:
        """Return ``True`` when the mitol EnvParser is importable."""
        try:
            import mitol.common.envs  # noqa: F401, PLC0415
        except ImportError:
            return False
        return True
