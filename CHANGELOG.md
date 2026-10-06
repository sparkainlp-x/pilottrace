# Changelog

All notable changes to PilotTrace are documented here. The project uses
[Semantic Versioning](https://semver.org/).

## [0.1.1] - 2026-10-06

### Changed

- README: added CI, licence, research-prototype and SYNTHETIC badges.
- Version metadata bumped to 0.1.1 in `pilottrace/__init__.py`, `CITATION.cff`
  and `.zenodo.json` (first release archived on Zenodo).

### Scope

- No change to validation or scoring behaviour. Synthetic examples and
  contract tests only; no empirical, field, hardware, medical, safety, alarm,
  or process-control claims.

## [0.1.0] - 2026-10-01

### Added

- Strict UTF-8 JSON trace and manifest validation.
- Deterministic 512-value frame scoring across sixteen contiguous 32-value blocks.
- Inclusive threshold comparison and exact-input SHA-256 hashes in reports.
- Rejection reports with zero-based frame indices and explicit reasons.
- Descriptive frame-classification metrics for supplied boolean labels only.
- Modular Python package, standalone script, examples, tests, explanatory paper,
  `CITATION.cff`, and Zenodo metadata.

### Scope

- Synthetic examples and contract tests only.
- No empirical, field, hardware, medical, safety, alarm, or process-control claims.
