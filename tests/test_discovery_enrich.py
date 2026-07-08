"""Tests for discovery.enrich — merging runtime/usage evidence into IR."""

from __future__ import annotations

from django_aqueduct.discovery.enrich import (
    apply_runtime_enrichment,
    apply_url_type_hints,
    apply_usage_enrichment,
    apply_usage_range_enrichment,
)
from django_aqueduct.discovery.ir import (
    Default,
    DefaultStrategy,
    Provenance,
    SettingField,
    TypeRef,
)
from django_aqueduct.discovery.usage import RangeEvidence


def _field(name, base="Any", *, default=None, needs_refinement=False) -> SettingField:
    return SettingField(
        name=name,
        type=TypeRef(base, needs_refinement=needs_refinement),
        default=default or Default.literal_(None),
        provenance=Provenance(source_module="m"),
    )


# ------------------------------------------------------------------ #
# apply_runtime_enrichment — dict shape                                 #
# ------------------------------------------------------------------ #


def test_runtime_dict_enrichment_refines_derived_field():
    """A DERIVED dict field (default was never a literal) gets genson enrichment."""
    f = _field(
        "DATABASES", base="Any", needs_refinement=True, default=Default.derived()
    )
    samples = [
        {"DATABASES": {"default": {"ENGINE": "postgres", "NAME": "db"}}},
        {"DATABASES": {"default": {"ENGINE": "postgres", "NAME": "db2"}}},
    ]
    fields = [f]
    enrichment = apply_runtime_enrichment(fields, samples)
    assert "DATABASES" in enrichment
    annotation, _defs = enrichment["DATABASES"]
    assert annotation == "dict[str, DatabasesEntry]"
    # dict-shape refinement goes through the enrichment overlay, not f.type directly
    assert fields[0] is f


def test_runtime_enrichment_no_samples_is_noop():
    f = _field("X")
    assert apply_runtime_enrichment([f], []) == {}
    assert f.type.base == "Any"


# ------------------------------------------------------------------ #
# apply_runtime_enrichment — scalar Literal promotion                   #
# ------------------------------------------------------------------ #


def test_runtime_scalar_literal_promotion():
    f = _field("ENVIRONMENT", base="str")
    samples = [
        {"ENVIRONMENT": "dev"},
        {"ENVIRONMENT": "staging"},
        {"ENVIRONMENT": "dev"},
    ]
    apply_runtime_enrichment([f], samples)
    assert f.type.base == 'Literal["dev", "staging"]'
    assert f.type.needs_refinement is True


def test_runtime_single_sample_does_not_promote_to_literal():
    f = _field("ENVIRONMENT", base="str")
    apply_runtime_enrichment([f], [{"ENVIRONMENT": "dev"}])
    assert f.type.base == "str"


def test_runtime_too_many_distinct_values_does_not_promote():
    f = _field("PORT", base="int")
    samples = [{"PORT": i} for i in range(20)]
    apply_runtime_enrichment([f], samples, literal_max_values=8)
    assert f.type.base == "int"


def test_runtime_single_distinct_value_across_samples_does_not_promote():
    f = _field("SITE_NAME", base="str")
    apply_runtime_enrichment([f], [{"SITE_NAME": "x"}, {"SITE_NAME": "x"}])
    assert f.type.base == "str"


# ------------------------------------------------------------------ #
# apply_runtime_enrichment — runtime-only names                         #
# ------------------------------------------------------------------ #


def test_runtime_only_name_appended_as_new_field():
    fields = [_field("KNOWN")]
    apply_runtime_enrichment(fields, [{"KNOWN": "x", "MYSTERY": "y"}])
    names = {f.name for f in fields}
    assert names == {"KNOWN", "MYSTERY"}
    mystery = next(f for f in fields if f.name == "MYSTERY")
    assert mystery.default.strategy is DefaultStrategy.RUNTIME_ONLY
    assert mystery.provenance.runtime_only is True
    # never carries the observed value
    assert mystery.default.literal is None


def test_runtime_only_name_gets_literal_type_when_evidence_supports_it():
    fields: list[SettingField] = []
    samples = [{"MYSTERY": "a"}, {"MYSTERY": "b"}]
    apply_runtime_enrichment(fields, samples)
    mystery = fields[0]
    assert mystery.type.base == 'Literal["a", "b"]'


# ------------------------------------------------------------------ #
# apply_runtime_enrichment — URL corroboration                          #
# ------------------------------------------------------------------ #


