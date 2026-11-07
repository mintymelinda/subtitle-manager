import json
import subprocess
from pathlib import Path


def inspect_media(path: Path) -> dict:
    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=format_name,duration,size:stream=index,codec_type,codec_name,codec_name:stream_tags=language,title",
        "-of", "json", str(path)
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams", [])
    subtitles = [
        {
            "index": stream.get("index"),
            "codec": stream.get("codec_name"),
            "language": (stream.get("tags") or {}).get("language", "und"),
            "title": (stream.get("tags") or {}).get("title", "")
        }
        for stream in streams if stream.get("codec_type") == "subtitle"
    ]
    return {
        "format": data.get("format", {}).get("format_name"),
        "duration": data.get("format", {}).get("duration"),
        "size": data.get("format", {}).get("size"),
        "video_streams": sum(1 for s in streams if s.get("codec_type") == "video"),
        "audio_streams": sum(1 for s in streams if s.get("codec_type") == "audio"),
        "subtitle_streams": subtitles,
        "has_english_subtitle_metadata": any(
            s["language"].lower() in {"en", "eng", "english"} for s in subtitles
        )
    }
