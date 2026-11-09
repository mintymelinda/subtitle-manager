# Changelog

All notable changes to Subtitle Manager are documented here.

## [1.0.2] - 2026-11-09

### Added
- Publish multi-platform Docker images to GitHub Container Registry for version tags.
- Provide a Compose deployment configuration that pulls the published GHCR image without building locally.
- Add OCI image metadata and document GHCR deployment, version pinning, and private-package authentication.

## [1.0.1] - 2026-11-09

### Changed
- Add the MIT License to clarify the project's reuse, modification, and redistribution terms.
- Document the project's license in the README.

## [1.0.0] - 2026-11-09

First stable release.

### Added
- Recursive MKV media scanning with FFprobe-based stream inspection and subtitle language detection.
- OpenSubtitles.com search, ranking, candidate staging, and persisted provider settings.
- Explicit per-file candidate approval and confirmation before any media modification.
- Transactional MKV subtitle injection with validation before and after replacement, recovery copies, and serialized patch operations.
- Dashboard support for scanning, candidate review, media track editing, job progress, cancellation, and activity history.
- Persistent SQLite state for jobs, candidates, approvals, and patch history.
- Safe mounted-media browsing and path/symlink boundary checks.
- Structured, credential-redacted JSON logs.
- Clear saved data & caches action, including recursive removal of regular `.bak` files under `/media`.

### Supported scope
- MKV is the only supported container for scanning and subtitle injection.
- Other video containers are intentionally excluded.
- Test mode still performs the actual media replacement and retains the recovery copy; it is not a dry run.
