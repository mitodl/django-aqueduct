"""Tests for the generate_aqueduct_settings management command."""

from __future__ import annotations

import ast
import json
import pathlib

import pytest
from django.core.management import call_command

_Capsys = pytest.CaptureFixture[str]


def test_outputs_valid_python(capsys: pytest.CaptureFixture[str]) -> None:
    """Command produces ast-parseable Python when given a fixture module."""
    call_command("generate_aqueduct_settings", modules="fixture_settings")
    captured = capsys.readouterr()
    ast.parse(captured.out)


def test_contains_aqueduct_settings_class(capsys: pytest.CaptureFixture[str]) -> None:
    """Output defines AqueductSettings."""
    call_command("generate_aqueduct_settings", modules="fixture_settings")
    captured = capsys.readouterr()
    assert "class AqueductSettings(BaseSettings):" in captured.out


def test_contains_fixture_field_names(capsys: pytest.CaptureFixture[str]) -> None:
    """Known field names from the fixture appear in the output."""
    call_command("generate_aqueduct_settings", modules="fixture_settings")
    captured = capsys.readouterr()
    for name in ("SITE_NAME", "DEBUG", "MAX_CONNECTIONS", "OPTIONAL_SETTING"):
        assert name in captured.out, f"Expected {name} in output"


