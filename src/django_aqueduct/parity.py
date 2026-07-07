"""Model-vs-legacy settings parity comparison.

While an app keeps its legacy ``settings.py`` as the deployed default and the
generated ``AqueductSettings`` model as a parallel artifact, nothing polices
drift between them — re-implemented Redis fallbacks, DB parsing, and conditional
middleware have shipped divergences before. :func:`compare` diffs the two
resolved settings dicts so CI can fail on unexplained drift, and so a team can
gate the eventual switch of ``DJANGO_SETTINGS_MODULE`` to the model shim.

This module is pure data-in / report-out; the management command
``check_aqueduct_settings`` builds the two dicts (instantiate the model, import
the legacy module) and renders a :class:`ParityReport`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Divergence:
    """A single per-key difference between the model and legacy settings."""

    name: str
    model_value: Any
    legacy_value: Any
    kind: str  # "missing_in_model" | "missing_in_legacy" | "value" | "type"

    def describe(self) -> str:
        """Return a one-line human description of this divergence."""
        if self.kind == "missing_in_model":
            return f"{self.name}: present in legacy but not in the model"
        if self.kind == "missing_in_legacy":
            return f"{self.name}: present in the model but not in legacy"
        if self.kind == "type":
            return (
                f"{self.name}: type differs — model "
                f"{type(self.model_value).__name__} vs legacy "
                f"{type(self.legacy_value).__name__}"
            )
        return (
            f"{self.name}: value differs — model {self.model_value!r} vs "
            f"legacy {self.legacy_value!r}"
        )


@dataclass
class ParityReport:
    """The result of comparing model settings against legacy settings."""

    divergences: list[Divergence] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)

    @property
    def in_sync(self) -> bool:
        """True when there are no (non-ignored) divergences."""
        return not self.divergences

    def render(self) -> str:
        """Return a human-readable multi-line report."""
        if self.in_sync:
            suffix = f" ({len(self.ignored)} ignored)" if self.ignored else ""
            return f"Model and legacy settings are in parity{suffix}."
        lines = [f"{len(self.divergences)} divergence(s):"]
        lines.extend(f"  - {d.describe()}" for d in self.divergences)
        if self.ignored:
            lines.append(f"Ignored {len(self.ignored)} key(s): {sorted(self.ignored)}")
        return "\n".join(lines)


def compare(
    model_values: dict[str, Any],
    legacy_values: dict[str, Any],
    *,
    ignore: set[str] | None = None,
) -> ParityReport:
    """Compare *model_values* against *legacy_values*, returning a report.

    Reports keys missing from either side, value differences, and type
    mismatches. Keys in *ignore* (deliberate policy divergences, e.g. a setting
    the migration intentionally made required) are recorded as ignored rather
    than flagged.

    Args:
        model_values: The model's resolved settings (e.g. ``model_dump()``).
        legacy_values: The legacy module's UPPERCASE settings.
        ignore: Names to exclude from divergence reporting.

    Returns:
        A :class:`ParityReport`.
    """
    ignored_names = ignore or set()
    report = ParityReport()

    all_names = set(model_values) | set(legacy_values)
    for name in sorted(all_names):
        if name in ignored_names:
            report.ignored.append(name)
            continue
        in_model = name in model_values
        in_legacy = name in legacy_values
        if in_model and not in_legacy:
            report.divergences.append(
                Divergence(name, model_values[name], None, "missing_in_legacy")
            )
            continue
        if in_legacy and not in_model:
            report.divergences.append(
                Divergence(name, None, legacy_values[name], "missing_in_model")
            )
            continue

        m_val, l_val = model_values[name], legacy_values[name]
        # bool is a subclass of int; compare types before values so a bool vs
        # int (1 == True) does not slip through as "equal".
        if type(m_val) is not type(l_val):
            report.divergences.append(Divergence(name, m_val, l_val, "type"))
        elif m_val != l_val:
            report.divergences.append(Divergence(name, m_val, l_val, "value"))

    return report


def uppercase_settings(module: Any) -> dict[str, Any]:
    """Return the ``{NAME: value}`` mapping of UPPERCASE names on *module*."""
    return {name: getattr(module, name) for name in dir(module) if name.isupper()}
