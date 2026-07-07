"""Typed intermediate representation (IR) for codegen v2.

The v1 pipeline carried settings as a string ``type_annotation`` and a live
``default`` object (:mod:`django_aqueduct.discovery.base`), forcing every
downstream step to do string surgery on types and ``repr()`` on runtime
objects. That produced the ``NameError``/``"<" in repr``/optional-widening
failure classes documented in the codegen v2 RFC.

This module replaces that with an explicit, renderer-agnostic IR:

* :class:`TypeRef` — a resolved type expression plus the exact imports it
  needs. ``optional`` is a first-class flag, never a substring.
* :class:`Default` + :class:`DefaultStrategy` — *how* a default is produced,
  decoupled from *what* type the field has. A default captured from source is
  an :attr:`DefaultStrategy.EXPR` carrying verbatim source text, so the
  renderer never has to ``repr()`` a live object.
* :class:`SettingField` — one setting, carrying its type, default, env
  aliases, required-ness, description, and provenance.

Nothing in this module imports Django, pydantic, or the target settings
module: the IR is pure data so that discovery, rendering, golden-file tests,
and determinism tests can all operate on it independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


@dataclass(frozen=True)
class ImportSpec:
    """A single import the renderer must emit for a type or expression to resolve.

    Shapes supported:

    * ``ImportSpec("datetime")`` → ``import datetime``
    * ``ImportSpec("datetime", asname="dt")`` → ``import datetime as dt``
    * ``ImportSpec("datetime", "timedelta")`` → ``from datetime import timedelta``
    * ``ImportSpec("datetime", "timedelta", asname="td")``
      → ``from datetime import timedelta as td``

    Frozen and hashable so a whole model's imports can be de-duplicated with a
    ``set``.

    Attributes:
        module: The dotted module to import from, e.g. ``"datetime"``.
        name: The name to import from ``module``. ``None`` emits a plain
            ``import module``.
        asname: The bound alias (``import x as asname`` /
            ``from m import n as asname``). ``None`` emits no alias. Preserving
            the alias is required so a captured default expression that
            references the aliased name still resolves.
    """

    module: str
    name: str | None = None
    asname: str | None = None

    def render(self) -> str:
        """Return the import statement as a single line of source."""
        if self.name is None:
            stmt = f"import {self.module}"
        else:
            stmt = f"from {self.module} import {self.name}"
        if self.asname is not None:
            stmt = f"{stmt} as {self.asname}"
        return stmt

    def sort_key(self) -> tuple[int, str, str, str]:
        """Return a key that orders plain imports before ``from`` imports."""
        return (
            0 if self.name is None else 1,
            self.module,
            self.name or "",
            self.asname or "",
        )


@dataclass(frozen=True)
class TypeRef:
    """A resolved type expression that renders without guessing imports.

    Attributes:
        base: A normalized type expression, e.g. ``"str"``,
            ``"list[str]"``, ``"datetime.timedelta"``,
            ``"dict[str, DatabasesEntry]"``. Built from a closed vocabulary;
            never contains ``| None`` (use :attr:`optional`).
        imports: The exact imports required for :attr:`base` to resolve.
        optional: When ``True`` the field renders as ``base | None``. A
            first-class flag — replaces v1's ``_nullable_annotation`` string
            surgery.
        needs_refinement: When ``True`` the renderer emits a review marker
            (not a blanket suppression) because the type is a best-effort
            guess.
    """

    base: str
    imports: frozenset[ImportSpec] = frozenset()
    optional: bool = False
    needs_refinement: bool = False

    def render(self) -> str:
        """Return the annotation string, applying ``| None`` when optional."""
        if not self.optional:
            return self.base
        if self.base == "Any" or self.base == "None":
            return self.base
        return f"{self.base} | None"

    def with_optional(self, *, optional: bool = True) -> TypeRef:
        """Return a copy with :attr:`optional` set."""
        return TypeRef(
            base=self.base,
            imports=self.imports,
            optional=optional,
            needs_refinement=self.needs_refinement,
        )


class DefaultStrategy(str, Enum):  # noqa: UP042
    """How a field's default value is produced by the renderer.

    Decoupled from the field's *type*: a ``datetime.timedelta`` field may be
    ``EXPR`` (captured from source) while a plain ``str`` field is ``LITERAL``.

    Attributes:
        LITERAL: A renderer-serializable literal (str/int/float/bool/None,
            or a container thereof). Rendered inline as ``default=<literal>``.
        FACTORY: A mutable literal (list/dict/set). Rendered as
            ``default_factory=lambda: <literal>`` so instances don't share
            state.
        EXPR: A verbatim source expression captured by static discovery,
            e.g. ``"timedelta(days=1)"``. Rendered directly with its own
            imports; the generator never ``repr()``s a live object.
        REQUIRED: No default — the value must be supplied at runtime. Renders
            ``Field(...)`` (pydantic Ellipsis). Restores the required-ness v1
            erased.
        DERIVED: Computed from other settings (conditional branch, lazy
            proxy). Renders ``default=None`` with a pointer to the derivation
            library.
        REDACTED: Name looks secret-like; the observed value is never written.
            Renders ``default=None`` with a "set explicitly" comment.
    """

    LITERAL = "literal"
    FACTORY = "factory"
    EXPR = "expr"
    REQUIRED = "required"
    DERIVED = "derived"
    REDACTED = "redacted"


@dataclass(frozen=True)
class Default:
    """A field's default and everything the renderer needs to emit it.

    Attributes:
        strategy: Which :class:`DefaultStrategy` to render.
        literal: For ``LITERAL``/``FACTORY`` — the value to serialize. The
            renderer owns serialization and rejects anything it can't emit
            safely (falling back upstream to ``EXPR``/``DERIVED``).
        expr: For ``EXPR`` — verbatim source text of the default expression.
        expr_imports: Imports required for :attr:`expr` to resolve.
    """

    strategy: DefaultStrategy
    literal: object | None = None
    expr: str | None = None
    expr_imports: frozenset[ImportSpec] = frozenset()

    @classmethod
    def literal_(cls, value: object, *, factory: bool = False) -> Default:
        """Build a ``LITERAL`` (or ``FACTORY``) default from *value*."""
        return cls(
            strategy=DefaultStrategy.FACTORY if factory else DefaultStrategy.LITERAL,
            literal=value,
        )

    @classmethod
    def expr_(
        cls, source: str, imports: frozenset[ImportSpec] = frozenset()
    ) -> Default:
        """Build an ``EXPR`` default from verbatim *source* text."""
        return cls(strategy=DefaultStrategy.EXPR, expr=source, expr_imports=imports)

    @classmethod
    def required(cls) -> Default:
        """Build a ``REQUIRED`` default (renders ``Field(...)``)."""
        return cls(strategy=DefaultStrategy.REQUIRED)

    @classmethod
    def derived(cls) -> Default:
        """Build a ``DERIVED`` default (renders ``default=None`` + comment)."""
        return cls(strategy=DefaultStrategy.DERIVED)

    @classmethod
    def redacted(cls) -> Default:
        """Build a ``REDACTED`` default (renders ``default=None`` + comment)."""
        return cls(strategy=DefaultStrategy.REDACTED)


class DiscoveryMethod(str, Enum):  # noqa: UP042
    """How a field was discovered — recorded on :class:`Provenance`."""

    STATIC = "static"
    RUNTIME = "runtime"
    ENVPARSER = "envparser"


@dataclass(frozen=True)
class Provenance:
    """Where and how a field was discovered.

    Attributes:
        source_module: Dotted module or file the field came from.
        method: The discovery method that produced it.
        lineno: 1-indexed line of the declaration, when known.
        conditional: ``True`` when the value is assigned inside a conditional
            branch (so a single snapshot would freeze one branch).
        runtime_only: ``True`` when the name exists at runtime but has no
            static assignment — a human should review it.
    """

    source_module: str = ""
    method: DiscoveryMethod = DiscoveryMethod.STATIC
    lineno: int | None = None
    conditional: bool = False
    runtime_only: bool = False


@dataclass
class SettingField:
    """One discovered setting, as typed IR.

    Attributes:
        name: The UPPERCASE settings name.
        type: The resolved :class:`TypeRef`.
        default: The :class:`Default` describing how to render the default.
        env_aliases: Environment variable names this setting reads, rendered
            as ``validation_alias=AliasChoices(...)``.
        required: Whether the value must be supplied at runtime.
        description: Human-readable description (renderer escapes newlines).
        provenance: Discovery metadata.
        owning_package: PyPI distribution that owns this setting (Phase D
            attribution seam); empty until attributed.
        dev_only: Whether the setting is development-only.
    """

    name: str
    type: TypeRef
    default: Default
    env_aliases: tuple[str, ...] = ()
    required: bool = False
    description: str = ""
    provenance: Provenance = field(default_factory=Provenance)
    owning_package: str = ""
    dev_only: bool = False

    def all_imports(self) -> frozenset[ImportSpec]:
        """Return every import this field needs (type + expr default)."""
        return self.type.imports | self.default.expr_imports
