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
    for idx, line in enumerate(lines):
        m = _MARKER_RE.match(line)
        if m is None:
            continue
        kind, rid, direction = m["kind"], m["id"], m["dir"]
        if direction == ">>>":
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


def merge(existing: str, generated: str) -> str:
    """Return *existing* with each generated region's body replaced from *generated*.

    Preserved regions and any text outside a generated region are kept exactly.
    Raises :class:`RegionError` if *existing* lacks a generated region that
    *generated* provides (the file was hand-edited to drop a marker — refuse to
    silently lose managed output; the caller can offer ``--reset``).
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
