# Subtitle Manager

A self-hosted web application for finding missing English subtitles and adding approved subtitle tracks to media containers without re-encoding.

## Safety model

- Scanning and subtitle discovery do not modify media.
- OpenSubtitles results are ranked and downloaded into a private application staging directory, never into the media library.
- **Every media modification requires explicit approval for that file.**
- Test mode is enabled by default and is intended to process exactly one approved file.
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
- Transactional MKV muxing module using MKVToolNix, with explicit approval required at the function boundary and stream-preservation checks.
- Responsive web dashboard to start scans, review persisted media inspection and track summaries, preview or download staged candidates, and record explicit per-file candidate approval or rejection.
- Candidate review decisions are stored in SQLite, tied to the exact media path and scanned file size/mtime. Only one candidate can be approved for a media file at a time; changed media invalidates the previous decision.
- GitHub Actions checks for Python compilation, unit tests, and Docker image build.

## Not implemented yet

- API workflow that invokes the patcher after candidate approval (approval currently records the selected candidate only; it does not modify media).
- Full job progress, structured logs, and history views.
- A test-mode coordinator that enforces the one-file limit across a complete user session.

The patching module is not exposed as a web action yet. Scanning, candidate review, approval, and candidate downloads do not modify media.

## OpenSubtitles setup

1. Create an API consumer and obtain an API key from [OpenSubtitles.com](https://www.opensubtitles.com/en/consumers).
2. Copy `.env.example` to `.env` and set `OPEN_SUBTITLES_API_KEY`.
3. Optionally set `OPEN_SUBTITLES_USERNAME` and `OPEN_SUBTITLES_PASSWORD` to use your own account's download allowance. Without them, downloads use the API key's anonymous allowance.
4. Set `OPEN_SUBTITLES_MAX_CANDIDATES_PER_FILE` to control how many top-ranked results are downloaded for each media file (default: 2; maximum: 5).
5. Keep `SUBTITLE_STAGING_PATH` outside the media mount. The default is `/app/data/staged-subtitles`, persisted by the Compose `./data:/app/data` volume.

The API key and optional account credentials are read only by the server. They are not returned by `/api/config` or written to application logs. OpenSubtitles download quotas and rate limits are controlled by the provider and may change.

## Requirements

- Docker Engine and Docker Compose.
- The host media directory `/volume1/media` mounted into the container at `/media`, with write permission for approved patch operations.
- An OpenSubtitles.com API key to enable provider search and candidate downloads.

## Quick start

1. Copy `.env.example` to `.env` and add your OpenSubtitles API key.
2. The Compose volume maps the NAS media directory `/volume1/media` to `/media` in the container by default. You can override the host-side `MEDIA_PATH` in `.env` if the NAS folder differs.
3. Start the service:

   ```sh
   docker compose up -d --build
   ```

4. Open http://localhost:8080, enter the folder you want to scan (default `/media`, or a subfolder such as `/media/Movies`), and start the scan. The folder prompt is restricted to the mounted media volume; it cannot browse outside that mount.

Test mode is enabled by default. The current web interface can scan, inspect, search, and stage candidates, but it cannot approve or modify media yet.

## Development

Run the checks locally with:

```sh
python -m pip install -r requirements.txt
python -m compileall -q app tests
python -m unittest discover -s tests -v
docker build -t subtitle-manager:test .
```
