"""One-time migration helper: wrap a hand-refined v1 model in v2 region markers.

The v1 generator (removed when v2 became the sole engine) emitted a flat
``AqueductSettings(BaseSettings)`` class with no region markers at all: an
import block, ``model_config``, a run of ``NAME: Type = Field(...)``
declarations, and (once hand-edited) ``@model_validator``/``@field_validator``
methods appended below. The five app integration PRs adopted that shape and
then hand-refined it — adding derivations, aliases, and validators the
generator could never author.

``wrap_existing`` inserts the same ``# >>> aqueduct:generated:*`` /
``# >>> aqueduct:preserved:*`` markers :mod:`~django_aqueduct.codegen.renderer`
emits, around the *existing* import block, field block, and everything after
it — without moving, reformatting, or otherwise touching a single line of
source. It is purely a comment-insertion pass: the output is byte-identical to
the input except for the inserted marker lines, so it changes zero runtime
behavior. This lets a hand-refined v1 file adopt the v2 managed-region merge
writer (:mod:`~django_aqueduct.codegen.regions`) without losing any hand work.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from django_aqueduct.codegen.renderer import region_close, region_open


class WrapExistingError(Exception):
    """Raised when *source* cannot be safely wrapped."""


@dataclass(frozen=True)
class _Span:
    """1-indexed, inclusive line range."""

    start: int
    end: int


def _end_lineno(node: ast.stmt) -> int:
    """Return ``node.end_lineno``, which is always set for text-parsed source."""
    assert node.end_lineno is not None  # noqa: S101
    return node.end_lineno


def _find_settings_class(tree: ast.Module, class_name: str | None) -> ast.ClassDef:
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    if class_name is not None:
        matches = [c for c in classes if c.name == class_name]
        if not matches:
            raise WrapExistingError(
                f"No class named {class_name!r} found at module level."
            )
        return matches[0]

    def _bases_include_basesettings(c: ast.ClassDef) -> bool:
        for base in c.bases:
            name = base.attr if isinstance(base, ast.Attribute) else None
            name = name or (base.id if isinstance(base, ast.Name) else None)
            if name == "BaseSettings":
                return True
        return False

    matches = [c for c in classes if _bases_include_basesettings(c)]
    if not matches:
        raise WrapExistingError(
            "No class deriving from BaseSettings found at module level. "
            "Pass class_name explicitly if the class isn't named "
            "'AqueductSettings'."
        )
    if len(matches) > 1:
        names = ", ".join(c.name for c in matches)
        raise WrapExistingError(
            f"Multiple BaseSettings-derived classes found ({names}); "
            "pass class_name to disambiguate."
        )
    return matches[0]


def _import_span(tree: ast.Module) -> _Span | None:
    import_nodes = [n for n in tree.body if isinstance(n, ast.Import | ast.ImportFrom)]
    if not import_nodes:
        return None
    return _Span(import_nodes[0].lineno, _end_lineno(import_nodes[-1]))


def _classify_body(cls: ast.ClassDef) -> tuple[_Span, _Span | None]:
    """Return ``(fields_span, validators_span)`` for the class body.

    Docstring (leading string ``Expr``) and ``model_config = ...`` are left
    unmarked, matching what :class:`~django_aqueduct.codegen.renderer.ModelRenderer`
    emits outside any region. A contiguous run of ``AnnAssign`` statements
    (typed field declarations) becomes the ``fields`` region; every statement
    after it becomes the ``validators`` region (mirroring the fresh-render
    "preserved:validators" catch-all).

    Raises:
        WrapExistingError: If a non-field, non-config, non-docstring statement
            appears *before* the field block — the file's shape doesn't match
            what a v1-generated-and-refined model looks like, and guessing
            would risk silently misclassifying hand-written code.
    """
    body = list(cls.body)
    idx = 0
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        idx += 1
    while idx < len(body):
        stmt = body[idx]
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id == "model_config"
        ):
            idx += 1
            continue
        break

    field_start = idx
    while idx < len(body) and isinstance(body[idx], ast.AnnAssign):
        idx += 1
    field_end = idx

    if field_end == field_start:
        raise WrapExistingError(
            "No typed field declarations (NAME: Type = Field(...)) found "
            "immediately after the docstring/model_config — this doesn't "
            "look like a v1-generated model, refusing to guess."
        )

    fields_span = _Span(body[field_start].lineno, _end_lineno(body[field_end - 1]))
    validators_span = None
    if field_end < len(body):
        validators_span = _Span(body[field_end].lineno, _end_lineno(body[-1]))
    return fields_span, validators_span


def wrap_existing(source: str, *, class_name: str | None = None) -> str:
    """Return *source* with v2 region markers inserted, unchanged otherwise.

    Args:
        source: The existing hand-refined model file's full text.
        class_name: The ``BaseSettings`` subclass to wrap. Auto-detected when
            exactly one class derives from ``BaseSettings``.

    Raises:
        WrapExistingError: If *source* already carries aqueduct region
            markers, has no (unambiguous) ``BaseSettings`` class, or the
            class body's shape can't be safely classified.
    """
    if "# >>> aqueduct:" in source:
        raise WrapExistingError(
            "Source already contains aqueduct region markers — regenerate "
            "normally instead of wrapping again."
        )

    tree = ast.parse(source)
    cls = _find_settings_class(tree, class_name)
    fields_span, validators_span = _classify_body(cls)
    imports_span = _import_span(tree)

    # Insert bottom-up so earlier spans' line numbers stay valid.
    inserts: list[tuple[int, str]] = []  # (1-indexed line to insert BEFORE, text)
    if validators_span is not None:
        inserts.append(
            (validators_span.end + 1, region_close("preserved", "validators"))
        )
        inserts.append((validators_span.start, region_open("preserved", "validators")))
    inserts.append((fields_span.end + 1, region_close("generated", "fields")))
    inserts.append((fields_span.start, region_open("generated", "fields")))
    if imports_span is not None:
        inserts.append(
            (imports_span.end + 1, region_close("generated", "imports").lstrip())
        )
        inserts.append(
            (imports_span.start, region_open("generated", "imports").lstrip())
        )

    lines = source.splitlines(keepends=True)
    for lineno, marker in sorted(inserts, key=lambda pair: pair[0], reverse=True):
        lines.insert(lineno - 1, marker + "\n")
    return "".join(lines)
