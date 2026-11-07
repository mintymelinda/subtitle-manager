"""Transactional MKV subtitle injection. Never call without explicit per-file approval."""
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sup", ".sub"}


def _probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=format_name:stream=index,codec_type,codec_name,width,height,channels,sample_rate,channel_layout:stream_tags=language",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True, timeout=90
    )
    return json.loads(result.stdout or "{}")


def _signature(stream: dict) -> tuple:
    return tuple(stream.get(key) for key in (
        "codec_type", "codec_name", "width", "height",
        "channels", "sample_rate", "channel_layout"
    ))


def _english(stream: dict) -> bool:
    language = (stream.get("tags") or {}).get("language", "").strip().lower()
    return language in {"en", "eng", "english"}


def patch_mkv(source: Path, subtitle: Path, *, approved: bool, test_mode: bool = True) -> dict:
    """Mux an approved subtitle into an MKV without transcoding.

    A same-directory recovery copy is retained until the replacement is verified.
    In test mode the recovery copy is kept after success for manual inspection.
    """
    if approved is not True:
        raise PermissionError("Explicit approval for this media file is required")
    source = source.resolve(strict=True)
    subtitle = subtitle.resolve(strict=True)
    if source.suffix.lower() != ".mkv":
        raise ValueError("Only MKV containers are supported in this release")
    if subtitle.suffix.lower() not in SUBTITLE_EXTENSIONS or not subtitle.is_file():
        raise ValueError("Unsupported or missing subtitle file")
    if not source.is_file():
        raise ValueError("Source media file does not exist")
    if source == subtitle:
        raise ValueError("Subtitle file cannot be the media file")

    before = _probe(source)
    before_streams = before.get("streams", [])
    before_av = [_signature(s) for s in before_streams if s.get("codec_type") in {"video", "audio"}]
    before_subs = [_signature(s) for s in before_streams if s.get("codec_type") == "subtitle"]

    token = uuid.uuid4().hex
    backup = source.with_name(f".{source.name}.recovery-{token}.bak")
    fd, working_name = tempfile.mkstemp(
        prefix=f".{source.stem}.", suffix=".working.mkv", dir=str(source.parent)
    )
    os.close(fd)
    working = Path(working_name)
    working.unlink(missing_ok=True)
    replaced = False

    try:
        shutil.copy2(source, backup)
        command = [
            "mkvmerge", "-o", str(working), str(source),
            "--language", "0:eng", str(subtitle)
        ]
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=3600)
        if not working.is_file() or working.stat().st_size == 0:
            raise RuntimeError("MKVToolNix did not produce a valid working file")

        candidate = _probe(working)
        candidate_streams = candidate.get("streams", [])
        after_av = [_signature(s) for s in candidate_streams if s.get("codec_type") in {"video", "audio"}]
        after_subs = [s for s in candidate_streams if s.get("codec_type") == "subtitle"]
        if after_av != before_av:
            raise RuntimeError("Validation failed: existing video/audio streams changed")
        if len(after_subs) < len(before_subs) + 1:
            raise RuntimeError("Validation failed: the new subtitle stream is missing")
        # Existing subtitle tracks must remain represented in the output.
        remaining = list(after_subs)
        for old_sig in before_subs:
            match = next((i for i, stream in enumerate(remaining) if _signature(stream) == old_sig), None)
            if match is None:
                raise RuntimeError("Validation failed: an existing subtitle stream was lost")
            remaining.pop(match)
        if not any(_english(stream) for stream in remaining):
            raise RuntimeError("Validation failed: new subtitle is not tagged English")

        # Ensure file data is flushed before the atomic same-filesystem replacement.
        with working.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(working, source)
        replaced = True

        try:
            final = _probe(source)
            final_streams = final.get("streams", [])
            final_av = [_signature(s) for s in final_streams if s.get("codec_type") in {"video", "audio"}]
            final_subs = [s for s in final_streams if s.get("codec_type") == "subtitle"]
            if final_av != before_av or len(final_subs) < len(before_subs) + 1:
                raise RuntimeError("Post-replacement verification failed")
            if not any(_english(s) for s in final_subs):
                raise RuntimeError("Post-replacement English subtitle verification failed")
        except Exception:
            os.replace(backup, source)
            replaced = False
            raise

        if not test_mode:
            backup.unlink(missing_ok=True)
        return {
            "status": "patched",
            "source": str(source),
            "backup": str(backup) if backup.exists() else None,
            "test_mode": test_mode,
            "video_audio_streams_preserved": True,
            "existing_subtitles_preserved": True,
            "english_subtitle_added": True
        }
    except Exception:
        if replaced and backup.exists():
            os.replace(backup, source)
        raise
    finally:
        working.unlink(missing_ok=True)
