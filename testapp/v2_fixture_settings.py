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
SECRET_KEY = os.environ["SECRET_KEY"]
APP_BASE_URL = os.getenv("APP_BASE_URL")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ---- conditional branch: must be DERIVED, not a frozen snapshot ----
if DEBUG:
    CACHE_BACKEND = "locmem"
else:
    CACHE_BACKEND = "redis"
