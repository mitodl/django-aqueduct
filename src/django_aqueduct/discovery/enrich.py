"""Merge runtime-sampled and usage-mined evidence into static IR.

This is the one place the safety boundary for ``--enrich-runtime`` /
``--enrich-usage`` is enforced: both
:mod:`~django_aqueduct.discovery.runtime` (multi-snapshot live import) and
:mod:`~django_aqueduct.discovery.usage` (static whole-repo comparison
mining) may **only** refine a field's :class:`~django_aqueduct.discovery.ir.TypeRef`
or :class:`~django_aqueduct.discovery.ir.Constraints` — never its
:class:`~django_aqueduct.discovery.ir.Default`, required-ness, or env
aliases. Authoring those from an observed runtime value (or a usage-site
guess) is exactly the failure class v2's static-only discovery was built to
eliminate: a live/secret/environment-specific value baked into the generated
file as if it were the real default.

Refinements produced:

* **Dict shape** — an observed dict value (or several, across runtime
  samples) is run through :mod:`~django_aqueduct.codegen.dict_schema`'s
  genson enrichment, same as the renderer already does for literal dict
  defaults, but now reachable for fields whose default was never a literal
  (``DATABASES = dj_database_url.parse(...)``).
* **Closed-set scalar (``Literal``)** — a scalar field observed to take only
  a small, stable set of distinct values (across >=2 runtime samples, or
  compared for equality/membership against a small set of literals at usage
  sites) is promoted to ``Literal[...]``, flagged ``needs_refinement`` so a
  human confirms the set is actually exhaustive before trusting it in
  production.
* **URL-shaped string (``UrlStr``)** — a ``str`` field whose static default
  actually validates as an absolute URL, or (absent a concrete default to
  check) whose name looks like a URL (``*_URL``/``*_URI``, minus a denylist of
  Django settings that are conventionally relative), is promoted to
  :data:`~django_aqueduct.validation.UrlStr` — a ``str`` carrying an
  ``AfterValidator``, *not* ``pydantic.AnyUrl``, so the runtime type and the
  exact value are unchanged. Flagged ``needs_refinement`` (a human should
  confirm whether a stricter scheme check is wanted). Static and opt-in via
  ``--enrich-url-types`` — see :func:`apply_url_type_hints`.
* **Numeric range (``Constraints``)** — usage-site range comparisons
  (``if not (0 < TIMEOUT <= 3600): raise ...``) become ``Field(gt=, ge=,
  lt=, le=)`` bounds, rendered with an explicit review comment (see
  :mod:`~django_aqueduct.codegen.renderer`) since, like ``Literal``, a bound
  only reflects what the scanned code happens to check.

A name found at runtime with no matching static field at all becomes a new
``RUNTIME_ONLY`` field (``default=None``, flagged for review) — usage mining
never does this: a comparison site only proves someone compared against
*something* named that, not that a real assignment exists, and the "settings"
variable-name heuristic it uses has a real false-positive rate.
"""

from __future__ import annotations

from typing import Any

from pydantic import AnyUrl, TypeAdapter, ValidationError

from django_aqueduct.discovery.ir import (
    Constraints,
    Default,
    DefaultStrategy,
    DiscoveryMethod,
    ImportSpec,
    Provenance,
    SettingField,
    TypeRef,
    render_str_literal,
)
from django_aqueduct.discovery.usage import RangeEvidence

DEFAULT_LITERAL_MAX_VALUES = 8
DEFAULT_LITERAL_MIN_SAMPLES = 2

_ScalarSet = set[str | int | float | bool | None]

_URL_STR_IMPORTS = frozenset({ImportSpec("django_aqueduct", "UrlStr")})

# Sentinel for "this field's observed values all look like URLs"; the concrete
# TypeRef is built per-field by `_as_url_type` so the field's own `optional`
# flag survives promotion.
_URL_STR_TYPE_REF = TypeRef(
    base="UrlStr",
    imports=_URL_STR_IMPORTS,
    needs_refinement=True,
)

_ANY_URL_ADAPTER: TypeAdapter[AnyUrl] = TypeAdapter(AnyUrl)

# Django settings that are named `*_URL`/`*_URI` by convention but hold a
# relative path or URL-resolver name, never an absolute URL — promoting
# these to `AnyUrl` from the name alone is exactly the 0.8.0 regression
# (STATIC_URL='/static/', LOGIN_URL='login' both fail `AnyUrl` validation
# and crash model instantiation). Only consulted when there's no concrete
# default to validate against instead (see `apply_url_type_hints`).
_DJANGO_RELATIVE_URL_NAMES = frozenset(
    {
        "STATIC_URL",
        "MEDIA_URL",
        "LOGIN_URL",
        "LOGIN_REDIRECT_URL",
        "LOGOUT_REDIRECT_URL",
        "ACCOUNT_LOGIN_REDIRECT_URL",
        "ACCOUNT_LOGOUT_REDIRECT_URL",
        "ACCOUNT_SIGNUP_REDIRECT_URL",
        "FORCE_SCRIPT_NAME",
    }
)


