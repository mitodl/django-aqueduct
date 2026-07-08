"""django-aqueduct: structured, typed, auditable Django settings management."""

from django_aqueduct.adapter import (
    configure_django_programmatic,
    configure_django_settings,
    get_configured_model,
)

__version__ = "0.9.0"

__all__ = [
    "configure_django_programmatic",
    "configure_django_settings",
    "get_configured_model",
]
