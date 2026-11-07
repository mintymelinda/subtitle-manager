import json
import subprocess
from pathlib import Path

from langdetect import DetectorFactory, detect_langs

DetectorFactory.seed = 0
TEXT_SUBTITLE_CODECS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}


def _subtitle_content_language(path: Path, stream_index: int, codec: str) -> dict:
    if codec not in TEXT_SUBTITLE_CODECS:
        return {"status": "unknown", "language": None, "confidence": None}
    try:
        result = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
             "-map", f"0:{stream_index}", "-f", "srt", "-"],
            check=True, capture_output=True, text=True, timeout=90
        )
        # Strip SRT sequence numbers and timestamps before language detection.
        lines = [
            line.strip() for line in result.stdout.splitlines()
            if line.strip() and "-->" not in line and not line.strip().isdigit()
        ]
        sample = " ".join(lines)[:12000]
        if len(sample) < 60:
            return {"status": "unknown", "language": None, "confidence": None}
        prediction = detect_langs(sample)[0]
        language = prediction.lang
        confidence = float(prediction.prob)
        if confidence < 0.80:
            return {"status": "unknown", "language": language, "confidence": confidence}
        return {
            "status": "english" if language == "en" else "other",
            "language": language,
            "confidence": confidence
        }
    except (subprocess.SubprocessError, OSError, ValueError, IndexError):
        return {"status": "unknown", "language": None, "confidence": None}


def inspect_media(path: Path) -> dict:
    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=format_name,duration,size:stream=index,codec_type,codec_name:stream_tags=language,title",
        "-of", "json", str(path)
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams", [])
    subtitles = []
    for stream in streams:
        if stream.get("codec_type") != "subtitle":
            continue
        tags = stream.get("tags") or {}
        codec = stream.get("codec_name", "")
        content = _subtitle_content_language(path, stream.get("index"), codec)
        subtitles.append({
            "index": stream.get("index"),
            "codec": codec,
            "language_metadata": tags.get("language", "und"),
            "title": tags.get("title", ""),
            "content_language": content["language"],
            "content_language_confidence": content["confidence"],
            "content_status": content["status"]
        })
    has_verified_english = any(s["content_status"] == "english" for s in subtitles)
    return {
        "format": data.get("format", {}).get("format_name"),
        "duration": data.get("format", {}).get("duration"),
        "size": data.get("format", {}).get("size"),
        "video_streams": sum(1 for s in streams if s.get("codec_type") == "video"),
        "audio_streams": sum(1 for s in streams if s.get("codec_type") == "audio"),
        "subtitle_streams": subtitles,
        "has_verified_english_subtitle": has_verified_english,
        "subtitle_content_assessment": "verified" if has_verified_english else "incomplete_or_unknown"
    }