def _render_scalar(v: str | int | float | bool | None) -> str:
    return render_str_literal(v) if isinstance(v, str) else repr(v)


def _literal_type_ref(values: _ScalarSet) -> TypeRef:
    """Build a deterministically-ordered ``needs_refinement`` ``Literal[...]``."""
    ordered = sorted(values, key=lambda v: (type(v).__name__, repr(v)))
    body = ", ".join(_render_scalar(v) for v in ordered)
    return TypeRef(
        base=f"Literal[{body}]",
        imports=frozenset({ImportSpec("typing", "Literal")}),
        needs_refinement=True,
    )


def _looks_like_url_name(name: str) -> bool:
    if name in _DJANGO_RELATIVE_URL_NAMES:
        return False
    return name in ("URL", "URI") or name.endswith(("_URL", "_URI"))


def _looks_like_url_value(value: object) -> bool:
    """Return True only if *value* actually validates as a ``pydantic.AnyUrl``.

    Ground truth, not a lookalike heuristic — a relative path, a URL-resolver
    name, or an empty string (``STATIC_URL='/static/'``, ``LOGIN_URL='login'``,
    ``''``) all fail here, which is what stops those from ever being promoted.
    """
    if not isinstance(value, str):
        return False
    try:
        _ANY_URL_ADAPTER.validate_python(value)
    except ValidationError:
        return False
    return True


def _as_url_type(field_type: TypeRef) -> TypeRef:
    """Promote to :data:`~django_aqueduct.validation.UrlStr`, not ``AnyUrl``.

    ``UrlStr`` validates the same shape but stays a ``str`` at runtime. See
    :mod:`django_aqueduct.validation` for why the distinction matters.
    """
    return TypeRef(
        base="UrlStr",
        imports=_URL_STR_IMPORTS,
        optional=field_type.optional,
        needs_refinement=True,
    )


def apply_url_type_hints(fields: list[SettingField]) -> None:
    """Refine plain ``str`` fields to ``UrlStr`` from name/default-value alone.

    Static (only inspects a field's own name and its already
    statically-discovered literal default) but opt-in via
    ``--enrich-url-types`` — unlike the earlier unconditional behavior, a
    field with a concrete literal default is promoted *only* if that value
    actually validates as an absolute URL; the name-suffix heuristic
    (minus :data:`_DJANGO_RELATIVE_URL_NAMES`) is a fallback used only when
    there's no literal value to check (``REQUIRED``/``DERIVED``/``EXPR``
    defaults, or a literal ``None``). This is what stops Django's relative
    ``*_URL`` settings (``STATIC_URL='/static/'``, ``LOGIN_URL='login'``,
    an empty ``API_BASE_URL``) from being promoted into a type their own
    default can't satisfy.

    The promoted type is :data:`~django_aqueduct.validation.UrlStr`, which is
    a ``str``. That is what makes the flag usable at all: the value keeps
    working in ``urlparse``/``urljoin``/``.strip()`` inside a hand-written
    validator, survives being nested in a ``dict``-valued setting, and is not
    rewritten (``AnyUrl`` appends a trailing slash to a bare host). Those three
    paths, none of which a ``field_serializer`` reaches, are why 4 of 5 apps
    left ``--enrich-url-types`` off through 0.9.0-0.12.0.
    """
    for f in fields:
        if f.type.base != "str" or f.type.needs_refinement:
            continue
        has_literal_value = (
            f.default.strategy is DefaultStrategy.LITERAL
            and f.default.literal is not None
        )
        if has_literal_value:
            if _looks_like_url_value(f.default.literal):
                f.type = _as_url_type(f.type)
        elif _looks_like_url_name(f.name):
            f.type = _as_url_type(f.type)


