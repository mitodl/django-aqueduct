"""AWS SSM Parameter Store settings source for pydantic-settings.

Requires the ``[aws]`` extra::

    pip install django-aqueduct[aws]

Fetches all parameters under a path prefix with full pagination and
``WithDecryption=True`` so SecureString parameters are returned as
plaintext.

Example::

    from django_aqueduct.sources.aws_ssm import AWSParameterStoreSource
    from pydantic_settings import BaseSettings

    class AppSettings(BaseSettings):
        @classmethod
        def settings_customise_sources(cls, settings_cls, **kwargs):
            return (
                AWSParameterStoreSource(
                    settings_cls,
                    path_prefix="/myapp/production/",
                    region_name="us-east-1",
                ),
            )
"""

from __future__ import annotations

from typing import Any

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource


def _require_boto3() -> Any:
    """Import boto3 or raise a helpful error.

    Returns:
        The ``boto3`` module.

    Raises:
        ImportError: When ``boto3`` is not installed.
    """
    try:
        import boto3  # noqa: PLC0415

        return boto3
    except ImportError as exc:
        raise ImportError(
            "AWSParameterStoreSource requires 'boto3'. "
            "Install it with: pip install django-aqueduct[aws]"
        ) from exc


class AWSParameterStoreSource(PydanticBaseSettingsSource):
    """Load settings from AWS SSM Parameter Store under a path prefix.

    All parameters under *path_prefix* are fetched recursively with
    ``WithDecryption=True``. The prefix is stripped from each parameter name
    to produce plain settings keys (e.g. ``/myapp/prod/SECRET_KEY`` with
    prefix ``/myapp/prod/`` becomes ``SECRET_KEY``).

    Pagination is handled automatically — every page of results is fetched
    and merged before returning.

    Args:
        settings_cls: The settings class (passed automatically by pydantic-settings).
        path_prefix: SSM path prefix, e.g. ``"/myapp/production/"``. A
            trailing slash is recommended to avoid partial name matches.
        region_name: AWS region. If ``None``, boto3 uses the default region
            from the environment or instance metadata.
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        *,
        path_prefix: str,
        region_name: str | None = None,
    ) -> None:
        """Store the path prefix and optional region for SSM lookups."""
        super().__init__(settings_cls)
        self._path_prefix = path_prefix
        self._region_name = region_name

    def _fetch_all(self) -> dict[str, Any]:
        """Fetch every parameter under the prefix, following pagination tokens.

        Returns:
            Merged dict of stripped-name → value for all pages.

        Raises:
            ImportError: If ``boto3`` is not installed.
        """
        boto3 = _require_boto3()
        kwargs: dict[str, Any] = {}
        if self._region_name:
            kwargs["region_name"] = self._region_name
        client = boto3.client("ssm", **kwargs)

        results: dict[str, Any] = {}
        paginate_kwargs: dict[str, Any] = {
            "Path": self._path_prefix,
            "Recursive": True,
            "WithDecryption": True,
        }

        while True:
            response = client.get_parameters_by_path(**paginate_kwargs)
            for param in response.get("Parameters", []):
                key = param["Name"].removeprefix(self._path_prefix)
                results[key] = param["Value"]

            next_token = response.get("NextToken")
            if not next_token:
                break
            paginate_kwargs["NextToken"] = next_token

        return results

    def __call__(self) -> dict[str, Any]:
        """Return all parameters under the prefix as a flat dict.

        Returns:
            Settings dict with path prefix stripped from keys.

        Raises:
            ImportError: If ``boto3`` is not installed.
        """
        return self._fetch_all()

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        """Look up *field_name* from the cached SSM parameters dict."""
        if not hasattr(self, "_cache"):
            self._cache: dict[str, Any] = self._fetch_all()
        value = self._cache.get(field_name)
        return value, field_name, False
