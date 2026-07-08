"""Reconcile a dependency surface against a project and render the report.

Given the surface entries a project's dependencies introduce (see
:mod:`~django_aqueduct.discovery.dependency_surface`) and the settings the
project itself defines (static discovery + EnvParser), this module classifies
each surface setting as **set**, **unset**, or **overridden**, and renders the
result as a table, Markdown, or JSON.

Pure and deterministic: it takes already-discovered IR in, emits text out, reads
no environment, and never resolves a live value. Secret-shaped names are
redacted in every format.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from django_aqueduct.discovery.ir import DefaultStrategy
from django_aqueduct.discovery.secrets import looks_secret

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Mapping, Sequence

    from django_aqueduct.discovery.dependency_surface import SurfaceEntry
    from django_aqueduct.discovery.ir import SettingField
    from django_aqueduct.surface import Setting

_REDACTED = "(redacted)"
_REQUIRED = "(required)"
_MAX_VALUE_LEN = 48

_COLUMNS = ("PACKAGE", "SETTING", "TYPE", "DEFAULT", "PROJECT", "HINT")


@dataclass(frozen=True)
class ReportRow:
    """One reconciled row of the dependency-surface report.

    Attributes:
        package: Owning distribution label.
        setting: Setting name (``REST_FRAMEWORK.<KEY>`` for nested keys).
        type: The declared/known type-annotation string.
        package_default: Rendered package default (``(required)`` when the
            package declares none, ``(redacted)`` for secret-shaped names).
        project_status: ``"set"``, ``"unset"``, or ``"overridden"``.
        project_value: Rendered project value, ``"unset"``, or ``(redacted)``.
        hint: ``"OK"`` (project decided), ``"REVIEW"`` (unset with a meaningful
            default), ``"SECRET"`` (redacted), or ``"-"``.
    """

    package: str
    setting: str
    type: str
    package_default: str
    project_status: str
    project_value: str
    hint: str


def _short(text: str, limit: int = _MAX_VALUE_LEN) -> str:
    """Collapse whitespace and truncate *text* for single-line display."""
    collapsed = " ".join(text.split())
    if len(collapsed) > limit:
        return collapsed[: limit - 1] + "…"
    return collapsed


def _render_package_default(setting: Setting, *, secret: bool) -> str:
    """Render a package default for display."""
    if secret:
        return _REDACTED
    if not setting.has_default:
        return _REQUIRED
    return _short(repr(setting.default))


def _project_value(field: SettingField) -> str:
    """Render the project's own value for a discovered field."""
    default = field.default
    strategy = default.strategy
    if strategy in (DefaultStrategy.LITERAL, DefaultStrategy.FACTORY):
        return _short(repr(default.literal))
    if strategy is DefaultStrategy.EXPR:
        return _short(default.expr or "")
    if strategy is DefaultStrategy.REQUIRED:
        return _REQUIRED
    if strategy is DefaultStrategy.DERIVED:
        return "(derived)"
    if strategy is DefaultStrategy.REDACTED:
        return _REDACTED
    if strategy is DefaultStrategy.RUNTIME_ONLY:
        return "(runtime)"
    return "(set)"


def _is_override(setting: Setting, field: SettingField) -> bool:
    """Return ``True`` when the project's literal value differs from the default.

    Only a statically-known literal project value can be proven to override the
    package default; anything else (required/derived/expr) counts as merely
    "set" since the value cannot be compared.
    """
    if not setting.has_default:
        return False
    if field.default.strategy in (DefaultStrategy.LITERAL, DefaultStrategy.FACTORY):
        return field.default.literal != setting.default
    return False


def _hint(*, secret: bool, status: str, setting: Setting) -> str:
    """Return the decision hint for a row."""
    if secret:
        return "SECRET"
    if status in ("set", "overridden"):
        return "OK"
    # unset
    if not setting.has_default:
        return "REVIEW"
    if setting.default is not None:
        return "REVIEW"
    return "-"


def reconcile(
    entries: Sequence[SurfaceEntry],
    project_fields: Mapping[str, SettingField],
) -> list[ReportRow]:
    """Return one :class:`ReportRow` per surface entry, reconciled against the project.

    Args:
        entries: Surface entries (already sorted deterministically).
        project_fields: ``{name: SettingField}`` the project defines.

    Returns:
        Report rows in the same order as *entries*.
    """
    rows: list[ReportRow] = []
    for entry in entries:
        setting = entry.setting
        secret = looks_secret(setting.name)
        field = project_fields.get(setting.name)
        if field is None:
            status = "unset"
            project_value = "unset"
        elif _is_override(setting, field):
            status = "overridden"
            project_value = _REDACTED if secret else _project_value(field)
        else:
            status = "set"
            project_value = _REDACTED if secret else _project_value(field)
        rows.append(
            ReportRow(
                package=entry.dist,
                setting=setting.name,
                type=setting.type,
                package_default=_render_package_default(setting, secret=secret),
                project_status=status,
                project_value=project_value,
                hint=_hint(secret=secret, status=status, setting=setting),
            )
        )
    return rows


def _project_cell(row: ReportRow) -> str:
    """Combine status and value into a single ``PROJECT`` column cell."""
    if row.project_status == "unset":
        return "unset"
    return f"{row.project_status}: {row.project_value}"


def render_table(rows: Sequence[ReportRow]) -> str:
    """Render rows as a plain aligned text table."""
    if not rows:
        return "No dependency-surface settings found.\n"
    cells = [_COLUMNS]
    cells.extend(
        (r.package, r.setting, r.type, r.package_default, _project_cell(r), r.hint)
        for r in rows
    )
    widths = [max(len(row[i]) for row in cells) for i in range(len(_COLUMNS))]
    lines = [
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        for row in cells
    ]
    return "\n".join(lines) + "\n"


def render_markdown(rows: Sequence[ReportRow]) -> str:
    """Render rows as a GitHub-flavoured Markdown table."""
    header = "| " + " | ".join(_COLUMNS) + " |"
    sep = "| " + " | ".join("---" for _ in _COLUMNS) + " |"
    lines = [header, sep]
    for r in rows:
        lines.append(
            "| "
            + " | ".join(
                (
                    r.package,
                    r.setting,
                    r.type,
                    r.package_default,
                    _project_cell(r),
                    r.hint,
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def render_json(rows: Sequence[ReportRow]) -> str:
    """Render rows as a JSON array of objects."""
    return json.dumps([asdict(r) for r in rows], indent=2) + "\n"


def render(rows: Sequence[ReportRow], fmt: str) -> str:
    """Render *rows* in *fmt* (``table`` | ``json`` | ``markdown``)."""
    if fmt == "json":
        return render_json(rows)
    if fmt == "markdown":
        return render_markdown(rows)
    return render_table(rows)
