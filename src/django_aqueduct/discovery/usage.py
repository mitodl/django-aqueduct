"""Static whole-repo usage-site mining for constrained-value/range candidates.

A setting is often only ever assigned one of a handful of values in
practice — ``ENVIRONMENT`` is always ``"dev"``/``"staging"``/``"production"``
— or constrained to a numeric range (``if not (0 < TIMEOUT <= 3600): raise
...``). Both are opaque to static discovery (which only sees whatever
default happens to be assigned) and to a runtime snapshot (which only sees
one value at a time). They're usually only discoverable by reading
everywhere the setting is *compared against* — the app code that validates
or branches on it.

This module never imports anything; it walks ``.py`` files under given root
paths with :mod:`ast` and looks for comparisons/membership checks against
``settings.NAME`` (or ``getattr(settings, "NAME", ...)``), collecting:

* **Equality/membership candidates** — ``settings.X == "a"``,
  ``settings.X in ("a", "b")`` — evidence of a closed set of values.
  ``!=``/``not in`` are deliberately ignored: they only say a value is
  *excluded*, which is not evidence it's *valid*.
* **Range bounds** — ``settings.X > 0``, ``0 <= settings.X <= 3600`` (in
  either operand order, including chained comparisons) — evidence of a
  numeric range.

Both are heuristics, not proofs: the observed evidence only reflects what
the scanned code happens to check, not necessarily the field's full valid
domain (a value or a wider bound nobody has coded against yet stays
invisible). Callers must treat results as candidates for a human to
confirm, not a hard guarantee — see
:mod:`~django_aqueduct.discovery.enrich`, which renders equality candidates
as a ``needs_refinement`` ``Literal[...]`` and range bounds with an explicit
"confirm before trusting" comment, rather than silently trusting either.
"""

from __future__ import annotations

import ast
import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TypeGuard

# Same convention as discovery.package_attributor's AST scan: the local names
# usage sites bind `django.conf.settings` (or an app's own thin wrapper) to.
_SETTINGS_VAR_NAMES = frozenset({"settings", "django_settings", "conf"})

_SKIP_DIRS = frozenset(
    {"__pycache__", ".git", ".venv", "venv", "node_modules", "migrations", ".tox"}
)

_Scalar = str | int | float | bool | None
_Number = int | float


@dataclass
class RangeEvidence:
    """The tightest numeric bound seen at any usage site for one field.

    For each bound kind, only the tightest (most restrictive) value across
    every usage site is kept: the largest ``gt``/``ge``, the smallest
    ``lt``/``le``.
    """

    gt: _Number | None = None
    ge: _Number | None = None
    lt: _Number | None = None
    le: _Number | None = None

    def tighten_lower(self, *, strict: bool, value: _Number) -> None:
        """Record a lower-bound observation, keeping the tightest (largest)."""
        attr = "gt" if strict else "ge"
        current = getattr(self, attr)
        if current is None or value > current:
            setattr(self, attr, value)

    def tighten_upper(self, *, strict: bool, value: _Number) -> None:
        """Record an upper-bound observation, keeping the tightest (smallest)."""
        attr = "lt" if strict else "le"
        current = getattr(self, attr)
        if current is None or value < current:
            setattr(self, attr, value)

    def is_empty(self) -> bool:
        """Return ``True`` when no bound was ever recorded."""
        return (
            self.gt is None and self.ge is None and self.lt is None and self.le is None
        )


def _iter_python_files(root: str) -> Iterator[str]:
    if os.path.isfile(root):
        if root.endswith(".py"):
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        for fname in filenames:
            if fname.endswith(".py"):
                yield os.path.join(dirpath, fname)


def _settings_attr_name(node: ast.expr, names: set[str]) -> str | None:
    """Return the setting name if *node* is ``settings.NAME`` with NAME in *names*."""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in _SETTINGS_VAR_NAMES
        and node.attr in names
    ):
        return node.attr
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id in _SETTINGS_VAR_NAMES
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value in names
    ):
        return str(node.args[1].value)
    return None


def _literal(node: ast.expr) -> tuple[bool, object]:
    try:
        return True, ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return False, None


def _scalars_only(values: Sequence[object]) -> set[_Scalar]:
    return {v for v in values if v is None or isinstance(v, str | int | float | bool)}


def _is_number(value: object) -> TypeGuard[_Number]:
    # bool is an int subclass; a bare True/False is never a range bound.
    return isinstance(value, int | float) and not isinstance(value, bool)


