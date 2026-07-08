"""django-aqueduct: structured, typed, auditable Django settings management."""

from django_aqueduct.adapter import (
    configure_django_programmatic,
    configure_django_settings,
    get_configured_model,
)
from django_aqueduct.surface import UNSET, Setting

__version__ = "0.10.0"

__all__ = [
    "UNSET",
    "Setting",
    "configure_django_programmatic",
    "configure_django_settings",
    "get_configured_model",
]
