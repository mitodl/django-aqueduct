"""Tests for EnvParserInspector."""

import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


def _make_env_var(
    name: str,
    value: Any,
    default: Any = None,
    description: str = "",
    required: bool = False,
    dev_only: bool = False,
) -> Any:
    """Build a minimal EnvVariable-like namedtuple for testing."""
    from collections import namedtuple  # noqa: PLC0415

    EnvVariable = namedtuple(  # noqa: PYI024
        "EnvVariable",
        [
            "name",
            "default",
            "description",
            "required",
            "dev_only",
            "value",
            "write_app_json",
        ],
    )
    return EnvVariable(
        name=name,
        default=default,
        description=description,
        required=required,
        dev_only=dev_only,
        value=value,
        write_app_json=True,
    )


@pytest.fixture()
def mock_env():
    """Patch mitol.common.envs so EnvParserInspector can be tested without the extra."""
    mock_env_obj = MagicMock()
    mock_env_obj._configured_vars = {
        "STR_SETTING": _make_env_var("STR_SETTING", "hello", default="hello"),
        "BOOL_SETTING": _make_env_var("BOOL_SETTING", True, default=False),
        "INT_SETTING": _make_env_var("INT_SETTING", 42, default=0),
        "LIST_SETTING": _make_env_var("LIST_SETTING", ["a", "b"], default=[]),
        "DICT_SETTING": _make_env_var("DICT_SETTING", {"k": "v"}, default={}),
        "REQUIRED_SETTING": _make_env_var(
            "REQUIRED_SETTING", "x", required=True, description="Must be set"
        ),
        "DEV_ONLY_SETTING": _make_env_var("DEV_ONLY_SETTING", "dev", dev_only=True),
        "NONE_DEFAULT": _make_env_var("NONE_DEFAULT", None, default=None),
    }

    mock_module = MagicMock()
    mock_module.env = mock_env_obj

    with patch.dict(
        sys.modules,
        {
            "mitol": MagicMock(),
            "mitol.common": MagicMock(),
            "mitol.common.envs": mock_module,
        },
    ):
        yield mock_env_obj


def test_discovers_all_vars(mock_env):
    """EnvParserInspector returns one field per registered variable."""
    from django_aqueduct.discovery.envparser import EnvParserInspector  # noqa: PLC0415

    fields = EnvParserInspector().discover()
    names = {f.name for f in fields}
    assert names == set(mock_env._configured_vars.keys())


def test_str_mapping(mock_env):
    """str values map to 'str' annotation."""
    from django_aqueduct.discovery.envparser import EnvParserInspector  # noqa: PLC0415

    fields = {f.name: f for f in EnvParserInspector().discover()}
    assert fields["STR_SETTING"].type_annotation == "str"
    assert fields["STR_SETTING"].needs_refinement is False


def test_bool_mapping(mock_env):
    """bool values map to 'bool' annotation."""
    from django_aqueduct.discovery.envparser import EnvParserInspector  # noqa: PLC0415

    fields = {f.name: f for f in EnvParserInspector().discover()}
    assert fields["BOOL_SETTING"].type_annotation == "bool"


def test_int_mapping(mock_env):
    """int values map to 'int' annotation."""
    from django_aqueduct.discovery.envparser import EnvParserInspector  # noqa: PLC0415

    fields = {f.name: f for f in EnvParserInspector().discover()}
    assert fields["INT_SETTING"].type_annotation == "int"


def test_list_mapping(mock_env):
    """list values map to 'list[Any]' annotation."""
    from django_aqueduct.discovery.envparser import EnvParserInspector  # noqa: PLC0415

    fields = {f.name: f for f in EnvParserInspector().discover()}
    assert fields["LIST_SETTING"].type_annotation == "list[Any]"


def test_dict_mapping(mock_env):
    """dict values map to 'dict[str, Any]' annotation."""
    from django_aqueduct.discovery.envparser import EnvParserInspector  # noqa: PLC0415

    fields = {f.name: f for f in EnvParserInspector().discover()}
    assert fields["DICT_SETTING"].type_annotation == "dict[str, Any]"


def test_required_flag_propagated(mock_env):
    """required=True is propagated from EnvVariable."""
    from django_aqueduct.discovery.envparser import EnvParserInspector  # noqa: PLC0415

    fields = {f.name: f for f in EnvParserInspector().discover()}
    assert fields["REQUIRED_SETTING"].required is True
    assert fields["REQUIRED_SETTING"].description == "Must be set"


def test_dev_only_flag_propagated(mock_env):
    """dev_only=True is propagated from EnvVariable."""
    from django_aqueduct.discovery.envparser import EnvParserInspector  # noqa: PLC0415

    fields = {f.name: f for f in EnvParserInspector().discover()}
    assert fields["DEV_ONLY_SETTING"].dev_only is True


def test_none_value_needs_refinement(mock_env):
    """None value produces needs_refinement=True."""
    from django_aqueduct.discovery.envparser import EnvParserInspector  # noqa: PLC0415

    fields = {f.name: f for f in EnvParserInspector().discover()}
    assert fields["NONE_DEFAULT"].needs_refinement is True


def test_sorted_output(mock_env):
    """Fields are returned in sorted order."""
    from django_aqueduct.discovery.envparser import EnvParserInspector  # noqa: PLC0415

    fields = EnvParserInspector().discover()
    names = [f.name for f in fields]
    assert names == sorted(names)


def test_import_error_without_mitol():
    """ImportError with install hint fires when mitol.common.envs is unavailable."""
    from django_aqueduct.discovery.envparser import _load_env_parser  # noqa: PLC0415

    with patch.dict(sys.modules, {"mitol.common.envs": None}):  # type: ignore[dict-item]
        with pytest.raises(ImportError, match="mitol-django-common"):
            _load_env_parser()


def test_custom_source_module(mock_env):
    """source_module override is applied to all fields."""
    from django_aqueduct.discovery.envparser import EnvParserInspector  # noqa: PLC0415

    fields = EnvParserInspector(source_module="myapp.settings").discover()
    for f in fields:
        assert f.source_module == "myapp.settings"
