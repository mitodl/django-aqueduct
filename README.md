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

> **Security note:** `--modules` inspection imports your settings module and
> reads each setting's *live, resolved* value — i.e. whatever your
> environment supplies, not the static default in your source code. Fields
> whose name looks secret-like (`SECRET`, `PASSWORD`, `TOKEN`, `API_KEY`,
> etc.) are redacted automatically (rendered as `default=None`), but review
> the generated file for any other sensitive values before committing it.
> Prefer running the generator against a dev/CI environment with dummy
> values, never against a real production or staging environment.

### Step 2 — Refine the scaffold

Open `settings_model.py` and:

- Fix any `# TODO: refine type` annotations
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

2. **Review** `settings_model.py` — fix `# TODO: refine type` entries,
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