def test_static_discovery_recovers_aliases_and_required(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Static discovery yields parseable managed output with aliases/required."""
    call_command("generate_aqueduct_settings", modules="v2_fixture_settings")
    out = capsys.readouterr().out
    ast.parse(out)
    assert "class AqueductSettings(BaseSettings):" in out
    assert "# >>> aqueduct:generated:fields" in out
    # Env alias + required-ness recovered from source (the old engine lost these).
    assert 'validation_alias=AliasChoices("APP_BASE_URL")' in out
    # URL-type promotion is opt-in (--enrich-url-types) -- not applied here.
    assert (
        'APP_BASE_URL: str = Field(..., validation_alias=AliasChoices("APP_BASE_URL"))'
        in out
    )


def test_enrich_url_types_promotes_and_serializes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--enrich-url-types promotes str-typed fields and adds a field_serializer."""
    call_command(
        "generate_aqueduct_settings",
        modules="v2_fixture_settings",
        enrich_url_types=True,
    )
    out = capsys.readouterr().out
    ast.parse(out)
    # No literal default to check -> name hint promotes it (not a known
    # Django relative-URL setting).
    assert (
        "APP_BASE_URL: AnyUrl = Field(\n"
        '        ..., validation_alias=AliasChoices("APP_BASE_URL")\n'
        "    )"
    ) in out
    assert '@field_serializer("APP_BASE_URL", when_used="always")' in out
    assert "def _aqueduct_serialize_url_fields(self, value: object) -> object:" in out


def test_single_module_has_no_group_header(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One source module with no attribution renders a clean unlabelled block."""
    call_command("generate_aqueduct_settings", modules="fixture_settings")
    assert "# =====" not in capsys.readouterr().out


def test_write_to_file(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--output writes to a file and prints a success message to stdout."""
    output_file = tmp_path / "settings_model.py"
    call_command(
        "generate_aqueduct_settings",
        modules="fixture_settings",
        output=str(output_file),
    )
    assert output_file.exists()
    content = output_file.read_text()
    ast.parse(content)
    assert "AqueductSettings" in content

    captured = capsys.readouterr()
    assert str(output_file) in captured.out


def test_no_modules_emits_warning(capsys: pytest.CaptureFixture[str]) -> None:
    """No --modules and no envparser produces a warning, not an error."""
    call_command("generate_aqueduct_settings")
    captured = capsys.readouterr()
    # Should not crash; warning goes to stderr
    assert "AqueductSettings" in captured.out  # empty class is still emitted


def test_bad_module_raises_command_error() -> None:
    """An unimportable module raises CommandError."""
    from django.core.management.base import CommandError  # noqa: PLC0415

    with pytest.raises(CommandError, match="does.not.exist"):
        call_command("generate_aqueduct_settings", modules="does.not.exist")


def test_new_value_kinds_produce_valid_python(capsys: _Capsys) -> None:
    """Fixture fields of OPAQUE/CALLABLE type produce ast-parseable output."""
    call_command("generate_aqueduct_settings", modules="fixture_settings")
    captured = capsys.readouterr()
    # These fields come from the new fixture entries
    assert "SECURE_PROXY_HEADER" in captured.out
    assert "EXCLUDED_FIELDS" in captured.out
    assert "DATA_DIR" in captured.out
    ast.parse(captured.out)


def test_path_default_renders_as_pathlib(capsys: _Capsys) -> None:
    """pathlib.Path defaults are emitted as pathlib.Path(...) to preserve / operator."""
    call_command("generate_aqueduct_settings", modules="fixture_settings")
    captured = capsys.readouterr()
    assert "import pathlib" in captured.out
    assert 'pathlib.Path("/var/data")' in captured.out


# ------------------------------------------------------------------ #
# JSON Schema format                                                   #
# ------------------------------------------------------------------ #


def test_jsonschema_format_outputs_valid_json(capsys: _Capsys) -> None:
    """--format jsonschema produces valid JSON to stdout."""
    call_command(
        "generate_aqueduct_settings",
        modules="fixture_settings",
        format="jsonschema",
    )
    captured = capsys.readouterr()
    data = json.loads(captured.out)  # raises on invalid JSON
    assert isinstance(data, dict)


def test_jsonschema_has_required_top_level_keys(capsys: _Capsys) -> None:
    """Generated JSON Schema has the expected structural keys."""
    call_command(
        "generate_aqueduct_settings",
        modules="fixture_settings",
        format="jsonschema",
    )
    captured = capsys.readouterr()
    schema = json.loads(captured.out)
    assert schema["$schema"] == "http://json-schema.org/draft-07/schema#"
    assert schema["type"] == "object"
    assert "properties" in schema


def test_jsonschema_contains_fixture_fields(capsys: _Capsys) -> None:
    """All fixture fields appear as properties in the JSON Schema."""
    call_command(
        "generate_aqueduct_settings",
        modules="fixture_settings",
        format="jsonschema",
    )
    captured = capsys.readouterr()
    schema = json.loads(captured.out)
    properties = schema["properties"]
    for name in ("DEBUG", "SITE_NAME", "MAX_CONNECTIONS", "ALLOWED_HOSTS"):
        assert name in properties, f"Expected {name} in JSON Schema properties"


def test_jsonschema_type_annotations(capsys: _Capsys) -> None:
    """Primitive fields have correct JSON Schema type annotations."""
    call_command(
        "generate_aqueduct_settings",
        modules="fixture_settings",
        format="jsonschema",
    )
    captured = capsys.readouterr()
    schema = json.loads(captured.out)
    props = schema["properties"]
    assert props["DEBUG"]["type"] == "boolean"
    assert props["SITE_NAME"]["type"] == "string"
    assert props["MAX_CONNECTIONS"]["type"] == "integer"
    assert props["ALLOWED_HOSTS"]["type"] == "array"


def test_jsonschema_write_to_file(tmp_path: pathlib.Path, capsys: _Capsys) -> None:
    """--format jsonschema with --output writes a valid JSON file."""
    output_file = tmp_path / "settings.schema.json"
    call_command(
        "generate_aqueduct_settings",
        modules="fixture_settings",
        format="jsonschema",
        output=str(output_file),
    )
    assert output_file.exists()
    data = json.loads(output_file.read_text())
    assert "$schema" in data

    captured = capsys.readouterr()
    assert str(output_file) in captured.out


def test_python_format_is_default(capsys: _Capsys) -> None:
    """Without --format the command defaults to Python output."""
    call_command("generate_aqueduct_settings", modules="fixture_settings")
    captured = capsys.readouterr()
    # Python output starts with the generated-file comment block, not JSON
    assert captured.out.startswith("# This file was generated")


def test_multiple_modules(capsys: _Capsys) -> None:
    """Multiple comma-separated modules are all discovered."""
    call_command(
        "generate_aqueduct_settings",
        modules="fixture_settings,testapp.settings",
    )
    captured = capsys.readouterr()
    ast.parse(captured.out)
    assert "SITE_NAME" in captured.out
    assert "fixture_settings" in captured.out
    assert "testapp.settings" in captured.out


# ------------------------------------------------------------------ #
# Lifecycle: managed-region merge, --check, --reset, --class-name     #
# ------------------------------------------------------------------ #


def test_class_name_option(capsys: _Capsys) -> None:
    """--class-name renames the generated BaseSettings subclass."""
    call_command(
        "generate_aqueduct_settings",
        modules="v2_fixture_settings",
        class_name="MyAppSettings",
    )
    assert "class MyAppSettings(BaseSettings):" in capsys.readouterr().out


def test_regeneration_preserves_hand_written_region(
    tmp_path: pathlib.Path, capsys: _Capsys
) -> None:
    """Re-running the command keeps code written in the preserved region."""
    out = tmp_path / "settings_model.py"
    call_command(
        "generate_aqueduct_settings", modules="v2_fixture_settings", output=str(out)
    )

    # Insert a hand-written validator into the preserved region.
    text = out.read_text()
    marker = "    # >>> aqueduct:preserved:validators\n"
    text = text.replace(marker, marker + "    HAND_WRITTEN = 42\n")
    out.write_text(text)

    # Regenerate — the hand-written line must survive the merge.
    call_command(
        "generate_aqueduct_settings", modules="v2_fixture_settings", output=str(out)
    )
    assert "HAND_WRITTEN = 42" in out.read_text()


def test_reset_discards_preserved_region(
    tmp_path: pathlib.Path, capsys: _Capsys
) -> None:
    """--reset overwrites the whole file, dropping hand-written preserved code."""
    out = tmp_path / "settings_model.py"
    call_command(
        "generate_aqueduct_settings", modules="v2_fixture_settings", output=str(out)
    )
    text = out.read_text().replace(
        "    # >>> aqueduct:preserved:validators\n",
        "    # >>> aqueduct:preserved:validators\n    HAND_WRITTEN = 42\n",
    )
    out.write_text(text)

    call_command(
        "generate_aqueduct_settings",
        modules="v2_fixture_settings",
        output=str(out),
        reset=True,
    )
    assert "HAND_WRITTEN = 42" not in out.read_text()


def test_check_passes_when_in_sync(tmp_path: pathlib.Path, capsys: _Capsys) -> None:
    """--check succeeds when the on-disk file matches a fresh render."""
    out = tmp_path / "settings_model.py"
    call_command(
        "generate_aqueduct_settings", modules="v2_fixture_settings", output=str(out)
    )
    call_command(
        "generate_aqueduct_settings",
        modules="v2_fixture_settings",
        output=str(out),
        check=True,
    )
    assert "up to date" in capsys.readouterr().out


def test_check_fails_on_drift(tmp_path: pathlib.Path) -> None:
    """--check exits non-zero when a generated region has drifted."""
    from django.core.management.base import CommandError

    out = tmp_path / "settings_model.py"
    call_command(
        "generate_aqueduct_settings", modules="v2_fixture_settings", output=str(out)
    )
    # Corrupt a generated field body.
    text = out.read_text().replace(
        "MAX_CONNECTIONS: int = Field(default=100)",
        "MAX_CONNECTIONS: int = Field(default=999)",
    )
    out.write_text(text)
    with pytest.raises(CommandError, match="out of date"):
        call_command(
            "generate_aqueduct_settings",
            modules="v2_fixture_settings",
            output=str(out),
            check=True,
        )


def test_check_stdout_is_rejected() -> None:
    """--check needs a real file, not stdout."""
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="requires --output"):
        call_command(
            "generate_aqueduct_settings", modules="v2_fixture_settings", check=True
        )


def test_pyproject_output_config_is_used(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[tool.aqueduct] output is honored when --output is omitted (regression)."""
    from django_aqueduct import config as config_mod

    out = tmp_path / "from_config.py"
    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda *a, **k: config_mod.AqueductConfig(output=str(out)),
    )
    call_command("generate_aqueduct_settings", modules="v2_fixture_settings")
    assert out.is_file()
    assert "class AqueductSettings(BaseSettings):" in out.read_text()


def test_extra_flag(capsys: _Capsys) -> None:
    """--extra sets the model_config extra policy."""
    call_command(
        "generate_aqueduct_settings", modules="v2_fixture_settings", extra="forbid"
    )
    assert 'extra="forbid"' in capsys.readouterr().out


def test_wrap_existing_flag(tmp_path: pathlib.Path, capsys: _Capsys) -> None:
    """--wrap-existing inserts markers in place and ignores other flags."""
    target = tmp_path / "hand_refined.py"
    target.write_text(
        "from pydantic_settings import BaseSettings, SettingsConfigDict\n"
        "from pydantic import Field\n\n\n"
        "class AqueductSettings(BaseSettings):\n"
        '    model_config = SettingsConfigDict(env_prefix="", extra="allow")\n\n'
        "    DEBUG: bool = Field(default=False)\n\n"
        "    @property\n"
        "    def is_prod(self) -> bool:\n"
        "        return not self.DEBUG\n"
    )
    call_command("generate_aqueduct_settings", wrap_existing=str(target))
    out = capsys.readouterr().out
    assert "written to" not in out  # not the normal _emit path

    wrapped = target.read_text()
    assert "# >>> aqueduct:generated:fields" in wrapped
    assert "# >>> aqueduct:preserved:validators" in wrapped
    assert "def is_prod" in wrapped
    ast.parse(wrapped)


def test_wrap_existing_missing_file() -> None:
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="does not exist"):
        call_command("generate_aqueduct_settings", wrap_existing="/no/such/file.py")


# ------------------------------------------------------------------ #
# --enrich-runtime / --enrich-usage                                     #
# ------------------------------------------------------------------ #


@pytest.fixture
def enrichable_module(tmp_path, monkeypatch):
    """A settings-like module whose values vary with the environment."""
    path = tmp_path / "enrichable_settings.py"
    path.write_text(
        "import os\n"
        "ENVIRONMENT = os.environ.get('APP_ENV', 'dev')\n"
        "DATABASES = {'default': {'ENGINE': 'postgres', "
        "'HOST': os.environ.get('DB_HOST', 'localhost')}}\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    import sys

    yield "enrichable_settings"
    sys.modules.pop("enrichable_settings", None)


def test_enrich_runtime_requires_modules() -> None:
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="requires --modules"):
        call_command("generate_aqueduct_settings", enrich_runtime=True)


def test_enrich_runtime_promotes_literal_from_env_files(
    tmp_path, enrichable_module, capsys: _Capsys
) -> None:
    env_a = tmp_path / "a.env"
    env_a.write_text("APP_ENV=staging\n")
    env_b = tmp_path / "b.env"
    env_b.write_text("APP_ENV=production\n")

    call_command(
        "generate_aqueduct_settings",
        modules=enrichable_module,
        enrich_runtime=True,
        runtime_env_file=[str(env_a), str(env_b)],
    )
    out = capsys.readouterr().out
    ast.parse(out)
    assert 'ENVIRONMENT: Literal["production", "staging"]' in out


def test_enrich_runtime_refines_dict_shape(
    tmp_path, enrichable_module, capsys: _Capsys
) -> None:
    env_a = tmp_path / "a.env"
    env_a.write_text("DB_HOST=db-a\n")
    env_b = tmp_path / "b.env"
    env_b.write_text("DB_HOST=db-b\n")

    call_command(
        "generate_aqueduct_settings",
        modules=enrichable_module,
        enrich_runtime=True,
        runtime_env_file=[str(env_a), str(env_b)],
    )
    out = capsys.readouterr().out
    ast.parse(out)
    assert "DATABASES: Annotated[dict[str, DatabasesEntry], NoDecode]" in out
    assert "class DatabasesEntry(TypedDict, total=False):" in out


def test_enrich_runtime_bad_env_file_raises(enrichable_module) -> None:
    from django.core.management.base import CommandError

    with pytest.raises(CommandError, match="cannot read"):
        call_command(
            "generate_aqueduct_settings",
            modules=enrichable_module,
            enrich_runtime=True,
            runtime_env_file=["/no/such/file.env"],
        )


def test_enrich_runtime_without_env_files_samples_current_process_env(
    enrichable_module, capsys: _Capsys
) -> None:
    """No --runtime-env-file: samples once under the current env, doesn't crash."""
    call_command(
        "generate_aqueduct_settings", modules=enrichable_module, enrich_runtime=True
    )
    out = capsys.readouterr().out
    ast.parse(out)
    assert "class AqueductSettings(BaseSettings):" in out


def test_enrich_usage_promotes_literal_and_range(tmp_path, capsys: _Capsys) -> None:
    usage_dir = tmp_path / "app"
    usage_dir.mkdir()
    (usage_dir / "views.py").write_text(
        "from django.conf import settings\n"
        "if settings.LOG_LEVEL == 'DEBUG':\n    pass\n"
        "elif settings.LOG_LEVEL == 'INFO':\n    pass\n"
        "if not (0 < settings.MAX_CONNECTIONS <= 1000):\n    raise ValueError\n"
    )
    call_command(
        "generate_aqueduct_settings",
        modules="v2_fixture_settings",
        enrich_usage=[str(usage_dir)],
    )
    out = capsys.readouterr().out
    ast.parse(out)
    assert 'LOG_LEVEL: Literal["DEBUG", "INFO"]' in out
    assert "MAX_CONNECTIONS: int = Field(\n        default=100, gt=0, le=1000" in out
    assert "usage-mined bound(s)" in out


def test_literal_max_values_flag_lowers_threshold(tmp_path, capsys: _Capsys) -> None:
    usage_dir = tmp_path / "app"
    usage_dir.mkdir()
    (usage_dir / "views.py").write_text(
        "from django.conf import settings\n"
        "if settings.LOG_LEVEL == 'DEBUG':\n    pass\n"
        "elif settings.LOG_LEVEL == 'INFO':\n    pass\n"
    )
    call_command(
        "generate_aqueduct_settings",
        modules="v2_fixture_settings",
        enrich_usage=[str(usage_dir)],
        literal_max_values=1,
    )
    out = capsys.readouterr().out
    assert "LOG_LEVEL: Literal[" not in out
    assert 'LOG_LEVEL: str = Field(\n        default="INFO"' in out
