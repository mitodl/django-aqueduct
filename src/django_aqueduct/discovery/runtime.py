"""Optional multi-snapshot runtime enrichment (``--enrich-runtime``).

Static discovery (:mod:`~django_aqueduct.discovery.static`) never imports the
target settings module, which is what makes it deterministic and secret-safe
— but it also means it can never know the *shape* of a value that is
computed rather than written as a literal (``DATABASES =
dj_database_url.parse(DATABASE_URL)``) or the *set of values a field is
actually constrained to* (an environment name that is always one of
``"dev"``/``"staging"``/``"production"``, say). Both of those require an
evaluated value, and no amount of cleverer AST analysis produces a value that
was never written as a literal.

This module imports the target module(s) — once per supplied env
snapshot — purely to observe values. It carries the same category of risk v1
(and its ``ModuleInspector``) carried, and the same hard boundary the
project's Phase C notes drew around this trade: the observed values are
**only** used to refine a :class:`~django_aqueduct.discovery.ir.TypeRef`
(dict shape via genson, closed scalar sets via ``Literal``), never to author
a :class:`~django_aqueduct.discovery.ir.Default`, required-ness, or env
aliases — that would reintroduce the secret-leak / frozen-branch / baked-
machine-value failure classes v2 was built to eliminate. See
:mod:`~django_aqueduct.discovery.enrich` for where the boundary is enforced.

Off by default; the caller supplies explicit env snapshots (each is a full
overlay onto ``os.environ`` for the duration of one import), so a single
invocation samples exactly what the caller asked for and nothing else.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import sys
from collections.abc import Iterator, Sequence
from typing import Any


class RuntimeSamplingError(Exception):
    """Raised when a module cannot be imported under a given env snapshot."""


def parse_env_file(text: str) -> dict[str, str]:
    """Parse a minimal ``.env``-style ``KEY=VALUE`` file into a dict.

    One assignment per line. Blank lines and lines starting with ``#`` are
    ignored. A value wrapped in a single matching pair of quotes has them
    stripped; nothing else about the value is interpreted (no escaping,
    no variable expansion) — this deliberately stays simple enough to audit
    at a glance, matching this module's minimal-trust posture.
    """
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            result[key] = value
    return result


@contextlib.contextmanager
def _env_overlay(overrides: dict[str, str]) -> Iterator[None]:
    """Temporarily overlay *overrides* onto ``os.environ``, restoring after."""
    saved = dict(os.environ)
    try:
        os.environ.update(overrides)
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _import_fresh(module_path: str) -> object:
    """Import *module_path*, forcing re-execution even if already cached.

    A settings module commonly reads env vars only at import time; without
    dropping it from ``sys.modules`` first, a second sample under different
    env overrides would just return the first sample's cached module.
    """
    sys.modules.pop(module_path, None)
    try:
        return importlib.import_module(module_path)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeSamplingError(
            f"Failed to import {module_path!r} under this env snapshot: {exc}"
        ) from exc


def sample_module_values(
    module_paths: Sequence[str],
    env_snapshots: Sequence[dict[str, str]],
) -> list[dict[str, Any]]:
    """Import *module_paths* once per snapshot in *env_snapshots*, observing values.

    Within one snapshot, later modules in *module_paths* override earlier
    ones (matching :class:`~django_aqueduct.discovery.static.StaticModuleInspector`'s
    override order). ``os.environ`` is restored after each snapshot even if
    the import raises.

    Args:
        module_paths: Dotted settings module paths, later-overrides-earlier.
        env_snapshots: One dict of env-var overrides per sample. An empty
            sequence samples nothing (returns ``[]``) — this function never
            samples the ambient environment implicitly; callers decide.

    Returns:
        One ``{UPPERCASE_NAME: value}`` dict per snapshot, in the same order
        as *env_snapshots*.

    Raises:
        RuntimeSamplingError: If a module fails to import under a snapshot.
    """
    samples: list[dict[str, Any]] = []
    for overrides in env_snapshots:
        with _env_overlay(overrides):
            by_name: dict[str, Any] = {}
            for module_path in module_paths:
                module = _import_fresh(module_path)
                for name in dir(module):
                    if name.isupper():
                        by_name[name] = getattr(module, name)
            samples.append(by_name)
    return samples
