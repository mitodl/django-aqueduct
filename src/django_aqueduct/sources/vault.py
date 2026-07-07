"""Vault KV v1/v2 settings source for pydantic-settings.

Requires the ``[vault]`` extra::

    pip install django-aqueduct[vault]

Supports both Vault KV secrets engine versions (``kv_version="1"`` or
``"2"``, defaulting to ``"2"``) and three authentication methods:

- ``"token"``   — static Vault token (simplest, not recommended for production)
- ``"oidc"``    — OIDC/JWT login via a browser callback (interactive or CI)
- ``"kubernetes"`` — Kubernetes Service Account JWT (recommended for K8s deployments)

Example (Kubernetes)::

    from django_aqueduct.sources.vault import VaultSettingsSource
    from pydantic_settings import BaseSettings

    class AppSettings(BaseSettings):
        model_config = SettingsConfigDict(
            extra="allow",
            secrets_dir=None,
        )

        @classmethod
        def settings_customise_sources(cls, settings_cls, **kwargs):
            return (
                VaultSettingsSource(
                    settings_cls,
                    vault_url="https://vault.example.com",
                    vault_path="myapp/production",
                    auth_method="kubernetes",
                    role="myapp",
                ),
            )
"""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING, Any, Literal

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from django_aqueduct.sources._base import SourceError, build_from_mapping

if TYPE_CHECKING:
    from collections.abc import Sequence


class VaultError(SourceError):
    """Raised when reading secrets from Vault fails (connection/auth/path)."""


_DEFAULT_K8S_JWT_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"


def _require_hvac() -> Any:
    """Import hvac or raise a helpful error.

    Returns:
        The ``hvac`` module.

    Raises:
        ImportError: When ``hvac`` is not installed.
    """
    try:
        import hvac  # noqa: PLC0415

        return hvac
    except ImportError as exc:
        raise ImportError(
            "VaultSettingsSource requires 'hvac'. "
            "Install it with: pip install django-aqueduct[vault]"
        ) from exc


class VaultSettingsSource(PydanticBaseSettingsSource):
    """Load settings from a Vault KV v1 or v2 secret path.

    Args:
        settings_cls: The settings class (passed automatically by pydantic-settings).
        vault_url: Full URL of the Vault server, e.g. ``"https://vault.example.com"``.
        vault_path: Path to the KV secret, e.g. ``"myapp/production"``.
        mount_point: KV mount point. Defaults to ``"secret"``.
        kv_version: KV secrets engine version at *mount_point*, ``"1"`` or
            ``"2"``. Defaults to ``"2"``.
        auth_method: One of ``"token"``, ``"oidc"``, or ``"kubernetes"``.
        vault_token: Static Vault token. Required when ``auth_method="token"``.
        role: Vault role name. Required for ``"oidc"`` and ``"kubernetes"`` auth.
        oidc_callback_port: Local port for the OIDC callback server.
            Defaults to ``8250``.
        jwt: Explicit Kubernetes SA JWT string. When omitted the JWT is read
            from *jwt_path* at call time.
        jwt_path: Path to the Kubernetes SA JWT file.
            Defaults to the standard K8s projected token mount
            ``/var/run/secrets/kubernetes.io/serviceaccount/token``.
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        *,
        vault_url: str,
        vault_path: str | Sequence[str],
        mount_point: str = "secret",
        kv_version: Literal["1", "2"] = "2",
        auth_method: Literal["token", "oidc", "kubernetes"] = "token",
        vault_token: str | None = None,
        role: str | None = None,
        oidc_callback_port: int = 8250,
        jwt: str | None = None,
        jwt_path: str = _DEFAULT_K8S_JWT_PATH,
    ) -> None:
        """Store configuration for Vault access and authentication."""
        super().__init__(settings_cls)
        if str(kv_version) not in ("1", "2"):
            raise ValueError(f"kv_version must be '1' or '2', got {kv_version!r}")
        self._vault_url = vault_url
        # One or more KV paths, read and merged in order (later paths win).
        self._vault_paths: tuple[str, ...] = (
            (vault_path,) if isinstance(vault_path, str) else tuple(vault_path)
        )
        self._mount_point = mount_point
        self._secrets_cache: dict[str, Any] | None = None
        self._kv_version = str(kv_version)
        self._auth_method = auth_method
        self._vault_token = vault_token
        self._role = role
        self._oidc_callback_port = oidc_callback_port
        self._jwt = jwt
        self._jwt_path = jwt_path

    def _authenticate(self, client: Any) -> None:
        """Authenticate the hvac client using the configured method.

        Args:
            client: An ``hvac.Client`` instance.
        """
        if self._auth_method == "token":
            client.token = self._vault_token

        elif self._auth_method == "oidc":
            client.auth.oidc.oidc_authorization_url_request(
                role=self._role,
                redirect_uri=f"http://localhost:{self._oidc_callback_port}/oidc/callback",
            )
            # Perform the full OIDC login flow (opens browser / callback server)
            client.auth.oidc.oidc_callback(
                state=None,
                nonce=None,
                code=None,
            )

        elif self._auth_method == "kubernetes":
            jwt = self._jwt
            if jwt is None:
                jwt = pathlib.Path(self._jwt_path).read_text(encoding="utf-8").strip()
            client.auth.kubernetes.login(role=self._role, jwt=jwt)

    @property
    def _secrets(self) -> dict[str, Any]:
        """Fetch (once) and cache the merged secrets from all configured paths."""
        if self._secrets_cache is None:
            self._secrets_cache = self._fetch_secrets()
        return self._secrets_cache

    def __call__(self) -> dict[str, Any]:
        """Return the Vault secrets as a validated settings dict.

        Complex fields (dict/list/model) whose Vault value is a JSON string are
        decoded via ``prepare_field_value``.

        Raises:
            VaultError: On connection/auth/read failure.
            ImportError: If ``hvac`` is not installed.
        """
        return build_from_mapping(self, self._secrets)

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        """Look up *field_name* from the cached Vault secrets dict."""
        return self._secrets.get(field_name), field_name, self.field_is_complex(field)

    def _fetch_secrets(self) -> dict[str, Any]:
        """Fetch and merge secrets from every configured path (later wins)."""
        hvac = _require_hvac()
        try:
            client = hvac.Client(url=self._vault_url)
            self._authenticate(client)
            merged: dict[str, Any] = {}
            for path in self._vault_paths:
                merged.update(self._read_path(client, path))
        except VaultError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise VaultError(
                f"Failed to read secrets from Vault at {self._vault_url!r}: {exc}"
            ) from exc
        return merged

    def _read_path(self, client: Any, path: str) -> dict[str, Any]:
        """Read a single KV path and return its data dict."""
        if self._kv_version == "1":
            response = client.secrets.kv.v1.read_secret(
                path=path, mount_point=self._mount_point
            )
            return dict(response["data"])
        response = client.secrets.kv.v2.read_secret_version(
            path=path, mount_point=self._mount_point
        )
        return dict(response["data"]["data"])
