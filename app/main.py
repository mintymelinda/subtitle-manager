from pathlib import Path
import os
import subprocess
import uuid

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, FileResponse, PlainTextResponse

from app.config import settings
from app.observability import configure_logging, logger

from app.database import (
    initialize, record_scan, save_candidate, mark_candidate_staged,
    mark_candidate_error, list_candidates, list_media_files, set_candidate_decision,
    get_candidate, claim_candidate_patch, finish_candidate_patch, recover_interrupted_patches,
    create_job, start_job, update_job_progress, record_job_item, finish_job,
    get_job, list_jobs, recover_interrupted_jobs,
)
from app.media import inspect_media
from app.opensubtitles import OpenSubtitlesClient, OpenSubtitlesError, assess_subtitle_payload
from app.scanner import scan_media
from app.patcher import patch_mkv

app = FastAPI(title=settings.app_name)


class ScanRequest(BaseModel):
    path: str = "/media"


class CandidateDecisionRequest(BaseModel):
    decision: str


class InjectRequest(BaseModel):
    confirm: bool = False


@app.on_event("startup")
def startup():
    configure_logging()
    initialize()
    patches = recover_interrupted_patches()
    jobs = recover_interrupted_jobs()
    logger.warning("startup_recovery", extra={"event":"startup_recovery","status":"interrupted","error_type":f"patches={patches},jobs={jobs}"})


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
body{font:16px system-ui;margin:2rem auto;padding:0 1rem;max-width:1100px;background:#111827;color:#e5e7eb}
h1{margin-bottom:.25rem}p{color:#aebbd0}.panel{background:#1f2937;padding:1rem;border-radius:12px;margin:1rem 0}
button{background:#2563eb;color:white;border:0;border-radius:8px;padding:.7rem 1rem;cursor:pointer}
button:disabled{opacity:.5;cursor:not-allowed}.notice{border-left:4px solid #eab308;padding:.7rem 1rem;background:#292524}
.candidate{background:#111827;border:1px solid #374151;border-radius:10px;padding:1rem;margin:1rem 0;min-width:0}
.candidate h3,.candidate p{overflow-wrap:anywhere}.actions{display:flex;flex-wrap:wrap;gap:.8rem;margin-top:1rem}
.actions a{color:#93c5fd;padding:.5rem 0}.secondary{background:#4b5563}.warning{color:#fbbf24}
input{box-sizing:border-box;width:100%;padding:.7rem;margin:.5rem 0 1rem;border-radius:8px;border:1px solid #64748b;background:#111827;color:#e5e7eb}
</style></head><body><h1>Subtitle Manager</h1>
<p>Find missing English subtitles and review candidates before changing media.</p>
<div class="notice"><strong>Safety:</strong> scanning and downloads never change media. Injection requires a separately approved candidate and a second confirmation. Current mode: <strong id="mode">loading…</strong>.</div>
<section class="panel"><h2>Media scan</h2><label for="scan-path">Folder to scan</label><input id="scan-path" type="text" value="/media"><p>Enter a folder within the mounted /media directory.</p>
<button id="scan">Start scan</button><p id="status">Ready.</p><div id="scan-results"></div></section>
<section class="panel"><h2>Subtitle candidate review</h2><p>Every candidate is shown under its exact media file. Inspect the release, match rationale, and subtitle content before deciding.</p><div id="candidate-list">Loading saved candidates…</div></section>
<script>
const scanButton=document.querySelector("#scan"),status=document.querySelector("#status"),scanResults=document.querySelector("#scan-results"),candidateList=document.querySelector("#candidate-list");
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function line(parent,label,value){const p=el("p");p.append(el("strong",label+": "),document.createTextNode(value===null||value===undefined?"Unknown":String(value)));parent.append(p);}
function streams(value){if(!Array.isArray(value)||!value.length)return "None reported";return value.map(s=>[s.codec,s.language,s.title].filter(Boolean).join(" · ")||JSON.stringify(s)).join("; ");}
function renderScan(files){scanResults.replaceChildren();for(const f of files){const card=el("article",undefined,"candidate");card.append(el("h3",f.path));card.append(el("p",f.error?"Scan error":(f.has_verified_english_subtitle?"Verified English subtitle present":"Needs subtitle review")));line(card,"Audio tracks",streams(f.audio_streams));line(card,"Existing subtitle tracks",streams(f.subtitle_streams));if(f.subtitle_candidates)line(card,"Candidate search",f.subtitle_candidates.status+" — "+f.subtitle_candidates.staged+" staged");scanResults.append(card);}}
async function loadCandidates(){const r=await fetch("/api/candidates");const d=await r.json();if(!r.ok)throw Error(d.detail||r.statusText);renderCandidates(d.candidates);}
function renderCandidates(items){candidateList.replaceChildren();if(!items.length){candidateList.append(el("p","No saved candidates yet. Run a scan with OpenSubtitles configured."));return;}
for(const c of items){const card=el("article",undefined,"candidate");card.append(el("h3",c.media_relative_path));card.append(el("p","Candidate #"+c.id+" · "+c.file_name));line(card,"Provider language / format",c.language+" / "+c.format);line(card,"Release",c.release||"Not provided");line(card,"Match score",c.match_score);line(card,"Exact file hash match",c.moviehash_match?"Yes":"No");line(card,"Candidate download status",c.status);line(card,"Match rationale",(c.match_reasons||[]).join("; ")||"No rationale supplied");line(card,"Content check",c.content_status+(c.content_language?" · "+c.content_language:"")+(c.content_language_confidence?" · confidence "+Number(c.content_language_confidence).toFixed(2):""));line(card,"Existing audio tracks",streams(c.inspection?.audio_streams));line(card,"Existing subtitle tracks",streams(c.inspection?.subtitle_streams));
if(c.is_stale)card.append(el("p","This candidate is stale: the media file changed after discovery. Rescan before reviewing.","warning"));
card.append(el("p","Review decision: "+c.review_decision+(c.review_updated_at?" · "+c.review_updated_at:"")));
line(card,"Injection status",c.patch_status||"not_started");
if(c.patch_status==="interrupted")card.append(el("p","An earlier injection was interrupted. Inspect the media file and retained recovery copy manually before proceeding.","warning"));
if(c.status==="staged"){const links=el("div",undefined,"actions"),preview=el("a","Preview subtitle"),download=el("a","Download candidate");preview.href="/api/candidates/"+c.id+"/preview";preview.target="_blank";preview.rel="noopener";download.href="/api/candidates/"+c.id+"/download";links.append(preview,download);card.append(links);}else if(c.last_error){card.append(el("p",c.last_error,"warning"));}
if(c.status==="staged"&&c.review_decision==="pending"&&!c.is_stale){const actions=el("div",undefined,"actions"),approve=el("button","Approve this candidate"),reject=el("button","Reject this candidate");reject.className="secondary";approve.addEventListener("click",()=>decide(c.id,"approved",approve));reject.addEventListener("click",()=>decide(c.id,"rejected",reject));actions.append(approve,reject);card.append(actions);}
if(c.status==="staged"&&c.review_decision==="approved"&&!c.is_stale&&c.patch_status!=="patched"&&c.patch_status!=="running"&&c.patch_status!=="interrupted"){const injectButton=el("button",c.patch_status==="failed"?"Retry injection":"Inject approved subtitle");injectButton.addEventListener("click",()=>inject(c.id,injectButton));card.append(injectButton);}
candidateList.append(card);}}
async function decide(id,decision,button){if(!confirm((decision==="approved"?"Approve":"Reject")+" this subtitle for this exact media file?"))return;button.disabled=true;try{const r=await fetch("/api/candidates/"+id+"/decision",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({decision})});const d=await r.json();if(!r.ok)throw Error(d.detail||r.statusText);await loadCandidates();}catch(e){alert("Could not save decision: "+e.message);button.disabled=false;}}
async function inject(id,button){const mode=document.querySelector("#mode").textContent;const warning=mode==="TEST MODE"?"TEST MODE will replace this MKV and retain a recovery copy beside it.":"LIVE MODE will replace this MKV; its recovery copy is deleted only after post-replacement verification.";if(!confirm(warning+"\n\nInject the explicitly approved subtitle into this exact media file now?"))return;button.disabled=true;try{const r=await fetch("/api/candidates/"+id+"/inject",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({confirm:true})});const d=await r.json();if(!r.ok)throw Error(d.detail||r.statusText);alert("Subtitle injection completed. "+(d.backup?"Recovery copy retained at: "+d.backup:"Temporary recovery copy removed after verification."));await loadCandidates();}catch(e){alert("Injection failed: "+e.message);button.disabled=false;await loadCandidates();}}
fetch("/api/config").then(r=>r.json()).then(c=>{document.querySelector("#mode").textContent=c.test_mode?"TEST MODE":"LIVE MODE";}).catch(()=>{document.querySelector("#mode").textContent="unknown";});
scanButton.addEventListener("click",async()=>{scanButton.disabled=true;status.textContent="Scanning media and staging candidates…";scanResults.replaceChildren();try{const r=await fetch("/api/scan",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({path:document.querySelector("#scan-path").value})});const d=await r.json();if(!r.ok)throw Error(d.detail||r.statusText);status.textContent="Scan complete: "+d.files.length+" media files inspected.";renderScan(d.files);await loadCandidates();}catch(e){status.textContent="Scan failed: "+e.message;}finally{scanButton.disabled=false;}});
loadCandidates().catch(e=>candidateList.textContent="Could not load candidates: "+e.message);
fetch("/api/media").then(r=>r.json()).then(d=>renderScan(d.media.map(m=>({path:m.relative_path,...m.inspection,has_verified_english_subtitle:m.has_verified_english_subtitle})))).catch(()=>{});
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


@app.get("/api/media")
def scanned_media():
    return {"media": list_media_files()}


@app.get("/api/candidates")
def candidates():
    rows = list_candidates()
    media_root = Path(settings.media_root).resolve()
    for row in rows:
        media_path = (media_root / row["media_relative_path"]).resolve()
        if not media_path.is_relative_to(media_root) or not media_path.is_file():
            row["is_stale"] = True
        else:
            current = media_path.stat()
            row["is_stale"] = (
                current.st_size != row["media_size"]
                or current.st_mtime != row["media_modified_at"]
            )
    fields = (
        "id", "media_relative_path", "file_name", "language", "format",
        "content_language", "content_language_confidence", "content_status",
        "release", "moviehash_match", "download_count", "rating",
        "trusted_uploader", "hearing_impaired", "ai_translated", "match_score",
        "match_reasons", "status", "review_decision", "review_updated_at",
        "last_error", "is_stale", "inspection", "patch_status", "patch_started_at", "patch_completed_at",
    )
    return {"candidates": [{key: row.get(key) for key in fields} for row in rows], "count": len(rows)}


def get_staged_candidate(candidate_id: int) -> tuple[dict, Path]:
    row = next((item for item in list_candidates() if item["id"] == candidate_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail="Candidate not found.")
    if row["status"] != "staged" or not row.get("staged_path"):
        raise HTTPException(status_code=409, detail="This candidate has not been staged successfully.")
    staging_root = Path(settings.subtitle_staging_path).resolve()
    staged_path = Path(row["staged_path"]).resolve()
    if not staged_path.is_relative_to(staging_root) or not staged_path.is_file():
        raise HTTPException(status_code=404, detail="Staged subtitle file is unavailable.")
    return row, staged_path


@app.get("/api/candidates/{candidate_id}/preview")
def preview_candidate(candidate_id: int):
    row, staged_path = get_staged_candidate(candidate_id)
    if row["format"] == "sup":
        raise HTTPException(status_code=415, detail="Bitmap subtitles cannot be previewed as text; download the file to inspect it.")
    try:
        preview_text = staged_path.read_bytes()[:200_000].decode("utf-8-sig", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Could not read the staged subtitle.") from exc
    return PlainTextResponse(preview_text, headers={"Content-Disposition": "inline"})


@app.get("/api/candidates/{candidate_id}/download")
def download_candidate(candidate_id: int):
    row, staged_path = get_staged_candidate(candidate_id)
    return FileResponse(staged_path, filename=row["file_name"], media_type="application/octet-stream")


@app.post("/api/candidates/{candidate_id}/decision")
def candidate_decision(candidate_id: int, request: CandidateDecisionRequest):
    if request.decision not in {"approved", "rejected"}:
        raise HTTPException(status_code=400, detail="Decision must be approved or rejected.")
    row, _ = get_staged_candidate(candidate_id)
    media_root = Path(settings.media_root).resolve()
    media_path = (media_root / row["media_relative_path"]).resolve()
    if not media_path.is_relative_to(media_root) or not media_path.is_file():
        raise HTTPException(status_code=409, detail="The associated media file is no longer available. Rescan the folder.")
    stat_result = media_path.stat()
    if stat_result.st_size != row["media_size"] or stat_result.st_mtime != row["media_modified_at"]:
        raise HTTPException(status_code=409, detail="The media file changed after this candidate was found. Rescan before reviewing.")
    try:
        decision = set_candidate_decision(candidate_id, request.decision)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if decision is None:
        raise HTTPException(status_code=404, detail="Candidate not found.")
    return decision


def _run_injection_job(job_id: int, candidate_id: int, row: dict,
                       source: Path, subtitle: Path):
    start_job(job_id)
    update_job_progress(job_id, phase="muxing", current_file=row["media_relative_path"], total=1)
    logger.info("injection_started", extra={"event":"injection_started","job_id":job_id,
                "candidate_id":candidate_id,"media_path":row["media_relative_path"],"phase":"muxing"})
    pattern = f".{source.name}.recovery-*.bak"
    before = set(source.parent.glob(pattern))
    try:
        result = patch_mkv(source, subtitle, approved=True, test_mode=settings.test_mode,
                           expected_size=row["media_size"], expected_mtime=row["media_modified_at"])
        finish_candidate_patch(candidate_id, "patched", result, result.get("backup"))
        record_job_item(job_id, media_path=row["media_relative_path"], status="completed",
                        phase="verified", candidate_id=candidate_id, review_decision="approved",
                        patch_status="patched", details=result)
        update_job_progress(job_id, phase="completed", completed=1, succeeded=1)
        finish_job(job_id, "completed")
        logger.info("injection_completed", extra={"event":"injection_completed","job_id":job_id,
                    "candidate_id":candidate_id,"media_path":row["media_relative_path"],
                    "phase":"verified","status":"patched"})
    except Exception as exc:
        backups = set(source.parent.glob(pattern)) - before
        backup = str(max(backups, key=lambda p:p.stat().st_mtime_ns)) if backups else None
        error = f"{type(exc).__name__}: {str(exc)[:400]}"
        try:
            finish_candidate_patch(candidate_id, "failed", {"error":error,"recovery_path":backup}, backup)
        except Exception:
            logger.exception("patch_history_write_failed", extra={"event":"patch_history_write_failed",
                             "job_id":job_id,"candidate_id":candidate_id})
        record_job_item(job_id, media_path=row["media_relative_path"], status="failed",
                        phase="injection", candidate_id=candidate_id, review_decision="approved",
                        patch_status="failed", error=error, details={"recovery_path":backup})
        update_job_progress(job_id, phase="failed", completed=1, failed=1)
        finish_job(job_id, "failed", error)
        logger.exception("injection_failed", extra={"event":"injection_failed","job_id":job_id,
                         "candidate_id":candidate_id,"media_path":row["media_relative_path"],
                         "phase":"injection","error_type":type(exc).__name__})


@app.post("/api/candidates/{candidate_id}/inject")
def inject_candidate(candidate_id: int, request: InjectRequest, background_tasks: BackgroundTasks):
    if request.confirm is not True:
        raise HTTPException(status_code=400, detail="Explicit confirmation is required for media modification.")
    row = get_candidate(candidate_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Candidate not found.")
    if row["status"] != "staged" or row["review_decision"] != "approved":
        raise HTTPException(status_code=409, detail="This exact subtitle candidate must be explicitly approved before injection.")
    if row.get("patch_status") == "patched":
        raise HTTPException(status_code=409, detail="This candidate has already been injected.")
    media_root = Path(settings.media_root).resolve(strict=True)
    relative = Path(row["media_relative_path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise HTTPException(status_code=400, detail="Unsafe media path.")
    source = media_root
    try:
        for part in relative.parts:
            source = source / part
            if source.is_symlink():
                raise HTTPException(status_code=400, detail="Symlink components are not allowed in media paths.")
        if not source.resolve(strict=True).is_relative_to(media_root):
            raise HTTPException(status_code=400, detail="Media path escapes the configured media root.")
        source = source.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=404, detail="The associated media file is unavailable.")
    if not source.is_file() or source.suffix.lower() != ".mkv":
        raise HTTPException(status_code=415, detail="Only existing MKV media files can be patched.")
    current = source.stat()
    if current.st_size != row["media_size"] or current.st_mtime != row["media_modified_at"]:
        raise HTTPException(status_code=409, detail="The media file changed after approval. Rescan and approve a fresh candidate.")
    staging_root = Path(settings.subtitle_staging_path).resolve(strict=True)
    if staging_root == media_root or staging_root.is_relative_to(media_root):
        raise HTTPException(status_code=400, detail="Subtitle staging must remain outside the media library.")
    staged_value = Path(row.get("staged_path") or "")
    try:
        if not staged_value.is_absolute():
            raise HTTPException(status_code=404, detail="Staged subtitle file is unavailable.")
        staged_relative = staged_value.relative_to(staging_root)
        staged_component = staging_root
        for part in staged_relative.parts:
            staged_component = staged_component / part
            if staged_component.is_symlink():
                raise HTTPException(status_code=400, detail="Symlink components are not allowed in staging paths.")
        subtitle = staged_value.resolve(strict=True)
        if not subtitle.is_relative_to(staging_root) or not subtitle.is_file():
            raise HTTPException(status_code=404, detail="Staged subtitle file is unavailable.")
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(status_code=404, detail="Staged subtitle file is unavailable.")
    if row["language"].strip().lower() not in {"en", "eng", "english"}:
        raise HTTPException(status_code=415, detail="Only English subtitle candidates can be injected.")
    if subtitle.suffix.lower() not in {".srt", ".ass", ".ssa", ".sup", ".sub"}:
        raise HTTPException(status_code=415, detail="Unsupported subtitle format.")
    if Path(row["file_name"]).suffix.lower() != subtitle.suffix.lower():
        raise HTTPException(status_code=409, detail="Staged subtitle format does not match the reviewed candidate.")
    try:
        claimed = claim_candidate_patch(candidate_id, test_mode=settings.test_mode)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if claimed is None:
        raise HTTPException(status_code=404, detail="Candidate not found.")
    job = create_job("inject", total=1, metadata={"candidate_id":candidate_id,
                      "test_mode":bool(settings.test_mode)})
    background_tasks.add_task(_run_injection_job, job["id"], candidate_id, row, source, subtitle)
    logger.info("injection_queued", extra={"event":"injection_queued","job_id":job["id"],
                "candidate_id":candidate_id,"media_path":row["media_relative_path"],"phase":"queued"})
    return {"job_id":job["id"],"candidate_id":candidate_id,"status":"queued",
            "test_mode":settings.test_mode}


def _scan_worker(job_id: int, root: Path):
    start_job(job_id)
    try:
        paths = list(scan_media(root))
        update_job_progress(job_id, phase="inspecting", total=len(paths))
        client = None
        if settings.open_subtitles_api_key.strip():
            client = OpenSubtitlesClient(settings.open_subtitles_api_key,
                                         settings.open_subtitles_username,
                                         settings.open_subtitles_password)
        completed = succeeded = failed = 0
        for path in paths:
            if not media_path_is_safe(path, root):
                continue
            relative_path = str(path.relative_to(root))
            media_relative_path = str(path.relative_to(Path(settings.media_root).resolve()))
            update_job_progress(job_id, phase="inspecting", current_file=media_relative_path)
            try:
                details = inspect_media(path)
                stat_result = path.stat()
                record_scan(media_relative_path, stat_result.st_size, stat_result.st_mtime, details)
                summary = {"status":"not_configured","found":0,"staged":0,"candidates":[]}
                phase = "inspected"
                if details["has_verified_english_subtitle"]:
                    summary["status"] = "verified_english_present"
                elif client is not None:
                    phase = "discovery"
                    update_job_progress(job_id, phase=phase, current_file=media_relative_path)
                    try:
                        summary = stage_candidates(client, path, Path(settings.media_root).resolve(),
                                                   stat_result, details.get("metadata") or {})
                    except OpenSubtitlesError as exc:
                        summary = {"status":"provider_error","found":0,"staged":0,
                                   "candidates":[],"error":str(exc)}
                record_job_item(job_id, media_path=media_relative_path, status="completed",
                                phase=phase, details={"candidate_status":summary["status"],
                                "candidates_found":summary["found"],"candidates_staged":summary["staged"]})
                succeeded += 1
                logger.info("media_scan_completed", extra={"event":"media_scan_completed",
                            "job_id":job_id,"media_path":media_relative_path,"phase":phase,"status":"completed"})
            except (OSError, subprocess.SubprocessError, ValueError, OpenSubtitlesError) as exc:
                record_job_item(job_id, media_path=media_relative_path, status="failed",
                                phase="scan", error=f"{type(exc).__name__}: {str(exc)[:400]}")
                failed += 1
                logger.exception("media_scan_failed", extra={"event":"media_scan_failed",
                                 "job_id":job_id,"media_path":media_relative_path,
                                 "phase":"scan","error_type":type(exc).__name__})
            completed += 1
            update_job_progress(job_id, completed=completed, succeeded=succeeded, failed=failed)
        finish_job(job_id, "completed", f"{failed} file(s) failed." if failed else None)
        logger.info("scan_job_completed", extra={"event":"scan_job_completed",
                    "job_id":job_id,"phase":"completed","status":"completed"})
    except Exception as exc:
        finish_job(job_id, "failed", f"{type(exc).__name__}: {str(exc)[:400]}")
        logger.exception("scan_job_failed", extra={"event":"scan_job_failed","job_id":job_id,
                         "phase":"scan","error_type":type(exc).__name__})


@app.post("/api/scan")
def scan(request: ScanRequest, background_tasks: BackgroundTasks):
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
    job = create_job("scan", total=len(paths), metadata={"scan_root":str(root)})
    background_tasks.add_task(_scan_worker, job["id"], root)
    logger.info("scan_job_queued", extra={"event":"scan_job_queued","job_id":job["id"],
                "phase":"queued","status":"queued"})
    return {"job_id":job["id"],"status":"queued","total":len(paths)}


@app.get("/api/jobs")
def jobs(limit: int = 25):
    return {"jobs":list_jobs(limit)}


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: int):
    result = get_job(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return result
