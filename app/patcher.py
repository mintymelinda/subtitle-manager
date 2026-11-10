"""Transactional MKV subtitle injection with same-directory recovery."""
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from app.observability import timed_stage

SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sup", ".sub"}


def _probe(path: Path) -> dict:
    with timed_stage("injection_ffprobe", media_path=str(path), tool="ffprobe",
                     file_size_bytes=path.stat().st_size):
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=format_name:stream=index,codec_type,codec_name,width,height,channels,sample_rate,channel_layout:stream_tags=language,language_ietf",
             "-of", "json", str(path)],
            check=True, capture_output=True, text=True, timeout=90
        )
    return json.loads(result.stdout or "{}")


def _signature(stream: dict) -> tuple:
    return tuple(stream.get(key) for key in (
        "codec_type", "codec_name", "width", "height",
        "channels", "sample_rate", "channel_layout"
    ))


def _language_matches(stream: dict, expected: str) -> bool:
    from app.languages import language_matches
    tags = stream.get("tags") or {}
    return any(language_matches(value, expected)
               for value in (tags.get("language_ietf"), tags.get("language")) if value)


def _subtitle_has_content(path: Path, stream: dict) -> bool:
    """Require real subtitle packets, and visible cue text for text codecs."""
    stream_index = stream.get("index")
    if stream_index is None:
        raise RuntimeError("Validation failed: added subtitle stream has no index")
    packets = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", str(stream_index),
         "-show_packets", "-show_entries", "packet=size", "-of", "json", str(path)],
        check=True, capture_output=True, text=True, timeout=90,
    )
    packet_data = json.loads(packets.stdout or "{}").get("packets", [])
    if not any(int(packet.get("size") or 0) > 0 for packet in packet_data):
        return False

    codec = str(stream.get("codec_name") or "").lower()
    if codec in {"subrip", "ass", "ssa", "webvtt", "mov_text", "text"}:
        decoded = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-map", f"0:{stream_index}",
             "-f", "srt", "pipe:1"],
            check=True, capture_output=True, text=True, timeout=90,
        )
        return bool(re.search(
            r"(?m)^.*-->.*$\n(?:[^\n]*\S[^\n]*\n?)+",
            decoded.stdout or "",
        ))
    # Bitmap subtitles cannot be validated as text without OCR.
    return True


def _validate_streams(before: dict, after: dict, output_path: Path,
                      keep_subtitle_indices: list[int] | None = None,
                      target_language: str = "en", *, subtitle_added: bool = True) -> None:
    output_format = (after.get("format") or {}).get("format_name", "").lower()
    if "matroska" not in output_format:
        raise RuntimeError("Validation failed: output is not a Matroska container")
    before_streams = before.get("streams", [])
    after_streams = after.get("streams", [])
    before_av = [_signature(s) for s in before_streams if s.get("codec_type") in {"video", "audio"}]
    after_av = [_signature(s) for s in after_streams if s.get("codec_type") in {"video", "audio"}]
    if after_av != before_av:
        raise RuntimeError("Validation failed: existing video/audio streams changed")

    indexed_before_subs = [(s.get("index"), _signature(s)) for s in before_streams
                           if s.get("codec_type") == "subtitle"]
    selected = indexed_before_subs if keep_subtitle_indices is None else [
        item for item in indexed_before_subs if item[0] in set(keep_subtitle_indices)
    ]
    before_subs = [signature for _, signature in selected]
    after_subs = [s for s in after_streams if s.get("codec_type") == "subtitle"]
    expected_count = len(before_subs) + (1 if subtitle_added else 0)
    if len(after_subs) != expected_count:
        if subtitle_added:
            raise RuntimeError("Validation failed: the new subtitle stream is missing or unexpected existing subtitle tracks remain")
        raise RuntimeError("Validation failed: output subtitle track count does not match the requested edit")

    remaining = list(after_subs)
    for old_signature in before_subs:
        match = next((i for i, stream in enumerate(remaining)
                      if _signature(stream) == old_signature), None)
        if match is None:
            raise RuntimeError("Validation failed: an existing subtitle stream was lost")
        remaining.pop(match)
    if not subtitle_added:
        if remaining:
            raise RuntimeError("Validation failed: existing subtitle tracks do not match the requested selection")
        return
    added_target_language = [stream for stream in remaining if _language_matches(stream, target_language)]
    if not added_target_language:
        raise RuntimeError("Validation failed: the added subtitle is not tagged with the selected target language")
    if not any(_subtitle_has_content(output_path, stream) for stream in added_target_language):
        raise RuntimeError("Validation failed: the added subtitle contains no subtitle cues")


