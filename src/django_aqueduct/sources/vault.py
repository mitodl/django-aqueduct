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

if TYPE_CHECKING:
    pass

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
        vault_path: str,
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
        self._vault_url = vault_url
        self._vault_path = vault_path
        self._mount_point = mount_point
        self._kv_version = kv_version
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

    def __call__(self) -> dict[str, Any]:
        """Fetch secrets from Vault and return them as a flat dict.

        Returns:
            A dict mapping secret keys to their values.

        Raises:
            ImportError: If ``hvac`` is not installed.
        """
        return self._fetch_secrets()

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        """Look up *field_name* from the cached Vault secrets dict."""
        if not hasattr(self, "_cache"):
            self._cache: dict[str, Any] = self._fetch_secrets()
        value = self._cache.get(field_name)
        return value, field_name, False

    def _fetch_secrets(self) -> dict[str, Any]:
        """Fetch and return all secrets as a dict."""
        hvac = _require_hvac()
        client = hvac.Client(url=self._vault_url)
        self._authenticate(client)
        if self._kv_version == "1":
            response = client.secrets.kv.v1.read_secret(
                path=self._vault_path,
                mount_point=self._mount_point,
            )
            return dict(response["data"])
        response = client.secrets.kv.v2.read_secret_version(
            path=self._vault_path,
            mount_point=self._mount_point,
        )
        return dict(response["data"]["data"])
