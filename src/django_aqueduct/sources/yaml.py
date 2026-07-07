"""YAML file settings source for pydantic-settings.

Requires the ``[yaml]`` extra::

    pip install django-aqueduct[yaml]

Reads a YAML mapping from a file and feeds it to a settings model, decoding
complex (dict/list) fields via ``prepare_field_value`` like the other sources.

Example::

    from django_aqueduct.sources.yaml import YamlSettingsSource

    class AppSettings(BaseSettings):
        @classmethod
        def settings_customise_sources(cls, settings_cls, **kwargs):
            return (YamlSettingsSource(settings_cls, "config/settings.yaml"),)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from django_aqueduct.sources._base import SourceError, build_from_mapping


class YamlError(SourceError):
    """Raised when the YAML settings file cannot be read or parsed."""


def _require_yaml() -> Any:
    """Import ``yaml`` (PyYAML) or raise an actionable error."""
    try:
        import yaml  # noqa: PLC0415

        return yaml
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise ImportError(
            "YamlSettingsSource requires 'pyyaml'. "
            "Install it with: pip install django-aqueduct[yaml]"
        ) from exc


class YamlSettingsSource(PydanticBaseSettingsSource):
    """Load settings from a YAML file.

    Args:
        settings_cls: The settings class (passed by pydantic-settings).
        path: Path to the YAML file.
        optional: When ``True`` a missing file yields no settings instead of
            raising — useful for an optional local override file.
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        path: str | Path,
        *,
        optional: bool = False,
    ) -> None:
        """Store the YAML file path."""
        super().__init__(settings_cls)
        self._path = Path(path)
        self._optional = optional
        self._data_cache: dict[str, Any] | None = None

    @property
    def _data(self) -> dict[str, Any]:
        """Load (once) and cache the YAML mapping."""
        if self._data_cache is None:
            self._data_cache = self._load()
        return self._data_cache

    def _load(self) -> dict[str, Any]:
        if not self._path.is_file():
            if self._optional:
                return {}
            raise YamlError(f"YAML settings file not found: {self._path}")
        yaml = _require_yaml()
        try:
            loaded = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise YamlError(f"Could not parse {self._path}: {exc}") from exc
        if loaded is None:
            return {}
        if not isinstance(loaded, dict):
            raise YamlError(
                f"{self._path} must contain a mapping at the top level, "
                f"got {type(loaded).__name__}."
            )
        return loaded

    def __call__(self) -> dict[str, Any]:
        """Return the YAML data as a validated settings dict (complex-aware)."""
        return build_from_mapping(self, self._data)

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        """Look up *field_name* from the cached YAML data."""
        return self._data.get(field_name), field_name, self.field_is_complex(field)
