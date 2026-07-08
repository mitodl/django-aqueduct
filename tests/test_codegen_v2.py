"""End-to-end tests for codegen v2: static discovery → IR → renderer."""

from __future__ import annotations

import ast

import pytest

from django_aqueduct.codegen.renderer import ModelRenderer
from django_aqueduct.discovery.ir import (
    Default,
    DefaultStrategy,
    Provenance,
    SettingField,
    TypeRef,
)
from django_aqueduct.discovery.static import StaticModuleInspector

FIXTURE = "testapp.v2_fixture_settings"


@pytest.fixture
def fields():
    return StaticModuleInspector(FIXTURE).discover()


def _by_name(fields):
    return {f.name: f for f in fields}


def test_only_uppercase_discovered(fields):
    names = {f.name for f in fields}
    assert "SITE_NAME" in names
    assert all(n.isupper() for n in names)


def test_literals_are_literal_defaults(fields):
    f = _by_name(fields)["MAX_CONNECTIONS"]
    assert f.default.strategy is DefaultStrategy.LITERAL
    assert f.default.literal == 100
    assert f.type.render() == "int"


def test_mutable_literal_uses_factory(fields):
    f = _by_name(fields)["ALLOWED_HOSTS"]
    assert f.default.strategy is DefaultStrategy.FACTORY
    assert f.type.render() == "list[Any]"


def test_string_with_angle_bracket_survives(fields):
    # v1 replaced this with None via the `"<" in repr` heuristic.
    f = _by_name(fields)["XML_PREAMBLE"]
    assert f.default.strategy is DefaultStrategy.LITERAL
    assert f.default.literal == "<?xml version='1.0'?>"


def test_timedelta_is_expr_with_imports(fields):
    f = _by_name(fields)["SESSION_AGE"]
    assert f.default.strategy is DefaultStrategy.EXPR
    assert f.default.expr == "datetime.timedelta(days=14)"
    assert f.type.render() == "datetime.timedelta"
    assert any(i.module == "datetime" for i in f.default.expr_imports)


def test_decimal_and_path_expr(fields):
    by = _by_name(fields)
    assert by["DEFAULT_PRICE"].default.strategy is DefaultStrategy.EXPR
    assert by["DEFAULT_PRICE"].type.render() == "decimal.Decimal"
    assert by["DATA_DIR"].type.render() == "pathlib.Path"


def test_env_alias_and_required(fields):
    by = _by_name(fields)
    # secret-like name read via os.environ[...] → required AND enforced
    # (rendered Field(...), not a leaked/optional value).
    secret = by["SECRET_KEY"]
    assert secret.default.strategy is DefaultStrategy.REQUIRED
    assert secret.required is True
    assert secret.env_aliases == ("SECRET_KEY",)

    base_url = by["APP_BASE_URL"]
    assert base_url.default.strategy is DefaultStrategy.REQUIRED
    assert base_url.required is True
    assert base_url.env_aliases == ("APP_BASE_URL",)

    # os.getenv with a default → optional literal default, not required.
    log = by["LOG_LEVEL"]
    assert log.required is False
    assert log.default.strategy is DefaultStrategy.LITERAL
    assert log.default.literal == "INFO"
    assert log.env_aliases == ("LOG_LEVEL",)

    # os.getenv with NO default → optional None, never required (the v1 bug).
    extra = by["EXTRA_HOST"]
    assert extra.required is False
    assert extra.default.strategy is DefaultStrategy.LITERAL
    assert extra.default.literal is None
    assert extra.type.optional is True
    assert extra.env_aliases == ("EXTRA_HOST",)


def test_explicit_required_wins_over_default(fields):
    # get_string("X", default="fallback", required=True) → required despite the
    # default (the explicit required= flag wins).
    f = _by_name(fields)["REQUIRED_WITH_DEFAULT"]
    assert f.required is True
    assert f.default.strategy is DefaultStrategy.REQUIRED
    assert f.env_aliases == ("REQUIRED_WITH_DEFAULT",)


