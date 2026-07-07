"""Tests for VaultSettingsSource."""

import pathlib
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from django_aqueduct.sources.vault import _DEFAULT_K8S_JWT_PATH, VaultSettingsSource

_FAKE_SECRET = {"DB_PASSWORD": "s3cr3t", "API_KEY": "abc123"}


def _make_hvac_client(secret_data: dict[str, Any] = _FAKE_SECRET) -> MagicMock:
    """Build a mock hvac.Client that returns *secret_data* from KV v2."""
    client = MagicMock()
    client.secrets.kv.v2.read_secret_version.return_value = {
        "data": {"data": secret_data}
    }
    return client


def _make_kv_v1_hvac_client(secret_data: dict[str, Any] = _FAKE_SECRET) -> MagicMock:
    """Build a mock hvac.Client that returns *secret_data* from KV v1."""
    client = MagicMock()
    client.secrets.kv.v1.read_secret.return_value = {"data": secret_data}
    return client


def _vault_source(**kwargs: Any) -> VaultSettingsSource:
    """Convenience constructor with sensible defaults."""
    return VaultSettingsSource(
        MagicMock(),  # settings_cls placeholder
        vault_url="https://vault.example.com",
        vault_path="myapp/production",
        **kwargs,
    )


class TestTokenAuth:
    """VaultSettingsSource with auth_method='token'."""

    def test_sets_client_token(self):
        """Token is assigned to client.token."""
        mock_client = _make_hvac_client()
        source = _vault_source(auth_method="token", vault_token="my-token")

        with patch("hvac.Client", return_value=mock_client):
            result = source()

        assert mock_client.token == "my-token"
        assert result == _FAKE_SECRET

    def test_returns_secret_data(self):
        """__call__ returns the data dict from KV v2."""
        mock_client = _make_hvac_client({"SETTING": "value"})
        source = _vault_source(auth_method="token", vault_token="t")

        with patch("hvac.Client", return_value=mock_client):
            result = source()

        assert result == {"SETTING": "value"}


class TestOIDCAuth:
    """VaultSettingsSource with auth_method='oidc'."""

    def test_calls_oidc_auth(self):
        """OIDC auth methods are invoked on the client."""
        mock_client = _make_hvac_client()
        source = _vault_source(
            auth_method="oidc",
            role="myapp",
            oidc_callback_port=8250,
        )

        with patch("hvac.Client", return_value=mock_client):
            result = source()

        mock_client.auth.oidc.oidc_authorization_url_request.assert_called_once()
        mock_client.auth.oidc.oidc_callback.assert_called_once()
        assert result == _FAKE_SECRET

    def test_custom_callback_port(self):
        """Custom oidc_callback_port is used in the redirect URI."""
        mock_client = _make_hvac_client()
        source = _vault_source(auth_method="oidc", role="r", oidc_callback_port=9999)

        with patch("hvac.Client", return_value=mock_client):
            source()

        call_kwargs = mock_client.auth.oidc.oidc_authorization_url_request.call_args
        assert "9999" in call_kwargs.kwargs.get("redirect_uri", "")


