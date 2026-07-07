"""Env-driven Vault configuration for local development settings models.

Every app that adopted django-aqueduct copy-pasted a ``DevAqueductSettings``
base that wired :class:`~django_aqueduct.sources.vault.VaultSettingsSource`
from the environment. Those copies shared three flaws this module fixes:

* ``os.environ["VAULT_ADDR"]`` raised a bare ``KeyError`` when unset — an
  opaque crash instead of an actionable message (or a graceful skip).
* mount/path/role were hardcoded placeholders "not discoverable from the repo".
* ``kv_version`` was hardcoded.

:func:`vault_source_from_env` reads every parameter from the environment with
sensible defaults, and either returns a configured source or (when Vault is not
configured) ``None`` so a developer can run without Vault. :class:`VaultDevBase`
wires it into ``settings_customise_sources`` for the common case.

Environment variables (all optional unless noted):

===========================  ============================================
``VAULT_ADDR``               Vault server URL. **Required** to enable Vault;
                             absent → source disabled (returns ``None``).
``VAULT_MOUNT``              KV mount point. Default ``"secret"``.
``VAULT_PATH``               KV secret path. **Required** when ``VAULT_ADDR``
                             is set.
``VAULT_KV_VERSION``         ``"1"`` or ``"2"``. Default ``"2"``.
``VAULT_AUTH_METHOD``        ``token`` | ``oidc`` | ``kubernetes``.
                             Default ``"token"``.
``VAULT_TOKEN``              Static token (``token`` auth).
``VAULT_ROLE``               Role name (``oidc`` / ``kubernetes`` auth).
===========================  ============================================
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Literal

from pydantic_settings import BaseSettings

if TYPE_CHECKING:  # pragma: no cover
    from pydantic_settings import PydanticBaseSettingsSource

    from django_aqueduct.sources.vault import VaultSettingsSource


class VaultConfigError(Exception):
    """Raised when Vault env configuration is present but incomplete/invalid."""


def _kv_version_from_env(raw: str | None) -> Literal["1", "2"]:
    version = (raw or "2").strip()
    if version not in ("1", "2"):
        raise VaultConfigError(f"VAULT_KV_VERSION must be '1' or '2', got {raw!r}.")
    return version  # type: ignore[return-value]


def _auth_method_from_env(raw: str | None) -> Literal["token", "oidc", "kubernetes"]:
    method = (raw or "token").strip()
    if method not in ("token", "oidc", "kubernetes"):
        raise VaultConfigError(
            f"VAULT_AUTH_METHOD must be one of token/oidc/kubernetes, got {raw!r}."
        )
    return method  # type: ignore[return-value]


def vault_source_from_env(
    settings_cls: type[BaseSettings],
    env: dict[str, str] | None = None,
) -> VaultSettingsSource | None:
    """Build a Vault source from environment variables, or ``None`` if disabled.

    Returns ``None`` when ``VAULT_ADDR`` is unset — the caller runs without
    Vault (typical local dev). When ``VAULT_ADDR`` *is* set, missing companion
    variables raise :class:`VaultConfigError` with a clear message rather than a
    bare ``KeyError``.

    Args:
        settings_cls: The ``BaseSettings`` subclass the source populates.
        env: Environment mapping (defaults to ``os.environ``); injectable for
            tests.

    Raises:
        VaultConfigError: When Vault is enabled but misconfigured.
        ImportError: When the ``[vault]`` extra (hvac) is not installed.
    """
    environ = env if env is not None else dict(os.environ)

    vault_addr = environ.get("VAULT_ADDR")
    if not vault_addr:
        return None

    vault_path = environ.get("VAULT_PATH")
    if not vault_path:
        raise VaultConfigError(
            "VAULT_ADDR is set but VAULT_PATH is not; set VAULT_PATH to the KV "
            "secret path (e.g. 'myapp/production')."
        )

    auth_method = _auth_method_from_env(environ.get("VAULT_AUTH_METHOD"))
    kv_version = _kv_version_from_env(environ.get("VAULT_KV_VERSION"))
    vault_token = environ.get("VAULT_TOKEN")
    role = environ.get("VAULT_ROLE")

    if auth_method == "token" and not vault_token:
        raise VaultConfigError(
            "VAULT_AUTH_METHOD=token requires VAULT_TOKEN to be set."
        )
    if auth_method in ("oidc", "kubernetes") and not role:
        raise VaultConfigError(
            f"VAULT_AUTH_METHOD={auth_method} requires VAULT_ROLE to be set."
        )

    from django_aqueduct.sources.vault import VaultSettingsSource  # noqa: PLC0415

    return VaultSettingsSource(
        settings_cls,
        vault_url=vault_addr,
        vault_path=vault_path,
        mount_point=environ.get("VAULT_MOUNT", "secret"),
        kv_version=kv_version,
        auth_method=auth_method,
        vault_token=vault_token,
        role=role,
    )


class VaultDevBase(BaseSettings):
    """Base settings model that layers an env-configured Vault source on top.

    Subclass it instead of ``BaseSettings`` to pick up Vault automatically when
    the ``VAULT_*`` environment is present, and to run cleanly without Vault
    when it is not::

        class AppSettings(VaultDevBase):
            SECRET_KEY: str
    """

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Prepend the env-configured Vault source when it is available."""
        base = (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )
        vault = vault_source_from_env(settings_cls)
        return (vault, *base) if vault is not None else base
