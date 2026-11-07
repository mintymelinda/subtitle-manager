from pathlib import Path
import os
import subprocess
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, FileResponse, PlainTextResponse

from app.config import settings
from app.database import (
    initialize, record_scan, save_candidate, mark_candidate_staged,
    mark_candidate_error, list_candidates, set_candidate_decision,
)
from app.media import inspect_media
from app.opensubtitles import OpenSubtitlesClient, OpenSubtitlesError, assess_subtitle_payload
from app.scanner import scan_media

app = FastAPI(title=settings.app_name)


class ScanRequest(BaseModel):
    path: str = "/media"


class CandidateDecisionRequest(BaseModel):
    decision: str


@app.on_event("startup")
def startup():
    initialize()


def media_path_is_safe(path: Path, root: Path) -> bool:
    try:
        return path.resolve(strict=True).is_relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError):
        return False


def stage_candidates(client: OpenSubtitlesClient, path: Path, media_root: Path,
                     stat_result, metadata: dict) -> dict:
    """Search and stage the top candidates outside the media mount."""
    relative_media_path = str(path.relative_to(media_root))
    staging_root = Path(settings.subtitle_staging_path).resolve()
    if staging_root == media_root or staging_root.is_relative_to(media_root):
        raise OpenSubtitlesError("Subtitle staging directory must be outside the media library.")
    candidates = client.search(path, metadata)
    summary = {"status": "no_candidates" if not candidates else "complete",
               "found": len(candidates), "staged": 0, "candidates": []}
    limit = max(0, min(5, int(settings.open_subtitles_max_candidates_per_file)))
    for candidate in candidates[:limit]:
        row = save_candidate(
            relative_media_path, stat_result.st_size, stat_result.st_mtime, candidate
        )
        status = row["status"]
        assessment = {
            "content_status": row.get("content_status", "unknown"),
            "content_language": row.get("content_language"),
            "content_confidence": row.get("content_language_confidence"),
        }
        if status != "staged":
            if client.remaining_downloads == 0:
                status = "download_quota_reached"
            else:
                try:
                    file_name, payload, remaining = client.download(candidate["file_id"])
                    assessment = assess_subtitle_payload(file_name, payload)
                    if assessment["content_status"] == "other":
                        mark_candidate_error(
                            row["id"],
                            "Downloaded subtitle content does not appear to be English.",
                            status="rejected_non_english",
                        )
                        status = "rejected_non_english"
                    else:
                        staging_root.mkdir(parents=True, exist_ok=True)
                        extension = Path(file_name).suffix.lower()
                        target = staging_root / f"candidate-{row['id']}{extension}"
                        temporary = staging_root / f".candidate-{row['id']}-{uuid.uuid4().hex}.part"
                        descriptor = os.open(
                            temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                        )
                        with os.fdopen(descriptor, "wb") as staged_file:
                            staged_file.write(payload)
                            staged_file.flush()
                            os.fsync(staged_file.fileno())
                        os.replace(temporary, target)
                        mark_candidate_staged(row["id"], str(target), remaining, assessment)
                        status = "staged"
                except OpenSubtitlesError as exc:
                    mark_candidate_error(row["id"], str(exc))
                    status = "download_failed"
                except OSError:
                    mark_candidate_error(row["id"], "Could not write the staged subtitle file.")
                    status = "download_failed"
        if status == "staged":
            summary["staged"] += 1
        summary["candidates"].append({
            "id": row["id"],
            "subtitle_id": candidate["subtitle_id"],
            "file_id": candidate["file_id"],
            "file_name": candidate["file_name"],
            "language": candidate["language"],
            "format": candidate["format"],
            "release": candidate["release"],
            "moviehash_match": candidate["moviehash_match"],
            "download_count": candidate["download_count"],
            "trusted_uploader": candidate["trusted_uploader"],
            "hearing_impaired": candidate["hearing_impaired"],
            "ai_translated": candidate["ai_translated"],
            "match_score": candidate["match_score"],
            "match_reasons": candidate["match_reasons"],
            "content_status": assessment.get("content_status"),
            "content_language": assessment.get("content_language"),
            "content_confidence": assessment.get("content_confidence"),
            "status": status,
        })
    return summary

