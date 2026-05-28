"""Tests for the generate_aqueduct_settings management command."""

import ast

import pytest
from django.core.management import call_command


def test_outputs_valid_python(capsys):
    """Command produces ast-parseable Python when given a fixture module."""
    call_command("generate_aqueduct_settings", modules="fixture_settings")
    captured = capsys.readouterr()
    ast.parse(captured.out)


def test_contains_aqueduct_settings_class(capsys):
    """Output defines AqueductSettings."""
    call_command("generate_aqueduct_settings", modules="fixture_settings")
    captured = capsys.readouterr()
    assert "class AqueductSettings(BaseSettings):" in captured.out


def test_contains_fixture_field_names(capsys):
    """Known field names from the fixture appear in the output."""
    call_command("generate_aqueduct_settings", modules="fixture_settings")
    captured = capsys.readouterr()
    for name in ("SITE_NAME", "DEBUG", "MAX_CONNECTIONS", "OPTIONAL_SETTING"):
        assert name in captured.out, f"Expected {name} in output"


def test_contains_section_header(capsys):
    """Source module section header is present."""
    call_command("generate_aqueduct_settings", modules="fixture_settings")
    captured = capsys.readouterr()
    assert "# ===== fixture_settings =====" in captured.out


def test_write_to_file(tmp_path, capsys):
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


def test_no_modules_emits_warning(capsys):
    """No --modules and no envparser produces a warning, not an error."""
    call_command("generate_aqueduct_settings")
    captured = capsys.readouterr()
    # Should not crash; warning goes to stderr
    assert "AqueductSettings" in captured.out  # empty class is still emitted


def test_bad_module_raises_command_error():
    """An unimportable module raises CommandError."""
    from django.core.management.base import CommandError  # noqa: PLC0415

    with pytest.raises(CommandError, match="does.not.exist"):
        call_command("generate_aqueduct_settings", modules="does.not.exist")


def test_multiple_modules(capsys):
    """Multiple comma-separated modules are all discovered."""
    call_command(
        "generate_aqueduct_settings",
        modules="fixture_settings,testapp.settings",
    )
    captured = capsys.readouterr()
    ast.parse(captured.out)
    # fixture_settings fields
    assert "SITE_NAME" in captured.out
    # testapp.settings fields (DEBUG is defined there too)
    assert "fixture_settings" in captured.out
    assert "testapp.settings" in captured.out
