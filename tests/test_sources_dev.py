"""Tests for the env-driven Vault dev base (sources.dev)."""

from __future__ import annotations

import pytest
from pydantic_settings import BaseSettings

from django_aqueduct.sources.dev import (
    VaultConfigError,
    VaultDevBase,
    vault_source_from_env,
)


class _Settings(BaseSettings):
    pass


def test_disabled_when_no_vault_addr() -> None:
    assert vault_source_from_env(_Settings, env={}) is None


def test_missing_path_raises() -> None:
    with pytest.raises(VaultConfigError, match="VAULT_PATH"):
        vault_source_from_env(_Settings, env={"VAULT_ADDR": "https://v"})


def test_token_auth_requires_token() -> None:
    with pytest.raises(VaultConfigError, match="VAULT_TOKEN"):
        vault_source_from_env(
            _Settings, env={"VAULT_ADDR": "https://v", "VAULT_PATH": "app"}
        )


def test_kubernetes_auth_requires_role() -> None:
    with pytest.raises(VaultConfigError, match="VAULT_ROLE"):
        vault_source_from_env(
            _Settings,
            env={
                "VAULT_ADDR": "https://v",
                "VAULT_PATH": "app",
                "VAULT_AUTH_METHOD": "kubernetes",
            },
        )


def test_invalid_kv_version_raises() -> None:
    with pytest.raises(VaultConfigError, match="VAULT_KV_VERSION"):
        vault_source_from_env(
            _Settings,
            env={
                "VAULT_ADDR": "https://v",
                "VAULT_PATH": "app",
                "VAULT_TOKEN": "t",
                "VAULT_KV_VERSION": "9",
            },
        )


def test_invalid_auth_method_raises() -> None:
    with pytest.raises(VaultConfigError, match="VAULT_AUTH_METHOD"):
        vault_source_from_env(
            _Settings,
            env={
                "VAULT_ADDR": "https://v",
                "VAULT_PATH": "app",
                "VAULT_AUTH_METHOD": "ldap",
            },
        )


def test_whitespace_vault_addr_is_unset() -> None:
    # VAULT_ADDR="   " is a common way to disable Vault locally.
    assert vault_source_from_env(_Settings, env={"VAULT_ADDR": "   "}) is None


def test_valid_config_builds_source() -> None:
    source = vault_source_from_env(
        _Settings,
        env={
            "VAULT_ADDR": "https://v",
            "VAULT_PATH": "myapp/prod",
            "VAULT_MOUNT": "kv",
            "VAULT_AUTH_METHOD": "kubernetes",
            "VAULT_ROLE": "myapp",
            "VAULT_KV_VERSION": "1",
        },
    )
    assert source is not None
    assert source._vault_url == "https://v"
    assert source._vault_path == "myapp/prod"
    assert source._mount_point == "kv"
    assert source._kv_version == "1"
    assert source._role == "myapp"


def test_devbase_sources_without_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    base = ("init", "env", "dotenv", "secret")
    result = VaultDevBase.settings_customise_sources(
        _Settings,
        init_settings="init",
        env_settings="env",
        dotenv_settings="dotenv",
        file_secret_settings="secret",
    )
    assert result == base


def test_devbase_prepends_vault_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VAULT_ADDR", "https://v")
    monkeypatch.setenv("VAULT_PATH", "app")
    monkeypatch.setenv("VAULT_TOKEN", "t")
    result = VaultDevBase.settings_customise_sources(
        _Settings,
        init_settings="init",
        env_settings="env",
        dotenv_settings="dotenv",
        file_secret_settings="secret",
    )
    # Vault source is prepended ahead of the standard sources.
    assert result[1:] == ("init", "env", "dotenv", "secret")
    assert result[0] not in ("init", "env", "dotenv", "secret")
