"""Static AST discovery for codegen v2.

The v1 :class:`~django_aqueduct.discovery.module.ModuleInspector` imports the
settings module and records *resolved runtime values*, which bakes
generation-machine state into defaults, freezes conditional branches, erases
required-ness, and loses env-var aliases (see the codegen v2 RFC).

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
from pathlib import Path

from django_aqueduct.discovery.ir import (
    Default,
    DiscoveryMethod,
    ImportSpec,
    Provenance,
    SettingField,
    TypeRef,
)
from django_aqueduct.discovery.module import _looks_secret

# Reader-call method name → (annotation base, imports). Used both for
# ``obj.method("VAR")`` attribute calls and bare ``method("VAR")`` calls.
_READER_TYPES: dict[str, tuple[str, frozenset[ImportSpec]]] = {
    "get_string": ("str", frozenset()),
    "get_bool": ("bool", frozenset()),
    "get_int": ("int", frozenset()),
    "get_float": ("float", frozenset()),
    "get_list_literal": ("list[Any]", frozenset()),
    "get_delimited_list": ("list[str]", frozenset()),
    "str": ("str", frozenset()),
    "bool": ("bool", frozenset()),
    "int": ("int", frozenset()),
    "float": ("float", frozenset()),
}

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


class _ImportTable:
    """Maps bound names in a module to the :class:`ImportSpec` that provides them."""

    def __init__(self) -> None:
        self._by_name: dict[str, ImportSpec] = {}

    def add_import(self, node: ast.Import) -> None:
        for alias in node.names:
            # `import a.b` binds `a` but the statement imports `a.b`; an
            # `as` alias binds that name instead.
            bound = alias.asname or alias.name.split(".")[0]
            self._by_name[bound] = ImportSpec(alias.name, None)

    def add_importfrom(self, node: ast.ImportFrom) -> None:
        if node.module is None or node.level:  # skip relative imports
            return
        for alias in node.names:
            bound = alias.asname or alias.name
            self._by_name[bound] = ImportSpec(node.module, alias.name)

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
        if text.startswith(("type:", "noqa", "ruff:", "pragma:")):
            break
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

        # Conditional assignments (inside if/try/for bodies) — a single
        # snapshot would freeze one branch, so these render as DERIVED.
        for stmt in tree.body:
            if isinstance(stmt, ast.If | ast.Try | ast.For | ast.With):
                for inner in ast.walk(stmt):
                    if inner is stmt:
                        continue
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
        for target in targets:
            if not isinstance(target, ast.Name) or not target.id.isupper():
                continue
            name = target.id
            # An unconditional assignment wins over a conditional one for the
            # same name (the unconditional pass runs first; don't clobber it).
            if name in fields and conditional:
                continue
            fields[name] = self._build_field(
                name, value, imports, source, source_lines, conditional=conditional
            )

    def _build_field(
        self,
        name: str,
        value: ast.expr,
        imports: _ImportTable,
        source: str,
        source_lines: list[str],
        *,
        conditional: bool,
    ) -> SettingField:
        lineno = getattr(value, "lineno", None)
        description = (
            _leading_comment(source_lines, lineno) if lineno is not None else ""
        )
        prov = Provenance(
            source_module=self._module_path,
            method=DiscoveryMethod.STATIC,
            lineno=lineno,
            conditional=conditional,
        )

        aliases, reader_type, required = self._reader_info(value)

        # Redaction takes precedence: never emit an observed/secret value.
        if _looks_secret(name):
            type_ref = reader_type or TypeRef("str", needs_refinement=True)
            return SettingField(
                name=name,
                type=type_ref.with_optional(),
                default=Default.redacted(),
                env_aliases=aliases,
                required=required,
                description=description,
                provenance=prov,
            )

        # A conditionally-assigned setting is DERIVED — reproduce the branch in
        # a validator rather than freeze one side of it.
        if conditional:
            type_ref = reader_type or self._infer_expr_type(value, imports)
            return SettingField(
                name=name,
                type=type_ref.with_optional(),
                default=Default.derived(),
                env_aliases=aliases,
                description=description,
                provenance=prov,
            )

        # A required env read with no default → REQUIRED (restores the
        # required-ness v1 erased).
        if required:
            type_ref = reader_type or TypeRef("str")
            return SettingField(
                name=name,
                type=type_ref,
                default=Default.required(),
                env_aliases=aliases,
                required=True,
                description=description,
                provenance=prov,
            )

        # Pure literal → LITERAL/FACTORY.
        is_literal, literal_value = _try_literal(value)
        if is_literal:
            type_ref = reader_type or _literal_type(literal_value)
            factory = isinstance(literal_value, list | dict | set)
            default = Default.literal_(literal_value, factory=factory)
            optional = literal_value is None
            return SettingField(
                name=name,
                type=type_ref.with_optional(optional=optional),
                default=default,
                env_aliases=aliases,
                description=description,
                provenance=prov,
            )

        # Anything else → capture the verbatim source expression (EXPR).
        expr_src = ast.get_source_segment(source, value) or ""
        expr_imports = imports.imports_for(value)
        type_ref = reader_type or self._infer_expr_type(value, imports)
        return SettingField(
            name=name,
            type=type_ref,
            default=Default.expr_(expr_src, expr_imports),
            env_aliases=aliases,
            description=description,
            provenance=prov,
        )

    def _reader_info(
        self, value: ast.expr
    ) -> tuple[tuple[str, ...], TypeRef | None, bool]:
        """Extract (env aliases, reader type, required) from an env-reader call.

        Recognises ``obj.get_string("VAR")`` / ``env.str("VAR")`` style calls,
        ``os.getenv("VAR"[, default])``, and ``os.environ["VAR"]`` /
        ``os.environ.get("VAR")``. Returns empty/None/False when *value* is not
        a recognised reader.
        """
        # os.environ["VAR"]  → subscript
        if isinstance(value, ast.Subscript):
            if _dotted(value.value) == "os.environ" and isinstance(
                value.slice, ast.Constant
            ):
                var = value.slice.value
                if isinstance(var, str):
                    return (var,), TypeRef("str"), True
            return (), None, False

        if not isinstance(value, ast.Call):
            return (), None, False

        target = _call_target(value)
        if target is None:
            return (), None, False
        method = target.rsplit(".", 1)[-1]

        # os.getenv / os.environ.get: first str arg is the alias; a second
        # positional arg (or `default=`) means not required.
        if target in ("os.getenv", "os.environ.get"):
            aliases = self._first_str_args(value)
            has_default = len(value.args) >= 2 or any(
                kw.arg == "default" for kw in value.keywords
            )
            return aliases, TypeRef("str"), not has_default

        # mitol / django-environ style typed readers.
        if method in _READER_TYPES:
            base, imps = _READER_TYPES[method]
            aliases = self._first_str_args(value)
            required = self._reader_required(value)
            return aliases, TypeRef(base, imps), required

        return (), None, False

    @staticmethod
    def _first_str_args(call: ast.Call) -> tuple[str, ...]:
        """Return the first string-literal positional arg (the var name) of *call*.

        Only the first positional is the env-var alias; any later string
        positional is the default value, not another alias.
        """
        if call.args and isinstance(call.args[0], ast.Constant):
            first = call.args[0].value
            if isinstance(first, str):
                return (first,)
        return ()

    @staticmethod
    def _reader_required(call: ast.Call) -> bool:
        """Return True when a typed reader call declares no default value.

        A reader is required when it has no ``default=``/second positional arg
        and is not explicitly ``required=False``.
        """
        for kw in call.keywords:
            if kw.arg == "required" and isinstance(kw.value, ast.Constant):
                return bool(kw.value.value)
            if kw.arg == "default":
                return False
        # positional default (arg after the var name)
        if len(call.args) >= 2:
            return False
        return True

    def _infer_expr_type(self, value: ast.expr, imports: _ImportTable) -> TypeRef:
        """Best-effort type for a non-literal expression (EXPR default)."""
        if isinstance(value, ast.Call):
            target = _call_target(value)
            if target and target in _KNOWN_CALL_TYPES:
                return _KNOWN_CALL_TYPES[target]
        return TypeRef("Any", needs_refinement=True)
