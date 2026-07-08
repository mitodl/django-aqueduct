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

    A dict-valued setting is compared with :func:`_dict_subset_matches`
    rather than strict equality: *legacy_values* is typically read from
    ``settings.X`` after Django has fully initialized, so it can carry keys
    Django or a third-party app injected at runtime (``DATABASES`` entries
    gaining ``ATOMIC_REQUESTS``/``TEST``/etc., ``HEALTH_CHECK`` gaining
    ``DISK_USAGE_MAX``/etc.) that the model's raw ``model_dump()`` never had
    a chance to produce. Those extra legacy-only keys are not flagged; a
    model key missing from legacy, or a shared key with a different value,
    still is.

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
        # Normalize list/tuple so a list-vs-tuple difference isn't a false type
        # divergence; keep the originals in the report for readability.
        n_m, n_l = _normalize(m_val), _normalize(l_val)
        # bool is a subclass of int; compare types before values so a bool vs
        # int (1 == True) does not slip through as "equal".
        if type(n_m) is not type(n_l):
            report.divergences.append(Divergence(name, m_val, l_val, "type"))
        elif isinstance(n_m, dict):
            if not _dict_subset_matches(n_m, n_l):
                report.divergences.append(Divergence(name, m_val, l_val, "value"))
        elif n_m != n_l:
            report.divergences.append(Divergence(name, m_val, l_val, "value"))

    return report


def _dict_subset_matches(model_val: Any, legacy_val: Any) -> bool:
    """One-way dict comparison: every key/value in *model_val* must be in *legacy_val*.

    *legacy_val* may carry extra keys the model doesn't. ``legacy_values`` is
    read from ``settings.X`` *after* Django has finished initializing — by
    which point Django itself (``ConnectionHandler.
    ensure_defaults`` adding ``ATOMIC_REQUESTS``/``AUTOCOMMIT``/``TEST``/
    ``TIME_ZONE`` to each ``DATABASES`` entry) and third-party
    ``AppConfig.ready()`` hooks (django-health-check injecting
    ``DISK_USAGE_MAX``/``MEMORY_MIN``/... into ``HEALTH_CHECK``) have already
    mutated it, while ``model_values`` is the model's raw, un-augmented
    ``model_dump()``. Comparing those two dicts key-for-key would flag every
    runtime-injected key as a divergence on every run. A model key missing
    from legacy, or present in both with a different value, is still a real
    divergence.
    """
    if not isinstance(model_val, dict):
        return bool(model_val == legacy_val)
    if not isinstance(legacy_val, dict):
        return False
    return all(
        key in legacy_val and _dict_subset_matches(sub, legacy_val[key])
        for key, sub in model_val.items()
    )


def _normalize(value: Any) -> Any:
    """Coerce tuples to lists (recursively) for comparison.

    Django uses lists and tuples interchangeably (``ALLOWED_HOSTS``,
    ``INSTALLED_APPS``, ``MIDDLEWARE``), and pydantic's ``model_dump()`` emits
    lists where a legacy module may use tuples. Without this, that difference
    would be reported as a spurious type divergence.
    """
    if isinstance(value, list | tuple):
        return [_normalize(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    return value


def uppercase_settings(module: Any) -> dict[str, Any]:
    """Return the ``{NAME: value}`` mapping of UPPERCASE names on *module*."""
    return {name: getattr(module, name) for name in dir(module) if name.isupper()}