def test_leading_comment_uses_statement_line(tmp_path):
    # A parenthesized value starts on a later line than the assignment, so
    # anchoring on the statement line (not value.lineno) is required to find
    # the leading comment. Written to a temp file so the formatter can't
    # collapse the parentheses.
    src = 'import os  # noqa\n\n# the description\nWRAPPED = (\n    "v"\n)\n'
    path = tmp_path / "wrapped_settings.py"
    path.write_text(src, encoding="utf-8")
    fields = StaticModuleInspector("wrapped_settings", source_file=path).discover()
    wrapped = _by_name(fields)["WRAPPED"]
    assert wrapped.description == "the description"
    assert wrapped.default.literal == "v"


def test_description_survives_pragma_line(fields):
    # A standalone noqa pragma between the description and the assignment is
    # skipped, not treated as the end of the description block.
    f = _by_name(fields)["PRAGMA_ABOVE"]
    assert f.description == "Real description above a standalone pragma line."


def test_conditional_is_derived(fields):
    f = _by_name(fields)["CACHE_BACKEND"]
    assert f.default.strategy is DefaultStrategy.DERIVED
    assert f.provenance.conditional is True


def test_nested_function_local_not_discovered(fields):
    # A local inside a def nested in an `if` is not a setting.
    assert "NESTED_LOCAL" not in _by_name(fields)


def test_tuple_unpacking_discovered(fields):
    by = _by_name(fields)
    assert by["LANG_CODE"].default.literal == "en"
    assert by["TZ_NAME"].default.literal == "UTC"


def test_aliased_import_preserved_in_expr(fields):
    f = _by_name(fields)["ALT_PRICE"]
    assert f.default.strategy is DefaultStrategy.EXPR
    assert f.default.expr == 'Dec("1.50")'
    # the `as` alias must be carried on the emitted import
    assert any(i.asname == "Dec" for i in f.default.expr_imports), (
        f.default.expr_imports
    )


def test_builtin_cast_is_not_a_reader(fields):
    f = _by_name(fields)["POOL_SIZE"]
    assert f.env_aliases == ()
    assert f.required is False
    assert f.default.strategy is DefaultStrategy.EXPR


def test_module_local_reference_falls_back_to_derived(fields):
    # `DERIVED_URL = APP_BASE_URL + "/api"` references a module-local name that
    # cannot be reproduced in the generated file → DERIVED, not a NameError EXPR.
    f = _by_name(fields)["DERIVED_URL"]
    assert f.default.strategy is DefaultStrategy.DERIVED
    assert f.type.optional is True