def apply_runtime_enrichment(
    fields: list[SettingField],
    samples: list[dict[str, Any]],
    *,
    literal_max_values: int = DEFAULT_LITERAL_MAX_VALUES,
    literal_min_samples: int = DEFAULT_LITERAL_MIN_SAMPLES,
) -> dict[str, tuple[str, list[Any]]]:
    """Merge runtime *samples* into *fields* (mutated in place).

    Args:
        fields: Statically discovered fields. Mutated in place: an existing
            field's ``type`` may be refined to a ``Literal[...]`` or
            ``AnyUrl``; a name observed at runtime with no matching field
            gets a new ``RUNTIME_ONLY`` field appended.
        samples: One ``{NAME: value}`` dict per env snapshot, from
            :func:`~django_aqueduct.discovery.runtime.sample_module_values`.
        literal_max_values: A scalar field's distinct observed values must
            number at most this many (and at least 2) to be promoted to
            ``Literal[...]`` — above this it's more likely an open-ended
            value than a closed enum.
        literal_min_samples: Minimum number of samples a field must appear
            in before its value set is trusted for ``Literal`` promotion. A
            single snapshot is just "the observed default", not evidence of
            a closed set.

    Returns:
        A ``{field_name: (annotation, typeddict_defs)}`` overlay to pass as
        ``ModelRenderer(..., dict_enrichment=...)`` for dict-shaped fields.
    """
    from django_aqueduct.codegen.dict_schema import (  # noqa: PLC0415
        enrich_dict_annotation_multi,
    )

    if not samples:
        return {}

    by_name = {f.name: f for f in fields}
    dict_enrichment: dict[str, tuple[str, list[Any]]] = {}

    all_names: set[str] = set()
    for sample in samples:
        all_names.update(sample)

    for name in sorted(all_names):
        observed = [sample[name] for sample in samples if name in sample]
        if not observed:
            continue

        dict_result: tuple[str, list[Any]] | None = None
        if all(isinstance(v, dict) for v in observed):
            annotation, typeddict_defs = enrich_dict_annotation_multi(name, observed)
            if annotation != "dict[str, Any]":
                dict_result = (annotation, typeddict_defs)

        refined_type: TypeRef | None = None
        if (
            dict_result is None
            and len(observed) >= literal_min_samples
            and all(
                v is None or isinstance(v, str | int | float | bool) for v in observed
            )
        ):
            distinct: _ScalarSet = set(observed)
            if 1 < len(distinct) <= literal_max_values:
                refined_type = _literal_type_ref(distinct)
            elif all(isinstance(v, str) and _looks_like_url_value(v) for v in observed):
                refined_type = _URL_STR_TYPE_REF

        field = by_name.get(name)
        if field is not None:
            if dict_result is not None:
                dict_enrichment[name] = dict_result
            elif refined_type is not None:
                field.type = (
                    _as_url_type(field.type)
                    if refined_type is _URL_STR_TYPE_REF
                    else refined_type
                )
            continue

        # Runtime-only: dir(module) found a name static discovery never did.
        new_type = refined_type
        if new_type is None and dict_result is not None:
            new_type = TypeRef("dict[str, Any]")
            dict_enrichment[name] = dict_result
        if new_type is None:
            new_type = TypeRef("Any", needs_refinement=True)
        fields.append(
            SettingField(
                name=name,
                type=new_type,
                default=Default.runtime_only(),
                provenance=Provenance(
                    source_module="<runtime>",
                    method=DiscoveryMethod.RUNTIME,
                    runtime_only=True,
                ),
            )
        )

    return dict_enrichment


def apply_usage_enrichment(
    fields: list[SettingField],
    candidates: dict[str, set[Any]],
    *,
    literal_max_values: int = DEFAULT_LITERAL_MAX_VALUES,
) -> None:
    """Merge usage-mined literal *candidates* into *fields* (mutated in place).

    Only refines a field that already exists — a comparison site is
    evidence someone compared against a name, not proof a real setting
    assignment exists, so unlike :func:`apply_runtime_enrichment` this never
    authors a new field. Skips a field whose type is already a ``dict[``
    (don't clobber TypedDict enrichment) or already a ``Literal[``/``AnyUrl``
    (first refinement wins).

    Args:
        fields: Statically (and optionally runtime-) discovered fields.
        candidates: ``{name: {observed_value, ...}}`` from
            :func:`~django_aqueduct.discovery.usage.find_usage_candidates`.
        literal_max_values: Same meaning as in :func:`apply_runtime_enrichment`.
    """
    by_name = {f.name: f for f in fields}
    for name, values in candidates.items():
        field = by_name.get(name)
        if field is None:
            continue
        if (
            field.type.base.startswith(("dict[", "Literal["))
            or field.type.base == "UrlStr"
        ):
            continue
        scalars: _ScalarSet = {
            v for v in values if v is None or isinstance(v, str | int | float | bool)
        }
        if 1 < len(scalars) <= literal_max_values:
            field.type = _literal_type_ref(scalars)


def apply_usage_range_enrichment(
    fields: list[SettingField],
    ranges: dict[str, RangeEvidence],
) -> None:
    """Merge usage-mined numeric bounds into ``fields[*].constraints`` (in place).

    Only refines a field that already exists, for the same reason
    :func:`apply_usage_enrichment` does. A field whose type isn't a plain
    ``int``/``float`` (e.g. it was promoted to ``Literal[...]``, or is a
    container) is skipped — a numeric bound on a non-numeric field is
    meaningless.

    Args:
        fields: Statically (and optionally runtime-) discovered fields.
        ranges: ``{name: RangeEvidence}`` from
            :func:`~django_aqueduct.discovery.usage.find_usage_candidates`.
    """
    by_name = {f.name: f for f in fields}
    for name, evidence in ranges.items():
        field = by_name.get(name)
        if field is None or field.type.base not in ("int", "float"):
            continue
        field.constraints = Constraints(
            gt=evidence.gt, ge=evidence.ge, lt=evidence.lt, le=evidence.le
        )
