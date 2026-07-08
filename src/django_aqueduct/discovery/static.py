"""Static AST discovery for codegen v2.

Importing a settings module and recording its *resolved runtime values* bakes
generation-machine state into defaults, freezes conditional branches, erases
required-ness, and loses env-var aliases.

This inspector reads the settings *source* instead. It parses the module with
:mod:`ast` and, for each module-level ``UPPERCASE = <expr>`` assignment,
produces a :class:`~django_aqueduct.discovery.ir.SettingField` whose default is
either a serializable literal (``LITERAL``/``FACTORY``) or the **verbatim
source expression** (``EXPR``) — so the renderer never has to ``repr()`` a live
object. It also extracts:

* **env-var aliases** from recognised reader calls (``get_string("X")``,
  ``os.environ["X"]``, ``os.getenv("X", default)``, …);
* **required-ness** from reader calls with no default / ``required=True``;
* **conditional provenance** for names assigned inside ``if``/``else`` bodies,
  which render as ``DERIVED`` rather than one frozen branch.

Because it never resolves a value, static discovery is deterministic and
secret-safe by construction; the name-marker redaction list is a fallback, not
the primary defence.
"""

from __future__ import annotations

import ast
import builtins
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import NamedTuple

from django_aqueduct.discovery.ir import (
    Default,
    DefaultStrategy,
    DiscoveryMethod,
    ImportSpec,
    Provenance,
    SettingField,
    TypeRef,
)
from django_aqueduct.discovery.secrets import looks_secret

# Names that are always available in generated code without an import.
_BUILTIN_NAMES: frozenset[str] = frozenset(dir(builtins))

# Statement nodes that open a new scope; the conditional-assignment walk must
# not descend into them (a local inside a nested def/class is not a setting).
_SCOPE_NODES: tuple[type[ast.AST], ...] = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
)

# Env-reader method name → annotation base. Deliberately excludes the builtin
# converters (``str``/``int``/``bool``/``float``): matching is on the trailing
# call name, so treating those as readers would misfire on any ``int(x)`` cast
# and fabricate a bogus alias/required flag.
_READER_TYPES: dict[str, str] = {
    "get_string": "str",
    "get_bool": "bool",
    "get_int": "int",
    "get_float": "float",
    "get_list_literal": "list[Any]",
    "get_delimited_list": "list[str]",
    "get_list_of_str": "list[str]",
    # django-environ style: env.str(...), env.bool(...), env.int(...)
    "str": "str",
    "bool": "bool",
    "int": "int",
    "float": "float",
}

# Only the django-environ ``env.<type>(...)`` readers use the builtin-shadowing
# names; a bare ``int(...)`` / ``str(...)`` at module scope is a cast, not a
# reader, so those names are recognised *only* as attribute calls.
_ATTRIBUTE_ONLY_READERS: frozenset[str] = frozenset({"str", "bool", "int", "float"})

# Callable expressions whose result type we can name precisely, keyed by the
# rendered call target. Lets ``FOO = timedelta(days=1)`` get a real annotation
# instead of ``Any``.
_KNOWN_CALL_TYPES: dict[str, TypeRef] = {
    "timedelta": TypeRef("datetime.timedelta", frozenset({ImportSpec("datetime")})),
    "datetime.timedelta": TypeRef(
        "datetime.timedelta", frozenset({ImportSpec("datetime")})
    ),
    "Decimal": TypeRef("decimal.Decimal", frozenset({ImportSpec("decimal")})),
    "decimal.Decimal": TypeRef("decimal.Decimal", frozenset({ImportSpec("decimal")})),
    "Path": TypeRef("pathlib.Path", frozenset({ImportSpec("pathlib")})),
    "pathlib.Path": TypeRef("pathlib.Path", frozenset({ImportSpec("pathlib")})),
}


def _call_target(node: ast.Call) -> str | None:
    """Return the dotted call target of a :class:`ast.Call`, e.g. ``"os.getenv"``."""
    return _dotted(node.func)


