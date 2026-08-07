"""Managed-region merge and drift-check for generated settings files.

The renderer fences its output into regions with sentinel comments::

    # >>> aqueduct:generated:fields
    ...machine-owned lines...
    # <<< aqueduct:generated:fields

    # >>> aqueduct:preserved:validators
    ...hand-written lines that survive regeneration...
    # <<< aqueduct:preserved:validators

Regeneration must rewrite only the bodies of ``generated`` regions and leave
everything else — ``preserved`` regions *and* any free-form code outside a
region — byte-for-byte intact. This module implements that merge, plus a
read-only drift check for CI.
"""

from __future__ import annotations

import ast
import difflib
import re
from collections.abc import Iterator
from dataclasses import dataclass

# Matches an opening/closing marker at any indentation, e.g.
# ``    # >>> aqueduct:generated:fields``.
_MARKER_RE = re.compile(
    r"^\s*#\s*(?P<dir>>>>|<<<)\s*aqueduct:(?P<kind>generated|preserved):(?P<id>\S+)\s*$"
)


class RegionError(Exception):
    """Raised when region markers are malformed, unbalanced, or missing."""


def _iter_marked_regions(text: str) -> Iterator[tuple[str, str, int, int]]:
    """Yield ``(kind, id, open_idx, close_idx)`` for each balanced marker pair.

    Line indices are into ``text.splitlines()``; ``open_idx``/``close_idx`` are
    the marker line indices. Raises :class:`RegionError` on an unbalanced or
    mismatched marker pair.
    """
    lines = text.splitlines()
    open_stack: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str]] = set()
    for idx, line in enumerate(lines):
        m = _MARKER_RE.match(line)
        if m is None:
            continue
        kind, rid, direction = m["kind"], m["id"], m["dir"]
        if direction == ">>>":
            if (kind, rid) in seen:
                raise RegionError(
                    f"Duplicate region aqueduct:{kind}:{rid} "
                    f"(reopened at line {idx + 1})."
                )
            seen.add((kind, rid))
            open_stack.append((kind, rid, idx))
        else:  # <<<
            if not open_stack:
                raise RegionError(
                    f"Closing marker for aqueduct:{kind}:{rid} at line {idx + 1} "
                    f"has no matching opening marker."
                )
            o_kind, o_id, o_idx = open_stack.pop()
            if (o_kind, o_id) != (kind, rid):
                raise RegionError(
                    f"Marker mismatch: opened aqueduct:{o_kind}:{o_id} but closed "
                    f"aqueduct:{kind}:{rid} at line {idx + 1}."
                )
            yield o_kind, o_id, o_idx, idx
    if open_stack:
        o_kind, o_id, o_idx = open_stack[-1]
        raise RegionError(
            f"Unclosed marker aqueduct:{o_kind}:{o_id} opened at line {o_idx + 1}."
        )


def generated_regions(text: str) -> dict[str, str]:
    """Return ``{region_id: body}`` for every ``generated`` region in *text*."""
    lines = text.splitlines()
    out: dict[str, str] = {}
    for kind, rid, o_idx, c_idx in _iter_marked_regions(text):
        if kind == "generated":
            out[rid] = "\n".join(lines[o_idx + 1 : c_idx])
    return out


def _region_span(text: str, region_id: str) -> tuple[int, int] | None:
    """Return the ``(open_idx, close_idx)`` of one generated region, if present."""
    for kind, rid, o_idx, c_idx in _iter_marked_regions(text):
        if kind == "generated" and rid == region_id:
            return o_idx, c_idx
    return None


def _enclosing_class(tree: ast.Module, open_line: int) -> ast.ClassDef | None:
    """Return the class whose body contains the ``fields`` region marker.

    Identified by the region's *opening* marker, never its closing one.
    ``ClassDef.end_lineno`` stops at the last syntactic statement, and every
    region marker is a comment — so when nothing after the fields region is a
    statement (no container decoders, no URL serializers, a preserved region
    holding only the placeholder comments), ``end_lineno`` lands on the last
    generated field, *before* the closing marker. Requiring the class to span
    the closing marker would reject the settings class outright in that layout,
    silently disabling override suppression exactly where an override is most
    likely: a declaration placed above the fields region, which the README
    explicitly permits ("anywhere else outside a generated region").

    The opening marker has no such problem: any class body containing the
    region necessarily starts before it, and a class that closed earlier cannot
    reach it. Among the classes that qualify, the innermost (latest-starting)
    one wins, so a helper class nested above the region isn't mistaken for the
    settings class.
    """
    candidates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and node.lineno < open_line <= (node.end_lineno or 0)
    ]
    return max(candidates, key=lambda n: n.lineno) if candidates else None


