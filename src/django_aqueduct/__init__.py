"""django-aqueduct: structured, typed, auditable Django settings management."""

from django_aqueduct.adapter import (
    configure_django_programmatic,
    configure_django_settings,
    get_configured_model,
)
from django_aqueduct.surface import UNSET, Setting
from django_aqueduct.validation import UrlStr, validate_url_str

__version__ = "0.13.0"

__all__ = [
    "UNSET",
    "Setting",
    "UrlStr",
    "configure_django_programmatic",
    "configure_django_settings",
    "get_configured_model",
    "validate_url_str",
]
