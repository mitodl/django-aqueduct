# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0]

### Added

- `VaultSettingsSource` accepts a `kv_version` argument (`"1"` or `"2"`,
  defaulting to `"2"`) to support Vault mounts still running the KV v1
  secrets engine, in addition to the previously KV-v2-only implementation.

### Changed

- Lowered the minimum supported Django version from `5.0` to `4.2`. Nothing
  in the package used a Django 5-only API; the `>=5.0` pin was blocking
  adoption by projects (e.g. mit-learn) still on Django 4.2 LTS.

### Fixed

- `ModuleInspector` (the `--modules` codegen path) no longer writes the live,
  environment-resolved value of secret-shaped settings (names containing
  `SECRET`, `PASSWORD`, `TOKEN`, `PRIVATE_KEY`, `API_KEY`, `CREDENTIAL`, etc.)
  into the generated file. Because this inspector reads settings by
  importing the target module and reading resolved attribute values, running
  it in an environment with real secrets set previously baked those values
  verbatim into the (likely committed) generated scaffold. Matching fields
  are now rendered as `default=None` with a `# REDACTED` comment instead —
  the same treatment already given to `CALLABLE`/`DERIVED` values.
- Generated files now `import datetime`. Any setting whose default is a
  `datetime.timedelta` (or other `datetime` value) rendered as
  `datetime.timedelta(...)` via `repr()`, but the import was missing,
  causing a `NameError` the first time the generated `AqueductSettings` was
  instantiated — `ast.parse()`-only tests hadn't caught this since it's a
  runtime error, not a syntax error.

## [0.5.0]

### Fixed

- `os.PathLike` settings values (including `pathlib.Path` and `path.Path`) are
  now annotated as `pathlib.Path` in generated models instead of `str`. The
  previous `str` annotation caused Pydantic to coerce the default value to a
  plain string at instantiation, breaking any code that used the `/` operator
  for path joining (e.g. `settings.PROJECT_ROOT / "static"`). Generated files
  now include `import pathlib` in their header.

## [0.4.0]

### Added

- `configure_django_settings` now accepts an optional `base` argument (a dotted
  module path, an imported module, or a mapping). When supplied, the model is
  **overlaid** onto the base settings instead of replacing them: any setting the
  model does not carry — or that the generator could not serialise (rendered as a
  `None` default, e.g. `INSTALLED_APPS` built from class references,
  `XBLOCK_MIXINS`) — degrades to the real base value rather than silently
  vanishing to Django's empty default. The merge rule is "model value wins unless
  it is `None` and the base has a non-`None` value." With no `base`, the previous
  replace behaviour is preserved.

### Fixed

- The code generator now widens any field rendered with `default=None`
  (DERIVED / CALLABLE / OPAQUE-fallback) to an Optional annotation (`T | None`).
  Previously an opaque dict whose `repr` was not serialisable (e.g. `JWT_AUTH`,
  `CELERYBEAT_SCHEDULE`) was emitted as `dict[str, Any]` with `default=None`,
  which raised a Pydantic `ValidationError` at instantiation when a source
  supplied `None`.

## [0.3.0]

- Package attribution via `--attribute-packages`.
- genson dict enrichment, JSON Schema export, and generator bug fixes.
