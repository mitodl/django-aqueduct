"""Tests for django_aqueduct.validation — UrlStr.

These cover the three consumption paths that broke every app that tried the
0.9.0 `AnyUrl` promotion. A `field_serializer` intercepts only serialization,
so it fixed none of them; each test below is a real app failure reproduced
against `AnyUrl` and asserted fixed under `UrlStr`.
"""

from __future__ import annotations

from urllib.parse import urljoin, urlparse

import pytest
from pydantic import AnyUrl, BaseModel, Field, ValidationError, model_validator

from django_aqueduct import UrlStr
from django_aqueduct.validation import validate_url_str


def test_accepts_an_absolute_url_and_returns_it_unchanged():
    assert validate_url_str("https://example.com/path") == "https://example.com/path"


@pytest.mark.parametrize("value", ["/static/", "login", "", "not a url", "example.com"])
def test_rejects_non_absolute_urls(value):
    with pytest.raises(ValueError, match="not an absolute URL"):
        validate_url_str(value)


def test_stays_a_str_on_the_instance():
    class M(BaseModel):
        URL: UrlStr

    inst = M(URL="https://example.com/api")
    assert type(inst.URL) is str
    assert inst.model_dump()["URL"] == "https://example.com/api"


def test_no_trailing_slash_normalization():
    """AnyUrl rewrites a bare host; injecting a changed value breaks parity."""
    assert str(AnyUrl("https://app.posthog.com")) == "https://app.posthog.com/"

    class M(BaseModel):
        URL: UrlStr

    assert M(URL="https://app.posthog.com").URL == "https://app.posthog.com"


def test_readable_as_a_string_inside_a_validator():
    """mitxpro `urlparse(self.SITE_BASE_URL)`, ocw-studio `.strip()`, mit-learn
    `urljoin(...)` — all AttributeError/TypeError against an AnyUrl instance.
    """

    class M(BaseModel):
        SITE_BASE_URL: UrlStr
        HOST: str = ""
        JOINED: str = ""
        STRIPPED: str = ""

        @model_validator(mode="after")
        def derive(self) -> M:
            self.HOST = urlparse(self.SITE_BASE_URL).netloc
            self.JOINED = urljoin(self.SITE_BASE_URL, "/callback")
            self.STRIPPED = self.SITE_BASE_URL.strip().rstrip("/")
            return self

    inst = M(SITE_BASE_URL="https://mitxpro.example.com/")
    assert inst.HOST == "mitxpro.example.com"
    assert inst.JOINED == "https://mitxpro.example.com/callback"
    assert inst.STRIPPED == "https://mitxpro.example.com"


def test_url_nested_in_a_dict_setting_stays_a_str():
    """A top-level field_serializer never reached these (CACHES LOCATION,
    OAUTH2_PROVIDER entries built from a promoted *_URL field).
    """

    class M(BaseModel):
        CELERY_BROKER_URL: UrlStr
        CACHES: dict[str, dict[str, str]] = Field(default_factory=dict)

        @model_validator(mode="after")
        def build_caches(self) -> M:
            self.CACHES = {"default": {"LOCATION": self.CELERY_BROKER_URL}}
            return self

    inst = M(CELERY_BROKER_URL="redis://localhost:6379/0")
    location = inst.model_dump()["CACHES"]["default"]["LOCATION"]
    assert type(location) is str
    assert location == "redis://localhost:6379/0"


def test_optional_url_field_accepts_none():
    class M(BaseModel):
        URL: UrlStr | None = None

    assert M().URL is None
    assert M(URL="https://example.com").URL == "https://example.com"


def test_invalid_url_still_fails_validation():
    """The point of promoting at all — a malformed value is still caught."""

    class M(BaseModel):
        URL: UrlStr

    with pytest.raises(ValidationError):
        M(URL="/relative/path")
