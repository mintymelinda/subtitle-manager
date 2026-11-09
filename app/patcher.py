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


def _validate_streams(before: dict, after: dict, output_path: Path) -> None:
    output_format = (after.get("format") or {}).get("format_name", "").lower()
    if "matroska" not in output_format:
        raise RuntimeError("Validation failed: output is not a Matroska container")
    before_streams = before.get("streams", [])
    after_streams = after.get("streams", [])
    before_av = [_signature(s) for s in before_streams if s.get("codec_type") in {"video", "audio"}]
    after_av = [_signature(s) for s in after_streams if s.get("codec_type") in {"video", "audio"}]
    if after_av != before_av:
        raise RuntimeError("Validation failed: existing video/audio streams changed")

    before_subs = [_signature(s) for s in before_streams if s.get("codec_type") == "subtitle"]
    after_subs = [s for s in after_streams if s.get("codec_type") == "subtitle"]
    if len(after_subs) < len(before_subs) + 1:
        raise RuntimeError("Validation failed: the new subtitle stream is missing")

    remaining = list(after_subs)
    for old_signature in before_subs:
        match = next((i for i, stream in enumerate(remaining)
                      if _signature(stream) == old_signature), None)
        if match is None:
            raise RuntimeError("Validation failed: an existing subtitle stream was lost")
        remaining.pop(match)
    added_english = [stream for stream in remaining if _english(stream)]
    if not added_english:
        raise RuntimeError("Validation failed: the added subtitle is not tagged English")
    if not any(_subtitle_has_content(output_path, stream) for stream in added_english):
        raise RuntimeError("Validation failed: the added English subtitle contains no subtitle cues")


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


def _patch_mkv_unlocked(source: Path, subtitle: Path, *, approved: bool, test_mode: bool = True, expected_size: int | None = None, expected_mtime: float | None = None) -> dict:
    """Patch one explicitly approved MKV. Test mode always retains the recovery copy."""
    if approved is not True:
        raise PermissionError("Explicit approval for this media file is required")

    source = source.resolve(strict=True)
    subtitle = subtitle.resolve(strict=True)
    if source.suffix.lower() != ".mkv":
        raise ValueError("Only MKV containers are supported in this release")
    if subtitle.suffix.lower() not in SUBTITLE_EXTENSIONS or not subtitle.is_file():
        raise ValueError("Unsupported or missing subtitle file")
    if not source.is_file() or source == subtitle:
        raise ValueError("Source media file is invalid")
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
        if _snapshot(source) != original_snapshot:
            raise RuntimeError("Source media changed while the recovery copy was being created")
        command = [
            "mkvmerge", "-o", str(working), str(source),
            "--language", "0:eng", str(subtitle)
        ]
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=3600)
        if not working.is_file() or working.stat().st_size == 0:
            raise RuntimeError("MKVToolNix did not produce a valid working file")

        candidate = _probe(working)
        _validate_streams(before, candidate, working)
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
            final = _probe(source)
            _validate_streams(before, final, source)
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
            "existing_subtitles_preserved": True,
            "english_subtitle_added": True,
            "metadata_preserved": True,
        }
    except Exception:
        if replaced and backup.exists():
            _restore_from_backup(backup, source)
        raise
    finally:
        working.unlink(missing_ok=True)


def patch_mkv(source: Path, subtitle: Path, *, approved: bool, test_mode: bool = True, expected_size: int | None = None, expected_mtime: float | None = None) -> dict:
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
                                   expected_mtime=expected_mtime)
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
