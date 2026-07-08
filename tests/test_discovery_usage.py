"""Tests for discovery.usage — static whole-repo comparison mining."""

from __future__ import annotations

from django_aqueduct.discovery.usage import RangeEvidence, find_usage_candidates


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content)
    return path


def test_finds_equality_candidates(tmp_path):
    _write(
        tmp_path,
        "a.py",
        "from django.conf import settings\n"
        "if settings.ENVIRONMENT == 'dev':\n"
        "    pass\n"
        "elif settings.ENVIRONMENT == 'staging':\n"
        "    pass\n",
    )
    literals, ranges = find_usage_candidates([str(tmp_path)], ["ENVIRONMENT"])
    assert literals["ENVIRONMENT"] == {"dev", "staging"}
    assert ranges == {}


def test_finds_membership_candidates(tmp_path):
    _write(
        tmp_path,
        "a.py",
        "from django.conf import settings\n"
        "if settings.LOG_LEVEL in ('DEBUG', 'INFO', 'WARNING'):\n"
        "    pass\n",
    )
    literals, _ = find_usage_candidates([str(tmp_path)], ["LOG_LEVEL"])
    assert literals["LOG_LEVEL"] == {"DEBUG", "INFO", "WARNING"}


def test_not_equal_is_not_evidence_of_a_valid_value(tmp_path):
    # `!=` means the value is excluded, not that it's a valid candidate --
    # counting it would produce exactly backwards evidence.
    _write(
        tmp_path,
        "a.py",
        "from django.conf import settings\n"
        "if settings.ENVIRONMENT != 'production':\n"
        "    pass\n",
    )
    literals, _ = find_usage_candidates([str(tmp_path)], ["ENVIRONMENT"])
    assert literals == {}


def test_not_in_is_not_evidence_of_valid_values(tmp_path):
    _write(
        tmp_path,
        "a.py",
        "from django.conf import settings\n"
        "if settings.ENVIRONMENT not in ('dev', 'test'):\n"
        "    pass\n",
    )
    literals, _ = find_usage_candidates([str(tmp_path)], ["ENVIRONMENT"])
    assert literals == {}


def test_not_equal_alongside_equal_only_counts_the_equal(tmp_path):
    _write(
        tmp_path,
        "a.py",
        "from django.conf import settings\n"
        "if settings.ENVIRONMENT == 'dev':\n    pass\n"
        "if settings.ENVIRONMENT != 'production':\n    pass\n",
    )
    literals, _ = find_usage_candidates([str(tmp_path)], ["ENVIRONMENT"])
    assert literals["ENVIRONMENT"] == {"dev"}


def test_literal_reverse_operand_order(tmp_path):
    _write(
        tmp_path,
        "a.py",
        "from django.conf import settings\n"
        "if 'dev' == settings.ENVIRONMENT:\n"
        "    pass\n",
    )
    literals, _ = find_usage_candidates([str(tmp_path)], ["ENVIRONMENT"])
    assert literals["ENVIRONMENT"] == {"dev"}


def test_getattr_form_detected(tmp_path):
    _write(
        tmp_path,
        "a.py",
        "if getattr(settings, 'ENVIRONMENT', None) == 'prod':\n    pass\n",
    )
    literals, _ = find_usage_candidates([str(tmp_path)], ["ENVIRONMENT"])
    assert literals["ENVIRONMENT"] == {"prod"}


def test_finds_simple_range_bound(tmp_path):
    _write(
        tmp_path,
        "a.py",
        "from django.conf import settings\nif settings.TIMEOUT > 0:\n    pass\n",
    )
    _, ranges = find_usage_candidates([str(tmp_path)], ["TIMEOUT"])
    assert ranges["TIMEOUT"] == RangeEvidence(gt=0)


