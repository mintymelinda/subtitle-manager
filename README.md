# Subtitle Manager

A self-hosted web application for finding missing English subtitles and adding approved subtitle tracks to media containers without re-encoding.

## Safety model

- Scanning and subtitle discovery do not modify media.
- Downloaded subtitle candidates are staged for review.
- **Every media modification requires explicit approval for that file.**
- Test mode is enabled by default and processes exactly one approved file.
- Patching is transactional: the application builds and validates a working copy, retains a temporary original for recovery, then atomically replaces the source on the same filesystem where possible.
- No permanent library-wide backups are created. Temporary recovery copies are removed after successful verification in normal mode; test mode retains its backup for inspection.
- On failure, the original is left in place and temporary recovery files are retained.

## Initial scope

- Recursive media scanning within a configured root.
- Inspection of container streams and subtitle language metadata with FFprobe.
- Candidate staging and review workflow.
- MKV subtitle injection with MKVToolNix, without re-encoding existing audio or video.
- SQLite job and history database.
- FastAPI web/API service and Docker Compose deployment.

## Requirements

- Docker Engine and Docker Compose.
- A media directory mounted into the container with write permission for approved patch operations.
- OpenSubtitles API credentials for online subtitle discovery.

## Quick start

1. Copy `.env.example` to `.env` and configure the media root and subtitle provider credentials.
2. Start the service:

   ```sh
   docker compose up -d --build
   ```

3. Open http://localhost:8080.
4. Review scan results and subtitle candidates. Do not approve a patch unless the candidate is correct.

The first-run configuration uses test mode. Test mode will patch no more than one approved media file and will retain its temporary original copy.

## Development

The initial implementation is being built incrementally. The web service, scanner, media inspection, and transactional MKV patching are kept in separate modules so each safety boundary can be tested independently.
