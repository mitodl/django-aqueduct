"""Tests for the reusable derivation helpers."""

from __future__ import annotations

import pytest

from django_aqueduct import derivations as dv


class _Model:
    """Stand-in settings object exposing model_dump()."""

    def __init__(self, values: dict) -> None:
        self._values = values

    def model_dump(self) -> dict:
        return dict(self._values)


# ---- database_config ----


def test_database_config_postgres() -> None:
    cfg = dv.database_config("postgres://u:p@host:5432/db")
    assert "postgresql" in cfg["ENGINE"]
    assert cfg["NAME"] == "db"


def test_database_config_ssl_require_adds_sslmode() -> None:
    cfg = dv.database_config("postgres://u:p@host:5432/db", ssl_require=True)
    assert cfg["OPTIONS"]["sslmode"] == "require"


def test_database_config_sqlite_never_gets_sslmode() -> None:
    # The shipped odl-video-service bug: sslmode must not be applied to sqlite.
    cfg = dv.database_config("sqlite:///db.sqlite3", ssl_require=True)
    assert "sslmode" not in (cfg.get("OPTIONS") or {})


def test_database_config_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dv, "_require_dj_database_url", lambda: (_ for _ in ()).throw(ImportError("x"))
    )
    with pytest.raises(ImportError):
        dv.database_config("postgres://u:p@h/db")


# ---- first_url ----


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((None, "", "redis://a"), "redis://a"),
        (("redis://first", "redis://second"), "redis://first"),
        ((None, "", None), None),
    ],
)
def test_first_url(args: tuple, expected: str | None) -> None:
    assert dv.first_url(*args) == expected


# ---- redis_cache ----


def test_redis_cache_shape() -> None:
    entry = dv.redis_cache("redis://h:6379/0")
    assert entry["BACKEND"] == "django_redis.cache.RedisCache"
    assert entry["LOCATION"] == "redis://h:6379/0"
    assert "OPTIONS" not in entry


def test_redis_cache_options() -> None:
    entry = dv.redis_cache("redis://h", CLIENT_CLASS="x")
    assert entry["OPTIONS"] == {"CLIENT_CLASS": "x"}


# ---- admins_from_csv ----


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", []),
        (None, []),
        ("a@b.com", [("a", "a@b.com")]),
        ("Alice <a@b.com>, Bob <b@c.com>", [("Alice", "a@b.com"), ("Bob", "b@c.com")]),
        ("a@b.com;c@d.com", [("a", "a@b.com"), ("c", "c@d.com")]),
    ],
)
def test_admins_from_csv(value: str | None, expected: list) -> None:
    assert dv.admins_from_csv(value) == expected


# ---- feature_flags ----


def test_feature_flags_from_dict_strips_prefix() -> None:
    flags = dv.feature_flags(
        {"FEATURE_A": True, "FEATURE_B": False, "DEBUG": True, "feature_c": 1}
    )
    assert flags == {"A": True, "B": False}


def test_feature_flags_keep_prefix() -> None:
    flags = dv.feature_flags({"FEATURE_A": True}, strip_prefix=False)
    assert flags == {"FEATURE_A": True}


def test_feature_flags_from_model_dump() -> None:
    # Reads model values (incl. Vault-sourced) rather than os.environ.
    flags = dv.feature_flags(_Model({"FEATURE_X": 1, "OTHER": 2}))
    assert flags == {"X": 1}


def test_feature_flags_custom_prefix() -> None:
    flags = dv.feature_flags({"FF_A": 1, "FEATURE_B": 2}, prefix="FF_")
    assert flags == {"A": 1}


def test_feature_flags_non_dict_mapping() -> None:
    # Any Mapping (not just dict) is read via .items(), not attribute lookup.
    from types import MappingProxyType

    flags = dv.feature_flags(MappingProxyType({"FEATURE_A": True, "X": 1}))
    assert flags == {"A": True}


def test_database_config_empty_url_no_stray_options() -> None:
    # An empty URL parses to {}; ssl_require must not attach a bogus OPTIONS.
    assert dv.database_config("", ssl_require=True) == {}


def test_first_url_strips_whitespace() -> None:
    assert dv.first_url("   ", "  redis://h  ") == "redis://h"
