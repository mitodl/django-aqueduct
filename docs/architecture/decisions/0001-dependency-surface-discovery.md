# 1. Dependency-surface discovery and reporting

## Status

Accepted (targets 0.10.0). Implements the report phase of the RFC
"Dependency-surface discovery for django-aqueduct"; tracks
`tk-dependency-surface-discovery-enumerate-third-par-0baac6`.

## Context

A primary, still-unmet goal of django-aqueduct is to make visible what the
settings a project *inherits from its dependencies* look like: what each
third-party/Django package introduces into the settings namespace, what its
default is, and whether the project has made an explicit decision about it.

The 0.9.0 generator is **source-driven**. `StaticModuleInspector` reads the
modules in `[tool.aqueduct] modules`; EnvParser/usage inspectors add what the
project references. `PackageAttributor` then only *labels* already-discovered
fields with an owning package — it imports `django.conf.global_settings`,
`rest_framework.settings.DEFAULTS`, and `celery.app.defaults` purely as a
name→package lookup, never to enumerate a dependency's surface. A setting a
dependency reads with its own internal default that the project never sets is
therefore invisible: no field, no default, nothing to decide about.
`--enrich-runtime` does not help — package defaults live inside the package
(`getattr(settings, "X", default)`), never on the settings object a `dir()`
walk sees.

Source-driven discovery is *correct* for the generated model: deterministic and
secret-safe. Dependency-surface visibility is a separate, complementary
capability and must not compromise those properties.

## Decision

Add a **dependency-surface capability**: enumerate, per installed dependency,
the settings it introduces (name, type, package default), reconcile each against
what the project sets, and present the result as a **report**. Specifically:

1. **A separate management command, `report_settings_surface` — not a flag on
   `generate_aqueduct_settings`.** Reporting is a distinct concern from
   generation. Keeping it separate keeps `generate --check` drift output clean
   and guarantees the advisory report can never bloat the generated model.

2. **A public, import-light `Setting` declaration dataclass**
   (`django_aqueduct/surface.py`, stdlib-only — no Django, no pydantic) plus a
   `django_aqueduct.settings_surface` entry-point group, so any package can
   declare its own settings surface. This is the generalized analogue of DRF's
   `DEFAULTS`. `Setting` distinguishes "required / no default" from "default is
   `None`" via an `UNSET` sentinel (and a `required` flag).

3. **Providers and precedence** (first wins on a setting-name collision):
   1. **Declared surface** via the `django_aqueduct.settings_surface` entry
      point — authoritative.
   2. **Built-in extractors** for Django `global_settings`, DRF
      `rest_framework.settings.DEFAULTS` (with `IMPORT_STRINGS` awareness), and
      Celery `celery.app.defaults`. These **reuse the imports the attributor
      already performs**: the extraction pass (name → default + type) is
      factored into `discovery/dependency_surface.py`, and `PackageAttributor`
      now derives its name→label maps from that same module rather than
      re-importing.
   3. **INSTALLED_APPS scoping.** Built-in extractors are included only for
      packages present in the project's `INSTALLED_APPS`; declared surfaces are
      always included (a package advertising one is opting in). A
      `dependency_surface_packages` restriction narrows the report further.
   4. **Generic AST-default fallback** — *deferred* (see below).

4. **Reconciliation model.** Each surface setting is classified against the
   project's own discovered fields as **set** (project defines it; value shown
   when statically known), **overridden** (project's literal differs from the
   package default), or **unset**. This is exactly the "package introduces X,
   default Y, you set Z / unset" view the goal calls for.

5. **Two modes.** *Report* (primary — this ADR/PR). *Opt-in model emission*
   (materializing unset package settings as typed, overridable fields grouped
   by package) is a **deferred follow-up**.

6. **Safety / determinism invariants.**
   - No arbitrary execution: providers import only a package's *own* defaults
     module (the same ones the attributor already imports); reconciliation reuses
     existing static discovery and imports no additional project code.
   - Secret-safe: package defaults are library defaults (non-secret), but names
     are still run through `discovery/secrets.py`; secret-shaped names are
     redacted (value never printed).
   - Deterministic: stable sort by `(dist, name)`; no environment or machine
     reads; a determinism test asserts two runs match.

## Consequences

- Teams gain a decision aid (`report_settings_surface`, `--format
  table|json|markdown`) showing, per dependency, every setting it introduces and
  whether the project has decided about it — without touching the generated
  model or CI drift checks.
- Packages (starting with the mitol libraries) can publish a first-class,
  high-fidelity surface with a few lines and one entry point, independent of
  aqueduct's release cadence.
- The Django/DRF/Celery imports now live in one place
  (`discovery/dependency_surface.py`); attribution and the report share it,
  removing duplication.
- New public API (`Setting`, `UNSET`) and a new entry-point group become a
  supported surface aqueduct must keep stable.
- Built-in coverage is best-effort: a package that neither ships a known
  defaults structure nor declares a surface is not fully enumerated until the
  generic AST fallback lands.

## Alternatives considered

- **A `--report-surface` flag on `generate_aqueduct_settings`.** Rejected: it
  mixes a distinct concern into the generator, risks polluting `--check` output,
  and invites the report to leak into the emitted model. A dedicated command is
  cleaner.
- **Emitting the dependency surface into the model by default.** Rejected:
  enumerating every Django/DRF/Celery default would add hundreds of fields.
  Model emission is therefore deferred and will be strictly opt-in.
- **Importing each dependency and snapshotting its live defaults.** Rejected: it
  breaks the determinism/no-arbitrary-execution invariants. Only a package's own
  declarative defaults module is imported.
- **A generic AST scan capturing `getattr(settings, "X", DEFAULT)` operands as
  a fallback provider.** Deferred, not rejected — valuable but lowest fidelity;
  scoped out of this PR to keep the first cut small and safe.
