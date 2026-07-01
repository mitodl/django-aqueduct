# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `VaultSettingsSource` accepts a `kv_version` argument (`"1"` or `"2"`,
  defaulting to `"2"`) to support Vault mounts still running the KV v1
  secrets engine, in addition to the previously KV-v2-only implementation.

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
