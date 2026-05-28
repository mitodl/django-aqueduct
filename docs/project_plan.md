# Implementation: django-aqueduct

## Context
`django-aqueduct` is a standalone Django library that replaces scattered, untyped settings management with a structured Pydantic-based interface. A management command introspects any Django project's existing settings — from `mitol.common.envs.EnvParser` registries, arbitrary Python settings modules, or both — and generates a typed `BaseSettings` scaffold. Developers refine that scaffold with validators and source configuration, then replace their settings file with a thin shim that instantiates the model and injects its values into Django via `globals().update()`, preserving the `django.conf.settings.FOO` access pattern everywhere. The library ships custom pydantic-settings sources for Vault (token, OIDC, and Kubernetes SA auth) and AWS SSM Parameter Store with full pagination, making it a first-class citizen in Kubernetes environments where secrets and config arrive from multiple external sources.

## Prerequisites

- GitHub repo `github.com/mitodl/django-aqueduct` created with BSD-3-Clause license, default branch `main`
- `uv` installed locally (`uv >= 0.4`)
- `prek` installed locally
- PyPI trusted publishing configured for the `mitodl` org (OIDC, no token needed at setup time)
- Python 3.12, 3.13, and 3.14 available locally (e.g. via `pyenv`)
- Vault instance accessible for manual smoke-testing of OIDC and Kubernetes SA auth paths (can be a local dev Vault in dev mode)

## Tasks

1. **Initialise repo structure and `pyproject.toml`**
   - Clone the new repo locally
   - Create `src/django_aqueduct/`, `tests/`, `testapp/`, `.github/workflows/`
   - Write `pyproject.toml`: `name = "django-aqueduct"`, `license = "BSD-3-Clause"`, `requires-python = ">=3.12"`, core deps (`pydantic>=2.0`, `pydantic-settings>=2.0`, `django>=5.0`), optional extras `[vault]` (hvac), `[aws]` (boto3), `[mitol]` (mitol-django-common), hatchling build backend
   - Run `uv sync` and confirm the environment resolves cleanly
   - **Done:** `uv sync` exits 0, `src/django_aqueduct/` exists

2. **Add `prek.toml` and verify hooks**
   - Write `prek.toml` with built-in hooks (trailing-whitespace, end-of-file-fixer, check-merge-conflict, check-case-conflict, check-added-large-files, check-toml, check-yaml, check-json, detect-private-key) at priority 0; gitleaks + shellcheck + actionlint at priority 10; ruff-format at priority 20; ruff lint at priority 30
   - Add `ruff` configuration to `pyproject.toml` (`line-length = 88`, `select = ["E", "F", "I", "UP", "S"]`, `[tool.ruff.lint.per-file-ignores]` allowing `S101` in tests)
   - Run `prek run --all-files` and fix any findings
   - **Done:** `prek run --all-files` exits 0 on the initial file set

3. **Scaffold the package core**
   - Write `src/django_aqueduct/__init__.py` with placeholder imports and `__version__`
   - Write `src/django_aqueduct/apps.py` with `AqueductConfig(AppConfig)`: `name = "django_aqueduct"`, `verbose_name = "Django Aqueduct"`
   - Add `src/django_aqueduct/py.typed`
   - Create empty `__init__.py` files for `discovery/`, `codegen/`, `sources/`, `management/`, `management/commands/`
   - **Done:** `python -c "import django_aqueduct"` succeeds; `python -c "from django_aqueduct.apps import AqueductConfig"` succeeds

4. **Set up testapp and pytest wiring**
   - Write `testapp/manage.py` and `testapp/settings.py` (minimal: `INSTALLED_APPS = ["django_aqueduct"]`, `DATABASES`, `SECRET_KEY`)
   - Write `testapp/fixture_settings.py` with ~10 known UPPERCASE names of varied types (`str`, `bool`, `int`, `list`, `dict`, `None`) to serve as the codegen fixture
   - Write root `conftest.py` setting `DJANGO_SETTINGS_MODULE = "testapp.settings"`
   - Add `pytest-django`, `pytest`, `mypy`, `django-stubs` to dev deps; add `[tool.pytest.ini_options]` with `pythonpath = ["src", "testapp"]`
   - **Done:** `uv run pytest --collect-only` exits 0 with no errors

5. **Implement `DiscoveredField` and `BaseInspector`**
   - Write `src/django_aqueduct/discovery/base.py`:
     - `@dataclass DiscoveredField` with `name: str`, `type_annotation: str`, `default: Any`, `description: str`, `required: bool`, `source_module: str`, `dev_only: bool`
     - `BaseInspector(Protocol)` with `def discover(self) -> list[DiscoveredField]`
   - Write `tests/test_discovery_base.py` asserting `DiscoveredField` is constructable and fields are accessible
   - **Done:** tests pass; `mypy src/django_aqueduct/discovery/base.py` reports no errors
   - *Depends on: tasks 3, 4*

