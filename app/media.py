import json
import re
import subprocess
from pathlib import Path

from langdetect import DetectorFactory, detect_langs
from langdetect.lang_detect_exception import LangDetectException

DetectorFactory.seed = 0
TEXT_SUBTITLE_CODECS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}

# Stream titles are free-form, but often carry an explicit language hint.
# Complete-token matching avoids interpreting ordinary words as language codes.
_SUBTITLE_TITLE_LANGUAGE_ALIASES = {
    "en": {"en", "eng", "english"},
    "de": {"de", "deu", "ger", "german", "deutsch"},
    "fr": {"fr", "fra", "fre", "french", "francais", "français"},
    "es": {"es", "spa", "spanish", "espanol", "español", "castilian"},
    "it": {"it", "ita", "italian", "italiano"},
    "pt": {"pt", "por", "portuguese", "português"},
    "nl": {"nl", "nld", "dut", "dutch", "nederlands"},
    "ja": {"ja", "jpn", "japanese", "日本語"},
    "zh": {"zh", "zho", "chi", "chinese", "中文"},
    "ru": {"ru", "rus", "russian", "русский"},
    "ko": {"ko", "kor", "korean", "한국어"},
    "pl": {"pl", "pol", "polish", "polski"},
    "sv": {"sv", "swe", "swedish", "svenska"},
    "da": {"da", "dan", "danish", "dansk"},
    "no": {"no", "nor", "norwegian", "norsk"},
    "fi": {"fi", "fin", "finnish", "suomi"},
    "tr": {"tr", "tur", "turkish", "türkçe"},
    "ar": {"ar", "ara", "arabic", "العربية"},
    "he": {"he", "heb", "hebrew", "עברית"},
    "el": {"el", "ell", "gre", "greek", "ελληνικά"},
    "cs": {"cs", "ces", "cze", "czech", "čeština"},
    "hu": {"hu", "hun", "hungarian", "magyar"},
    "ro": {"ro", "ron", "rum", "romanian", "română"},
    "uk": {"uk", "ukr", "ukrainian", "українська"},
    "th": {"th", "tha", "thai", "ไทย"},
    "vi": {"vi", "vie", "vietnamese"},
}
_TITLE_LANGUAGE_LOOKUP = {
    alias: language
    for language, aliases in _SUBTITLE_TITLE_LANGUAGE_ALIASES.items()
    for alias in aliases
}


def _subtitle_title_language(title: str) -> str | None:
    """Return a language explicitly named in a subtitle stream title."""
    tokens = re.findall(r"[\\wÀ-ÿ]+", str(title or "").casefold())
    matches = {_TITLE_LANGUAGE_LOOKUP[token] for token in tokens
               if token in _TITLE_LANGUAGE_LOOKUP}
    return next(iter(matches)) if len(matches) == 1 else None


def _subtitle_content_language(path: Path, stream_index: int, codec: str) -> dict:
    if codec not in TEXT_SUBTITLE_CODECS:
        return {"status": "unknown", "language": None, "confidence": None}
    try:
        result = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
             "-map", f"0:{stream_index}", "-f", "srt", "-"],
            check=True, capture_output=True, text=True, timeout=90
        )
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
    except (subprocess.SubprocessError, OSError, ValueError, IndexError, LangDetectException):
        return {"status": "unknown", "language": None, "confidence": None}


def _container_metadata(format_tags: dict) -> dict:
    tags = {str(key).lower(): str(value).strip() for key, value in (format_tags or {}).items()}
    title = tags.get("title") or tags.get("show") or ""
    year = tags.get("date") or tags.get("year") or tags.get("creation_time") or ""
    year_match = re.search("[0-9]{4}", year)
    imdb_id = (
        tags.get("imdb_id") or tags.get("imdb") or tags.get("imdbid")
        or tags.get("www.imdb.com/title") or ""
    )
    return {
        "title": title,
        "year": year_match.group(0) if year_match else "",
        "imdb_id": imdb_id,
    }


def inspect_media(path: Path) -> dict:
    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=format_name,duration,size:format_tags:stream=index,codec_type,codec_name:stream_tags=language,title",
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
        title = tags.get("title", "")
        title_language = _subtitle_title_language(title)
        # Explicit non-English titles override misleading metadata and detector output.
        if title_language and title_language != "en":
            content = {**content, "status": "other", "language": title_language}
        subtitles.append({
            "index": stream.get("index"),
            "codec": codec,
            "language_metadata": tags.get("language", "und"),
            "title": title,
            "title_language": title_language,
            "content_language": content["language"],
            "content_language_confidence": content["confidence"],
            "content_status": content["status"]
        })
    has_verified_english = any(s["content_status"] == "english" for s in subtitles)
    return {
        "format": data.get("format", {}).get("format_name"),
        "duration": data.get("format", {}).get("duration"),
        "size": data.get("format", {}).get("size"),
        "metadata": _container_metadata(data.get("format", {}).get("tags") or {}),
        "video_streams": sum(1 for s in streams if s.get("codec_type") == "video"),
        "audio_streams": sum(1 for s in streams if s.get("codec_type") == "audio"),
        "subtitle_streams": subtitles,
        "has_verified_english_subtitle": has_verified_english,
        "subtitle_content_assessment": "verified" if has_verified_english else "incomplete_or_unknown"
    }
