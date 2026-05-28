"""Tests for AWSParameterStoreSource."""

import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from django_aqueduct.sources.aws_ssm import AWSParameterStoreSource

_PREFIX = "/myapp/production/"


def _make_param(name: str, value: str) -> dict[str, str]:
    return {"Name": f"{_PREFIX}{name}", "Value": value}


def _ssm_source(**kwargs: Any) -> AWSParameterStoreSource:
    return AWSParameterStoreSource(
        MagicMock(),  # settings_cls placeholder
        path_prefix=_PREFIX,
        **kwargs,
    )


class TestFetch:
    """AWSParameterStoreSource fetches and strips the prefix."""

    def test_single_page(self):
        """Single-page response returns all parameters with prefix stripped."""
        mock_client = MagicMock()
        mock_client.get_parameters_by_path.return_value = {
            "Parameters": [
                _make_param("SECRET_KEY", "s3cr3t"),
                _make_param("DEBUG", "false"),
            ]
        }

        source = _ssm_source()
        with patch("boto3.client", return_value=mock_client):
            result = source()

        assert result == {"SECRET_KEY": "s3cr3t", "DEBUG": "false"}

    def test_two_page_pagination(self):
        """Two-page response is fully fetched and merged."""
        mock_client = MagicMock()
        mock_client.get_parameters_by_path.side_effect = [
            {
                "Parameters": [_make_param("PAGE1_KEY", "v1")],
                "NextToken": "tok-abc",
            },
            {
                "Parameters": [_make_param("PAGE2_KEY", "v2")],
                # No NextToken — last page
            },
        ]

        source = _ssm_source()
        with patch("boto3.client", return_value=mock_client):
            result = source()

        assert result == {"PAGE1_KEY": "v1", "PAGE2_KEY": "v2"}

    def test_pagination_passes_next_token(self):
        """NextToken from first page is passed to the second request."""
        mock_client = MagicMock()
        mock_client.get_parameters_by_path.side_effect = [
            {"Parameters": [], "NextToken": "token-123"},
            {"Parameters": []},
        ]

        source = _ssm_source()
        with patch("boto3.client", return_value=mock_client):
            source()

        calls = mock_client.get_parameters_by_path.call_args_list
        assert len(calls) == 2
        assert (
            calls[1].kwargs.get("NextToken") == "token-123"
            or calls[1].args[0].get("NextToken") == "token-123"
            if calls[1].args
            else "NextToken" in str(calls[1])
        )

    def test_prefix_stripped_on_both_pages(self):
        """Prefix stripping applies to parameters from all pages."""
        mock_client = MagicMock()
        mock_client.get_parameters_by_path.side_effect = [
            {
                "Parameters": [_make_param("ALPHA", "a")],
                "NextToken": "tok",
            },
            {
                "Parameters": [_make_param("BETA", "b")],
            },
        ]

        source = _ssm_source()
        with patch("boto3.client", return_value=mock_client):
            result = source()

        assert "ALPHA" in result
        assert "BETA" in result
        # Raw prefixed names must not appear
        assert f"{_PREFIX}ALPHA" not in result
        assert f"{_PREFIX}BETA" not in result

    def test_uses_region_when_specified(self):
        """region_name is forwarded to boto3.client."""
        mock_client = MagicMock()
        mock_client.get_parameters_by_path.return_value = {"Parameters": []}

        source = _ssm_source(region_name="eu-west-1")
        with patch("boto3.client", return_value=mock_client) as mock_boto:
            source()

        mock_boto.assert_called_once_with("ssm", region_name="eu-west-1")

    def test_no_region_omits_kwarg(self):
        """When region_name is None, region_name is not passed to boto3.client."""
        mock_client = MagicMock()
        mock_client.get_parameters_by_path.return_value = {"Parameters": []}

        source = _ssm_source()
        with patch("boto3.client", return_value=mock_client) as mock_boto:
            source()

        mock_boto.assert_called_once_with("ssm")


class TestImportGuard:
    """AWSParameterStoreSource raises ImportError when boto3 is missing."""

    def test_import_error_without_boto3(self):
        """ImportError with install hint fires when boto3 is absent."""
        source = _ssm_source()
        with patch.dict(sys.modules, {"boto3": None}):  # type: ignore[dict-item]
            with pytest.raises(ImportError, match="django-aqueduct\\[aws\\]"):
                source()