def overridden_field_names(existing: str) -> set[str]:
    """Return field names the settings class declares *outside* a generated region.

    The managed-region model invites refining a generated field by re-declaring
    it — with a narrower type, a different default, or extra validation — in a
    preserved region. Re-emitting the generated declaration as well leaves two
    class-level assignments of the same name in one class body, which is a
    genuine lint finding (ruff ``PIE794`` / ``F811``) and not one a project can
    reasonably silence per-line: it fires on the *generated* declaration, so an
    app whose ruleset selects those rules is pushed into excluding the whole
    file, losing lint coverage of the hand-written regions too. The caller drops
    the generated declaration instead, leaving exactly one.

    Only *annotated* declarations count. A bare ``NAME = value`` is not a
    complete pydantic field — it borrows the annotation from the generated
    declaration above it. Dropping that declaration would leave the model with
    an unannotated class attribute, which pydantic v2 rejects outright::

        PydanticUserError: A non-annotated attribute was detected:
        `POOL_SIZE = 5`. All model fields require a type annotation

    That turns a lint finding (two class-level assignments) into a model that
    won't import at all — strictly worse than the problem being solved. So a
    plain assignment is left alone: its generated declaration stays, keeping the
    model valid, and the ``PIE794``/``F811`` pair remains for that one field.
    Annotate the override to have it suppressed.

    Only the class that encloses the ``fields`` region is inspected, so helper
    classes elsewhere in the module can reuse a settings name freely. A file
    that doesn't parse yields no names — regenerating over a half-edited file
    should not also silently drop managed declarations.
    """
    fields_span = _region_span(existing, "fields")
    if fields_span is None:
        return set()
    try:
        tree = ast.parse(existing)
    except SyntaxError:
        return set()

    # ast linenos are 1-based; region indices are 0-based line offsets.
    open_line = fields_span[0] + 1
    generated_spans = [
        (o + 1, c + 1)
        for kind, _rid, o, c in _iter_marked_regions(existing)
        if kind == "generated"
    ]

    cls = _enclosing_class(tree, open_line)
    if cls is None:
        return set()

    names: set[str] = set()
    for stmt in cls.body:
        # ast.Assign (`NAME = value`) is deliberately not handled — see above.
        if not isinstance(stmt, ast.AnnAssign):
            continue
        if any(o <= stmt.lineno <= c for o, c in generated_spans):
            continue
        if isinstance(stmt.target, ast.Name):
            names.add(stmt.target.id)
    return names


def merge(existing: str, generated: str) -> str:
    """Return *existing* with each generated region's body replaced from *generated*.

    Preserved regions and any text outside a generated region are kept exactly.
    Raises :class:`RegionError` if *existing* lacks a generated region that
    *generated* provides (the file was hand-edited to drop a marker — refuse to
    silently lose managed output; the caller can offer ``--reset``).

    This is a pure text merge. Dropping the declarations *existing* overrides is
    not done here: it has to happen while rendering, so the ``imports`` region
    is computed from the same reduced field set (see
    :func:`overridden_field_names` and ``ModelRenderer(overridden=...)``).
    """
    new_bodies = generated_regions(generated)
    existing_lines = existing.splitlines(keepends=True)

    # Collect the (open_idx, close_idx) spans of existing generated regions,
    # keyed by id, from an index-only pass over the existing file.
    spans: dict[str, tuple[int, int]] = {}
    for kind, rid, o_idx, c_idx in _iter_marked_regions(existing):
        if kind == "generated":
            spans[rid] = (o_idx, c_idx)

    missing = set(new_bodies) - set(spans)
    if missing:
        raise RegionError(
            "Generated region(s) missing from the target file: "
            + ", ".join(f"aqueduct:generated:{r}" for r in sorted(missing))
            + ". Re-add the markers or regenerate from scratch (--reset)."
        )

    # A generated region on disk that the generator no longer produces is stale
    # content we would otherwise leave behind untouched. Surface it rather than
    # silently keeping it (consistent with the missing-region check above).
    obsolete = set(spans) - set(new_bodies)
    if obsolete:
        raise RegionError(
            "Target file has generated region(s) no longer produced by the "
            "generator: "
            + ", ".join(f"aqueduct:generated:{r}" for r in sorted(obsolete))
            + ". Remove the markers or regenerate from scratch (--reset)."
        )

    # Rebuild line-by-line, swapping each generated region body.
    replace_at: dict[int, tuple[int, str]] = {
        o_idx: (c_idx, new_bodies[rid]) for rid, (o_idx, c_idx) in spans.items()
    }
    out: list[str] = []
    i = 0
    n = len(existing_lines)
    while i < n:
        if i in replace_at:
            c_idx, body = replace_at[i]
            out.append(existing_lines[i])  # opening marker
            if body:
                out.append(body + "\n")
            out.append(_ensure_newline(existing_lines[c_idx]))  # closing marker
            i = c_idx + 1
        else:
            out.append(existing_lines[i])
            i += 1
    return "".join(out)


def _ensure_newline(line: str) -> str:
    return line if line.endswith("\n") else line + "\n"


@dataclass(frozen=True)
class DriftResult:
    """Outcome of a drift check between on-disk and freshly-generated output."""

    in_sync: bool
    diff: str  # unified diff of the generated regions (empty when in sync)


def check_drift(existing: str, generated: str) -> DriftResult:
    """Compare the *generated* regions of *existing* against a fresh render.

    Preserved regions and free-form code are ignored. Returns a
    :class:`DriftResult` whose ``diff`` is a unified diff of only the managed
    regions that differ (or are missing).

    *generated* must have been rendered with the same ``overridden=`` set the
    write path uses, or a file with a hand-written override reports permanent
    drift; the management command derives both from the on-disk file.
    """
    old = generated_regions(existing)
    new = generated_regions(generated)
    diff_lines: list[str] = []
    for rid in sorted(set(old) | set(new)):
        old_body = old.get(rid)
        new_body = new.get(rid)
        if old_body == new_body:
            continue
        diff_lines.extend(
            difflib.unified_diff(
                (old_body or "").splitlines(),
                (new_body or "").splitlines(),
                fromfile=f"on-disk:aqueduct:generated:{rid}",
                tofile=f"generated:aqueduct:generated:{rid}",
                lineterm="",
            )
        )
    return DriftResult(in_sync=not diff_lines, diff="\n".join(diff_lines))