@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse("""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Subtitle Manager</title><style>
body{font:16px system-ui;margin:2rem auto;padding:0 1rem;max-width:1000px;background:#111827;color:#e5e7eb}
h1{margin-bottom:.25rem}p{color:#aebbd0}.panel{background:#1f2937;padding:1rem;border-radius:12px;margin:1rem 0}
button{background:#2563eb;color:white;border:0;border-radius:8px;padding:.7rem 1rem;cursor:pointer}
button:disabled{opacity:.5;cursor:not-allowed}pre{white-space:pre-wrap;overflow-wrap:anywhere}
.notice{border-left:4px solid #eab308;padding:.7rem 1rem;background:#292524}
</style></head><body><h1>Subtitle Manager</h1>
<p>Find missing English subtitles and review candidates before changing media.</p>
<div class="notice"><strong>Safety:</strong> scanning and subtitle downloads never change media files.
Every media patch requires explicit per-file approval.</div>
<section class="panel"><h2>Media scan</h2><label for="scan-path">Folder to scan</label><br><input id="scan-path" type="text" value="/media" style="box-sizing:border-box;width:100%;padding:.7rem;margin:.5rem 0 1rem;border-radius:8px;border:1px solid #64748b;background:#111827;color:#e5e7eb"><p id="root">Mounted media is available under /media. Enter a folder within that mount.</p>
<button id="scan">Start scan</button><p id="status">Ready.</p><pre id="results"></pre></section>
<script>
const scan=document.querySelector('#scan'),status=document.querySelector('#status'),results=document.querySelector('#results');
scan.addEventListener('click',async()=>{scan.disabled=true;status.textContent='Scanning…';results.textContent='';
try{const r=await fetch('/api/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:document.querySelector('#scan-path').value})});const data=await r.json();if(!r.ok)throw Error(data.detail||r.statusText);
status.textContent='Scan complete: '+data.files.length+' media files inspected.';results.textContent=JSON.stringify(data.files,null,2);
}catch(e){status.textContent='Scan failed: '+e.message;}finally{scan.disabled=false;}});
</script></body></html>""")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/config")
def config():
    return {
        "media_root": settings.media_root,
        "test_mode": settings.test_mode,
        "opensubtitles_configured": bool(settings.open_subtitles_api_key.strip()),
        "candidate_staging_enabled": bool(settings.open_subtitles_api_key.strip()),
    }


@app.post("/api/scan")
def scan(request: ScanRequest):
    media_root = Path(settings.media_root).resolve()
    requested = Path(request.path.strip() or "/media")
    if requested.is_absolute() and str(requested).startswith("/media/"):
        requested = Path(str(requested)[len("/media/"):])
    elif requested.is_absolute() and str(requested) == "/media":
        requested = Path(".")
    elif requested.is_absolute():
        raise HTTPException(status_code=400, detail="Choose a folder inside the mounted /media directory.")
    if ".." in requested.parts:
        raise HTTPException(status_code=400, detail="Parent-directory paths are not allowed.")
    root = (media_root / requested).resolve()
    if not root.is_relative_to(media_root):
        raise HTTPException(status_code=400, detail="The selected folder must be inside /media.")
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="The selected folder does not exist or is not a directory.")
    try:
        paths = list(scan_media(root))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    client = None
    if settings.open_subtitles_api_key.strip():
        client = OpenSubtitlesClient(
            settings.open_subtitles_api_key,
            settings.open_subtitles_username,
            settings.open_subtitles_password,
        )

    inspected = []
    for path in paths:
        if not media_path_is_safe(path, root):
            continue
        try:
            details = inspect_media(path)
            stat_result = path.stat()
            relative_path = str(path.relative_to(root))
            media_relative_path = str(path.relative_to(media_root))
            record_scan(media_relative_path, stat_result.st_size, stat_result.st_mtime, details)
            candidate_summary = {"status": "not_configured", "found": 0, "staged": 0, "candidates": []}
            if details["has_verified_english_subtitle"]:
                candidate_summary["status"] = "verified_english_present"
            elif client is not None:
                try:
                    candidate_summary = stage_candidates(
                        client, path, media_root, stat_result, details.get("metadata") or {}
                    )
                except OpenSubtitlesError as exc:
                    candidate_summary = {
                        "status": "provider_error", "found": 0, "staged": 0,
                        "candidates": [], "error": str(exc)
                    }
            inspected.append({
                "path": relative_path,
                "size": details["size"],
                "duration": details["duration"],
                "video_streams": details["video_streams"],
                "audio_streams": details["audio_streams"],
                "subtitle_streams": details["subtitle_streams"],
                "metadata": details.get("metadata", {}),
                "has_verified_english_subtitle": details["has_verified_english_subtitle"],
                "subtitle_content_assessment": details["subtitle_content_assessment"],
                "needs_review": not details["has_verified_english_subtitle"],
                "subtitle_candidates": candidate_summary,
            })
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            inspected.append({"path": str(path.relative_to(root)), "error": str(exc)})
    return {
        "files": inspected,
        "count": len(inspected),
        "test_mode": settings.test_mode,
        "opensubtitles_configured": client is not None,
    }