def test_aliased_import_renders_and_instantiates(fields, tmp_path):
    import importlib.util
    import sys

    src = ModelRenderer(fields).render()
    assert "from decimal import Decimal as Dec" in src
    path = tmp_path / "gen_alias.py"
    path.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("gen_alias", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_alias"] = module
    spec.loader.exec_module(module)  # NameError here if the alias was lost


def test_fields_grouped_by_package_with_ordering():
    def fld(name: str, package: str) -> SettingField:
        return SettingField(
            name=name,
            type=TypeRef("str"),
            default=Default.literal_("x"),
            owning_package=package,
            provenance=Provenance(source_module="m"),
        )

    src = ModelRenderer(
        [
            fld("ZEBRA", "project"),
            fld("SECRET_HELPER", "celery"),
            fld("DEBUG", "django"),
        ]
    ).render()
    # django first, third-party middle, project last.
    d = src.index("# ===== django =====")
    c = src.index("# ===== celery =====")
    p = src.index("# ===== project =====")
    assert d < c < p


def test_single_module_has_no_group_header(fields):
    # No attribution + one source module → a clean unlabelled block.
    src = ModelRenderer(fields).render()
    assert "# =====" not in src


def test_extra_strictness_configurable():
    import ast as _ast

    for policy in ("allow", "ignore", "forbid"):
        src = ModelRenderer(
            [
                SettingField(
                    name="DEBUG",
                    type=TypeRef("bool"),
                    default=Default.literal_(False),
                    provenance=Provenance(source_module="m"),
                )
            ],
            extra=policy,
        ).render()
        _ast.parse(src)
        assert f'extra="{policy}"' in src


def test_invalid_extra_rejected():
    with pytest.raises(ValueError, match="extra must be"):
        ModelRenderer([], extra="bogus")


def test_dict_setting_enriched_to_typeddict():
    f = SettingField(
        name="DATABASES",
        type=TypeRef("dict[str, Any]"),
        default=Default.literal_(
            {"default": {"ENGINE": "x", "NAME": "db"}}, factory=True
        ),
        provenance=Provenance(source_module="m"),
    )
    src = ModelRenderer([f]).render()
    assert "class DatabasesEntry(TypedDict, total=False):" in src
    assert "DATABASES: Annotated[dict[str, DatabasesEntry], NoDecode]" in src
    assert "# >>> aqueduct:generated:typeddicts" in src
    ast.parse(src)


def test_no_typeddicts_when_no_enrichable_dicts(fields):
    # The fixture has no homogeneous-struct dicts → no typeddict region.
    src = ModelRenderer(fields).render()
    assert "aqueduct:generated:typeddicts" not in src


def test_render_parses(fields):
    src = ModelRenderer(fields).render()
    ast.parse(src)  # must be valid Python
    assert "AliasChoices" in src
    assert "# >>> aqueduct:generated:fields" in src
    assert "# >>> aqueduct:preserved:validators" in src


def test_render_is_deterministic(fields):
    a = ModelRenderer(fields).render()
    b = ModelRenderer(StaticModuleInspector(FIXTURE).discover()).render()
    assert a == b


def test_no_secret_value_in_output(fields, monkeypatch):
    # Even with a real value in the environment, static discovery never reads
    # it, so it can never appear in the generated file.
    monkeypatch.setenv("SECRET_KEY", "super-secret-live-value")
    src = ModelRenderer(StaticModuleInspector(FIXTURE).discover()).render()
    assert "super-secret-live-value" not in src


def test_generated_model_instantiates(fields, tmp_path):
    """Import the generated module as a real module and instantiate the model."""
    import importlib.util

    src = ModelRenderer(fields).render()
    path = tmp_path / "generated_settings.py"
    path.write_text(src, encoding="utf-8")

    import sys

    spec = importlib.util.spec_from_file_location("generated_settings", path)
    module = importlib.util.module_from_spec(spec)
    # Register so pydantic can resolve `from __future__` forward-ref annotations
    # against the module globals, exactly as a real import would.
    sys.modules["generated_settings"] = module
    spec.loader.exec_module(module)  # raises on NameError/SyntaxError

    inst = module.AqueductSettings(
        SECRET_KEY="x",
        APP_BASE_URL="https://example.test",
        REQUIRED_WITH_DEFAULT="v",
    )
    assert inst.APP_BASE_URL == "https://example.test"
    assert inst.SESSION_AGE.days == 14


def test_container_env_value_decodes_without_json(fields, tmp_path, monkeypatch):
    """A non-JSON env value for a list field must decode, not raise SettingsError.

    Reproduces the mit-learn failure: EnvParser (or a bare os.environ value)
    feeds ALLOWED_HOSTS as a comma-separated string, and pydantic-settings'
    default env source tries `json.loads` on any complex-typed field unless it
    carries `NoDecode` plus a `field_validator` that parses the raw string.
    """
    import importlib.util
    import sys

    src = ModelRenderer(fields).render()
    path = tmp_path / "generated_settings_env.py"
    path.write_text(src, encoding="utf-8")

    spec = importlib.util.spec_from_file_location("generated_settings_env", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["generated_settings_env"] = module
    spec.loader.exec_module(module)

    monkeypatch.setenv("ALLOWED_HOSTS", "example.com, *.example.com")
    comma_inst = module.AqueductSettings(
        SECRET_KEY="x",
        APP_BASE_URL="https://example.test",
        REQUIRED_WITH_DEFAULT="v",
    )
    assert comma_inst.ALLOWED_HOSTS == ["example.com", "*.example.com"]

    monkeypatch.setenv("ALLOWED_HOSTS", "['example.com', '*.example.com']")
    literal_inst = module.AqueductSettings(
        SECRET_KEY="x",
        APP_BASE_URL="https://example.test",
        REQUIRED_WITH_DEFAULT="v",
    )
    assert literal_inst.ALLOWED_HOSTS == ["example.com", "*.example.com"]

    # Genuine JSON — including tokens (true/false/null) that aren't valid
    # Python literals — must still decode correctly, not regress vs. the
    # json.loads pydantic-settings used before NoDecode was added.
    monkeypatch.setenv("ALLOWED_HOSTS", '["example.com", "*.example.com"]')
    json_inst = module.AqueductSettings(
        SECRET_KEY="x",
        APP_BASE_URL="https://example.test",
        REQUIRED_WITH_DEFAULT="v",
    )
    assert json_inst.ALLOWED_HOSTS == ["example.com", "*.example.com"]


def test_list_container_json_with_non_python_tokens_decodes(tmp_path, monkeypatch):
    """`[true, false]`/`[null]` are valid JSON but not valid Python literals.

    ast.literal_eval alone would fail these and mis-fire the comma-split
    fallback (producing garbage like '[true' / 'false]'); json.loads must be
    tried first so genuine JSON keeps decoding correctly.
    """
    import importlib.util
    import sys

    f = SettingField(
        name="FEATURE_FLAGS",
        type=TypeRef("list[Any]"),
        default=Default.literal_([], factory=True),
        provenance=Provenance(source_module="m"),
    )
    src = ModelRenderer([f]).render()
    path = tmp_path / "generated_settings_list_json.py"
    path.write_text(src, encoding="utf-8")

    spec = importlib.util.spec_from_file_location("generated_settings_list_json", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["generated_settings_list_json"] = module
    spec.loader.exec_module(module)

    monkeypatch.setenv("FEATURE_FLAGS", "[true, false, null]")
    inst = module.AqueductSettings()
    assert inst.FEATURE_FLAGS == [True, False, None]


def test_dict_container_env_value_decodes_without_json(tmp_path, monkeypatch):
    """A non-JSON env value for a dict field must decode, not raise SettingsError."""
    import importlib.util
    import sys

    f = SettingField(
        name="BEAT_SCHEDULE_EXTRA",
        type=TypeRef("dict[str, Any]"),
        default=Default.literal_({}, factory=True),
        provenance=Provenance(source_module="m"),
    )
    src = ModelRenderer([f]).render()
    path = tmp_path / "generated_settings_dict_env.py"
    path.write_text(src, encoding="utf-8")

    spec = importlib.util.spec_from_file_location("generated_settings_dict_env", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["generated_settings_dict_env"] = module
    spec.loader.exec_module(module)

    monkeypatch.setenv("BEAT_SCHEDULE_EXTRA", "{'minute': '*/5'}")
    inst = module.AqueductSettings()
    assert inst.BEAT_SCHEDULE_EXTRA == {"minute": "*/5"}

    # Genuine JSON with `null` (not a valid Python literal token) must still
    # decode correctly via json.loads, not regress vs. prior behavior.
    monkeypatch.setenv("BEAT_SCHEDULE_EXTRA", '{"minute": "*/5", "task": null}')
    json_inst = module.AqueductSettings()
    assert json_inst.BEAT_SCHEDULE_EXTRA == {"minute": "*/5", "task": None}


def test_golden_file_matches(fields):
    """Rendered output must match the committed golden file byte-for-byte.

    Regenerate with:
        uv run python -c "import sys; sys.path.insert(0,'testapp'); \
from django_aqueduct.discovery.static import StaticModuleInspector as S; \
from django_aqueduct.codegen.renderer import ModelRenderer as R; \
open('tests/golden/v2_fixture_model.py.golden','w').write(R(S('v2_fixture_settings').discover()).render())"
    """
    import pathlib

    golden = pathlib.Path(__file__).parent / "golden" / "v2_fixture_model.py.golden"
    assert ModelRenderer(fields).render() == golden.read_text(encoding="utf-8")
