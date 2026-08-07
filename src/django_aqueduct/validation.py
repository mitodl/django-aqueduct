"""Annotated types that validate a value's *shape* without changing its type.

Generated settings models are read by application code that was written
against Django's plain settings, so a refined annotation must not change what
the attribute *is* at runtime. ``pydantic.AnyUrl`` fails that test three ways —
it hands validators and derivations a ``Url`` object instead of a ``str``, it
doesn't reach URL values nested inside a ``dict`` setting, and it rewrites the
value (a bare host gains a trailing slash). The types here validate and return
the input unchanged.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, AnyUrl, TypeAdapter, ValidationError

__all__ = ["UrlStr", "validate_url_str"]

_ANY_URL_ADAPTER: TypeAdapter[AnyUrl] = TypeAdapter(AnyUrl)


def validate_url_str(value: str) -> str:
    """Return *value* unchanged if it parses as an absolute URL, else raise.

    Deliberately returns the original string rather than
    ``str(AnyUrl(value))``: pydantic's ``Url`` normalises as it parses, most
    visibly by appending a trailing slash to a bare host
    (``https://app.posthog.com`` -> ``https://app.posthog.com/``). Injecting a
    value the legacy settings module never produced is a parity break, so
    validation here is a check and nothing more.
    """
    try:
        _ANY_URL_ADAPTER.validate_python(value)
    except ValidationError as exc:
        msg = f"{value!r} is not an absolute URL"
        raise ValueError(msg) from exc
    return value


UrlStr = Annotated[str, AfterValidator(validate_url_str)]
"""A ``str`` that must parse as an absolute URL.

Still a ``str`` everywhere it matters: ``.strip()``/``.rstrip()``,
``urlparse``/``urljoin``, f-strings, and ``model_dump()`` all behave exactly as
they did before the field was promoted, and a ``UrlStr`` nested in a
``dict``-valued setting needs no serializer to come back out as a string.
"""