def _snapshot(path: Path) -> tuple:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _preserve_metadata(source: Path, working: Path) -> None:
    shutil.copystat(source, working, follow_symlinks=False)
    source_stat = source.stat()
    try:
        os.chown(working, source_stat.st_uid, source_stat.st_gid)
    except PermissionError:
        # Non-root containers cannot change ownership; mode/times/xattrs remain copied.
        pass


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _restore_from_backup(backup: Path, source: Path) -> None:
    descriptor, rollback_name = tempfile.mkstemp(
        prefix=f".{source.stem}.", suffix=".rollback.mkv", dir=str(source.parent)
    )
    os.close(descriptor)
    rollback = Path(rollback_name)
    try:
        shutil.copy2(backup, rollback)
        os.replace(rollback, source)
        _fsync_directory(source.parent)
    finally:
        rollback.unlink(missing_ok=True)


def _patch_mkv_unlocked(source: Path, subtitle: Path | None, *, approved: bool, test_mode: bool = True, expected_size: int | None = None, expected_mtime: float | None = None, keep_subtitle_indices: list[int] | None = None, target_language: str = "en") -> dict:
    """Patch one explicitly approved MKV. Test mode always retains the recovery copy."""
    if approved is not True:
        raise PermissionError("Explicit approval for this media file is required")

    source = source.resolve(strict=True)
    if subtitle is not None:
        subtitle = subtitle.resolve(strict=True)
    if source.suffix.lower() != ".mkv":
        raise ValueError("Only MKV containers are supported in this release")
    if subtitle is not None and (subtitle.suffix.lower() not in SUBTITLE_EXTENSIONS or not subtitle.is_file()):
        raise ValueError("Unsupported or missing subtitle file")
    if not source.is_file() or source == subtitle:
        raise ValueError("Source media file is invalid")
    if subtitle is None and keep_subtitle_indices is None:
        raise ValueError("Existing-track edits require an explicit subtitle stream selection")
    original_snapshot = _snapshot(source)
    current_stat = source.stat()
    if expected_size is not None and current_stat.st_size != expected_size:
        raise RuntimeError("Approved media file size no longer matches the reviewed version")
    if expected_mtime is not None and current_stat.st_mtime != expected_mtime:
        raise RuntimeError("Approved media modification time no longer matches the reviewed version")
    before = _probe(source)
    source_format = (before.get("format") or {}).get("format_name", "").lower()
    if "matroska" not in source_format:
        raise ValueError("Source file is not a valid Matroska container")
    if not any(s.get("codec_type") == "video" for s in before.get("streams", [])):
        raise ValueError("Source MKV does not contain a video stream")

    subtitle_streams = [s for s in before.get("streams", []) if s.get("codec_type") == "subtitle"]
    selected_indices = ({s.get("index") for s in subtitle_streams}
                        if keep_subtitle_indices is None else set(keep_subtitle_indices))
    known_indices = {s.get("index") for s in subtitle_streams}
    if not selected_indices.issubset(known_indices):
        raise ValueError("Selected subtitle streams do not match the current media file")
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
        with timed_stage("injection_recovery_copy", media_path=str(source),
                         bytes_processed=source.stat().st_size):
            shutil.copy2(source, backup)
        if _snapshot(source) != original_snapshot:
            raise RuntimeError("Source media changed while the recovery copy was being created")
        with timed_stage("injection_mkvmerge_inspection", media_path=str(source), tool="mkvmerge"):
            mux_info = subprocess.run(
                ["mkvmerge", "-J", str(source)], check=True, capture_output=True,
                text=True, timeout=90,
            )
        mux_tracks = [track for track in json.loads(mux_info.stdout or "{}").get("tracks", [])
                      if track.get("type") == "subtitles"]
        if len(mux_tracks) != len(subtitle_streams):
            raise RuntimeError("Could not safely map inspected subtitle streams to MKVToolNix track IDs")
        selected_track_ids = [str(track["id"]) for stream, track in zip(subtitle_streams, mux_tracks)
                              if stream.get("index") in selected_indices]
        command = ["mkvmerge", "-o", str(working)]
        if selected_track_ids:
            command.extend(["--subtitle-tracks", ",".join(selected_track_ids)])
        else:
            command.append("--no-subtitles")
        command.append(str(source))
        if subtitle is not None:
            command.extend(["--language", f"0:{target_language}", str(subtitle)])
        with timed_stage("injection_remux", media_path=str(source), tool="mkvmerge",
                         bytes_processed=source.stat().st_size):
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=3600)
        if not working.is_file() or working.stat().st_size == 0:
            raise RuntimeError("MKVToolNix did not produce a valid working file")

        with timed_stage("injection_candidate_validation", media_path=str(source),
                         file_size_bytes=working.stat().st_size):
            candidate = _probe(working)
            _validate_streams(before, candidate, working, list(selected_indices), target_language,
                              subtitle_added=subtitle is not None)
        with working.open("rb") as handle:
            os.fsync(handle.fileno())
        _preserve_metadata(source, working)

        # Refuse to replace a file changed by another process during muxing.
        if _snapshot(source) != original_snapshot:
            raise RuntimeError("Source media changed during subtitle injection; original left untouched")

        os.replace(working, source)
        replaced = True
        _fsync_directory(source.parent)

        try:
            with timed_stage("injection_post_replace_validation", media_path=str(source),
                             file_size_bytes=source.stat().st_size):
                final = _probe(source)
                _validate_streams(before, final, source, list(selected_indices), target_language,
                                  subtitle_added=subtitle is not None)
            final_stat = source.stat()
            original_stat = backup.stat()
            if final_stat.st_mode & 0o7777 != original_stat.st_mode & 0o7777:
                raise RuntimeError("Post-replacement verification failed: file permissions changed")
            if final_stat.st_mtime_ns != original_stat.st_mtime_ns:
                raise RuntimeError("Post-replacement verification failed: modification timestamp changed")
        except Exception:
            _restore_from_backup(backup, source)
            replaced = False
            _fsync_directory(source.parent)
            raise

        if not test_mode:
            backup.unlink(missing_ok=True)
            _fsync_directory(source.parent)

        return {
            "status": "patched",
            "source": str(source),
            "backup": str(backup) if backup.exists() else None,
            "test_mode": test_mode,
            "video_audio_streams_preserved": True,
            "existing_subtitles_preserved": len(selected_indices) == len(subtitle_streams),
            "existing_subtitle_streams_kept": sorted(selected_indices),
            "existing_subtitle_streams_removed": sorted(known_indices - selected_indices),
            "english_subtitle_added": subtitle is not None and target_language == "en",
            "target_language": target_language,
            "target_language_subtitle_added": subtitle is not None,
            "existing_tracks_only": subtitle is None,
            "metadata_preserved": True,
        }
    except Exception:
        if replaced and backup.exists():
            _restore_from_backup(backup, source)
        raise
    finally:
        working.unlink(missing_ok=True)


def patch_mkv(source: Path, subtitle: Path | None, *, approved: bool, test_mode: bool = True, expected_size: int | None = None, expected_mtime: float | None = None, keep_subtitle_indices: list[int] | None = None, target_language: str = "en") -> dict:
    """Serialize patch operations by canonical media path across app workers."""
    if approved is not True:
        raise PermissionError("Explicit approval for this media file is required")
    canonical = Path(source).resolve(strict=True)
    lock_root = Path(tempfile.gettempdir()) / "subtitle-manager-locks"
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_name = hashlib.sha256(os.fsencode(str(canonical))).hexdigest() + ".lock"
    lock_path = lock_root / lock_name
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return _patch_mkv_unlocked(canonical, subtitle, approved=approved,
                                   test_mode=test_mode, expected_size=expected_size,
                                   expected_mtime=expected_mtime,
                                   keep_subtitle_indices=keep_subtitle_indices,
                                   target_language=target_language)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
