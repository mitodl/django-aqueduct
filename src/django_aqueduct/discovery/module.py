"""Settings inspector that walks an arbitrary Python module."""

import importlib
from types import ModuleType
from typing import Any

from django_aqueduct.discovery.base import DiscoveredField, ValueKind
from django_aqueduct.discovery.type_inference import infer_annotation

# Substrings that mark a settings name as likely holding a secret. Module
# inspection reads the *live* resolved value (whatever was in the environment
# when the generator ran), not the static default in the source code — so a
# name like SECRET_KEY or MAILGUN_API_TOKEN would otherwise get its real,
# environment-supplied value baked verbatim into the generated (and likely
# committed) file. Matched fields are redacted; see ValueKind.REDACTED.
_SENSITIVE_NAME_MARKERS: tuple[str, ...] = (
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "TOKEN",
    "PRIVATE_KEY",
    "API_KEY",
    "APIKEY",
    "ACCESS_KEY",
    "CREDENTIAL",
    "SIGNING_KEY",
    "ENCRYPTION_KEY",
    "_DSN",
)


def _looks_secret(name: str) -> bool:
    """Return True if *name* contains a substring commonly used for secrets."""
    return any(marker in name for marker in _SENSITIVE_NAME_MARKERS)


class ModuleInspector:
    """Discover settings by inspecting UPPERCASE names in a Python module.

    Any name that satisfies ``name.isupper()`` is treated as a settings field.
    Type annotations are inferred from the runtime value via
    :func:`~django_aqueduct.discovery.type_inference.infer_annotation`.

    .. warning::
        This inspector imports *module_path* and reads the live, resolved
        value of each setting — i.e. whatever the environment supplied at
        generation time, not the static default written in the source code.
        Fields whose name looks secret-like (see ``_looks_secret``) are
        redacted (``ValueKind.REDACTED``, rendered as ``default=None``)
        rather than written verbatim, but review the generated file for any
        other sensitive values before committing it — especially if the
        generator was run against an environment with real credentials.

    Args:
        module_path: Dotted Python import path, e.g. ``"myapp.settings"``.

    Raises:
        ImportError: If *module_path* cannot be imported, with an actionable
            message that includes the path.

    Example::

        inspector = ModuleInspector("myapp.settings.production")
        fields = inspector.discover()
    """

    def __init__(self, module_path: str) -> None:
        """Store the dotted module path to inspect."""
        self._module_path = module_path
        self._module: ModuleType | None = None

    def _load(self) -> ModuleType:
        if self._module is None:
            try:
                self._module = importlib.import_module(self._module_path)
            except ImportError as exc:
                raise ImportError(
                    f"django-aqueduct could not import settings module "
                    f"'{self._module_path}'. "
                    f"Ensure the module is on sys.path and has no import errors.\n"
                    f"Original error: {exc}"
                ) from exc
        return self._module

    def discover(self) -> list[DiscoveredField]:
        """Return one :class:`~django_aqueduct.discovery.base.DiscoveredField` per UPPERCASE name.

        Returns:
            Fields sorted by name for deterministic output.
        """  # noqa: E501
        module = self._load()
        source = module.__name__

        fields: list[DiscoveredField] = []
        for name in sorted(dir(module)):
            if not name.isupper():
                continue

            value: Any = getattr(module, name)
            result = infer_annotation(value)

            if _looks_secret(name):
                fields.append(
                    DiscoveredField(
                        name=name,
                        type_annotation=result.annotation,
                        default=None,
                        description="",
                        required=False,
                        source_module=source,
                        dev_only=False,
                        needs_refinement=result.needs_refinement,
                        value_kind=ValueKind.REDACTED,
                    )
                )
                continue

            fields.append(
                DiscoveredField(
                    name=name,
                    type_annotation=result.annotation,
                    default=value,
                    description="",
                    required=False,
                    source_module=source,
                    dev_only=False,
                    needs_refinement=result.needs_refinement,
                    value_kind=result.value_kind,
                )
            )

        return fields
