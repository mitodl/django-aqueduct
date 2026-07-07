"""Reusable derivations for common Django settings shapes.

Every app that adopted django-aqueduct hand-wrote the same ~12-16 validators to
turn primitive settings (a ``DATABASE_URL`` string, a Redis URL, a ``FEATURE_*``
prefix) into the structured objects Django expects (``DATABASES``, ``CACHES``,
Celery broker config, ``ADMINS``). This module centralises those derivations as
small, well-tested functions an app calls from a single ``@model_validator``::

    from django_aqueduct import derivations as dv

    class AppSettings(BaseSettings):
        DATABASE_URL: str
        REDIS_URL: str | None = None
        REDISCLOUD_URL: str | None = None
        DATABASES: dict = {}
        CACHES: dict = {}
        CELERY_BROKER_URL: str | None = None
        FEATURES: dict = {}

        @model_validator(mode="after")
        def _derive(self):
            self.DATABASES = {"default": dv.database_config(self.DATABASE_URL)}
            redis = dv.first_url(self.REDIS_URL, self.REDISCLOUD_URL)
            self.CACHES = {"default": dv.redis_cache(redis)} if redis else {}
            self.CELERY_BROKER_URL = redis
            self.FEATURES = dv.feature_flags(self)
            return self

The two rules these encode that the hand-written copies kept getting wrong:

* SQLite URLs must not receive an ``sslmode`` option (the source of a shipped
  odl-video-service bug).
* Feature-flag / prefix scans read from the **settings values**, never
  ``os.environ`` directly — reading the environment bypasses Vault/SSM sources.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def _require_dj_database_url() -> Any:
    """Import ``dj_database_url`` or raise an actionable error."""
    try:
        import dj_database_url  # noqa: PLC0415

        return dj_database_url
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise ImportError(
            "database_config() requires 'dj-database-url'. "
            "Install it with: pip install django-aqueduct[derivations]"
        ) from exc


def database_config(
    url: str,
    *,
    conn_max_age: int = 0,
    ssl_require: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Parse a database URL into a Django ``DATABASES`` entry.

    Wraps :func:`dj_database_url.parse`. ``ssl_require`` adds
    ``OPTIONS={"sslmode": "require"}`` — but never for SQLite, whose driver
    rejects ``sslmode`` (the bug that shipped when a hand-written validator
    applied sslmode unconditionally).

    Args:
        url: A database URL, e.g. ``postgres://u:p@host:5432/db``.
        conn_max_age: Persistent-connection lifetime in seconds.
        ssl_require: Require SSL (ignored for SQLite backends).
        **kwargs: Passed through to :func:`dj_database_url.parse`.

    Returns:
        A single ``DATABASES["default"]``-shaped dict.
    """
    if not url or not url.strip():
        # An empty DATABASE_URL yields an empty config rather than a parse
        # error or a config carrying only a stray sslmode OPTIONS.
        return {}
    parsed: dict[str, Any] = _require_dj_database_url().parse(
        url, conn_max_age=conn_max_age, **kwargs
    )
    engine = str(parsed.get("ENGINE", ""))
    if ssl_require and "sqlite" not in engine:
        options = dict(parsed.get("OPTIONS") or {})
        options.setdefault("sslmode", "require")
        parsed["OPTIONS"] = options
    return parsed


def first_url(*candidates: str | None) -> str | None:
    """Return the first non-empty URL from *candidates*, else ``None``.

    Encodes the ``REDIS_URL`` → ``REDISCLOUD_URL`` → broker fallback chain the
    apps re-implemented by hand. Candidates are stripped, and whitespace-only
    values are treated as unset (a common env-var footgun).
    """
    for candidate in candidates:
        if candidate and candidate.strip():
            return candidate.strip()
    return None


def redis_cache(
    url: str,
    *,
    backend: str = "django_redis.cache.RedisCache",
    **options: Any,
) -> dict[str, Any]:
    """Return a Django ``CACHES`` entry backed by Redis at *url*.

    Args:
        url: The Redis connection URL.
        backend: Cache backend dotted path.
        **options: Extra keys merged into ``OPTIONS``.
    """
    entry: dict[str, Any] = {"BACKEND": backend, "LOCATION": url}
    if options:
        entry["OPTIONS"] = dict(options)
    return entry


def admins_from_csv(value: str | None) -> list[tuple[str, str]]:
    """Parse an ``ADMINS`` list from a delimited string.

    Accepts ``"Name <a@b.com>, Other <c@d.com>"`` or a bare comma/semicolon
    list of addresses (``"a@b.com,c@d.com"``). Returns ``(name, email)`` pairs
    in Django's ``ADMINS`` shape; the name defaults to the local part.
    """
    if not value:
        return []
    admins: list[tuple[str, str]] = []
    for raw in value.replace(";", ",").split(","):
        entry = raw.strip()
        if not entry:
            continue
        if "<" in entry and entry.endswith(">"):
            name, _, rest = entry.partition("<")
            email = rest[:-1].strip()
            label = name.strip() or email.split("@")[0]
        else:
            email = entry
            label = email.split("@")[0]
        admins.append((label, email))
    return admins


def feature_flags(
    source: Mapping[str, Any] | object,
    *,
    prefix: str = "FEATURE_",
    strip_prefix: bool = True,
) -> dict[str, Any]:
    """Collect ``PREFIX_*`` settings into a feature-flag dict.

    Reads from a mapping or a settings-model instance — **never** ``os.environ``
    directly, so values that arrive via Vault/SSM sources are included (reading
    the environment was the bug that made Vault-supplied flags invisible).

    Args:
        source: A mapping of setting name → value, or an object whose
            uppercase attributes are the settings (e.g. a ``BaseSettings``
            instance). Pydantic models are read via ``model_dump()`` when
            available so aliased/derived values are seen.
        prefix: The name prefix that marks a feature flag.
        strip_prefix: When ``True`` the returned keys drop *prefix*
            (``FEATURE_FOO`` → ``FOO``).

    Returns:
        ``{flag_name: value}`` for every setting whose name starts with
        *prefix*.
    """
    items = _as_items(source)
    out: dict[str, Any] = {}
    for name, value in items:
        if name.startswith(prefix) and name.isupper():
            key = name[len(prefix) :] if strip_prefix else name
            out[key] = value
    return out


def _as_items(source: Mapping[str, Any] | object) -> Iterable[tuple[str, Any]]:
    """Return ``(name, value)`` pairs from a mapping or settings object."""
    if isinstance(source, Mapping):
        return source.items()
    dump = getattr(source, "model_dump", None)
    if callable(dump):
        dumped: dict[str, Any] = dump()
        return dumped.items()
    # Fall back to public uppercase attributes on an arbitrary object.
    return [
        (name, getattr(source, name))
        for name in dir(source)
        if name.isupper() and not name.startswith("_")
    ]
