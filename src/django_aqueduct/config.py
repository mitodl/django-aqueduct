"""``[tool.aqueduct]`` configuration loaded from ``pyproject.toml``.

Lets a project record its generation settings once so ``manage.py
generate_aqueduct_settings`` is reproducible (and ``--check`` compares against
a known baseline) without re-passing flags::

    [tool.aqueduct]
    modules = ["myapp.settings.base", "myapp.settings.production"]
    output = "src/myapp/settings_model.py"
    include_envparser = true
    attribute_packages = true
    class_name = "AqueductSettings"
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_VALID_FORMATS = ("python", "jsonschema")
_VALID_EXTRA = ("allow", "ignore", "forbid")


class ConfigError(Exception):
    """Raised when ``[tool.aqueduct]`` cannot be read or holds an invalid value."""


@dataclass
class AqueductConfig:
    """Resolved ``[tool.aqueduct]`` settings (all fields optional)."""

    modules: list[str] = field(default_factory=list)
    output: str | None = None
    include_envparser: bool | None = None
    attribute_packages: bool = False
    class_name: str = "AqueductSettings"
    output_format: str = "python"
    extra: str = "allow"
    parity_model: str | None = None
    parity_legacy: str | None = None
    parity_ignore: list[str] = field(default_factory=list)


def find_pyproject(start: Path | None = None) -> Path | None:
    """Return the nearest ``pyproject.toml`` at or above *start* (cwd default)."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / "pyproject.toml"
        if candidate.is_file():
            return candidate
    return None


def load_config(start: Path | None = None) -> AqueductConfig:
    """Load ``[tool.aqueduct]`` from the nearest pyproject.toml, or defaults.

    Unknown keys are ignored; a missing file or table yields an empty config.
    """
    path = find_pyproject(start)
    if path is None:
        return AqueductConfig()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc
    table = data.get("tool", {}).get("aqueduct", {})
    if not isinstance(table, dict):
        return AqueductConfig()

    cfg = AqueductConfig()
    modules = table.get("modules")
    if isinstance(modules, list):
        cfg.modules = [str(m) for m in modules]
    if isinstance(table.get("output"), str):
        cfg.output = table["output"]
    if isinstance(table.get("include_envparser"), bool):
        cfg.include_envparser = table["include_envparser"]
    if isinstance(table.get("attribute_packages"), bool):
        cfg.attribute_packages = table["attribute_packages"]
    if isinstance(table.get("class_name"), str):
        cfg.class_name = table["class_name"]
    if "format" in table:
        fmt = table["format"]
        if fmt not in _VALID_FORMATS:
            raise ConfigError(
                f"[tool.aqueduct] format={fmt!r} is invalid; "
                f"expected one of {', '.join(_VALID_FORMATS)}."
            )
        cfg.output_format = fmt
    if "extra" in table:
        extra = table["extra"]
        if extra not in _VALID_EXTRA:
            raise ConfigError(
                f"[tool.aqueduct] extra={extra!r} is invalid; "
                f"expected one of {', '.join(_VALID_EXTRA)}."
            )
        cfg.extra = extra
    if isinstance(table.get("parity_model"), str):
        cfg.parity_model = table["parity_model"]
    if isinstance(table.get("parity_legacy"), str):
        cfg.parity_legacy = table["parity_legacy"]
    parity_ignore = table.get("parity_ignore")
    if isinstance(parity_ignore, list):
        cfg.parity_ignore = [
            stripped for x in parity_ignore if (stripped := str(x).strip())
        ]
    return cfg
