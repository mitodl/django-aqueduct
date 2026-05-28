"""Settings inspector that walks an arbitrary Python module."""

import importlib
from types import ModuleType
from typing import Any

from django_aqueduct.discovery.base import DiscoveredField
from django_aqueduct.discovery.type_inference import infer_annotation


class ModuleInspector:
    """Discover settings by inspecting UPPERCASE names in a Python module.

    Any name that satisfies ``name.isupper()`` is treated as a settings field.
    Type annotations are inferred from the runtime value via
    :func:`~django_aqueduct.discovery.type_inference.infer_annotation`.

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
            annotation, needs_refinement = infer_annotation(value)

            fields.append(
                DiscoveredField(
                    name=name,
                    type_annotation=annotation,
                    default=value,
                    description="",
                    required=False,
                    source_module=source,
                    dev_only=False,
                    needs_refinement=needs_refinement,
                )
            )

        return fields