class TestKubernetesAuth:
    """VaultSettingsSource with auth_method='kubernetes'."""

    def test_explicit_jwt_bypasses_file_read(self):
        """An explicit jwt kwarg does not read jwt_path from disk."""
        mock_client = _make_hvac_client()
        source = _vault_source(
            auth_method="kubernetes",
            role="myapp",
            jwt="explicit-jwt-token",
        )

        with patch("hvac.Client", return_value=mock_client):
            with patch.object(pathlib.Path, "read_text") as mock_read:
                source()

        mock_read.assert_not_called()
        mock_client.auth.kubernetes.login.assert_called_once_with(
            role="myapp", jwt="explicit-jwt-token"
        )

    def test_reads_jwt_from_default_path(self):
        """When jwt is None, the JWT is read from the default path."""
        mock_client = _make_hvac_client()
        source = _vault_source(auth_method="kubernetes", role="myapp")

        with patch("hvac.Client", return_value=mock_client):
            with patch.object(
                pathlib.Path, "read_text", return_value="file-jwt\n"
            ) as mock_read:
                source()

        mock_read.assert_called_once()
        mock_client.auth.kubernetes.login.assert_called_once_with(
            role="myapp", jwt="file-jwt"
        )

    def test_reads_jwt_from_custom_path(self):
        """A custom jwt_path is used when specified."""
        mock_client = _make_hvac_client()
        source = _vault_source(
            auth_method="kubernetes",
            role="myapp",
            jwt_path="/custom/sa/token",
        )

        with patch("hvac.Client", return_value=mock_client):
            with patch.object(
                pathlib.Path, "read_text", return_value="custom-jwt"
            ) as mock_read:
                result = source()

        mock_read.assert_called_once()
        mock_client.auth.kubernetes.login.assert_called_once_with(
            role="myapp", jwt="custom-jwt"
        )
        assert result == _FAKE_SECRET

    def test_default_jwt_path_constant(self):
        """Default JWT path matches the standard K8s SA token mount."""
        assert _DEFAULT_K8S_JWT_PATH == (
            "/var/run/secrets/kubernetes.io/serviceaccount/token"
        )


class TestKVVersion:
    """VaultSettingsSource with kv_version='1' vs the default '2'."""

    def test_kv_v2_is_default(self):
        """With no kv_version specified, KV v2 API is used."""
        mock_client = _make_hvac_client()
        source = _vault_source(auth_method="token", vault_token="t")

        with patch("hvac.Client", return_value=mock_client):
            result = source()

        mock_client.secrets.kv.v2.read_secret_version.assert_called_once_with(
            path="myapp/production", mount_point="secret"
        )
        mock_client.secrets.kv.v1.read_secret.assert_not_called()
        assert result == _FAKE_SECRET

    def test_kv_v1_reads_via_v1_api(self):
        """kv_version='1' calls the KV v1 API and unwraps 'data' once."""
        mock_client = _make_kv_v1_hvac_client({"SETTING": "value"})
        source = _vault_source(auth_method="token", vault_token="t", kv_version="1")

        with patch("hvac.Client", return_value=mock_client):
            result = source()

        mock_client.secrets.kv.v1.read_secret.assert_called_once_with(
            path="myapp/production", mount_point="secret"
        )
        mock_client.secrets.kv.v2.read_secret_version.assert_not_called()
        assert result == {"SETTING": "value"}

    def test_kv_v1_respects_custom_mount_point(self):
        """A custom mount_point is passed through to the KV v1 API call."""
        mock_client = _make_kv_v1_hvac_client()
        source = _vault_source(
            auth_method="token",
            vault_token="t",
            kv_version="1",
            mount_point="secret-mitxonline",
        )

        with patch("hvac.Client", return_value=mock_client):
            source()

        mock_client.secrets.kv.v1.read_secret.assert_called_once_with(
            path="myapp/production", mount_point="secret-mitxonline"
        )

    def test_kv_version_accepts_int(self):
        """An integer kv_version (e.g. 1) is coerced to the matching string API."""
        mock_client = _make_kv_v1_hvac_client()
        source = _vault_source(auth_method="token", vault_token="t", kv_version=1)

        with patch("hvac.Client", return_value=mock_client):
            source()

        mock_client.secrets.kv.v1.read_secret.assert_called_once_with(
            path="myapp/production", mount_point="secret"
        )
        mock_client.secrets.kv.v2.read_secret_version.assert_not_called()

    def test_invalid_kv_version_raises(self):
        """An unsupported kv_version raises ValueError at construction time."""
        with pytest.raises(ValueError, match="kv_version must be '1' or '2'"):
            _vault_source(auth_method="token", vault_token="t", kv_version="3")


