"""django-aqueduct: structured, typed, auditable Django settings management."""

from django_aqueduct.adapter import (
    configure_django_programmatic,
    configure_django_settings,
)

__version__ = "0.3.0"

__all__ = [
    "configure_django_programmatic",
    "configure_django_settings",
]
