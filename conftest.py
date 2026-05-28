"""Root pytest configuration."""

from django.conf import settings


def pytest_configure():
    """Configure Django settings for the test suite."""
    if not settings.configured:
        settings.configure(
            SECRET_KEY="test-secret-key",  # noqa: S106
            INSTALLED_APPS=[
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "django_aqueduct",
            ],
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": ":memory:",
                }
            },
            USE_TZ=True,
            DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        )
