# Architecture Decision Records

This directory records the significant architecture decisions made in
django-aqueduct, using the [Nygard ADR format](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions).

Each record is immutable once accepted: to change a decision, add a new ADR that
supersedes the old one (and mark the old one `Superseded by ADR-N`).

## Format

Each ADR has: **Title**, **Status**, **Context**, **Decision**,
**Consequences**, and **Alternatives considered**.

Statuses: `Proposed` · `Accepted` · `Superseded by ADR-N` · `Deprecated`.

## Index

| ADR | Title | Status |
| --- | --- | --- |
| [0001](0001-dependency-surface-discovery.md) | Dependency-surface discovery and reporting | Accepted |
