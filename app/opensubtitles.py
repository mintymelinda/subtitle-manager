"""OpenSubtitles.com API client and candidate matching helpers.

This module never writes to the media library. Downloads are returned as bytes
for the caller to persist in the application's private staging directory.
"""
from __future__ import annotations

import json
import re

from langdetect import DetectorFactory, detect_langs
from langdetect.lang_detect_exception import LangDetectException
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any

API_BASE_URL = "https://api.opensubtitles.com/api/v1"
USER_AGENT = "SubtitleManager v0.1.0"
HASH_CHUNK_SIZE = 64 * 1024
SUPPORTED_EXTENSIONS = {".srt", ".ass", ".ssa", ".sup"}
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
DetectorFactory.seed = 0


class OpenSubtitlesError(RuntimeError):
    """A sanitized provider or download error safe to show in the UI."""


def compute_moviehash(path: Path) -> str | None:
    """Return OpenSubtitles' 16-character moviehash, or None below 128 KiB."""
    size = path.stat().st_size
    if size < HASH_CHUNK_SIZE * 2:
        return None
    value = size
    with path.open("rb") as media:
        for offset in (0, size - HASH_CHUNK_SIZE):
            media.seek(offset)
            for (word,) in struct.iter_unpack("<Q", media.read(HASH_CHUNK_SIZE)):
                value = (value + word) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}"


def filename_query(path: Path) -> str:
    return " ".join(re.sub(r"[._]+", " ", path.stem).split()).lower()


def _tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", value.lower()) if len(token) > 1}


def rank_candidate(attributes: dict[str, Any], media_name: str) -> tuple[int, list[str]]:
    """Rank hash/release matches. The score is a review aid, not a quality claim."""
    reasons: list[str] = []
    if attributes.get("moviehash_match"):
        score = 100
        reasons.append("Exact file fingerprint match")
    else:
        wanted = _tokens(media_name)
        actual = _tokens(str(attributes.get("release") or ""))
        overlap = len(wanted & actual) / max(1, len(wanted | actual))
        score = round(overlap * 70)
        reasons.append(
            f"Release-name token overlap: {overlap:.0%}" if overlap
            else "No exact fingerprint or meaningful release-name overlap"
        )
    if attributes.get("from_trusted"):
        score += 5
        reasons.append("Trusted uploader")
    downloads = max(0, int(attributes.get("download_count") or 0))
    if downloads:
        score += min(5, int(downloads ** 0.5))
        reasons.append(f"{downloads} prior downloads")
    if attributes.get("machine_translated"):
        score -= 30
        reasons.append("Machine-translated flag")
    if attributes.get("hearing_impaired"):
        reasons.append("Hearing-impaired subtitle")
    return max(0, min(110, score)), reasons


def assess_subtitle_payload(file_name: str, payload: bytes) -> dict[str, Any]:
    """Check readable subtitle text; bitmap subtitles remain explicitly unknown."""
    extension = Path(file_name).suffix.lower()
    if extension == ".sup":
        return {"content_status": "unknown", "content_language": None, "content_confidence": None}
    try:
        text = payload.decode("utf-8-sig", errors="replace")
        text = re.sub(r"[{][^}]*[}]", " ", text)
        text = re.sub(r"(?m)^ *[0-9]+ *$", " ", text)
        text = re.sub(r"(?m)^.*[0-9]{2}:[0-9]{2}:[0-9]{2}[,.][0-9]+ *-->.*$", " ", text)
        text = text.replace(chr(92) + "N", " ").replace(chr(92) + "n", " ")
        text = re.sub(r"<[^>]+>", " ", text)
        sample = " ".join(text.split())[:12000]
        if len(sample) < 60:
            return {"content_status": "unknown", "content_language": None, "content_confidence": None}
        prediction = detect_langs(sample)[0]
        language = prediction.lang
        confidence = float(prediction.prob)
        status = "unknown" if confidence < 0.80 else ("english" if language == "en" else "other")
        return {
            "content_status": status,
            "content_language": language,
            "content_confidence": confidence,
        }
    except (UnicodeError, LangDetectException, IndexError, ValueError):
        return {"content_status": "unknown", "content_language": None, "content_confidence": None}