def _walk_compare(
    node: ast.Compare,
    names: set[str],
    literals: dict[str, set[_Scalar]],
    ranges: dict[str, RangeEvidence],
) -> None:
    """Extract equality/membership and range-bound evidence from one ``Compare``.

    Handles (possibly chained) comparisons pairwise, exactly as Python
    evaluates them: ``a == b == c`` is ``a == b and b == c``; this also
    correctly handles ``0 <= settings.X <= 100`` as two independent bound
    observations on the same field.
    """
    operands = [node.left, *node.comparators]
    for op, left, right in zip(node.ops, operands[:-1], operands[1:], strict=True):
        left_name = _settings_attr_name(left, names)
        right_name = _settings_attr_name(right, names)

        # Only positive matches (==, in) are evidence of a *valid* value.
        # `!=`/`not in` say the opposite — the compared value is explicitly
        # excluded — so counting them as candidates would be backwards.
        if isinstance(op, ast.Eq):
            if left_name and not right_name:
                ok, val = _literal(right)
                if ok:
                    literals.setdefault(left_name, set()).update(_scalars_only([val]))
            elif right_name and not left_name:
                ok, val = _literal(left)
                if ok:
                    literals.setdefault(right_name, set()).update(_scalars_only([val]))
            continue

        if isinstance(op, ast.In):
            if left_name and not right_name:
                ok, val = _literal(right)
                if ok and isinstance(val, list | tuple | set | frozenset):
                    literals.setdefault(left_name, set()).update(
                        _scalars_only(list(val))
                    )
            continue

        if isinstance(op, ast.NotEq | ast.NotIn):
            continue

        if isinstance(op, ast.Gt | ast.GtE | ast.Lt | ast.LtE):
            # settings.X <op> <literal>
            if left_name and not right_name:
                ok, val = _literal(right)
                if ok and _is_number(val):
                    evidence = ranges.setdefault(left_name, RangeEvidence())
                    if isinstance(op, ast.Gt):
                        evidence.tighten_lower(strict=True, value=val)
                    elif isinstance(op, ast.GtE):
                        evidence.tighten_lower(strict=False, value=val)
                    elif isinstance(op, ast.Lt):
                        evidence.tighten_upper(strict=True, value=val)
                    else:
                        evidence.tighten_upper(strict=False, value=val)
            # <literal> <op> settings.X — mirror the operator's meaning.
            elif right_name and not left_name:
                ok, val = _literal(left)
                if ok and _is_number(val):
                    evidence = ranges.setdefault(right_name, RangeEvidence())
                    if isinstance(op, ast.Gt):  # N > X  <=>  X < N
                        evidence.tighten_upper(strict=True, value=val)
                    elif isinstance(op, ast.GtE):  # N >= X <=> X <= N
                        evidence.tighten_upper(strict=False, value=val)
                    elif isinstance(op, ast.Lt):  # N < X  <=>  X > N
                        evidence.tighten_lower(strict=True, value=val)
                    else:  # N <= X <=> X >= N
                        evidence.tighten_lower(strict=False, value=val)


def find_usage_candidates(
    root_paths: Sequence[str],
    setting_names: Sequence[str],
) -> tuple[dict[str, set[_Scalar]], dict[str, RangeEvidence]]:
    """Return ``(literal_candidates, range_candidates)`` from one repo scan.

    Scans every ``.py`` file reachable under *root_paths* (a file path is
    scanned directly; a directory is walked recursively, skipping
    ``__pycache__``/VCS/venv/migration directories). Files that fail to
    read or parse are silently skipped — this is a best-effort heuristic
    pass, not a build-breaking one.

    Args:
        root_paths: File or directory paths to scan.
        setting_names: The UPPERCASE setting names to look for; only
            comparisons against these names are recorded.

    Returns:
        ``literal_candidates``: ``{name: {observed_value, ...}}`` for
        settings compared for equality/membership against a literal.
        ``range_candidates``: ``{name: RangeEvidence}`` for settings compared
        against a numeric bound. Names with no evidence are omitted from
        each mapping independently.
    """
    names = set(setting_names)
    literals: dict[str, set[_Scalar]] = {}
    ranges: dict[str, RangeEvidence] = {}
    for root in root_paths:
        for path in _iter_python_files(root):
            try:
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    src = fh.read()
                tree = ast.parse(src)
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Compare):
                    _walk_compare(node, names, literals, ranges)
    literals = {name: values for name, values in literals.items() if values}
    ranges = {name: ev for name, ev in ranges.items() if not ev.is_empty()}
    return literals, ranges