class TestImportGuard:
    """VaultSettingsSource raises ImportError when hvac is missing."""

    def test_import_error_without_hvac(self):
        """ImportError with install hint fires when hvac is absent."""
        source = _vault_source(auth_method="token", vault_token="t")

        with patch.dict(sys.modules, {"hvac": None}):  # type: ignore[dict-item]
            with pytest.raises(ImportError, match="django-aqueduct\\[vault\\]"):
                source()


# ------------------------------------------------------------------ #
# Hardening: caching, complex values, multi-path, error wrapping      #
# ------------------------------------------------------------------ #

from pydantic_settings import BaseSettings, SettingsConfigDict  # noqa: E402

from django_aqueduct.sources.vault import VaultError  # noqa: E402


class _Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="allow")
    DB_PASSWORD: str = ""
    JWT_AUTH: dict = {}


def _real_source(**kwargs: Any) -> VaultSettingsSource:
    return VaultSettingsSource(
        _Settings,
        vault_url="https://vault.example.com",
        vault_path="myapp/production",
        vault_token="t",
        **kwargs,
    )


def test_secrets_fetched_once_and_cached() -> None:
    client = _make_hvac_client({"DB_PASSWORD": "x"})
    src = _real_source()
    with patch("hvac.Client", return_value=client):
        src()
        src()  # second call must not re-read Vault
        src.get_field_value(_Settings.model_fields["DB_PASSWORD"], "DB_PASSWORD")
    assert client.secrets.kv.v2.read_secret_version.call_count == 1


def test_json_string_decoded_for_complex_field() -> None:
    # JWT_AUTH is a dict field; its Vault value is a JSON string.
    client = _make_hvac_client({"JWT_AUTH": '{"ALGORITHM": "HS256"}'})
    src = _real_source()
    with patch("hvac.Client", return_value=client):
        data = src()
    assert data["JWT_AUTH"] == {"ALGORITHM": "HS256"}


def test_extra_keys_pass_through() -> None:
    client = _make_hvac_client({"UNDECLARED": "keepme"})
    src = _real_source()
    with patch("hvac.Client", return_value=client):
        data = src()
    assert data["UNDECLARED"] == "keepme"


def test_multi_path_merge_later_wins() -> None:
    client = MagicMock()
    client.secrets.kv.v2.read_secret_version.side_effect = [
        {"data": {"data": {"A": "1", "B": "base"}}},
        {"data": {"data": {"B": "override", "C": "3"}}},
    ]
    src = VaultSettingsSource(
        _Settings,
        vault_url="https://v",
        vault_path=["base/path", "env/path"],
        vault_token="t",
    )
    with patch("hvac.Client", return_value=client):
        data = src()
    assert data["A"] == "1"
    assert data["B"] == "override"  # later path wins
    assert data["C"] == "3"


def test_read_failure_wrapped_in_vault_error() -> None:
    client = MagicMock()
    client.secrets.kv.v2.read_secret_version.side_effect = RuntimeError("boom")
    src = _real_source()
    with patch("hvac.Client", return_value=client):
        with pytest.raises(VaultError, match="Failed to read secrets"):
            src()


def test_empty_vault_path_sequence_rejected() -> None:
    with pytest.raises(ValueError, match="at least one non-empty path"):
        VaultSettingsSource(
            _Settings, vault_url="https://v", vault_path=[], vault_token="t"
        )


def test_blank_vault_path_rejected() -> None:
    with pytest.raises(ValueError, match="at least one non-empty path"):
        VaultSettingsSource(
            _Settings, vault_url="https://v", vault_path=["", "  "], vault_token="t"
        )


def test_malformed_json_in_complex_field_wrapped() -> None:
    from django_aqueduct.sources._base import SourceError

    client = _make_hvac_client({"JWT_AUTH": "{not json"})
    src = _real_source()
    with patch("hvac.Client", return_value=client):
        with pytest.raises(SourceError, match="Failed to decode complex value"):
            src()
