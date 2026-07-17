"""Fixture exercising codegen v2 static-discovery failure classes.

Each block below corresponds to a confirmed v1 failure the static AST pass must
handle: EXPR defaults (timedelta/Path/Decimal), env-var aliases, required-ness,
conditional branches, secret redaction, multiline descriptions, and strings
containing ``<``.
"""

import datetime
import os
import pathlib
from decimal import Decimal
from decimal import Decimal as Dec

# ---- literals ----
SITE_NAME = "My Django App"
DEBUG = False
MAX_CONNECTIONS = 100
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

# A legitimate string containing an angle bracket. v1's `"<" in repr`
# heuristic silently replaced this with None.
XML_PREAMBLE = "<?xml version='1.0'?>"

# ---- EXPR defaults (v1 raised NameError / dropped these) ----
SESSION_AGE = datetime.timedelta(days=14)
DATA_DIR = pathlib.Path("/var/data")
DEFAULT_PRICE = Decimal("9.99")

# ---- env readers: alias + type + required-ness ----
# os.environ[...] raises on a missing key → required.
SECRET_KEY = os.environ["SECRET_KEY"]
APP_BASE_URL = os.environ["APP_BASE_URL"]
# os.getenv with a default → optional with that default.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
# os.getenv with no default → optional, None (v1 wrongly marked this required).
EXTRA_HOST = os.getenv("EXTRA_HOST")
# Env reader whose literal fallback contradicts the reader's str type: the field
# is a bool toggle (env strings coerce), so the annotation must follow the
# default's type — `str = False` would fail validation against its own default.
FEATURE_TOGGLE = os.environ.get("FEATURE_TOGGLE", False)

# Typed reader with BOTH a default and an explicit required=True: the explicit
# flag must win (env is only referenced statically; this file is never imported).
REQUIRED_WITH_DEFAULT = env.get_string(  # noqa: F821
    "REQUIRED_WITH_DEFAULT", default="fallback", required=True
)

# mitol EnvParser's get_list_of_str reader must be typed list[str] (and get
# NoDecode + the container decoder), not fall through to Any.
CORS_ALLOWED_ORIGINS = env.get_list_of_str(  # noqa: F821
    "CORS_ALLOWED_ORIGINS", default=[]
)

# Real description above a standalone pragma line.
# noqa: E501
PRAGMA_ABOVE = 1

# ---- aliased import: the `as` alias must survive into the generated import ----
ALT_PRICE = Dec("1.50")

# ---- builtin cast is NOT an env reader (no bogus alias / required flag) ----
POOL_SIZE = int("10")

# ---- tuple unpacking must not be silently dropped ----
LANG_CODE, TZ_NAME = "en", "UTC"

# ---- EXPR referencing a module-local name → DERIVED (cannot be reproduced) ----
DERIVED_URL = APP_BASE_URL + "/api"

# ---- conditional branch: must be DERIVED, not a frozen snapshot ----
if DEBUG:
    CACHE_BACKEND = "locmem"

    def _make_helper():
        NESTED_LOCAL = 1  # a function local, NOT a setting
        return NESTED_LOCAL
else:
    CACHE_BACKEND = "redis"