def _dotted(node: ast.expr) -> str | None:
    """Render a name/attribute chain as a dotted string, else ``None``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


class _ReaderInfo(NamedTuple):
    """Metadata extracted from an env-reader expression."""

    aliases: tuple[str, ...]
    type_ref: TypeRef
    required: bool
    default_node: ast.expr | None = None


# Keyword names under which an env reader may receive its variable name.
_ALIAS_KWARGS: frozenset[str] = frozenset({"name", "var", "key"})


def _reader_alias(call: ast.Call) -> tuple[str, ...]:
    """Return the env-var name from a reader call's first positional or name kwarg."""
    if call.args and isinstance(call.args[0], ast.Constant):
        first = call.args[0].value
        if isinstance(first, str):
            return (first,)
    for kw in call.keywords:
        if kw.arg in _ALIAS_KWARGS and isinstance(kw.value, ast.Constant):
            val = kw.value.value
            if isinstance(val, str):
                return (val,)
    return ()


def _reader_default_node(call: ast.Call) -> ast.expr | None:
    """Return the reader's default value node (2nd positional or ``default=`` kwarg)."""
    if len(call.args) >= 2:
        return call.args[1]
    for kw in call.keywords:
        if kw.arg == "default":
            return kw.value
    return None


def _reader_required_kwarg(call: ast.Call) -> bool | None:
    """Return the explicit ``required=`` value of a reader call, or ``None``.

    An explicit ``required=`` wins over the presence of a default, so
    ``get_string("X", default="d", required=True)`` is correctly required.
    """
    for kw in call.keywords:
        if kw.arg == "required" and isinstance(kw.value, ast.Constant):
            return bool(kw.value.value)
    return None


class _ImportTable:
    """Maps bound names in a module to the :class:`ImportSpec` that provides them."""

    def __init__(self) -> None:
        self._by_name: dict[str, ImportSpec] = {}

    def add_import(self, node: ast.Import) -> None:
        for alias in node.names:
            # `import a.b` binds `a` but the statement imports `a.b`; an
            # `as` alias binds that name instead and must be preserved so a
            # captured expression referencing the alias still resolves.
            bound = alias.asname or alias.name.split(".")[0]
            self._by_name[bound] = ImportSpec(alias.name, None, asname=alias.asname)

    def add_importfrom(self, node: ast.ImportFrom) -> None:
        if node.module is None or node.level:  # skip relative imports
            return
        for alias in node.names:
            bound = alias.asname or alias.name
            self._by_name[bound] = ImportSpec(
                node.module, alias.name, asname=alias.asname
            )

    def resolve(self, name: str) -> ImportSpec | None:
        return self._by_name.get(name)

    def imports_for(self, expr: ast.expr) -> frozenset[ImportSpec]:
        """Return every import the names referenced in *expr* require."""
        specs: set[ImportSpec] = set()
        for sub in ast.walk(expr):
            if isinstance(sub, ast.Name):
                spec = self.resolve(sub.id)
                if spec is not None:
                    specs.add(spec)
        return frozenset(specs)

    def unresolved_names(self, expr: ast.expr) -> set[str]:
        """Return names referenced in *expr* that resolve to no import or builtin.

        A captured default expression can only be rendered safely if every
        free name is reproducible in the generated file — i.e. it comes from an
        import (in this table) or is a Python builtin. A name bound by a
        module-level assignment or a relative import is *not* reproducible, so
        rendering ``default_factory=lambda: <expr>`` would raise ``NameError``.
        Callers use this to fall back to ``DERIVED`` instead.
        """
        referenced = {
            sub.id
            for sub in ast.walk(expr)
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)
        }
        return {
            name
            for name in referenced
            if self.resolve(name) is None and name not in _BUILTIN_NAMES
        }


