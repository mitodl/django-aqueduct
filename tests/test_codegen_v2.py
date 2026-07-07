"""End-to-end tests for codegen v2: static discovery → IR → renderer."""

from __future__ import annotations

import ast

import pytest

from django_aqueduct.codegen.renderer import ModelRenderer
from django_aqueduct.discovery.ir import DefaultStrategy
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

    inst = module.AqueductSettings(SECRET_KEY="x", APP_BASE_URL="https://example.test")
    assert inst.APP_BASE_URL == "https://example.test"
    assert inst.SESSION_AGE.days == 14
