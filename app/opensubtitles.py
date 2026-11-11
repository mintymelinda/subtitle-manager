"""OpenSubtitles.com API client and candidate matching helpers.

This module never writes to the media library. Downloads are returned as bytes
for the caller to persist in the application's private staging directory.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any

from langdetect import DetectorFactory, detect_langs
from langdetect.lang_detect_exception import LangDetectException

from app.languages import normalize_languages, canonical_language, language_matches
from app.observability import logger, timed_stage

API_BASE_URL = "https://api.opensubtitles.com/api/v1"
USER_AGENT = "SubtitleManager v0.1.0"
HASH_CHUNK_SIZE = 64 * 1024
SUPPORTED_EXTENSIONS = {".srt", ".ass", ".ssa", ".sup"}
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
DetectorFactory.seed = 0


class OpenSubtitlesError(RuntimeError):
    """A sanitized provider or download error safe to show in the UI."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "provider_error",
        retryable: bool = False,
        http_status: int | None = None,
        retry_at: str | None = None,
        provider_error_detail: str | None = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
        self.http_status = http_status
        self.retry_at = retry_at
        self.provider_error_detail = provider_error_detail


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


def episode_numbers(path: Path) -> tuple[int, int] | None:
    """Extract season and episode numbers from common TV release filenames."""
    patterns = (
        r"(?i)(?:^|[^a-z0-9])s(?P<season>\d{1,2})[ ._-]*e(?P<episode>\d{1,3})(?:[^a-z0-9]|$)",
        r"(?i)(?:^|[^0-9])(?P<season>\d{1,2})x(?P<episode>\d{1,3})(?:[^0-9]|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, path.stem)
                                r"\b(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*UTC\b",
            return int(match.group("season")), int(match.group("episode"))
    return None


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


def assess_subtitle_payload(file_name: str, payload: bytes,
                            expected_language: str | None = None) -> dict[str, Any]:
    """Check readable subtitle text; bitmap subtitles remain explicitly unknown."""
    extension = Path(file_name).suffix.lower()
    if extension == ".sup":
        return {"content_status": "unknown", "content_language": None, "content_confidence": None}
    try:
        text = payload.decode("utf-8-sig", errors="replace")
        if extension in {".ass", ".ssa"}:
            dialogue = []
            for line in text.splitlines():
                if line.lower().startswith("dialogue:"):
                    fields = line.split(",", 9)
                    if len(fields) == 10:
                        dialogue.append(fields[9])
            text = " ".join(dialogue)
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
        if confidence < 0.80:
            status = "unknown"
        elif expected_language:
            status = "matched" if language_matches(language, expected_language) else "mismatch"
        else:
            status = "english" if language == "en" else "other"
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
        self.quota_retry_at: str | None = None
        self._login_lock = Lock()
        self._download_quota_lock = Lock()
        self._download_probe_lock = Lock()

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
            if exc.code == 406 and endpoint == "/download":
                response_body = b""
                try:
                    response_body = exc.read(16_384)
                except OSError:
                    pass
                try:
                    details = json.loads(response_body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    details = {}

                messages = []
                reset_values = []
                def inspect_error(value, key=""):
                    if isinstance(value, dict):
                        for child_key, child in value.items():
                            normalized_key = str(child_key).lower()
                            if normalized_key in {"message", "error", "detail", "description"} and isinstance(child, str):
                                messages.append(child[:500])
                            if normalized_key in {"reset", "reset_at", "reset_time", "reset_timestamp", "retry_at", "retry_after", "next_reset", "next_reset_at", "quota_reset", "quota_reset_at"}:
                                reset_values.append(child)
                            inspect_error(child, normalized_key)
                    elif isinstance(value, list):
                        for child in value[:10]:
                            inspect_error(child, key)
                inspect_error(details)
                provider_text = " ".join(messages).lower()
                # Keep only a short, sanitized provider message for diagnostics.
                # Never retain raw response bodies, URLs, or arbitrary response fields.
                provider_detail = messages[0] if messages else None
                if provider_detail:
                    from app.observability import redact
                    provider_detail = redact(provider_detail)
                    provider_detail = re.sub(r"https?://\S+", "[URL REDACTED]", provider_detail)
                    provider_detail = re.sub(r"[\r\n\t]+", " ", provider_detail)
                    provider_detail = "".join(ch for ch in provider_detail if ch.isprintable())[:240].strip()
                    provider_detail = provider_detail or None
                quota_match = any(term in provider_text for term in (
                    "quota exceeded", "daily limit", "download limit",
                    "download quota", "limit exceeded", "downloads limit",
                    "downloaded your allowed", "quota will be renewed",
                ))
                if quota_match:
                    retry_at = None
                    for value in reset_values:
                        try:
                            if isinstance(value, (int, float)) or str(value).isdigit():
                                seconds = float(value)
                                if seconds > 10_000_000_000:
                                    retry_at = datetime.fromtimestamp(seconds / 1000, timezone.utc)
                                elif seconds > 1_000_000_000:
                                    retry_at = datetime.fromtimestamp(seconds, timezone.utc)
                                else:
                                    retry_at = datetime.now(timezone.utc) + timedelta(seconds=max(0, seconds))
                            else:
                                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                                retry_at = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
                            break
                        except (TypeError, ValueError, OverflowError, OSError):
                            continue
                    # OpenSubtitles sometimes embeds the UTC renewal time in
                    # its human-readable quota message instead of a reset field.
                    if retry_at is None:
                        for message in messages:
                            match = re.search(
                                r"\\b(\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2})\\s*UTC\\b",
                                message,
                            )
                            if match:
                                try:
                                    retry_at = datetime.fromisoformat(
                                        match.group(1)
                                    ).replace(tzinfo=timezone.utc)
                                    break
                                except ValueError:
                                    continue
                    if retry_at is None:
                        retry_header = exc.headers.get("Retry-After") if exc.headers else None
                        try:
                            retry_at = datetime.now(timezone.utc) + timedelta(seconds=max(0, int(retry_header)))
                        except (TypeError, ValueError):
                            try:
                                retry_at = parsedate_to_datetime(retry_header).astimezone(timezone.utc)
                            except (TypeError, ValueError, OverflowError):
                                retry_at = datetime.now(timezone.utc) + timedelta(minutes=30)
                    self.quota_retry_at = retry_at.astimezone(timezone.utc).isoformat()
                    raise OpenSubtitlesError(
                        "OpenSubtitles download limit reached. Waiting before retrying.",
                        error_code="download_quota_reached",
                        retryable=True,
                        http_status=406,
                        retry_at=retry_at.astimezone(timezone.utc).isoformat(),
                        provider_error_detail=provider_detail,
                    ) from None
                raise OpenSubtitlesError(
                    "OpenSubtitles rejected this download request (HTTP 406).",
                    error_code="provider_request_rejected",
                    retryable=False,
                    http_status=406,
                    provider_error_detail=provider_detail,
                ) from None
            if exc.code == 429 and retry_rate_limit:
                self._sleep(2)
                return self._json_request(
                    endpoint, params=params, body=body, token=token,
                    retry_rate_limit=False
                )
            if exc.code in (401, 403):
                raise OpenSubtitlesError(
                    "OpenSubtitles rejected the API key or account credentials. Check the provider settings.",
                    error_code="authentication_rejected",
                    retryable=False,
                    http_status=exc.code,
                ) from None
            if exc.code == 429:
                raise OpenSubtitlesError(
                    "OpenSubtitles rate limit reached. Try again later.",
                    error_code="rate_limited",
                    retryable=True,
                    http_status=exc.code,
                ) from None
            if exc.code >= 500:
                raise OpenSubtitlesError(
                    "OpenSubtitles is temporarily unavailable.",
                    error_code="provider_unavailable",
                    retryable=True,
                    http_status=exc.code,
                ) from None
            raise OpenSubtitlesError(
                f"OpenSubtitles request failed (HTTP {exc.code}).",
                error_code="provider_request_rejected",
                retryable=False,
                http_status=exc.code,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise OpenSubtitlesError(
                "Could not reach OpenSubtitles.",
                error_code="provider_connection_failed",
                retryable=True,
            ) from None
        except ValueError:
            raise OpenSubtitlesError(
                "OpenSubtitles returned an invalid response.",
                error_code="invalid_provider_response",
                retryable=True,
            ) from None

    def login(self) -> str | None:
        if not self.username or not self.password:
            return None
        with self._login_lock:
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

    def download_if_available(self, file_id: int) -> tuple[str, bytes, int | None] | None:
        """Reserve quota safely while allowing concurrent downloads once known."""
        if self.quota_retry_at is not None:
            return None
        with self._download_quota_lock:
            if self.remaining_downloads is not None:
                if self.remaining_downloads == 0:
                    return None
                self.remaining_downloads -= 1
                quota_known = True
            else:
                quota_known = False
        if quota_known:
            return self.download(file_id)

        # Until the API reports the account's remaining quota, permit only one
        # in-flight download. Later calls can reserve known slots concurrently.
        with self._download_probe_lock:
            with self._download_quota_lock:
                if self.remaining_downloads is not None:
                    if self.remaining_downloads == 0:
                        return None
                    self.remaining_downloads -= 1
                    quota_known = True
                else:
                    quota_known = False
            if not quota_known:
                return self.download(file_id)
        return self.download(file_id)

    def search(self, media_path: Path, metadata: dict[str, Any] | None = None,
               *, languages: list[str] | None = None) -> list[dict[str, Any]]:
        metadata = metadata or {}
        # OpenSubtitles recommends searching by the actual filename alongside
        # the movie hash. Container titles often contain only a TV series name,
        # which drops the season/episode/release details and can return no match.
        query = filename_query(media_path)
        target_languages = normalize_languages(languages)
        params: dict[str, Any] = {"languages": ",".join(sorted(target_languages)), "query": query}
        with timed_stage("moviehash_calculation", media_path=str(media_path),
                         tool="opensubtitles"):
            moviehash = compute_moviehash(media_path)
        if moviehash:
            params["moviehash"] = moviehash
        imdb_id = str(metadata.get("imdb_id") or "").strip()
        if imdb_id:
            imdb_id = re.sub(r"^tt", "", imdb_id, flags=re.IGNORECASE).lstrip("0")
            if imdb_id.isdigit():
                episode = episode_numbers(media_path)
                if episode:
                    params["parent_imdb_id"] = imdb_id
                    params["season_number"], params["episode_number"] = episode
                else:
                    params["imdb_id"] = imdb_id
        with timed_stage("opensubtitles_provider_search", media_path=str(media_path),
                         tool="opensubtitles"):
            result = self._json_request("/subtitles", params=params)
        provider_items = result.get("data", [])
        candidates: list[dict[str, Any]] = []
        filtered: dict[str, int] = {}
        logger.debug(
            "opensubtitles_provider_results",
            extra={"event": "opensubtitles_provider_results", "media_path": str(media_path),
                   "phase": "candidate_selection", "provider_count": len(provider_items)},
        )

        def filtered_result(reason: str, **details) -> None:
            filtered[reason] = filtered.get(reason, 0) + 1
            logger.debug(
                "opensubtitles_candidate_filtered reason=%s", reason,
                extra={"event": "opensubtitles_candidate_filtered",
                       "media_path": str(media_path), "phase": "candidate_selection",
                       "filter_reason": reason, **details},
            )

        with timed_stage("opensubtitles_result_filtering",
                         media_path=str(media_path), tool="opensubtitles",
                         provider_result_count=len(provider_items)):
            for item in provider_items:
                attributes = item.get("attributes") or {}
                language = canonical_language(attributes.get("language"))
                if language not in target_languages:
                    filtered_result("unsupported_language", language=language or "missing")
                    continue
                if attributes.get("machine_translated"):
                    filtered_result("machine_translated")
                    continue
                score, reasons = rank_candidate(attributes, media_path.stem)
                files = attributes.get("files") or []
                if not files:
                    filtered_result("no_files")
                    continue
                for subtitle_file in files:
                    file_name = Path(str(subtitle_file.get("file_name") or "")).name
                    file_id = subtitle_file.get("file_id")
                    try:
                        file_id = int(file_id)
                    except (TypeError, ValueError):
                        filtered_result("missing_file_id", file_name=file_name)
                        continue
                    if file_id <= 0:
                        filtered_result("missing_file_id", file_name=file_name)
                        continue
                    # OpenSubtitles file_name is descriptive metadata, not a reliable
                    # format declaration. Some valid files are named *.eng, *.en, or
                    # have no extension. Keep identifiable downloads and validate
                    # the actual downloaded filename and payload in download().
                    extension = Path(file_name).suffix.lower()
                    subtitle_format = extension.lstrip(".") if extension in SUPPORTED_EXTENSIONS else ""
                    candidates.append({
                        "subtitle_id": attributes.get("subtitle_id") or item.get("id"),
                        "file_id": file_id,
                        "file_name": file_name,
                        "language": language,
                        "format": subtitle_format,
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
                    logger.debug(
                        "opensubtitles_candidate_accepted file=%s", Path(file_name).name,
                        extra={"event": "opensubtitles_candidate_accepted",
                               "media_path": str(media_path), "phase": "candidate_selection",
                               "file_name": file_name, "file_id": file_id,
                               "language": language, "match_score": score,
                               "match_reasons": reasons,
                               "moviehash_match": bool(attributes.get("moviehash_match")),
                               "download_count": int(attributes.get("download_count") or 0)},
                    )
            candidates.sort(
                key=lambda item: (
                    item["moviehash_match"], item["match_score"], item["download_count"]
                ),
                reverse=True,
            )
            logger.info(
                "opensubtitles_candidate_selection_summary provider_count=%d accepted_count=%d filtered_count=%d",
                len(provider_items), len(candidates), sum(filtered.values()),
                extra={"event": "opensubtitles_candidate_selection_summary",
                       "media_path": str(media_path), "phase": "candidate_selection",
                       "provider_count": len(provider_items), "accepted_count": len(candidates),
                       "filtered_count": sum(filtered.values())},
            )
            for reason, count in sorted(filtered.items()):
                logger.debug(
                    "opensubtitles_candidate_filter_summary",
                    extra={"event": "opensubtitles_candidate_filter_summary",
                           "media_path": str(media_path), "phase": "candidate_selection",
                           "filter_reason": reason, "filtered_count": count},
                )
            return candidates

    def download(self, file_id: int) -> tuple[str, bytes, int | None]:
        token = self.login()
        result = self._json_request(
            "/download", body={"file_id": int(file_id)}, token=token
        )
        link = result.get("link")
        if not isinstance(link, str):
            raise OpenSubtitlesError(
                "OpenSubtitles did not provide a download link.",
                error_code="download_link_missing",
                retryable=True,
            )
        parsed = urllib.parse.urlparse(link)
        if parsed.scheme != "https" or not parsed.hostname or not (
            parsed.hostname == "opensubtitles.com"
            or parsed.hostname.endswith(".opensubtitles.com")
        ):
            raise OpenSubtitlesError(
                "OpenSubtitles returned an unsafe download URL.",
                error_code="unsafe_download_url",
                retryable=False,
            )
        request = urllib.request.Request(link, headers={"User-Agent": USER_AGENT})
        try:
            with self._opener(request, timeout=60) as response:
                payload = response.read(MAX_DOWNLOAD_BYTES + 1)
                suggested_name = Path(
                    str(result.get("file_name") or urllib.parse.unquote(parsed.path.split("/")[-1]))
                ).name
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                raise OpenSubtitlesError(
                    "OpenSubtitles rejected or expired the subtitle download link.",
                    error_code="download_link_rejected",
                    retryable=True,
                    http_status=exc.code,
                ) from None
            if exc.code == 429:
                raise OpenSubtitlesError(
                    "OpenSubtitles download rate limit reached. Try again later.",
                    error_code="download_rate_limited",
                    retryable=True,
                    http_status=exc.code,
                ) from None
            if exc.code >= 500:
                raise OpenSubtitlesError(
                    "OpenSubtitles download service is temporarily unavailable.",
                    error_code="download_service_unavailable",
                    retryable=True,
                    http_status=exc.code,
                ) from None
            raise OpenSubtitlesError(
                f"Subtitle download request failed (HTTP {exc.code}).",
                error_code="download_request_rejected",
                retryable=False,
                http_status=exc.code,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise OpenSubtitlesError(
                "Subtitle download could not reach the provider.",
                error_code="download_connection_failed",
                retryable=True,
            ) from None
        if len(payload) > MAX_DOWNLOAD_BYTES:
            raise OpenSubtitlesError(
                "Subtitle download exceeded the 25 MiB safety limit.",
                error_code="download_too_large",
                retryable=False,
            )
        if payload.startswith(bytes((0x50, 0x4B, 0x03, 0x04))):
            suggested_name, payload = self._extract_zip(payload, suggested_name)
        extension = Path(suggested_name).suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            raise OpenSubtitlesError(
                "Downloaded file is not a supported subtitle format.",
                error_code="unsupported_subtitle_format",
                retryable=False,
            )
        if not payload or payload.lstrip().lower().startswith((b"<!doctype html", b"<html")):
            raise OpenSubtitlesError(
                "Downloaded content is not a subtitle file.",
                error_code="invalid_subtitle_payload",
                retryable=False,
            )
        if extension == ".srt" and not re.search(
            rb"[0-9]{2}:[0-9]{2}:[0-9]{2}[,.][0-9]{3} *--> *[0-9]{2}:[0-9]{2}:[0-9]{2}",
            payload,
        ):
            raise OpenSubtitlesError(
                "Downloaded SRT file does not contain valid subtitle timing.",
                error_code="invalid_srt_timing",
                retryable=False,
            )
        if extension in {".ass", ".ssa"}:
            text_payload = payload[:2 * 1024 * 1024].decode("utf-8-sig", errors="replace")
            if "[Events]" not in text_payload or "Dialogue:" not in text_payload:
                raise OpenSubtitlesError(
                    "Downloaded ASS/SSA file does not contain subtitle events.",
                    error_code="invalid_ass_payload",
                    retryable=False,
                )
        if extension == ".sup" and len(payload) < 100:
            raise OpenSubtitlesError(
                "Downloaded bitmap subtitle file is unexpectedly small.",
                error_code="invalid_sup_payload",
                retryable=False,
            )
        remaining = result.get("remaining")
        if remaining is not None:
            reported_remaining = int(remaining)
            with self._download_quota_lock:
                self.remaining_downloads = (
                    reported_remaining if self.remaining_downloads is None
                    else min(self.remaining_downloads, reported_remaining)
                )
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
                    raise OpenSubtitlesError(
                        "Archive contains no supported subtitle file.",
                        error_code="archive_without_subtitle",
                        retryable=False,
                    )
                selected = max(matches, key=lambda info: info.file_size)
                return Path(selected.filename).name, archive.read(selected)
        except zipfile.BadZipFile:
            raise OpenSubtitlesError(
                "OpenSubtitles returned an invalid archive.",
                error_code="invalid_archive",
                retryable=False,
            ) from None