def _leading_comment(source_lines: list[str], lineno: int) -> str:
    """Collect a contiguous block of ``# ...`` comments directly above *lineno*.

    Blank lines break the block. Returns joined comment text (newlines
    preserved) or an empty string. Skips ``# type:`` and lint pragmas.
    """
    out: list[str] = []
    idx = lineno - 2  # 0-indexed line directly above the assignment
    while idx >= 0:
        stripped = source_lines[idx].strip()
        if not stripped.startswith("#"):
            break
        text = stripped.lstrip("#").strip()
        # Skip (do not stop at) tooling pragmas so a real description on the
        # line above a noqa/type/ruff pragma is still collected.
        if text.startswith(("type:", "noqa", "ruff:", "pragma:")):
            idx -= 1
            continue
        # Skip decorative separators (e.g. `# ---- section ----`): a comment
        # with no alphanumeric content is a divider, not a description.
        if not any(ch.isalnum() for ch in text):
            idx -= 1
            continue
        # A `# ---- Section ----` banner is a grouping header, not this field's
        # description — stop before consuming it.
        if text.startswith(("----", "====", "####")) or text.endswith(
            ("----", "====", "####")
        ):
            break
        out.append(text)
        idx -= 1
    return "\n".join(reversed(out))


def _literal_type(value: object) -> TypeRef:
    """Return a :class:`TypeRef` for a Python literal value."""
    if isinstance(value, bool):
        return TypeRef("bool")
    if isinstance(value, int):
        return TypeRef("int")
    if isinstance(value, float):
        return TypeRef("float")
    if isinstance(value, str):
        return TypeRef("str")
    if value is None:
        return TypeRef("Any", needs_refinement=True)
    if isinstance(value, list):
        return TypeRef("list[Any]")
    if isinstance(value, dict):
        return TypeRef("dict[str, Any]")
    if isinstance(value, tuple):
        return TypeRef("tuple[Any, ...]", needs_refinement=True)
    if isinstance(value, set | frozenset):
        return TypeRef("set[Any]", needs_refinement=True)
    return TypeRef("Any", needs_refinement=True)


def _try_literal(node: ast.expr) -> tuple[bool, object]:
    """Return ``(True, value)`` if *node* is a pure literal, else ``(False, None)``."""
    try:
        return True, ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return False, None


def _iter_scoped_statements(stmt: ast.AST) -> Iterator[ast.AST]:
    """Yield statements nested in *stmt* without crossing a scope boundary.

    Descends into control-flow bodies (``if``/``for``/``while``/``try``/
    ``with``) but stops at any node that opens a new scope (``def``/``class``/
    ``lambda``), so a variable local to a nested function is never mistaken for
    a setting.
    """
    stack: list[ast.AST] = list(ast.iter_child_nodes(stmt))
    while stack:
        node = stack.pop()
        if isinstance(node, _SCOPE_NODES):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _assignment_pairs(
    targets: Sequence[ast.expr], value: ast.expr
) -> list[tuple[ast.Name, ast.expr]]:
    """Pair assignment target names with their value expressions.

    Handles both ``NAME = <expr>`` and literal tuple/list unpacking
    (``A, B = 1, 2``); the latter would otherwise be dropped silently. Targets
    that are not plain names (attribute/subscript/star) and unpackings whose
    shapes do not line up are skipped.
    """
    pairs: list[tuple[ast.Name, ast.expr]] = []
    for target in targets:
        if isinstance(target, ast.Name):
            pairs.append((target, value))
        elif isinstance(target, ast.Tuple | ast.List) and isinstance(
            value, ast.Tuple | ast.List
        ):
            if len(target.elts) == len(value.elts):
                for tgt_elt, val_elt in zip(target.elts, value.elts, strict=True):
                    if isinstance(tgt_elt, ast.Name):
                        pairs.append((tgt_elt, val_elt))
    return pairs


