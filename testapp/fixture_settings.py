"""Fixture settings module used as codegen input in tests.

Contains a representative spread of UPPERCASE names with varied value types
so tests can assert correct type inference and field generation.
"""

import pathlib

# str
SITE_NAME = "My Django App"

# str (empty)
API_KEY = ""

# bool
DEBUG = False
ENABLE_FEATURE_X = True

# int
MAX_CONNECTIONS = 100

# float
RATE_LIMIT = 1.5

# list
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

# dict (enriched by genson when available)
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# None — should produce Any + needs_refinement
OPTIONAL_SETTING = None

# tuple — OPAQUE: produces tuple[Any, ...] annotation
SECURE_PROXY_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# set — OPAQUE: produces set[Any] annotation
EXCLUDED_FIELDS = {"email", "username"}

# pathlib.Path — should produce str annotation
DATA_DIR = pathlib.Path("/var/data")

# Private / non-uppercase names that should NOT be discovered
_private = "ignored"
not_uppercase = "also ignored"