6. **Implement `type_inference.py`**
   - Write `src/django_aqueduct/discovery/type_inference.py` with `infer_annotation(value: Any) -> tuple[str, bool]`; `bool` checked before `int`; covers `bool`, `int`, `float`, `str`, `list`, `dict`, `None`, and unknown types
   - Write `tests/test_type_inference.py` as a parametrized test over all covered types plus at least two unrecognised types (e.g. `set`, a custom class instance)
   - **Done:** all parametrized cases pass; `needs_refinement=True` for `None` and unrecognised types
   - *Depends on: task 4*

7. **Implement `ModuleInspector`**
   - Write `src/django_aqueduct/discovery/module.py` with `ModuleInspector(module_path: str)` implementing `BaseInspector`; imports via `importlib.import_module`, walks `dir()` for `isupper()` names, calls `infer_annotation` for each, sets `source_module` from `module.__name__`; raises `ImportError` with actionable message including the dotted path on failure
   - Write `tests/test_discovery_module.py` pointing at `testapp.fixture_settings`; assert one `DiscoveredField` per known name, correct `type_annotation`, correct `source_module`, `needs_refinement` True only for `None`-valued names
   - **Done:** all assertions pass against the known fixture
   - *Depends on: tasks 5, 6*

8. **Implement `EnvParserInspector`**
   - Write `src/django_aqueduct/discovery/envparser.py`; guard `mitol.common.envs` import with try/except raising `ImportError` with `[mitol]` extra install hint; map `EnvVariable` to `DiscoveredField` using parser-name-to-annotation mapping (`get_string`→`str`, `get_bool`→`bool`, `get_int`→`int`, `get_list_literal`→`list[Any]`, `get_delimited_list`→`list[str]`, fallback→`Any`)
   - Write `tests/test_discovery_envparser.py` using `unittest.mock.patch` to substitute a known `_configured_vars` dict; assert correct field mapping for each parser type without needing `mitol-django-common` installed
   - **Done:** tests pass without `mitol-django-common` installed; `ImportError` fires with correct message when the guard is triggered
   - *Depends on: tasks 5, 6*

9. **Implement `SettingsModelGenerator`**
   - Write `src/django_aqueduct/codegen/generator.py` with `SettingsModelGenerator(fields: list[DiscoveredField])`; `render() -> str` produces: imports block, `model_config = SettingsConfigDict(env_prefix="", extra="allow")`, `class AqueductSettings(BaseSettings):` with fields grouped under `# ===== source.module =====` section comments, `Field(default=..., description=...)` annotations, `# TODO: refine type` on `needs_refinement` fields, and a `# TODO: add model_validators here` stub
   - Write `tests/test_codegen.py`; assert rendered string contains expected class name, section headers, field names, `Field(` calls, `extra="allow"`, and TODOs; validate output is parseable via `ast.parse`
   - **Done:** `ast.parse(generator.render())` raises no errors; all string assertions pass including `extra="allow"`
   - *Depends on: tasks 5, 6*

10. **Implement the adapter**
    - Write `src/django_aqueduct/adapter.py`:
      - `configure_django_settings(model_class: type[BaseSettings], scope: dict | None = None) -> None` — instantiates the model, calls `(scope or caller_globals).update(instance.model_dump())`; retrieves caller globals via `inspect.currentframe().f_back.f_globals`
      - `configure_django_programmatic(model_class: type[BaseSettings]) -> None` — instantiates the model, calls `django.conf.settings.configure(**instance.model_dump())`
    - Export both from `src/django_aqueduct/__init__.py`
    - Write `tests/test_adapter.py`: for `configure_django_settings` pass explicit `scope={}` and assert all model fields appear; for `configure_django_programmatic` use a subprocess or `_wrapped = None` reset
    - **Done:** both functions tested; public imports work
    - *Depends on: tasks 3, 9*