class StaticModuleInspector:
    """Discover settings by parsing a module's source with :mod:`ast`.

    Args:
        module_path: Dotted import path (used to locate the source file and to
            label provenance). The module is *not* imported.
        source_file: Optional explicit path to the source; when omitted it is
            resolved from *module_path* via the import machinery's finder
            without executing the module.
    """

    def __init__(self, module_path: str, source_file: str | Path | None = None) -> None:
        """Store the module path (and optional explicit source file)."""
        self._module_path = module_path
        self._source_file = Path(source_file) if source_file else None

    def _resolve_source(self) -> Path:
        if self._source_file is not None:
            return self._source_file
        import importlib.util  # noqa: PLC0415

        spec = importlib.util.find_spec(self._module_path)
        if spec is None or spec.origin is None:
            raise ImportError(
                f"django-aqueduct could not locate source for "
                f"'{self._module_path}'. Ensure it is on sys.path."
            )
        return Path(spec.origin)

    def discover(self) -> list[SettingField]:
        """Return one :class:`SettingField` per UPPERCASE module-level assignment."""
        path = self._resolve_source()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        source_lines = source.splitlines()

        imports = _ImportTable()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.add_import(node)
            elif isinstance(node, ast.ImportFrom):
                imports.add_importfrom(node)

        fields: dict[str, SettingField] = {}

        # Top-level (unconditional) assignments.
        for stmt in tree.body:
            self._collect_assignment(
                stmt, imports, source, source_lines, fields, conditional=False
            )

        # Conditional assignments (inside if/try/for/with bodies) — a single
        # snapshot would freeze one branch, so these render as DERIVED. Walk
        # only control-flow bodies, never descending into a nested def/class
        # (a local there is not a Django setting).
        for stmt in tree.body:
            if isinstance(stmt, ast.If | ast.Try | ast.For | ast.While | ast.With):
                for inner in _iter_scoped_statements(stmt):
                    self._collect_assignment(
                        inner,
                        imports,
                        source,
                        source_lines,
                        fields,
                        conditional=True,
                    )

        return [fields[name] for name in sorted(fields)]

    def _collect_assignment(
        self,
        stmt: ast.AST,
        imports: _ImportTable,
        source: str,
        source_lines: list[str],
        fields: dict[str, SettingField],
        *,
        conditional: bool,
    ) -> None:
        if not isinstance(stmt, ast.Assign | ast.AnnAssign):
            return
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
        value = stmt.value
        if value is None:
            return
        stmt_lineno = stmt.lineno
        for target, value_expr in _assignment_pairs(targets, value):
            if not target.id.isupper():
                continue
            name = target.id
            # An unconditional assignment wins over a conditional one for the
            # same name (the unconditional pass runs first; don't clobber it).
            if name in fields and conditional:
                continue
            fields[name] = self._build_field(
                name,
                value_expr,
                stmt_lineno,
                imports,
                source,
                source_lines,
                conditional=conditional,
            )

    def _build_field(
        self,
        name: str,
        value: ast.expr,
        stmt_lineno: int,
        imports: _ImportTable,
        source: str,
        source_lines: list[str],
        *,
        conditional: bool,
    ) -> SettingField:
        # Anchor comment extraction and provenance on the *statement* line, not
        # the value expression: for a parenthesized / multi-line assignment the
        # value starts on a later line, so value.lineno would miss the leading
        # comment block entirely.
        description = _leading_comment(source_lines, stmt_lineno)
        prov = Provenance(
            source_module=self._module_path,
            method=DiscoveryMethod.STATIC,
            lineno=stmt_lineno,
            conditional=conditional,
        )
        reader = self._reader_info(value)
        aliases = reader.aliases if reader else ()

        def field(
            type_ref: TypeRef, default: Default, *, required: bool = False
        ) -> SettingField:
            return SettingField(
                name=name,
                type=type_ref,
                default=default,
                env_aliases=aliases,
                required=required,
                description=description,
                provenance=prov,
            )

        # Redaction takes precedence: never emit an observed/secret value. A
        # required secret still renders REQUIRED (enforced, no value written);
        # otherwise it is optional-None.
        if looks_secret(name):
            type_ref = (
                reader.type_ref if reader else TypeRef("str", needs_refinement=True)
            )
            if reader and reader.required:
                return field(type_ref, Default.required(), required=True)
            return field(type_ref.with_optional(), Default.redacted())

        # A conditionally-assigned setting is DERIVED — reproduce the branch in
        # a validator rather than freeze one side of it.
        if conditional:
            type_ref = reader.type_ref if reader else self._infer_expr_type(value)
            return field(type_ref.with_optional(), Default.derived())

        # A recognised env reader: derive the default from the reader's own
        # default argument — never re-emit the reader *call* (which would read
        # os.environ directly or reference an un-imported parser object).
        if reader is not None:
            if reader.required:
                return field(reader.type_ref, Default.required(), required=True)
            if reader.default_node is None:
                # e.g. os.getenv("X") with no default → optional, None.
                return field(reader.type_ref.with_optional(), Default.literal_(None))
            default, optional = self._default_for(reader.default_node, imports, source)
            return field(reader.type_ref.with_optional(optional=optional), default)

        # Plain assignment: literal, reproducible expression, or DERIVED.
        default, optional = self._default_for(value, imports, source)
        if default.strategy is DefaultStrategy.DERIVED:
            type_ref = self._infer_expr_type(value).with_optional()
        else:
            type_ref = self._value_type(value).with_optional(optional=optional)
        return field(type_ref, default)

    def _default_for(
        self, node: ast.expr, imports: _ImportTable, source: str
    ) -> tuple[Default, bool]:
        """Return ``(Default, optional)`` for a value/default expression.

        Literals become ``LITERAL``/``FACTORY``; a reproducible expression
        (every free name resolves to an import or builtin) becomes ``EXPR``;
        anything referencing a module-local or relatively-imported name becomes
        ``DERIVED`` so the generated file cannot raise ``NameError``.
        """
        is_literal, literal_value = _try_literal(node)
        if is_literal:
            factory = isinstance(literal_value, list | dict | set)
            default = Default.literal_(literal_value, factory=factory)
            return default, literal_value is None
        if imports.unresolved_names(node):
            return Default.derived(), True
        expr_src = ast.get_source_segment(source, node) or ""
        return Default.expr_(expr_src, imports.imports_for(node)), False

    @staticmethod
    def _value_type(node: ast.expr) -> TypeRef:
        """Type for a non-reader plain assignment (literal or reproducible expr)."""
        is_literal, literal_value = _try_literal(node)
        if is_literal:
            return _literal_type(literal_value)
        return StaticModuleInspector._infer_expr_type(node)

    def _reader_info(self, value: ast.expr) -> _ReaderInfo | None:
        """Extract reader metadata from an env-reader expression, or ``None``.

        Recognises ``obj.get_string("VAR"[, default])`` / ``env.str("VAR")``
        typed readers, ``os.getenv("VAR"[, default])`` / ``os.environ.get(...)``,
        and ``os.environ["VAR"]``.
        """
        # os.environ["VAR"] → subscript; missing key raises KeyError → required.
        if isinstance(value, ast.Subscript):
            if _dotted(value.value) == "os.environ" and isinstance(
                value.slice, ast.Constant
            ):
                var = value.slice.value
                if isinstance(var, str):
                    return _ReaderInfo((var,), TypeRef("str"), required=True)
            return None

        if not isinstance(value, ast.Call):
            return None

        target = _call_target(value)
        if target is None:
            return None
        method = target.rsplit(".", 1)[-1]

        # os.getenv / os.environ.get return None when absent → never required.
        if target in ("os.getenv", "os.environ.get"):
            return _ReaderInfo(
                _reader_alias(value),
                TypeRef("str"),
                required=False,
                default_node=_reader_default_node(value),
            )

        # Typed readers. Builtin-shadowing names (str/int/bool/float) count
        # only as attribute calls (env.str(...)), never a bare int(...) cast.
        if method in _READER_TYPES:
            if method in _ATTRIBUTE_ONLY_READERS and not isinstance(
                value.func, ast.Attribute
            ):
                return None
            default_node = _reader_default_node(value)
            explicit_required = _reader_required_kwarg(value)
            required = (
                explicit_required
                if explicit_required is not None
                else default_node is None
            )
            return _ReaderInfo(
                _reader_alias(value),
                TypeRef(_READER_TYPES[method]),
                required=required,
                default_node=default_node,
            )

        return None

    @staticmethod
    def _infer_expr_type(value: ast.expr) -> TypeRef:
        """Best-effort type for a non-literal expression (EXPR default)."""
        if isinstance(value, ast.Call):
            target = _call_target(value)
            if target and target in _KNOWN_CALL_TYPES:
                return _KNOWN_CALL_TYPES[target]
        return TypeRef("Any", needs_refinement=True)
