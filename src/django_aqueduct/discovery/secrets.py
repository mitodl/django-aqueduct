"""Secret-name heuristics shared across discovery inspectors.

Static discovery never resolves a value, so this name-marker check is a
belt-and-suspenders guard (redact anything whose *name* looks secret-like)
rather than the primary defence against leaking real credentials.
"""

from __future__ import annotations

# Substrings that mark a settings name as likely holding a secret.
_SENSITIVE_NAME_MARKERS: tuple[str, ...] = (
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "TOKEN",
    "PRIVATE_KEY",
    "API_KEY",
    "APIKEY",
    "ACCESS_KEY",
    "CREDENTIAL",
    "SIGNING_KEY",
    "ENCRYPTION_KEY",
    "DSN",
    "DATABASE_URL",
    "REDIS_URL",
    "BROKER_URL",
)


def looks_secret(name: str) -> bool:
    """Return True if *name* contains a substring commonly used for secrets."""
    return any(marker in name for marker in _SENSITIVE_NAME_MARKERS)