def test_finds_chained_range_bound(tmp_path):
    _write(
        tmp_path,
        "a.py",
        "from django.conf import settings\n"
        "if not (0 < settings.TIMEOUT <= 3600):\n"
        "    raise ValueError\n",
    )
    _, ranges = find_usage_candidates([str(tmp_path)], ["TIMEOUT"])
    assert ranges["TIMEOUT"] == RangeEvidence(gt=0, le=3600)


def test_reversed_operand_range_bound(tmp_path):
    # `3600 >= settings.TIMEOUT` <=> `settings.TIMEOUT <= 3600`
    _write(
        tmp_path,
        "a.py",
        "from django.conf import settings\nif 3600 >= settings.TIMEOUT:\n    pass\n",
    )
    _, ranges = find_usage_candidates([str(tmp_path)], ["TIMEOUT"])
    assert ranges["TIMEOUT"] == RangeEvidence(le=3600)


def test_range_evidence_keeps_tightest_bound(tmp_path):
    _write(
        tmp_path,
        "a.py",
        "from django.conf import settings\n"
        "if settings.TIMEOUT > 0:\n    pass\n"
        "if settings.TIMEOUT > 5:\n    pass\n"
        "if settings.TIMEOUT < 100:\n    pass\n"
        "if settings.TIMEOUT < 60:\n    pass\n",
    )
    _, ranges = find_usage_candidates([str(tmp_path)], ["TIMEOUT"])
    # tightest lower bound is 5 (largest gt), tightest upper bound is 60 (smallest lt)
    assert ranges["TIMEOUT"] == RangeEvidence(gt=5, lt=60)


def test_bool_is_not_treated_as_range_bound(tmp_path):
    _write(
        tmp_path,
        "a.py",
        "from django.conf import settings\nif settings.FLAG > True:\n    pass\n",
    )
    _, ranges = find_usage_candidates([str(tmp_path)], ["FLAG"])
    assert ranges == {}


def test_unrelated_settings_name_ignored(tmp_path):
    _write(
        tmp_path,
        "a.py",
        "from django.conf import settings\nif settings.OTHER == 'x':\n    pass\n",
    )
    literals, ranges = find_usage_candidates([str(tmp_path)], ["ENVIRONMENT"])
    assert literals == {}
    assert ranges == {}


def test_non_settings_variable_named_the_same_is_ignored(tmp_path):
    _write(
        tmp_path,
        "a.py",
        "widget_settings = {'ENVIRONMENT': 'x'}\n"
        "if widget_settings['ENVIRONMENT'] == 'dev':\n    pass\n",
    )
    literals, _ = find_usage_candidates([str(tmp_path)], ["ENVIRONMENT"])
    assert literals == {}


def test_syntax_error_file_is_skipped(tmp_path):
    _write(tmp_path, "broken.py", "def (:\n")
    _write(
        tmp_path,
        "ok.py",
        "from django.conf import settings\n"
        "if settings.ENVIRONMENT == 'dev':\n"
        "    pass\n",
    )
    literals, _ = find_usage_candidates([str(tmp_path)], ["ENVIRONMENT"])
    assert literals["ENVIRONMENT"] == {"dev"}


def test_scan_single_file_path(tmp_path):
    path = _write(
        tmp_path,
        "a.py",
        "from django.conf import settings\n"
        "if settings.ENVIRONMENT == 'dev':\n"
        "    pass\n",
    )
    literals, _ = find_usage_candidates([str(path)], ["ENVIRONMENT"])
    assert literals["ENVIRONMENT"] == {"dev"}


def test_migrations_directory_is_skipped(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    _write(
        migrations,
        "0001_initial.py",
        "from django.conf import settings\n"
        "if settings.ENVIRONMENT == 'dev':\n"
        "    pass\n",
    )
    literals, _ = find_usage_candidates([str(tmp_path)], ["ENVIRONMENT"])
    assert literals == {}


def test_range_evidence_is_empty_by_default():
    assert RangeEvidence().is_empty() is True
    assert RangeEvidence(gt=0).is_empty() is False