class OpenSubtitlesClient:
    def __init__(
        self,
        api_key: str,
        username: str = "",
        password: str = "",
        *,
        opener=None,
        sleeper=time.sleep,
    ):
        if not api_key.strip():
            raise OpenSubtitlesError("OpenSubtitles API key is not configured.")
        self.api_key = api_key.strip()
        self.username = username.strip()
        self.password = password
        self.base_url = API_BASE_URL
        self._token: str | None = None
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleeper
        self.remaining_downloads: int | None = None

    def _headers(self, token: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Api-Key": self.api_key,
            "User-Agent": USER_AGENT,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _json_request(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        token: str | None = None,
        retry_rate_limit: bool = True,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        if params:
            canonical = {
                key: str(value).lower()
                for key, value in params.items()
                if value is not None and value != ""
            }
            url += "?" + urllib.parse.urlencode(sorted(canonical.items()))
        headers = self._headers(token)
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with self._opener(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
                remaining = response.headers.get("x-ratelimit-remaining-second")
                if remaining is not None and int(remaining) <= 1:
                    self._sleep(1)
                if endpoint == "/login" and payload.get("base_url"):
                    returned = str(payload["base_url"]).rstrip("/")
                    parsed = urllib.parse.urlparse(returned)
                    if parsed.scheme == "https" and parsed.hostname and (
                        parsed.hostname == "opensubtitles.com"
                        or parsed.hostname.endswith(".opensubtitles.com")
                    ):
                        if not parsed.path or parsed.path == "/":
                            returned += "/api/v1"
                        elif not parsed.path.rstrip("/").endswith("/api/v1"):
                            returned = ""
                        if returned:
                            self.base_url = returned
                return payload
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and retry_rate_limit:
                self._sleep(2)
                return self._json_request(
                    endpoint, params=params, body=body, token=token,
                    retry_rate_limit=False
                )
            if exc.code in (401, 403):
                raise OpenSubtitlesError(
                    "OpenSubtitles rejected the API key or account credentials. Check the provider settings."
                ) from None
            if exc.code == 429:
                raise OpenSubtitlesError("OpenSubtitles rate limit reached. Try again later.") from None
            if exc.code >= 500:
                raise OpenSubtitlesError("OpenSubtitles is temporarily unavailable.") from None
            raise OpenSubtitlesError(f"OpenSubtitles request failed (HTTP {exc.code}).") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise OpenSubtitlesError("Could not reach OpenSubtitles or parse its response.") from None

    def login(self) -> str | None:
        if not self.username or not self.password:
            return None
        if self._token:
            return self._token
        result = self._json_request(
            "/login", body={"username": self.username, "password": self.password}
        )
        token = result.get("token")
        if not isinstance(token, str) or not token:
            raise OpenSubtitlesError("OpenSubtitles login did not return an access token.")
        self._token = token
        return token

    def search(self, media_path: Path, metadata: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        metadata = metadata or {}
        title = str(metadata.get("title") or "").strip()
        year = str(metadata.get("year") or "").strip()
        query = " ".join(part for part in (title, year) if part) or filename_query(media_path)
        params: dict[str, Any] = {"languages": "en", "query": query}
        moviehash = compute_moviehash(media_path)
        if moviehash:
            params["moviehash"] = moviehash
        imdb_id = str(metadata.get("imdb_id") or "").strip()
        if imdb_id:
            imdb_id = re.sub(r"^tt", "", imdb_id, flags=re.IGNORECASE).lstrip("0")
            if imdb_id.isdigit():
                params["imdb_id"] = imdb_id
        result = self._json_request("/subtitles", params=params)
        candidates: list[dict[str, Any]] = []
        for item in result.get("data", []):
            attributes = item.get("attributes") or {}
            if str(attributes.get("language", "")).lower() not in {"en", "eng"}:
                continue
            if attributes.get("machine_translated"):
                continue
            score, reasons = rank_candidate(attributes, media_path.stem)
            for subtitle_file in attributes.get("files") or []:
                file_name = str(subtitle_file.get("file_name") or "")
                extension = Path(file_name).suffix.lower()
                if extension not in SUPPORTED_EXTENSIONS:
                    continue
                file_id = subtitle_file.get("file_id")
                if not file_id:
                    continue
                candidates.append({
                    "subtitle_id": attributes.get("subtitle_id") or item.get("id"),
                    "file_id": int(file_id),
                    "file_name": Path(file_name).name,
                    "language": "en",
                    "format": extension.lstrip("."),
                    "release": str(attributes.get("release") or ""),
                    "moviehash_match": bool(attributes.get("moviehash_match")),
                    "download_count": int(attributes.get("download_count") or 0),
                    "rating": attributes.get("ratings"),
                    "trusted_uploader": bool(attributes.get("from_trusted")),
                    "hearing_impaired": bool(attributes.get("hearing_impaired")),
                    "ai_translated": bool(attributes.get("ai_translated")),
                    "machine_translated": False,
                    "match_score": score,
                    "match_reasons": reasons,
                })
        candidates.sort(
            key=lambda item: (
                item["moviehash_match"], item["match_score"], item["download_count"]
            ),
            reverse=True,
        )
        return candidates

    def download(self, file_id: int) -> tuple[str, bytes, int | None]:
        token = self.login()
        result = self._json_request(
            "/download", body={"file_id": int(file_id)}, token=token
        )
        link = result.get("link")
        if not isinstance(link, str):
            raise OpenSubtitlesError("OpenSubtitles did not provide a download link.")
        parsed = urllib.parse.urlparse(link)
        if parsed.scheme != "https" or not parsed.hostname or not (
            parsed.hostname == "opensubtitles.com"
            or parsed.hostname.endswith(".opensubtitles.com")
        ):
            raise OpenSubtitlesError("OpenSubtitles returned an unsafe download URL.")
        request = urllib.request.Request(link, headers={"User-Agent": USER_AGENT})
        try:
            with self._opener(request, timeout=60) as response:
                payload = response.read(MAX_DOWNLOAD_BYTES + 1)
                suggested_name = Path(
                    str(result.get("file_name") or urllib.parse.unquote(parsed.path.split("/")[-1]))
                ).name
        except (urllib.error.URLError, TimeoutError, OSError):
            raise OpenSubtitlesError("Subtitle download failed.") from None
        if len(payload) > MAX_DOWNLOAD_BYTES:
            raise OpenSubtitlesError("Subtitle download exceeded the 25 MiB safety limit.")
        if payload.startswith(bytes((0x50, 0x4B, 0x03, 0x04))):
            suggested_name, payload = self._extract_zip(payload, suggested_name)
        extension = Path(suggested_name).suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            raise OpenSubtitlesError("Downloaded file is not a supported subtitle format.")
        if not payload or payload.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            raise OpenSubtitlesError("Downloaded content is not a subtitle file.")
        if extension == ".srt" and not re.search(
            rb"[0-9]{2}:[0-9]{2}:[0-9]{2}[,.][0-9]{3} *--> *[0-9]{2}:[0-9]{2}:[0-9]{2}",
            payload,
        ):
            raise OpenSubtitlesError("Downloaded SRT file does not contain valid subtitle timing.")
        if extension in {".ass", ".ssa"}:
            text_payload = payload[:2 * 1024 * 1024].decode("utf-8-sig", errors="replace")
            if "[Events]" not in text_payload or "Dialogue:" not in text_payload:
                raise OpenSubtitlesError("Downloaded ASS/SSA file does not contain subtitle events.")
        if extension == ".sup" and len(payload) < 100:
            raise OpenSubtitlesError("Downloaded bitmap subtitle file is unexpectedly small.")
        remaining = result.get("remaining")
        self.remaining_downloads = int(remaining) if remaining is not None else None
        return suggested_name, payload, self.remaining_downloads

    @staticmethod
    def _extract_zip(payload: bytes, fallback_name: str) -> tuple[str, bytes]:
        try:
            with zipfile.ZipFile(BytesIO(payload)) as archive:
                matches = [
                    info for info in archive.infolist()
                    if not info.is_dir()
                    and Path(info.filename).suffix.lower() in SUPPORTED_EXTENSIONS
                    and Path(info.filename).name == info.filename
                    and info.file_size <= MAX_DOWNLOAD_BYTES
                ]
                if not matches:
                    raise OpenSubtitlesError("Archive contains no supported subtitle file.")
                selected = max(matches, key=lambda info: info.file_size)
                return Path(selected.filename).name, archive.read(selected)
        except zipfile.BadZipFile:
            raise OpenSubtitlesError("OpenSubtitles returned an invalid archive.") from None
