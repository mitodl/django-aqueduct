# django-aqueduct

Structured, typed, auditable Django settings management powered by Pydantic.

`django-aqueduct` channels configuration from multiple sources — environment variables, YAML files, HashiCorp Vault, AWS SSM Parameter Store — into a single typed, validated model, making settings auditable and K8s-friendly without changing any application code.

[![CI](https://github.com/mitodl/django-aqueduct/actions/workflows/ci.yml/badge.svg)](https://github.com/mitodl/django-aqueduct/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/django-aqueduct)](https://pypi.org/project/django-aqueduct/)
[![Python](https://img.shields.io/pypi/pyversions/django-aqueduct)](https://pypi.org/project/django-aqueduct/)
[![License: BSD-3-Clause](https://img.shields.io/badge/License-BSD%203--Clause-blue.svg)](LICENSE)

---

## Installation

```bash
pip install django-aqueduct

# Optional extras
pip install django-aqueduct[vault]   # HashiCorp Vault support (hvac)
pip install django-aqueduct[aws]     # AWS SSM Parameter Store (boto3)
pip install django-aqueduct[mitol]   # mitol-django-common EnvParser integration
```

Add to `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    ...
    "django_aqueduct",
]
```

---

## Quickstart

### Step 1 — Generate a scaffold

Point `generate_aqueduct_settings` at your existing settings module:

```bash
python manage.py generate_aqueduct_settings \
    --modules myapp.settings.common \
    --output src/myapp/settings_model.py
```

This emits a typed `AqueductSettings(BaseSettings)` class with every
`UPPERCASE` name from your settings module as a Pydantic field, grouped
under section comments by source module.

> **Security note:** `--modules` discovery reads your settings module's
> *source* via AST — it never imports it, so it's safe to run against any
> environment (there's no live env-var value for it to leak). Fields whose
> name looks secret-like (`SECRET`, `PASSWORD`, `TOKEN`, `API_KEY`, etc.) are
> still redacted automatically (rendered as `default=None`) in case a
> literal secret was hardcoded in source; review the generated file for any
> other sensitive literals before committing it. The only flag that imports
> anything is the opt-in `--enrich-runtime` (see below) — everything else in
> this Quickstart is static-only.

### Step 2 — Refine the scaffold

Open `settings_model.py` and:

- Fix any `# refine type` annotations
- Add `model_validator` methods to derive complex objects from primitives:

```python
from pydantic import model_validator
import dj_database_url

class AqueductSettings(BaseSettings):
    DATABASE_URL: str = Field(default="sqlite:///db.sqlite3")

    # Derived — populated by the validator below
    DATABASES: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def build_databases(self) -> "AqueductSettings":
        self.DATABASES = {"default": dj_database_url.parse(self.DATABASE_URL)}
        return self
```

#### Overriding a generated field

To refine one field — a narrower type, a different default, extra validation —
re-declare it in the `# >>> aqueduct:preserved:validators` region (or anywhere
else outside a generated region):

```python
    # >>> aqueduct:preserved:validators
    POOL_SIZE: int = Field(default=10, gt=0)
    # <<< aqueduct:preserved:validators
```

Regeneration notices the hand-written declaration and **omits its own**, so the
class keeps exactly one class-level assignment per name — two would be a real
`PIE794`/`F811` finding on the generated line, and one a project can't silence
without excluding the whole file. Omitted fields drop out of every derived
region too (imports, container decoders, URL serializers, `TypedDict`s), so
taking over the last field that used an import doesn't strand it as `F401`.
They're listed at the end of the fields region:

```python
    # ===== declared outside this region =====
    # These settings were discovered, but this class already
    # declares them elsewhere in the file. Their generated
    # declarations are omitted so each name has exactly one
    # class-level assignment. Delete the hand-written one to hand
    # a setting back to the generator.
    #   POOL_SIZE
```

Deleting your declaration hands the field back to the generator on the next
run. `--reset` discards preserved regions entirely, so every field returns to
generated form.

### Step 2b — Optional: enrich types automatically

Static discovery can only see what's written literally in source, so a
computed value (`DATABASES = dj_database_url.parse(...)`) or a value that's
only ever one of a handful of options (`ENVIRONMENT` always being
`"dev"`/`"staging"`/`"production"`) shows up as `Any`/`str` with a
`# refine type` — the kind of thing you'd otherwise only discover by
trial-and-error or reading the whole codebase. Two optional passes recover it
automatically, refining *types only* (never a field's default, required-ness,
or env aliases):

```bash
python manage.py generate_aqueduct_settings \
    --modules myapp.settings \
    --enrich-runtime \
    --runtime-env-file .env.dev --runtime-env-file .env.staging --runtime-env-file .env.prod \
    --enrich-usage src/myapp \
    --output src/myapp/settings_model.py
```

- **`--enrich-runtime`** imports your settings module once per
  `--runtime-env-file` snapshot (a `.env`-style file) and observes the actual
  values. A dict whose default was never a literal gets real genson-inferred
  `TypedDict` shape; a scalar field observed to take only a small, stable set
  of values across snapshots is promoted to `Literal[...]`; a string that
  looks like a URL is promoted to `pydantic.AnyUrl`. Without any
  `--runtime-env-file`, it samples once under the current process
  environment. **This is the one flag that executes code** — only point it at
  modules and env files you trust.
- **`--enrich-usage <path>`** never executes anything: it's a plain AST scan
  of the given file/directory for `settings.X` comparisons in your app code —
  `if settings.LOG_LEVEL == "DEBUG":`, `if not (0 < TIMEOUT <= 3600): raise`
  — and promotes closed-value-set fields to `Literal[...]` and range-checked
  numeric fields to `Field(gt=/ge=/lt=/le=)`.
- Both are heuristics, not proofs — the renderer marks every result
  `# refine type` (`Literal`/`AnyUrl`) or a `# usage-mined bound(s) —
  confirm before trusting` comment (ranges) so you review before trusting
  the inferred constraint in production; the scanned code only reflects the
  values/bounds it happens to check, not necessarily the field's full valid
  domain.
- **`--enrich-url-types`** is a separate, static, opt-in flag: a `str` field
  whose literal default actually validates as an absolute `pydantic.AnyUrl`
  is promoted (and paired with a `field_serializer` so `model_dump()` keeps
  emitting `str`); a field with no literal value to check (required, derived,
  or `None`) falls back to its name ending in `_URL`/`_URI`, minus a denylist
  of Django settings that are conventionally relative (`STATIC_URL`,
  `MEDIA_URL`, `LOGIN_URL`, `LOGIN_REDIRECT_URL`, `LOGOUT_REDIRECT_URL`, ...).
  It's opt-in because the name-fallback path can still promote a required
  field whose real env value turns out to be relative — review any resulting
  `AnyUrl` before trusting it in production.

### Step 3 — Wire the shim

Replace your host settings file with a thin shim:

```python
# myapp/settings/production.py
from django_aqueduct import configure_django_settings
from myapp.settings_model import AqueductSettings

configure_django_settings(AqueductSettings)
```

That's it. `DJANGO_SETTINGS_MODULE` stays the same. All existing
`django.conf.settings.FOO` access in application code continues to work
with zero changes.

---

## Kubernetes deployment pattern

In Kubernetes, configuration typically arrives from multiple sources:

| Source | Typical content |
|--------|----------------|
| Pod environment variables | Non-secret config from ConfigMaps |
| Vault (Kubernetes SA auth) | Database passwords, API keys |
| AWS SSM Parameter Store | Secrets in AWS-hosted deployments |

Configure all three in your settings model:

```python
from django_aqueduct import configure_django_settings
from django_aqueduct.sources.vault import VaultSettingsSource
from django_aqueduct.sources.aws_ssm import AWSParameterStoreSource
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProductionSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="allow")

    SECRET_KEY: str = Field(...)
    DATABASE_URL: str = Field(...)

    @classmethod
    def settings_customise_sources(cls, settings_cls, **kwargs):
        return (
            # 1. Environment variables (from K8s ConfigMaps)
            kwargs["env_settings"],
            # 2. Vault via Kubernetes SA — reads JWT from default mount path
            #    /var/run/secrets/kubernetes.io/serviceaccount/token
            VaultSettingsSource(
                settings_cls,
                vault_url="https://vault.example.com",
                vault_path="myapp/production",
                auth_method="kubernetes",
                role="myapp",
                # Optional: custom JWT path for projected service accounts
                # jwt_path="/var/run/secrets/custom/token",
            ),
        )


# myapp/settings/production.py
configure_django_settings(ProductionSettings)
```

### Vault authentication methods

| Method | When to use |
|--------|-------------|
| `"token"` | Local dev, CI with a static token |
| `"oidc"` | Interactive / browser-based login |
| `"kubernetes"` | Production K8s — uses the pod's service account JWT |

```python
# Token auth (dev/CI)
VaultSettingsSource(settings_cls, ..., auth_method="token", vault_token="s.xxx")

# OIDC (interactive)
VaultSettingsSource(settings_cls, ..., auth_method="oidc", role="myapp")

# Kubernetes SA (production) — custom JWT path
VaultSettingsSource(
    settings_cls,
    ...,
    auth_method="kubernetes",
    role="myapp",
    jwt_path="/var/run/secrets/tokens/vault",  # projected SA token
)
```

### AWS SSM Parameter Store

```python
from django_aqueduct.sources.aws_ssm import AWSParameterStoreSource

# All parameters under /myapp/production/ are fetched with full pagination.
# The prefix is stripped: /myapp/production/SECRET_KEY → SECRET_KEY
AWSParameterStoreSource(
    settings_cls,
    path_prefix="/myapp/production/",
    region_name="us-east-1",
)
```

---

## Adapter modes

### Option A — Shim settings file (recommended)

`DJANGO_SETTINGS_MODULE` stays unchanged. The settings file becomes a
thin shim:

```python
# myapp/settings/production.py
from django_aqueduct import configure_django_settings
from myapp.settings_model import ProductionSettings

configure_django_settings(ProductionSettings)
```

Works with gunicorn, Celery, pytest-django, management commands, and every
other tool that reads `DJANGO_SETTINGS_MODULE` — no changes required.

### Option B — Programmatic configure (greenfield)

For new projects or container-native apps where you control all entry points
and want no `DJANGO_SETTINGS_MODULE`:

```python
# manage.py or WSGI/ASGI entry point — call before django.setup()
from django_aqueduct import configure_django_programmatic
from myapp.settings_model import AppSettings

configure_django_programmatic(AppSettings)

import django
django.setup()
```

---

## edx-platform migration walkthrough

edx-platform's `lms/envs/production.py` currently loads a YAML file and
applies hundreds of lines of post-processing. With `django-aqueduct`:

1. **Generate the scaffold** from `common.py`:

   ```bash
   python manage.py generate_aqueduct_settings \
       --modules lms.envs.common \
       --output lms/envs/settings_model.py
   ```

2. **Review** `settings_model.py` — fix `# refine type` entries,
   move `derive_settings` logic into `@model_validator` methods.

3. **Replace** `lms/envs/production.py`:

   ```python
   # lms/envs/production.py
   from django_aqueduct import configure_django_settings
   from lms.envs.settings_model import LMSSettings

   configure_django_settings(LMSSettings)
   ```

4. Set `DJANGO_SETTINGS_MODULE=lms.envs.production` as before.
   All LMS app code using `from django.conf import settings` is unchanged.

---

## `[mitol]` extra — EnvParser integration

If your project uses `mitol-django-common`'s `EnvParser`, install the
`[mitol]` extra and pass `--include-envparser` to the generator:

```bash
pip install django-aqueduct[mitol]

python manage.py generate_aqueduct_settings \
    --modules myapp.settings \
    --include-envparser
```

The `EnvParserInspector` reads the global `env._configured_vars` registry
and emits precisely-typed fields for every `get_string`/`get_bool`/`get_int`
call, preserving `description`, `required`, and `dev_only` metadata.

---

## Dependency-surface report

`generate_aqueduct_settings` only sees settings your *project* writes. A setting
a dependency reads with its own internal default that you never set is invisible
— there's no field and nothing to decide about. The `report_settings_surface`
command makes that surface visible: it enumerates, per installed dependency, the
settings it introduces (name, type, package default) and reconciles each against
what your project sets.

```bash
python manage.py report_settings_surface
python manage.py report_settings_surface --format markdown
python manage.py report_settings_surface --format json > surface.json
python manage.py report_settings_surface --packages djangorestframework,celery
```

Each row is classified as `set` (you define it — value shown when statically
known), `overridden` (your value differs from the package default), or `unset`,
with a hint column: `REVIEW` (unset with a meaningful default — a decision to
make), `OK` (you've decided), or `SECRET` (secret-shaped name, value redacted).

```
PACKAGE          SETTING                TYPE  DEFAULT  PROJECT            HINT
django-storages  AWS_S3_FILE_OVERWRITE  bool  True     unset              REVIEW
django-storages  AWS_QUERYSTRING_AUTH   bool  True     overridden: False  OK
```

It's a decision aid — it writes no model and adds nothing to your generated
file, so `generate_aqueduct_settings --check` drift output stays clean. Surface
data comes from packages that declare a surface (below), plus built-in knowledge
of Django, DRF, and Celery scoped to your `INSTALLED_APPS`. Output is
deterministic and secret-shaped names are always redacted.

Configure defaults in `[tool.aqueduct]` (command flags override them):

```toml
[tool.aqueduct]
dependency_surface_report_format = "markdown"
dependency_surface_packages = ["djangorestframework", "celery"]
```

### Declaring a surface from your own package

Any package can advertise the settings it introduces with the import-light
`django_aqueduct.surface.Setting` dataclass (stdlib-only — it imports neither
Django nor pydantic) and one entry point:

```python
# my_package/aqueduct_surface.py
from django_aqueduct.surface import UNSET, Setting


def surface() -> list[Setting]:
    return [
        Setting("MY_PACKAGE_FROM_EMAIL", type="str", default="",
                description="Envelope From for outbound mail."),
        Setting("MY_PACKAGE_REPLY_TO", type="str | None", default=None,
                description="Optional Reply-To address."),
        Setting("MY_PACKAGE_API_TOKEN", type="str", default=UNSET, required=True,
                description="Required API token; the project must supply it."),
    ]
```

```toml
# my_package's pyproject.toml
[project.entry-points."django_aqueduct.settings_surface"]
my-package = "my_package.aqueduct_surface:surface"
```

`default=UNSET` means "no default declared" (distinct from `default=None`, where
the default *is* `None`); pair it with `required=True` when the project must
supply a value. Declared surfaces are authoritative — they win over built-in
extractors on a name collision.

See [ADR-0001](docs/architecture/decisions/0001-dependency-surface-discovery.md)
for the design and rationale. Opt-in emission of unset dependency settings into
the model is a planned follow-up.

## Contributing

```bash
git clone https://github.com/mitodl/django-aqueduct
cd django-aqueduct
uv sync
uv run pytest
uv run mypy src/django_aqueduct
```

Install pre-commit hooks with [prek](https://prek.j178.dev):

```bash
pip install prek
prek install
```

Please open an issue before submitting a pull request for significant changes.

### Releasing

Bump `version` in `pyproject.toml` and add a matching entry to `CHANGELOG.md`
in the same PR. Once merged to `main`, the "Tag release" workflow pushes a
`vX.Y.Z` tag and directly invokes "Publish to PyPI" as a reusable workflow —
it does not rely on the tag push itself to trigger publishing, since a push
made with the default `GITHUB_TOKEN` does not fire other workflows' `push`
triggers.

**PyPI Trusted Publisher / workflow filename coupling:** PyPI verifies the
OIDC certificate's "Build Config URI" against the *top-level* (caller)
workflow filename, not the reusable workflow that actually runs the publish
step. Because "Tag release" invokes `publish.yml` via `uses:`, the
certificate names `tag-release.yml`, not `publish.yml`. If the PyPI project's
Trusted Publisher is only configured for `publish.yml`, every tag-triggered
release fails at upload with an error like:

```
Certificate's Build Config URI (.github/workflows/tag-release.yml@refs/heads/main)
does not match expected Trusted Publisher (publish.yml @ mitodl/django-aqueduct)
```

On pypi.org → project → Publishing, this repo needs **two** trusted
publishers registered against the `pypi` environment: one for
`tag-release.yml` (the automatic path) and one for `publish.yml` (manual
`workflow_dispatch`). If a release fails with the certificate mismatch above,
the immediate unblock is a direct dispatch, which goes through `publish.yml`
as the top-level workflow and matches the existing publisher:

```
gh workflow run publish.yml --ref vX.Y.Z
```

---

## License

BSD-3-Clause © MIT Open Learning Engineering