def test_runtime_url_shaped_strings_promote_to_anyurl():
    f = _field("SOME_ENDPOINT", base="str")
    samples = [
        {"SOME_ENDPOINT": "https://a.example.com"},
        {"SOME_ENDPOINT": "https://b.example.com"},
        {"SOME_ENDPOINT": "https://c.example.com"},
    ]
    # more distinct values than literal_max_values would allow as an enum
    apply_runtime_enrichment([f], samples, literal_max_values=1)
    assert f.type.base == "AnyUrl"
    assert f.type.needs_refinement is True


# ------------------------------------------------------------------ #
# apply_usage_enrichment                                                 #
# ------------------------------------------------------------------ #


def test_usage_enrichment_promotes_literal():
    f = _field("LOG_LEVEL", base="str")
    apply_usage_enrichment([f], {"LOG_LEVEL": {"DEBUG", "INFO", "WARNING"}})
    assert f.type.base == 'Literal["DEBUG", "INFO", "WARNING"]'


def test_usage_enrichment_ignores_unknown_field():
    apply_usage_enrichment([], {"GHOST": {"a", "b"}})  # must not raise


def test_usage_enrichment_never_clobbers_dict_type():
    f = _field("DATABASES", base="dict[str, DatabasesEntry]")
    apply_usage_enrichment([f], {"DATABASES": {"a", "b"}})
    assert f.type.base == "dict[str, DatabasesEntry]"


def test_usage_enrichment_never_clobbers_existing_literal():
    f = _field("ENVIRONMENT", base="Literal['dev', 'staging']")
    apply_usage_enrichment([f], {"ENVIRONMENT": {"prod", "test", "other"}})
    assert f.type.base == "Literal['dev', 'staging']"


def test_usage_enrichment_too_many_values_skipped():
    f = _field("X", base="str")
    apply_usage_enrichment([f], {"X": set("abcdefghij")}, literal_max_values=8)
    assert f.type.base == "str"


# ------------------------------------------------------------------ #
# apply_usage_range_enrichment                                          #
# ------------------------------------------------------------------ #


def test_usage_range_enrichment_sets_constraints():
    f = _field("TIMEOUT", base="int")
    apply_usage_range_enrichment([f], {"TIMEOUT": RangeEvidence(gt=0, le=3600)})
    assert f.constraints.gt == 0
    assert f.constraints.le == 3600


def test_usage_range_enrichment_skips_non_numeric_field():
    f = _field("ENVIRONMENT", base="str")
    apply_usage_range_enrichment([f], {"ENVIRONMENT": RangeEvidence(gt=0)})
    assert f.constraints.is_empty()


def test_usage_range_enrichment_ignores_unknown_field():
    apply_usage_range_enrichment([], {"GHOST": RangeEvidence(gt=0)})  # must not raise


# ------------------------------------------------------------------ #
# apply_url_type_hints — static, unconditional                          #
# ------------------------------------------------------------------ #


def test_url_hint_from_name():
    f = _field("API_BASE_URL", base="str")
    apply_url_type_hints([f])
    assert f.type.base == "AnyUrl"
    assert f.type.needs_refinement is True


def test_url_hint_from_default_value():
    f = _field(
        "SOME_ENDPOINT", base="str", default=Default.literal_("https://example.com")
    )
    apply_url_type_hints([f])
    assert f.type.base == "AnyUrl"


def test_url_hint_preserves_optional_flag():
    f = SettingField(
        name="OPTIONAL_URL",
        type=TypeRef("str", optional=True),
        default=Default.literal_(None),
        provenance=Provenance(source_module="m"),
    )
    apply_url_type_hints([f])
    assert f.type.base == "AnyUrl"
    assert f.type.optional is True


def test_url_hint_does_not_apply_to_non_str_fields():
    f = _field("SOME_URL", base="int")
    apply_url_type_hints([f])
    assert f.type.base == "int"


def test_url_hint_does_not_apply_to_already_needs_refinement_fields():
    f = _field("SOME_URL", base="str", needs_refinement=True)
    apply_url_type_hints([f])
    assert f.type.base == "str"


def test_url_hint_ignores_non_url_looking_plain_string():
    f = _field("SITE_NAME", base="str", default=Default.literal_("My App"))
    apply_url_type_hints([f])
    assert f.type.base == "str"


def test_url_hint_sqlite_style_uri_needs_name_hint():
    # sqlite:///db.sqlite3 has no netloc, so the value heuristic alone won't
    # catch it -- the _URL name suffix must carry it.
    f = _field(
        "DATABASE_URL", base="str", default=Default.literal_("sqlite:///db.sqlite3")
    )
    apply_url_type_hints([f])
    assert f.type.base == "AnyUrl"
