# Subtitle Manager

A self-hosted web application for finding missing English subtitles and adding approved subtitle tracks to media containers without re-encoding.

## Safety model

- Scanning and subtitle discovery do not modify media.
- OpenSubtitles results are ranked and downloaded into a private application staging directory, never into the media library.
- **Every media modification requires explicit approval for that file.**
- Test mode is enabled by default. Each injection action targets exactly one explicitly approved file and retains its recovery copy. **Test mode still performs the replacement**; it is not a dry run.
- Patching is transactional: the application builds and validates a working copy, retains a temporary original for recovery, then atomically replaces the source on the same filesystem where possible.
- No permanent library-wide backups are created. Temporary recovery copies are removed after successful verification in normal mode; test mode retains its backup for inspection.
- On failure, the original is left in place and temporary recovery files are retained.

## Current implementation

- Docker Compose and FastAPI service foundation.
- Recursive scanner for common video formats.
- FFprobe inspection of container streams and available title/year/IMDb metadata.
- Text subtitle content language detection for supported subtitle codecs; bitmap or too-short tracks are marked unknown rather than assumed English based on metadata.
- SQLite scan, candidate, and patch-history schema.
- OpenSubtitles.com REST API integration:
  - API-key authentication and optional user-account login for download quotas.
  - Searches English subtitles using the OpenSubtitles moviehash and filename/container metadata where available.
  - Ranks exact file-hash matches ahead of release-name matches and exposes the reasons behind the score.
  - Filters machine-translated results and unsupported subtitle formats.
  - Downloads up to the configured number of top candidates per media file into `/app/data/staged-subtitles`.
  - Applies provider rate-limit handling, download-size limits, safe archive extraction, and secret-safe error messages.
- Transactional MKV muxing using MKVToolNix, exposed through an API and dashboard action that requires both a persisted approval for the exact media/candidate pair and a separate confirmation.
- Injection is restricted to MKV media and staged English subtitles inside the configured staging directory; media/staging symlink components and path escapes are rejected.
- Before replacement, the working MKV is checked for unchanged video/audio streams, retained existing subtitle tracks, and a newly added English subtitle track. The output is verified again after atomic replacement.
- Original file mode and timestamps are copied to the replacement; ownership is preserved where container permissions allow. Same-directory recovery copies remain available after failures and in test mode.
- Responsive web dashboard to start scans, review persisted media inspection and track summaries, preview or download staged candidates, and record explicit per-file candidate approval or rejection.
- Persistent scan, review, and injection jobs with queued/running/completed/failed/interrupted states, phase/current-file reporting, progress counters, and per-file outcomes in SQLite.
- Live dashboard polling for active job progress and a recent activity/history view that survives application restarts.
- Structured JSON application logs with UTC timestamps, event/job/candidate context, exception details, and recursive redaction of API keys, passwords, tokens, authorization headers, and cookies.
- Candidate review decisions are stored in SQLite, tied to the exact media path and scanned file size/mtime. Only one candidate can be approved for a media file at a time; changed media invalidates the previous decision.
- GitHub Actions checks for Python compilation, unit tests, real FFmpeg/MKVToolNix integration tests, and Docker image build.
- Injection operations are serialized by canonical media path across application workers, so distinct approved candidates cannot concurrently replace the same MKV.

## Not implemented yet

- Support for non-MKV media containers; the transactional patcher currently supports MKV only.

## Jobs, progress, and logs

Scan and approved-injection requests return a `job_id` immediately and continue in the background. Poll `GET /api/jobs/{job_id}` for state, phase, current file, counters, and per-file results; `GET /api/jobs` lists recent jobs. The dashboard polls active jobs and displays recent history. Candidate approvals and rejections are also written to the job history. Job records and per-file outcomes are stored in SQLite and remain available after restart. Jobs left queued or running during a restart are marked `interrupted`; they are never reported as successful or automatically replayed. Interrupted injections still require manual media/recovery-copy inspection.

Application logs are emitted as one JSON object per line to standard output, suitable for Docker log collection. Secret-like fields and common credential assignments are redacted. Do not enable verbose HTTP wire logging in production because provider request headers may contain credentials.

## OpenSubtitles setup

1. Open the **OpenSubtitles setup** section in the web dashboard.
2. Create an API consumer and obtain an API key from [OpenSubtitles.com](https://www.opensubtitles.com/en/consumers).
3. Enter the API key and optionally your OpenSubtitles username and password, then save. Account credentials use your own account's download allowance; without them, downloads use the API key's anonymous allowance.
4. Set the maximum number of top-ranked candidates downloaded per media file (1–5; default 2), then use **Test connection** to validate the API key and optional account login.

Provider settings are written atomically to `/app/data/opensubtitles-settings.json` with owner-only file permissions. The Compose `./data:/app/data` volume keeps them across container recreation and image updates. Secrets are never returned by the settings or `/api/config` endpoints and are excluded from application log messages. Leave credential fields blank to retain saved values; use the explicit checkbox to clear optional account credentials. OpenSubtitles quotas and rate limits are controlled by the provider and may change.

Existing installations that still have OpenSubtitles values in `.env` are supported as a one-time fallback until settings are saved through the dashboard.
