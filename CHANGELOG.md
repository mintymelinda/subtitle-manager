# Changelog

All notable changes to Subtitle Manager are documented here.

## [1.0.3] - 2026-11-11

### Added
- Add an explicit, separately confirmed automatic subtitle cleanup action for files with no retained subtitle tracks.
- Add real-folder scan and opt-in real-media injection profiling tools.
- Add actionable, sanitized OpenSubtitles download diagnostics, including provider error details in structured logs.
- Detect daily OpenSubtitles download quota exhaustion, persist reset information, pause affected downloads, and retry deferred candidates when the quota is expected to renew.
- Add a profiler-only OpenSubtitles API-key override without changing saved provider credentials.

### Improved
- Cache media inspection results for unchanged files, avoiding repeated FFprobe and subtitle-content inspection.
- Run discovery and staging with bounded concurrency while keeping FFmpeg/FFprobe execution serialized.
- Move inventory traversal into the background scan job so scan requests return promptly.
- Serialize and batch SQLite scan writes, and use SQLite NORMAL synchronous mode for scan-writer connections.
- Add detailed scan-stage and database-writer timing instrumentation to make future performance regressions diagnosable.
- Remove obsolete application environment settings and the legacy OpenSubtitles .env credential fallback.

### Performance
- Reduce the measured 144-file Futurama scan from over an hour to approximately 56 seconds while completing all files and successfully staging ten subtitle candidates.

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
- Clear saved data & caches action, including recursive removal of regular .bak files under /media.

### Supported scope
- MKV is the only supported container for scanning and subtitle injection.
- Other video containers are intentionally excluded.
- Test mode still performs the actual media replacement and retains the recovery copy; it is not a dry run.
