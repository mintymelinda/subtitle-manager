import json
import os
import tempfile
from pathlib import Path
from typing import Any


def settings_path() -> Path:
    return Path(os.environ.get("SUBTITLE_SETTINGS_PATH", "/app/data/opensubtitles-settings.json"))


def load_provider_settings() -> dict[str, Any]:
    path = settings_path()
    if path.is_file():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return {
                    "api_key": str(value.get("api_key") or ""),
                    "username": str(value.get("username") or ""),
                    "password": str(value.get("password") or ""),
                    "max_candidates_per_file": max(1, min(5, int(value.get("max_candidates_per_file", 2)))),
                    "log_level": (str(value.get("log_level") or "INFO").upper()
                                  if str(value.get("log_level") or "INFO").upper()
                                  in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
                                  else "INFO"),
                }
        except (OSError, ValueError, TypeError):
            raise RuntimeError("Saved OpenSubtitles settings could not be read. Check the persistent settings file.")
    return {
        "api_key": "",
        "username": "",
        "password": "",
        "max_candidates_per_file": 2,
        "log_level": "INFO",
    }


def save_provider_settings(*, api_key: str, username: str, password: str,
                           max_candidates_per_file: int,
                           log_level: str = "INFO",
                           clear_account_credentials: bool = False) -> dict[str, Any]:
    current = load_provider_settings()
    key = api_key.strip() or current["api_key"]
    if clear_account_credentials:
        username, password = "", ""
    else:
        username = username.strip() or current["username"]
        password = password or current["password"]
    if bool(username) != bool(password):
        raise ValueError("Enter both the OpenSubtitles username and password, or leave both blank.")
    if not key and (username or password):
        raise ValueError("An OpenSubtitles API key is required when account credentials are provided.")
    limit = int(max_candidates_per_file)
    if not 1 <= limit <= 5:
        raise ValueError("Candidate download limit must be between 1 and 5.")
    level = str(log_level or "INFO").upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("Logging level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL.")
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "api_key": key,
        "username": username,
        "password": password,
        "max_candidates_per_file": limit,
        "log_level": level,
    }, separators=(",", ":")) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".opensubtitles-settings-", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return public_provider_settings()


def public_provider_settings() -> dict[str, Any]:
    value = load_provider_settings()
    return {
        "configured": bool(value["api_key"]),
        "has_account_credentials": bool(value["username"] and value["password"]),
        "max_candidates_per_file": value["max_candidates_per_file"],
        "log_level": value["log_level"],
        "settings_storage": "persistent_volume",
    }


def test_provider_settings() -> None:
    from app.opensubtitles import OpenSubtitlesClient
    value = load_provider_settings()
    if not value["api_key"]:
        raise ValueError("Save an API key before testing the connection.")
    client = OpenSubtitlesClient(value["api_key"], value["username"], value["password"])
    # A read-only search validates the API key without consuming a subtitle download.
    client._json_request("/subtitles", params={"languages": "en", "query": "subtitle manager connection test"})
    if value["username"] and value["password"]:
        client.login()