11. **Implement `VaultSettingsSource` with token, OIDC, and Kubernetes SA auth**
    - Write `src/django_aqueduct/sources/vault.py` with `VaultSettingsSource(BaseSettingsSource)` implementing the pydantic-settings v2 protocol
    - Constructor accepts `vault_url: str`, `vault_path: str`, `mount_point: str = "secret"`, `auth_method: Literal["token", "oidc", "kubernetes"] = "token"` and auth-method-specific kwargs:
      - `vault_token: str | None` for token auth
      - `role: str | None` + `oidc_callback_port: int = 8250` for OIDC
      - `role: str | None` + `jwt_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/token"` for Kubernetes SA — reads the JWT from `jwt_path` at call time if `jwt` kwarg not explicitly provided
    - `__call__` authenticates via the selected method using `hvac`, reads from Vault KV v2 at `vault_path`, returns `dict[str, Any]`
    - Guard `import hvac` with `[vault]` extra install hint
    - Write `tests/test_sources_vault.py` with three test classes, one per auth method; token and OIDC mock `hvac.Client` at their respective auth calls; Kubernetes SA test additionally asserts that `jwt_path` is read from disk when `jwt` is not provided (mock `pathlib.Path.read_text`) and that an explicit `jwt` kwarg bypasses the file read; assert `ImportError` fires when hvac is absent
    - **Done:** all three auth paths tested via mocks; custom `jwt_path` test passes; default path constant matches the standard K8s SA token mount; `ImportError` test passes
    - *Depends on: task 3*

12. **Implement `AWSParameterStoreSource` with full pagination**
    - Write `src/django_aqueduct/sources/aws_ssm.py` with `AWSParameterStoreSource(BaseSettingsSource)`; constructor accepts `path_prefix: str`, `region_name: str | None = None`; `__call__` fetches all parameters under `path_prefix` using a `NextToken` pagination loop over `get_parameters_by_path(Path=path_prefix, Recursive=True, WithDecryption=True)`, merges all pages, strips `path_prefix` from each parameter name, returns the merged `dict[str, Any]`; guard `import boto3` with `[aws]` extra install hint
    - Write `tests/test_sources_aws.py` mocking `boto3.client` with a two-page response (first response includes `NextToken`, second does not) to exercise the full pagination loop; assert prefix stripping on both pages; assert the merged dict contains parameters from both pages; assert `ImportError` fires when boto3 is absent
    - **Done:** two-page pagination test passes; prefix stripping confirmed across pages; `ImportError` test passes
    - *Depends on: task 3*

13. **Implement the `generate_aqueduct_settings` management command**
    - Write `src/django_aqueduct/management/commands/generate_aqueduct_settings.py` subclassing `BaseCommand`; args: `--modules` (comma-separated dotted paths, optional), `--output` (file path or `-` for stdout, default `-`), `--include-envparser` (boolean flag, auto-enabled if `mitol-django-common` in `django.apps.apps`); instantiates relevant inspectors, merges `DiscoveredField` lists, passes to `SettingsModelGenerator`, writes to stdout or file
    - Write `tests/test_management_command.py` using Django's `call_command` with `--modules testapp.fixture_settings`; capture stdout and assert valid Python via `ast.parse` and presence of `AqueductSettings`
    - **Done:** `uv run testapp/manage.py generate_aqueduct_settings --modules testapp.fixture_settings` prints valid Python to stdout
    - *Depends on: tasks 7, 8, 9*

14. **Wire up mypy**
    - Add `[tool.mypy]` to `pyproject.toml`: `strict = true`, `plugins = ["pydantic.mypy"]`, `ignore_missing_imports = true`
    - Run `uv run mypy src/django_aqueduct` and fix all errors; document any unavoidable `type: ignore` suppressions inline
    - **Done:** `uv run mypy src/django_aqueduct` exits 0
    - *Depends on: tasks 3–13*

15. **Set up GitHub Actions CI**
    - Write `.github/workflows/ci.yml`: jobs `lint` (`prek run --all-files`), `typecheck` (`mypy`), `test` (matrix: **Python 3.12 / 3.13 / 3.14 × Django 5.0 / 5.1 / 5.2 / 6.x**); each job uses `uv` for environment setup
    - Write `.github/workflows/publish.yml` triggered on `v*` tags; builds with `uv build`; publishes via PyPI trusted publishing (OIDC)
    - Push and confirm all CI jobs green on a draft PR
    - **Done:** all matrix combinations pass in CI

16. **Write `README.md`**
    - Sections: installation (including extras), quickstart (generate → refine → wire shim, ~15 lines of code total), K8s deployment pattern (env from ConfigMaps; Vault with Kubernetes SA auth using default and custom JWT paths; SSM for AWS-hosted deployments), both adapter modes with when-to-use guidance, edx-platform migration walkthrough (replacing `lms/envs/production.py`), `[mitol]` extra docs for `EnvParserInspector`, contributing guide
    - **Done:** README renders correctly on GitHub; quickstart is runnable end-to-end against the testapp

## Open questions

*All open questions resolved. Implementation is ready to begin.*
