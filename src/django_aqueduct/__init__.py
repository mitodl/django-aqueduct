"""django-aqueduct: structured, typed, auditable Django settings management."""

from importlib.metadata import PackageNotFoundError, version

from django_aqueduct.adapter import (
    configure_django_programmatic,
    configure_django_settings,
    get_configured_model,
)
from django_aqueduct.surface import UNSET, Setting
from django_aqueduct.validation import UrlStr, validate_url_str

try:
    #: Read from installed package metadata rather than duplicated here, so it
    #: cannot drift from ``pyproject.toml`` the way the hand-maintained string
    #: did -- it sat at 0.13.0 through the 0.14.0 and 0.14.1 releases, which is
    #: exactly the reading a consumer would use to gate on a fix being present.
    __version__ = version("django-aqueduct")
except PackageNotFoundError:  # pragma: no cover - source tree with no install
    __version__ = "0.0.0+unknown"

__all__ = [
    "UNSET",
    "Setting",
    "UrlStr",
    "configure_django_programmatic",
    "configure_django_settings",
    "get_configured_model",
    "validate_url_str",
]
