# Subtitle Manager

A self-hosted web application for finding missing English subtitles and adding approved subtitle tracks to media containers without re-encoding.

## Safety model

- Scanning and subtitle discovery do not modify media.
- Downloaded subtitle candidates are staged for review.
- **Every media modification requires explicit approval for that file.**
- Test mode is enabled by default and is intended to process exactly one approved file.
- Patching is transactional: the application builds and validates a working copy, retains a temporary original for recovery, then atomically replaces the source on the same filesystem where possible.
- No permanent library-wide backups are created. Temporary recovery copies are removed after successful verification in normal mode; test mode retains its backup for inspection.
- On failure, the original is left in place and temporary recovery files are retained.

## Current implementation

- Docker Compose and FastAPI service foundation.
- Recursive scanner for common video formats.
- FFprobe inspection of container streams.
- Text subtitle content language detection for supported subtitle codecs; bitmap or too-short tracks are marked unknown rather than assumed English based on metadata.
- SQLite scan and patch-history schema.
- Transactional MKV muxing module using MKVToolNix, with explicit approval required at the function boundary and stream-preservation checks.
- Basic web page to start a scan and inspect its results.
- GitHub Actions checks for Python compilation, unit tests, and Docker image build.

## Not implemented yet

- OpenSubtitles authentication, search, candidate ranking, and download staging.
- Per-file approval/rejection controls in the web UI and the API workflow that invokes the patcher.
- Full job progress, structured logs, and history views.
- A test-mode coordinator that enforces the one-file limit across a complete user session.

The patching module is not exposed as a web action yet. Media is not modified by scanning.

## Requirements

- Docker Engine and Docker Compose.
- The host media directory `/volume1/media` mounted into the container at `/media`, with write permission for approved patch operations.
- OpenSubtitles API credentials (provider integration is planned, not active yet).

## Quick start

1. Copy `.env.example` to `.env`.
2. The Compose volume maps the NAS media directory `/volume1/media` to `/media` in the container by default. You can override the host-side `MEDIA_PATH` in `.env` if the NAS folder differs.
3. Start the service:

   ```sh
   docker compose up -d --build
   ```

4. Open http://localhost:8080, enter the folder you want to scan (default `/media`, or a subfolder such as `/media/Movies`), and start the scan. The folder prompt is restricted to the mounted media volume; it cannot browse outside that mount.

Test mode is enabled by default. The current web interface only scans and inspects; it cannot approve or modify media yet.

## Development

Run the checks locally with:

```sh
python -m pip install -r requirements.txt
python -m compileall -q app tests
python -m unittest discover -s tests -v
docker build -t subtitle-manager:test .
```
