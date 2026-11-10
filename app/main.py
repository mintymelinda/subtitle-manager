from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import subprocess
import uuid

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, FileResponse, PlainTextResponse

from app.config import settings
from app.observability import configure_logging, logger, timed_stage

from app.database import (
    initialize, record_scan, get_cached_scan, save_candidate, mark_candidate_staged,
    mark_candidate_error, list_candidates, set_candidate_decision,
    get_candidate, claim_candidate_patch, finish_candidate_patch, recover_interrupted_patches,
    create_job, start_job, update_job_progress, record_job_item, finish_job,
    get_job, list_jobs, recover_interrupted_jobs, clear_pending_review_queue, clear_saved_data,
    list_media_files, record_media_patch, reset_candidate_reviews_for_media,
)
from app.media import inspect_media
from app.opensubtitles import OpenSubtitlesClient, OpenSubtitlesError, assess_subtitle_payload
from app.scanner import scan_media
from app.patcher import patch_mkv
from app.provider_settings import (
    load_provider_settings, public_provider_settings, save_provider_settings,
    test_provider_settings,
)
from app.languages import LANGUAGE_CODES, normalize_languages, language_matches

app = FastAPI(title=settings.app_name)

# Keep FFmpeg, provider requests, and SQLite writes bounded on modest hosts.
SCAN_WORKERS = 3


class ScanRequest(BaseModel):
    path: str = "/media"
    paths: list[str] = []
    languages: list[str] = ["en"]


class CandidateDecisionRequest(BaseModel):
    decision: str


class InjectRequest(BaseModel):
    confirm: bool = False
    keep_subtitle_indices: list[int] | None = None


class MediaEditRequest(BaseModel):
    confirm: bool = False
    media_relative_path: str
    keep_subtitle_indices: list[int]
    reprocess_if_empty: bool = False
    target_languages: list[str] = ["en"]


class OpenSubtitlesSettingsRequest(BaseModel):
    api_key: str = ""
    username: str = ""
    password: str = ""
    max_candidates_per_file: int = 2
    log_level: str = "INFO"
    clear_account_credentials: bool = False


@app.on_event("startup")
def startup():
    configure_logging(load_provider_settings()["log_level"])
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
                     stat_result, metadata: dict, target_languages: list[str] | None = None,
                     should_cancel=None) -> dict:
    """Search and stage the top candidates outside the media mount."""
    if should_cancel is not None and should_cancel():
        return {"status": "cancelled", "found": 0, "staged": 0, "candidates": []}
    relative_media_path = str(path.relative_to(media_root))
    staging_root = Path(settings.subtitle_staging_path).resolve()
    if staging_root == media_root or staging_root.is_relative_to(media_root):
        raise OpenSubtitlesError("Subtitle staging directory must be outside the media library.")
    logger.info("opensubtitles_search_started", extra={"event":"opensubtitles_search_started","media_path":relative_media_path,"phase":"discovery"})
    target_languages = normalize_languages(target_languages)
    with timed_stage("opensubtitles_search", media_path=relative_media_path,
                     tool="opensubtitles"):
        candidates = client.search(path, metadata, languages=target_languages)
    logger.info("opensubtitles_search_completed", extra={"event":"opensubtitles_search_completed","media_path":relative_media_path,"phase":"discovery","status":f"{len(candidates)} candidates","provider_count":len(candidates)})
    summary = {"status": "no_candidates" if not candidates else "complete",
               "found": len(candidates), "staged": 0, "candidates": []}
    limit = load_provider_settings()["max_candidates_per_file"]
    selected = candidates[:limit]
    logger.info(
        "subtitle_candidate_download_selection",
        extra={"event": "subtitle_candidate_download_selection",
               "media_path": relative_media_path, "phase": "candidate_selection",
               "provider_count": len(candidates), "selected_count": len(selected),
               "candidate_limit": limit, "remaining_count": max(0, len(candidates) - len(selected))},
    )
    for rank, candidate in enumerate(selected, start=1):
        if should_cancel is not None and should_cancel():
            summary["status"] = "cancelled"
            break
        logger.debug(
            "subtitle_candidate_selected_for_download",
            extra={"event": "subtitle_candidate_selected_for_download",
                   "media_path": relative_media_path, "phase": "candidate_selection",
                   "file_name": candidate["file_name"], "file_id": candidate["file_id"],
                   "match_score": candidate["match_score"],
                   "match_reasons": candidate["match_reasons"],
                   "moviehash_match": candidate["moviehash_match"],
                   "download_count": candidate["download_count"],
                   "candidate_limit": limit, "selected_count": rank},
        )
        row = save_candidate(
            relative_media_path, stat_result.st_size, stat_result.st_mtime, candidate,
            media_modified_ns=stat_result.st_mtime_ns,
            media_device=stat_result.st_dev,
            media_inode=stat_result.st_ino,
        )
        status = row["status"]
        logger.debug(
            "subtitle_candidate_persisted",
            extra={"event": "subtitle_candidate_persisted",
                   "candidate_id": row["id"], "media_path": relative_media_path,
                   "phase": "candidate_selection", "status": status,
                   "file_name": candidate["file_name"], "file_id": candidate["file_id"]},
        )
        assessment = {
            "content_status": row.get("content_status", "unknown"),
            "content_language": row.get("content_language"),
            "content_confidence": row.get("content_language_confidence"),
        }
        if status != "staged":
            try:
                with timed_stage("candidate_download_and_assessment",
                                 media_path=relative_media_path, candidate_id=row["id"],
                                 tool="opensubtitles"):
                    downloaded = client.download_if_available(candidate["file_id"])
                    if downloaded is None:
                        status = "download_quota_reached"
                    else:
                        file_name, payload, remaining = downloaded
                        assessment = assess_subtitle_payload(
                            file_name, payload, expected_language=candidate["language"]
                        )
                if status != "download_quota_reached":
                    detected_language = assessment.get("content_language")
                    if detected_language and not language_matches(detected_language, candidate["language"]):
                        mark_candidate_error(
                            row["id"],
                            "Downloaded subtitle content does not match the candidate's declared language.",
                            status="rejected_language_mismatch",
                        )
                        status = "rejected_language_mismatch"
                    else:
                        staging_root.mkdir(parents=True, exist_ok=True)
                        extension = Path(file_name).suffix.lower()
                        target = staging_root / f"candidate-{row['id']}{extension}"
                        temporary = staging_root / f".candidate-{row['id']}-{uuid.uuid4().hex}.part"
                        with timed_stage("candidate_stage_write",
                                         media_path=relative_media_path, candidate_id=row["id"],
                                         bytes_processed=len(payload)):
                            descriptor = os.open(
                                temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                            )
                            with os.fdopen(descriptor, "wb") as staged_file:
                                staged_file.write(payload)
                                staged_file.flush()
                                os.fsync(staged_file.fileno())
                            os.replace(temporary, target)
                        mark_candidate_staged(
                            row["id"], str(target), remaining, assessment,
                            file_name=Path(file_name).name,
                            subtitle_format=extension.lstrip("."),
                        )
                        status = "staged"
                        logger.info("subtitle_candidate_staged", extra={"event":"subtitle_candidate_staged","candidate_id":row["id"],"media_path":relative_media_path,"phase":"download","status":"staged"})
            except OpenSubtitlesError as exc:
                mark_candidate_error(row["id"], str(exc))
                status = "download_failed"
                logger.warning("subtitle_candidate_download_failed", extra={"event":"subtitle_candidate_download_failed","candidate_id":row["id"],"media_path":relative_media_path,"phase":"download","error_type":type(exc).__name__})
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
    for candidate in candidates[len(selected):]:
        logger.debug(
            "subtitle_candidate_not_downloaded",
            extra={"event": "subtitle_candidate_not_downloaded",
                   "media_path": relative_media_path, "phase": "candidate_selection",
                   "file_name": candidate["file_name"], "file_id": candidate["file_id"],
                   "filter_reason": "per_file_download_limit",
                   "candidate_limit": limit},
        )
    logger.info(
        "subtitle_candidate_staging_summary",
        extra={"event": "subtitle_candidate_staging_summary",
               "media_path": relative_media_path, "phase": "candidate_selection",
               "provider_count": len(candidates), "selected_count": len(selected),
               "accepted_count": summary["staged"], "candidate_limit": limit},
    )
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
.candidate{background:#111827;border:1px solid #374151;border-radius:10px;padding:.65rem .8rem;margin:.5rem 0;min-width:0}
.candidate h3,.candidate p{overflow-wrap:anywhere}.candidate h3{margin:.2rem 0 .4rem}.candidate p{margin:.25rem 0}
#candidate-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:.35rem}
.candidate-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:.75rem}
.candidate-title{min-width:0}.candidate-title h3{margin:.1rem 0 .2rem}
.candidate-media-path{font-size:.82rem;color:#9ca3af;margin:.1rem 0;overflow-wrap:anywhere}
.candidate-select{display:flex;align-items:center;gap:.35rem;white-space:nowrap;font-size:.9rem}
.candidate-select input{width:auto;margin:0}
.candidate-details{margin:.55rem 0;color:#cbd5e1}
 .candidate-details summary{cursor:pointer;color:#93c5fd}
.existing-subtitle-tracks{border:1px solid #475569;border-radius:8px;padding:.6rem;margin:.65rem 0}
.existing-subtitle-tracks legend{color:#cbd5e1;font-size:.9rem}
.existing-subtitle-track{display:flex;align-items:flex-start;gap:.5rem;margin:.4rem 0;color:#e5e7eb;font-size:.9rem}
.existing-subtitle-track input{width:auto;margin:.15rem 0 0}
.batch-toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:.65rem;margin:.5rem 0 1rem}
.batch-toolbar button:disabled{opacity:.45}
#batch-selection-count{color:#aebbd0;font-size:.9rem}
.language-picker summary{cursor:pointer;color:#93c5fd;margin:.5rem 0}.language-options{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:.25rem .75rem;max-height:220px;overflow:auto;padding:.5rem;border:1px solid #475569;border-radius:8px;background:#111827}
.language-option{display:flex;align-items:center;gap:.45rem;font-size:.9rem;padding:.2rem 0}
.language-option input{width:auto;margin:0}
.actions{display:flex;flex-wrap:wrap;gap:.8rem;margin-top:.65rem}
.actions a{color:#93c5fd;padding:.5rem 0}.secondary{background:#4b5563}.warning{color:#fbbf24}
input{box-sizing:border-box;width:100%;padding:.7rem;margin:.5rem 0 1rem;border-radius:8px;border:1px solid #64748b;background:#111827;color:#e5e7eb}
.folder-browser{border:1px solid #475569;border-radius:10px;padding:1rem;margin:.75rem 0;background:#111827}.folder-list{list-style:none;padding:0;margin:.5rem 0}.folder-list li{padding:.45rem 0;border-bottom:1px solid #374151}.folder-entry{display:flex;align-items:center;gap:.7rem}.folder-entry input{width:auto;margin:0}.folder-path{font-family:ui-monospace,monospace;overflow-wrap:anywhere}.selected-folders{display:flex;flex-wrap:wrap;gap:.5rem}.folder-chip{background:#374151;border-radius:8px;padding:.35rem .6rem}
</style></head><body><h1>Subtitle Manager</h1>
<p>Find missing English subtitles and review candidates before changing media.</p>
<div class="notice"><strong>Safety:</strong> scanning and downloads never change media. Injection requires a separately approved candidate and a second confirmation. Current mode: <strong id="mode">loading…</strong>.</div>
<section class="panel"><h2>OpenSubtitles setup</h2>
<p>Create an API key at <a href="https://www.opensubtitles.com/en/consumers" target="_blank" rel="noopener">OpenSubtitles.com</a>, then save it here. Settings are stored in the persistent application data volume, not returned by the API or shown in logs.</p>
<p id="provider-status" aria-live="polite">Checking saved provider settings…</p>
<label for="provider-api-key">API key</label><input id="provider-api-key" type="password" autocomplete="new-password" placeholder="Enter a new key (leave blank to keep the saved key)">
<label for="provider-username">OpenSubtitles username (optional)</label><input id="provider-username" type="text" autocomplete="username" placeholder="Optional account username">
<label for="provider-password">OpenSubtitles password (optional)</label><input id="provider-password" type="password" autocomplete="new-password" placeholder="Leave blank to keep the saved password">
<label for="provider-limit">Maximum candidates downloaded per media file (1–5)</label><input id="provider-limit" type="number" min="1" max="5" step="1" value="2">\n<label for="provider-log-level">Application logging level</label><select id="provider-log-level"><option value="DEBUG">DEBUG — detailed candidate selection diagnostics</option><option value="INFO">INFO — normal operational events</option><option value="WARNING">WARNING — warnings and errors</option><option value="ERROR">ERROR — errors only</option><option value="CRITICAL">CRITICAL — critical failures only</option></select>
<label><input id="provider-clear-account" type="checkbox" style="width:auto"> Clear saved account username and password</label>
<div class="actions"><button id="provider-save">Save settings</button><button id="provider-test" class="secondary">Test connection</button></div>
<p id="provider-message" aria-live="polite"></p>
</section>
<section class="panel"><h2>Media scan</h2><p>Browse the mounted media directory and select one or more folders to scan.</p><details class="language-picker"><summary>Choose target languages</summary><label for="language-search">Filter available languages</label><input id="language-search" type="search" placeholder="Search languages by name or code" autocomplete="off"><div id="language-options" class="language-options" aria-label="Available target languages"></div></details><p id="selected-languages" aria-live="polite">Selected: English</p><button id="browse-folders" class="secondary">Browse folders</button><button id="clear-folder-selection" class="secondary" type="button">Clear selection</button><div id="selected-folders" class="selected-folders"><span class="folder-chip">/media (default)</span></div><div id="folder-browser" class="folder-browser" hidden><div class="actions"><button id="folder-up" class="secondary" type="button">Up one level</button><button id="folder-select-current" class="secondary" type="button">Select this folder</button><button id="folder-close" class="secondary" type="button">Close</button></div><p id="folder-current" class="folder-path">/media</p><ul id="folder-list" class="folder-list"></ul><p id="folder-error" class="warning" aria-live="polite"></p></div><button id="scan">Start scan</button><button id="cancel-scan" class="secondary" type="button" hidden disabled>Cancel scan</button><p id="status">Ready.</p><div id="scan-results"></div><div id="job-progress" class="candidate" aria-live="polite">No active job.</div></section>
<section class="panel"><div class="actions" style="justify-content:space-between;align-items:center"><div><h2>Subtitle candidate review</h2><p>Review candidates for the latest scan.</p><label class="language-option"><input id="show-all-scanned-files" type="checkbox" style="width:auto;margin:0"> Show all scanned files (including non-eligible files)</label></div><button id="clear-saved-data" class="secondary" type="button">Clear saved data &amp; caches</button></div><div class="batch-toolbar">
<button id="select-all-candidates" class="secondary" type="button" disabled>Select all available</button>
<button id="clear-candidate-selection" class="secondary" type="button" disabled>Clear selection</button>
<button id="batch-run" disabled>Approve & patch selected</button>
<span id="batch-selection-count" aria-live="polite">0 candidates selected</span>
</div>
<p id="batch-message" aria-live="polite"></p>
<div id="candidate-list">Loading saved candidates…</div><p id="clear-data-message" aria-live="polite"></p></section>
<script>
const scanButton=document.querySelector("#scan"),cancelScanButton=document.querySelector("#cancel-scan"),status=document.querySelector("#status"),scanResults=document.querySelector("#scan-results"),candidateList=document.querySelector("#candidate-list");
let activeScanJobId=null;let activeTrackedJobId=null;
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function line(parent,label,value){const p=el("p");p.append(el("strong",label+": "),document.createTextNode(value===null||value===undefined?"Unknown":String(value)));parent.append(p);}
function streams(value){if(!Array.isArray(value)||!value.length)return "";return value.map(s=>[s.codec,s.language,s.title].filter(Boolean).join(" · ")||JSON.stringify(s)).join("; ");}
function renderExistingSubtitleControls(card,candidate,allCandidates){
    const tracks=candidate.inspection?.subtitle_streams||[];
    const fieldset=el("fieldset",undefined,"existing-subtitle-tracks");
    const legend=el("legend","Existing subtitle tracks — included by default");
    fieldset.append(legend);
    if(!tracks.length){fieldset.append(el("p","No existing subtitle tracks."));}
    for(const track of tracks){
        const label=el("label",undefined,"existing-subtitle-track");
        const checkbox=document.createElement("input");checkbox.type="checkbox";checkbox.checked=true;
        checkbox.value=String(track.index);checkbox.dataset.subtitleIndex=String(track.index);
        const description=["Stream "+track.index,track.language||"und",track.codec||"unknown codec",track.title].filter(Boolean).join(" · ");
        label.append(checkbox,document.createTextNode(description));fieldset.append(label);
    }
    if(!candidate.is_non_candidate){
        const automaticLabel=el("label",undefined,"automatic-cleanup-toggle");
        const automaticCheckbox=document.createElement("input");
        automaticCheckbox.type="checkbox";automaticCheckbox.checked=false;
        automaticCheckbox.setAttribute("aria-label","Select None — enable automatic subtitle cleanup");
        const automaticText=document.createTextNode("Select None — prepare automatic cleanup");
        automaticLabel.append(automaticCheckbox,automaticText);fieldset.append(automaticLabel);
        const run=el("button","Run automatic cleanup");
        run.type="button";run.hidden=true;run.disabled=false;
        run.setAttribute("aria-label","Run automatic subtitle cleanup for "+candidate.file_name);
        const result=el("p","", "automatic-cleanup-message");result.setAttribute("aria-live","polite");
        automaticCheckbox.addEventListener("change",()=>{
            const checked=automaticCheckbox.checked;
            for(const input of fieldset.querySelectorAll(".existing-subtitle-track input[type='checkbox']")){
                if(input!==automaticCheckbox)input.checked=!checked;
            }
            run.hidden=!checked;
            result.textContent="";
        });
        run.addEventListener("click",async()=>{
            if(!automaticCheckbox.checked||run.disabled)return;
            const options=allCandidates.filter(item=>item.media_relative_path===candidate.media_relative_path
                &&item.status==="staged"&&!item.is_stale
                &&item.patch_status!=="patched"&&item.patch_status!=="running"&&item.patch_status!=="interrupted"
                &&["pending","approved"].includes(item.review_decision));
            const first=options[0];
            if(!first){result.textContent="No valid staged subtitle candidate is available. Rescan or review candidates first.";return;}
            const mode=document.querySelector("#mode").textContent;
            const warning=mode==="TEST MODE"
                ?"TEST MODE will replace this MKV and retain a recovery copy."
                :"LIVE MODE will replace this MKV; its recovery copy is deleted only after post-replacement verification.";
            if(!confirm("Run automatic cleanup for "+candidate.file_name+"? The first valid subtitle candidate (“"+first.file_name+"”) will be injected, all existing subtitle tracks will be removed, and no further warnings or prompts will appear. "+warning))return;
            run.disabled=true;automaticCheckbox.disabled=true;
            result.textContent="Running automatic cleanup…";
            try{
                if(first.review_decision!=="approved"){
                    const approval=await fetch("/api/candidates/"+first.id+"/decision",{
                        method:"POST",headers:{"Content-Type":"application/json"},
                        body:JSON.stringify({decision:"approved"})
                    });
                    const approved=await approval.json();
                    if(!approval.ok)throw Error(approved.detail||approval.statusText);
                }
                const response=await fetch("/api/candidates/"+first.id+"/inject",{
                    method:"POST",headers:{"Content-Type":"application/json"},
                    body:JSON.stringify({confirm:true,keep_subtitle_indices:[]})
                });
                const injected=await response.json();
                if(!response.ok)throw Error(injected.detail||response.statusText);
                result.textContent="Automatic cleanup started with "+first.file_name+".";
                trackJob(injected.job_id);
            }catch(error){
                result.textContent="Automatic cleanup could not start: "+error.message;
                run.disabled=false;automaticCheckbox.disabled=false;
            }
        });
        card.append(fieldset);
        card.append(run,result);
        return;
    }
    card.append(fieldset);
}
function selectedSubtitleIndices(candidateId){
    const card=[...candidateList.querySelectorAll(".candidate")].find(item=>item.dataset.candidateId===String(candidateId));
    return card?[...card.querySelectorAll(".existing-subtitle-track input:checked")].map(input=>Number(input.value)):[];
}
function selectedMediaSubtitleIndices(path){
    const card=[...candidateList.querySelectorAll(".candidate")].find(item=>item.dataset.mediaPath===path);
    return card?[...card.querySelectorAll(".existing-subtitle-track input:checked")].map(input=>Number(input.value)):[];
}
async function editScannedMedia(path,button){
    const indices=selectedMediaSubtitleIndices(path);
    const reprocess=indices.length===0&&confirm("No subtitle tracks will remain. Re-mux this file with no subtitles, then re-process it and retry subtitle matching?");
    if(indices.length===0&&!reprocess)return;
    if(!confirm("Apply only the selected existing subtitle-track changes? No downloaded subtitle will be injected."))return;
    button.disabled=true;
    try{
        const response=await fetch("/api/media-files/edit",{method:"POST",headers:{"Content-Type":"application/json"},
            body:JSON.stringify({confirm:true,media_relative_path:path,keep_subtitle_indices:indices,
                reprocess_if_empty:reprocess,target_languages:[...selectedTargetLanguages]})});
        const result=await response.json();if(!response.ok)throw Error(result.detail||response.statusText);
        alert(result.message||"Existing subtitle tracks updated.");await loadCandidates();
    }catch(error){alert("Could not edit existing subtitle tracks: "+error.message);button.disabled=false;}
}
async function loadCandidates(){const all=document.querySelector("#show-all-scanned-files")?.checked;const r=await fetch("/api/candidates"+(all?"?all=true":""));const d=await r.json();if(!r.ok)throw Error(d.detail||r.statusText);renderCandidates(d.candidates);}
const selectedCandidateIds = new Set();
const selectedMediaPaths = new Set();

function eligibleForBatch(candidate) {
    return candidate.status === "staged"
        && !candidate.is_stale
        && candidate.patch_status !== "patched"
        && candidate.patch_status !== "running"
        && candidate.patch_status !== "interrupted"
        && ["pending", "approved"].includes(candidate.review_decision);
}

function updateBatchControls() {
    const count = selectedCandidateIds.size + selectedMediaPaths.size;
    document.querySelector("#batch-selection-count").textContent =
        count + (count === 1 ? " target selected" : " targets selected");
    document.querySelector("#batch-run").disabled = count === 0;
    const selectable = candidateList.querySelectorAll(".candidate-select input[type='checkbox']");
    const mediaSelectable = candidateList.querySelectorAll(".media-edit-select input[type='checkbox']");
    document.querySelector("#select-all-candidates").disabled =
        selectable.length + mediaSelectable.length === 0 || count === selectable.length + mediaSelectable.length;
    document.querySelector("#clear-candidate-selection").disabled = count === 0;
}

function selectAllCandidates() {
    const checkboxes = candidateList.querySelectorAll(".candidate-select input[type='checkbox']");
    for (const checkbox of checkboxes) {
        checkbox.checked = true;
        selectedCandidateIds.add(Number(checkbox.value));
    }
    for (const checkbox of candidateList.querySelectorAll(".media-edit-select input[type='checkbox']")) {
        checkbox.checked = true;
        selectedMediaPaths.add(checkbox.value);
    }
    updateBatchControls();
}

function clearCandidateSelection() {
    const checkboxes = candidateList.querySelectorAll(".candidate-select input[type='checkbox']");
    for (const checkbox of checkboxes) checkbox.checked = false;
    for (const checkbox of candidateList.querySelectorAll(".media-edit-select input[type='checkbox']")) checkbox.checked = false;
    selectedCandidateIds.clear();
    selectedMediaPaths.clear();
    updateBatchControls();
}

function renderCandidates(items) {
    candidateList.replaceChildren();
    for (const id of [...selectedCandidateIds]) if (!items.some(item => !item.is_non_candidate && item.id === id)) selectedCandidateIds.delete(id);
    for (const path of [...selectedMediaPaths]) if (!items.some(item => item.is_non_candidate && item.media_relative_path === path)) selectedMediaPaths.delete(path);
    updateBatchControls();
    if (!items.length) {
        candidateList.append(el("p", "No saved candidates yet. Run a scan with OpenSubtitles configured."));
        return;
    }

    for (const c of items) {
        if (c.is_non_candidate) {
            const card=el("article",undefined,"candidate");card.dataset.mediaPath=c.media_relative_path;
            const heading=el("div",undefined,"candidate-heading"),title=el("div",undefined,"candidate-title");
            title.append(el("h3",c.file_name),el("p",c.media_relative_path,"candidate-media-path"));
            const selectLabel=el("label",undefined,"candidate-select media-edit-select"),selectBox=document.createElement("input");
            selectBox.type="checkbox";selectBox.value=c.media_relative_path;selectBox.checked=selectedMediaPaths.has(c.media_relative_path);
            selectBox.setAttribute("aria-label","Select existing-track edit for "+c.file_name);
            selectBox.addEventListener("change",()=>{if(selectBox.checked)selectedMediaPaths.add(c.media_relative_path);else selectedMediaPaths.delete(c.media_relative_path);updateBatchControls();});
            selectLabel.append(selectBox,document.createTextNode("Select edit"));heading.append(title,selectLabel);card.append(heading);
            card.append(el("p","Scanned file · not eligible for subtitle injection"));
            line(card,"Existing audio tracks",streams(c.inspection?.audio_streams));renderExistingSubtitleControls(card,c,items);
            if(c.is_stale)card.append(el("p","This file changed after scanning. Rescan before editing.","warning"));
            else{const edit=el("button","Apply existing-track edits");edit.addEventListener("click",()=>editScannedMedia(c.media_relative_path,edit));card.append(edit);}
            candidateList.append(card);continue;
        }
        const card = el("article", undefined, "candidate");
        card.dataset.candidateId=String(c.id);
        const heading = el("div", undefined, "candidate-heading");
        const title = el("div", undefined, "candidate-title");
        title.append(el("h3", c.file_name));
        title.append(el("p", c.media_relative_path, "candidate-media-path"));
        heading.append(title);

        if (eligibleForBatch(c)) {
            const label = el("label", undefined, "candidate-select");
            const checkbox = document.createElement("input");
            checkbox.type = "checkbox";
            checkbox.value = String(c.id);
            checkbox.checked = selectedCandidateIds.has(c.id);
            checkbox.setAttribute("aria-label", "Select " + c.file_name);
            checkbox.addEventListener("change", () => {
                if (checkbox.checked) selectedCandidateIds.add(c.id);
                else selectedCandidateIds.delete(c.id);
                updateBatchControls();
            });
            label.append(checkbox, document.createTextNode("Select"));
            heading.append(label);
        }
        card.append(heading);
        card.append(el("p", "Candidate #" + c.id + " · " + c.language + " / " + c.format));
        card.append(el("p", "Review: " + c.review_decision + " · Injection: " + (c.patch_status || "not_started")));

        const details = document.createElement("details");
        details.className = "candidate-details";
        const summary = document.createElement("summary");
        summary.textContent = "Matching details";
        details.append(summary);
        line(details, "Release", c.release || "Not provided");
        line(details, "Match score", c.match_score);
        line(details, "Exact file hash match", c.moviehash_match ? "Yes" : "No");
        line(details, "Candidate download status", c.status);
        line(details, "Match rationale", (c.match_reasons || []).join("; ") || "No rationale supplied");
        line(details, "Content check", c.content_status + (c.content_language ? " · " + c.content_language : "") + (c.content_language_confidence ? " · confidence " + Number(c.content_language_confidence).toFixed(2) : ""));
        line(details, "Existing audio tracks", streams(c.inspection?.audio_streams));
        line(details, "Existing subtitle tracks", streams(c.inspection?.subtitle_streams));
        card.append(details);
        renderExistingSubtitleControls(card,c,items);

        if (c.is_stale) card.append(el("p", "This candidate is stale: the media file changed after discovery. Rescan before reviewing.", "warning"));
        if (c.patch_status === "interrupted") card.append(el("p", "An earlier injection was interrupted. Inspect the media file and retained recovery copy manually before proceeding.", "warning"));

        if (c.status === "staged") {
            const links = el("div", undefined, "actions");
            const preview = el("a", "Preview subtitle");
            const download = el("a", "Download candidate");
            preview.href = "/api/candidates/" + c.id + "/preview";
            preview.target = "_blank";
            preview.rel = "noopener";
            download.href = "/api/candidates/" + c.id + "/download";
            links.append(preview, download);
            card.append(links);
        } else if (c.last_error) {
            card.append(el("p", c.last_error, "warning"));
        }

        if (c.status === "staged" && c.review_decision === "pending" && !c.is_stale) {
            const actions = el("div", undefined, "actions");
            const approve = el("button", "Approve & inject");
            const reject = el("button", "Reject this candidate");
            reject.className = "secondary";
            approve.addEventListener("click", () => decide(c.id, "approved", approve));
            reject.addEventListener("click", () => decide(c.id, "rejected", reject));
            actions.append(approve, reject);
            card.append(actions);
        }
        if (c.status === "staged" && c.review_decision === "approved" && !c.is_stale
            && c.patch_status !== "patched" && c.patch_status !== "running"
            && c.patch_status !== "interrupted") {
            const injectButton = el("button", c.patch_status === "failed" ? "Retry injection" : "Inject approved subtitle");
            injectButton.addEventListener("click", () => inject(c.id, injectButton));
            card.append(injectButton);
        }
        candidateList.append(card);
    }
    updateBatchControls();
}

document.querySelector("#show-all-scanned-files").addEventListener("change", () => loadCandidates().catch(error => {
    candidateList.textContent = "Could not load scanned files: " + error.message;
}));
document.querySelector("#select-all-candidates").addEventListener("click", selectAllCandidates);
document.querySelector("#clear-candidate-selection").addEventListener("click", clearCandidateSelection);

async function waitForBatchInjection(jobId, candidateId, index, total) {
    const panel = document.querySelector("#job-progress");
    while (true) {
        let job;
        try {
            const response = await fetch("/api/jobs/" + jobId, {
                headers: {"Accept": "application/json"}
            });
            job = await readJsonResponse(response, "Batch injection status");
        } catch (error) {
            panel.replaceChildren(el("strong", "Batch injection · " + index + "/" + total
                + " · Candidate #" + candidateId + " · Job #" + jobId));
            panel.append(el("p", "Injection is still being tracked. Status request failed: "
                + error.message + ". Retrying without starting another injection…", "warning"));
            await new Promise(resolve => setTimeout(resolve, 2000));
            continue;
        }
        panel.replaceChildren();
        panel.append(el("strong", "Batch injection · " + index + "/" + total
            + " · Candidate #" + candidateId + " · Job #" + job.id + " · " + job.status));
        line(panel, "Progress", job.completed + "/" + job.total
            + " · " + job.succeeded + " succeeded · " + job.failed + " failed");
        line(panel, "Phase", job.phase || "queued");
        line(panel, "Current file", job.current_file || "—");
        for (const item of job.items || []) {
            const detail = item.status === "failed"
                ? item.media_path + " — failed: " + (item.error || "unknown error")
                : item.media_path + " — " + item.phase + " (" + item.status + ")";
            panel.append(el("p", detail, item.status === "failed" ? "warning" : ""));
        }
        if (job.error) panel.append(el("p", job.error, "warning"));
        if (["completed", "failed", "cancelled", "interrupted"].includes(job.status)) {
            return job;
        }
        await new Promise(resolve => setTimeout(resolve, 1000));
    }
}

async function runBatch() {
    const ids = [...selectedCandidateIds];
    const mediaPaths = [...selectedMediaPaths];
    if (!ids.length && !mediaPaths.length) return;
    const mode = document.querySelector("#mode").textContent;
    const warning = mode === "TEST MODE"
        ? " TEST MODE will replace each selected MKV and retain recovery copies."
        : " LIVE MODE will replace each selected MKV; recovery copies are deleted only after verification.";
    const mediaWarning = mediaPaths.length
        ? " " + mediaPaths.length + " non-candidate file(s) will receive existing-track-only edits; no downloaded subtitles will be injected."
        : "";
    if (!confirm("Approve and patch " + (ids.length + mediaPaths.length) + " selected target(s) in sequence?" + warning + mediaWarning)) return;

    const runButton = document.querySelector("#batch-run");
    runButton.disabled = true;
    const message = document.querySelector("#batch-message");
    const panel = document.querySelector("#job-progress");
    let succeeded = 0;
    const failures = [];
    message.textContent = "Preparing " + (ids.length + mediaPaths.length) + " selected target(s)…";
    let candidatesById;
    try {
        const response = await fetch("/api/candidates");
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || response.statusText);
        candidatesById = new Map(payload.candidates.map(candidate => [candidate.id, candidate]));
    } catch (error) {
        message.textContent = "Could not load selected candidates: " + error.message;
        runButton.disabled = false;
        return;
    }

    const totalTargets = ids.length + mediaPaths.length;
    for (let index = 0; index < totalTargets; index++) {
        if (index >= ids.length) {
            const path = mediaPaths[index - ids.length];
            const card = [...candidateList.querySelectorAll(".candidate")].find(item => item.dataset.mediaPath === path);
            const indices = card ? [...card.querySelectorAll(".existing-subtitle-track input:checked")].map(input => Number(input.value)) : [];
            message.textContent = "Editing existing subtitle tracks " + (index + 1) + "/" + totalTargets + " (" + path + ")…";
            panel.replaceChildren(el("strong", "Existing-track edit · " + (index + 1) + "/" + totalTargets + " · " + path));
            try {
                const reprocess = indices.length === 0 && confirm("No subtitle tracks will remain in " + path + ". Re-mux with no subtitles and retry matching?");
                if (indices.length === 0 && !reprocess) throw new Error("Skipped: removal of every subtitle track was not confirmed.");
                const response = await fetch("/api/media-files/edit", {method:"POST",headers:{"Content-Type":"application/json"},
                    body:JSON.stringify({confirm:true,media_relative_path:path,keep_subtitle_indices:indices,
                        reprocess_if_empty:reprocess,target_languages:[...selectedTargetLanguages]})});
                const result = await response.json();if(!response.ok)throw new Error(result.detail||response.statusText);
                succeeded++;
                panel.append(el("p",result.message||"Existing subtitle tracks updated."));
            } catch(error) {
                failures.push(path + ": " + error.message);
                panel.append(el("p",path + " failed: " + error.message,"warning"));
            }
            message.textContent = "Processed " + (index + 1) + "/" + totalTargets
                + " targets · " + succeeded + " succeeded · " + failures.length + " failed.";
            continue;
        }
        const id = ids[index];
        const candidate = candidatesById.get(id);
        message.textContent = "Processing candidate " + (index + 1) + "/" + totalTargets
            + " (#" + id + ")…";
        panel.replaceChildren(el("strong", "Batch injection · " + (index + 1) + "/" + totalTargets
            + " · Candidate #" + id + " · preparing"));
        try {
            if (!candidate) throw new Error("Candidate is no longer available.");
            if (candidate.review_decision !== "approved") {
                const approval = await fetch("/api/candidates/" + id + "/decision", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify({decision: "approved"})
                });
                const result = await approval.json();
                if (!approval.ok) throw new Error(result.detail || approval.statusText);
            }
            line(panel, "Review", "Approved");
            const response = await fetch("/api/candidates/" + id + "/inject", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({confirm: true, keep_subtitle_indices: selectedSubtitleIndices(id)})
            });
            const result = await response.json();
            if (!response.ok) throw new Error(result.detail || response.statusText);
            const job = await waitForBatchInjection(result.job_id, id, index + 1, totalTargets);
            if (job.status !== "completed") {
                throw new Error(job.error || "Injection job ended with status " + job.status + ".");
            }
            succeeded++;
            message.textContent = "Processed " + (index + 1) + "/" + totalTargets
                + " targets · " + succeeded + " succeeded · " + failures.length + " failed.";
        } catch (error) {
            failures.push("Candidate #" + id + ": " + error.message);
            panel.append(el("p", "Candidate #" + id + " failed: " + error.message, "warning"));
            message.textContent = "Processed " + (index + 1) + "/" + totalTargets
                + " targets · " + succeeded + " succeeded · " + failures.length + " failed.";
        }
    }

    message.textContent = succeeded + " patched; " + failures.length + " failed."
        + (failures.length ? " " + failures.join(" | ") : "");
    await loadCandidates();
    runButton.disabled = selectedCandidateIds.size + selectedMediaPaths.size === 0;
}

document.querySelector("#batch-run").addEventListener("click", runBatch);

async function decide(id,decision,button){
const mode=document.querySelector("#mode").textContent;
const approvalMessage=decision==="approved"
 ? (mode==="TEST MODE"?"Approve this candidate and inject it in TEST MODE? The media file will be replaced and a recovery copy retained.":"Approve this candidate and inject it into the media file now? LIVE MODE will replace the MKV; its recovery copy is deleted only after post-replacement verification.")
 : "Reject this subtitle candidate?";
if(!confirm(approvalMessage))return;
button.disabled=true;
try{
 const r=await fetch("/api/candidates/"+id+"/decision",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({decision})});
 const d=await r.json();if(!r.ok)throw Error(d.detail||r.statusText);
 if(decision==="approved"){
  const injection=await fetch("/api/candidates/"+id+"/inject",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({confirm:true,keep_subtitle_indices:selectedSubtitleIndices(id)})});
  const result=await injection.json();if(!injection.ok){await fetch("/api/candidates/"+id+"/decision",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({decision:"pending"})});throw Error(result.detail||injection.statusText);}
  trackJob(result.job_id);
 }else{await loadCandidates();}
}catch(e){alert("Could not "+(decision==="approved"?"approve and inject":"save decision")+": "+e.message);button.disabled=false;await loadCandidates();}
}
async function inject(id,button){const mode=document.querySelector("#mode").textContent;const warning=mode==="TEST MODE"?"TEST MODE will replace this MKV and retain a recovery copy beside it.":"LIVE MODE will replace this MKV; its recovery copy is deleted only after post-replacement verification.";if(!confirm(warning+"\\n\\nInject the explicitly approved subtitle into this exact media file now?"))return;button.disabled=true;try{const r=await fetch("/api/candidates/"+id+"/inject",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({confirm:true,keep_subtitle_indices:selectedSubtitleIndices(id)})});const d=await r.json();if(!r.ok)throw Error(d.detail||r.statusText);trackJob(d.job_id);}catch(e){alert("Could not start injection: "+e.message);button.disabled=false;await loadCandidates();}}
async function loadProviderSettings(){try{const r=await fetch("/api/settings/opensubtitles"),s=await r.json();if(!r.ok)throw Error(s.detail||r.statusText);document.querySelector("#provider-status").textContent=s.configured?"OpenSubtitles is configured"+(s.has_account_credentials?" with account credentials.":" using the API-key allowance."):"OpenSubtitles is not configured yet.";document.querySelector("#provider-limit").value=s.max_candidates_per_file;document.querySelector("#provider-log-level").value=s.log_level||"INFO";}catch(e){document.querySelector("#provider-status").textContent="Could not load provider settings: "+e.message;}}
document.querySelector("#provider-save").addEventListener("click",async()=>{const button=document.querySelector("#provider-save"),message=document.querySelector("#provider-message");button.disabled=true;message.textContent="Saving securely…";try{const r=await fetch("/api/settings/opensubtitles",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({api_key:document.querySelector("#provider-api-key").value,username:document.querySelector("#provider-username").value,password:document.querySelector("#provider-password").value,max_candidates_per_file:Number(document.querySelector("#provider-limit").value),log_level:document.querySelector("#provider-log-level").value,clear_account_credentials:document.querySelector("#provider-clear-account").checked})});const d=await r.json();if(!r.ok)throw Error(d.detail||r.statusText);document.querySelector("#provider-api-key").value="";document.querySelector("#provider-username").value="";document.querySelector("#provider-password").value="";document.querySelector("#provider-clear-account").checked=false;message.textContent="Settings saved to the persistent data volume. "+(d.configured?"Provider is configured.":"");await loadProviderSettings();const c=await fetch("/api/config").then(x=>x.json());document.querySelector("#provider-status").textContent=c.opensubtitles_configured?"OpenSubtitles is configured.":"OpenSubtitles is not configured.";}catch(e){message.textContent="Could not save settings: "+e.message;}finally{button.disabled=false;}});
document.querySelector("#provider-test").addEventListener("click",async()=>{const button=document.querySelector("#provider-test"),message=document.querySelector("#provider-message");button.disabled=true;message.textContent="Testing OpenSubtitles…";try{const r=await fetch("/api/settings/opensubtitles/test",{method:"POST"}),d=await r.json();if(!r.ok)throw Error(d.detail||r.statusText);message.textContent=d.message;}catch(e){message.textContent="Connection test failed: "+e.message;}finally{button.disabled=false;}});
loadProviderSettings();
fetch("/api/config").then(r=>r.json()).then(c=>{document.querySelector("#mode").textContent=c.test_mode?"TEST MODE":"LIVE MODE";}).catch(()=>{document.querySelector("#mode").textContent="unknown";});
let browserPath="";let selectedFolders=new Set();
const languageStateKey="subtitle-manager.target-languages.v1";
let availableLanguages=[];
let selectedTargetLanguages=new Set(["en"]);
const languageNames=new Intl.DisplayNames(["en"],{type:"language"});
function persistTargetLanguages(){try{localStorage.setItem(languageStateKey,JSON.stringify([...selectedTargetLanguages]));}catch(e){/* Language choices remain usable when storage is unavailable. */}}
function restoreTargetLanguages(){try{const raw=localStorage.getItem(languageStateKey);if(raw!==null){const saved=JSON.parse(raw);if(Array.isArray(saved))selectedTargetLanguages=new Set(saved.filter(code=>typeof code==="string"&&/^[a-z]{2}$/.test(code)));}}catch(e){selectedTargetLanguages=new Set(["en"]);}}
function languageLabel(code){try{return languageNames.of(code)||code.toUpperCase();}catch(e){return code.toUpperCase();}}
function renderLanguageOptions(){const host=document.querySelector("#language-options"),query=document.querySelector("#language-search").value.trim().toLowerCase();host.replaceChildren();const filtered=availableLanguages.filter(item=>item.name.toLowerCase().includes(query)||item.code.includes(query));for(const item of filtered){const label=el("label",undefined,"language-option"),check=el("input");check.type="checkbox";check.checked=selectedTargetLanguages.has(item.code);check.addEventListener("change",()=>{if(check.checked)selectedTargetLanguages.add(item.code);else selectedTargetLanguages.delete(item.code);persistTargetLanguages();renderLanguageOptions();});label.append(check,document.createTextNode(item.name+" ("+item.code+")"));host.append(label);}if(!filtered.length)host.append(el("p","No matching languages."));document.querySelector("#selected-languages").textContent=selectedTargetLanguages.size?"Selected: "+[...selectedTargetLanguages].map(languageLabel).join(", "):"Select at least one target language before scanning.";}
async function loadLanguages(){const response=await fetch("/api/languages"),data=await response.json();if(!response.ok)throw Error(data.detail||response.statusText);availableLanguages=data.languages.map(item=>({code:item.code,name:languageLabel(item.code)})).sort((a,b)=>a.name.localeCompare(b.name));const valid=new Set(availableLanguages.map(item=>item.code));selectedTargetLanguages=new Set([...selectedTargetLanguages].filter(code=>valid.has(code)));persistTargetLanguages();renderLanguageOptions();}
restoreTargetLanguages();
document.querySelector("#language-search").addEventListener("input",renderLanguageOptions);
loadLanguages().catch(error=>{document.querySelector("#selected-languages").textContent="Could not load language list: "+error.message;});
const folderStateKey="subtitle-manager.folder-state.v1";
function persistFolderState(){try{localStorage.setItem(folderStateKey,JSON.stringify({browserPath,selectedFolders:[...selectedFolders]}));}catch(e){/* Browsing remains usable when storage is unavailable. */}}
function restoreFolderState(){try{const saved=JSON.parse(localStorage.getItem(folderStateKey)||"null");if(!saved||typeof saved!=="object")return;if(typeof saved.browserPath==="string"&&!saved.browserPath.startsWith("/")&&!saved.browserPath.split("/").includes(".."))browserPath=saved.browserPath;if(Array.isArray(saved.selectedFolders))selectedFolders=new Set(saved.selectedFolders.filter(path=>typeof path==="string"&&!path.startsWith("/")&&!path.split("/").includes("..")));}catch(e){browserPath="";selectedFolders=new Set();}}
restoreFolderState();
function renderSelectedFolders(){const host=document.querySelector("#selected-folders");host.replaceChildren();for(const path of selectedFolders){const chip=el("span",path?"/media/"+path:"/media","folder-chip");host.append(chip);}if(!selectedFolders.size)host.append(el("span","/media (default)","folder-chip"));}
renderSelectedFolders();
async function loadFolderBrowser(path){const list=document.querySelector("#folder-list"),error=document.querySelector("#folder-error");list.replaceChildren();error.textContent="";try{const r=await fetch("/api/media/directories?path="+encodeURIComponent(path));const d=await r.json();if(!r.ok)throw Error(d.detail||r.statusText);browserPath=d.path;persistFolderState();document.querySelector("#folder-current").textContent=d.display_path;document.querySelector("#folder-up").disabled=!d.parent_path;for(const folder of d.directories){const li=el("li"),row=el("div",undefined,"folder-entry"),check=el("input"),open=el("button",folder.name);check.type="checkbox";check.checked=selectedFolders.has(folder.path);check.setAttribute("aria-label","Select "+folder.name);check.addEventListener("change",()=>{if(check.checked)selectedFolders.add(folder.path);else selectedFolders.delete(folder.path);persistFolderState();renderSelectedFolders();});open.type="button";open.className="secondary";open.addEventListener("click",()=>loadFolderBrowser(folder.path));row.append(check,open);li.append(row);list.append(li);}if(!d.directories.length)list.append(el("li","No subfolders in this folder."));}catch(e){error.textContent="Could not list folders: "+e.message;}}
document.querySelector("#browse-folders").addEventListener("click",()=>{document.querySelector("#folder-browser").hidden=false;loadFolderBrowser(browserPath);});
document.querySelector("#folder-up").addEventListener("click",()=>{const parent=browserPath.split("/").slice(0,-1).join("/");loadFolderBrowser(parent);});
document.querySelector("#folder-select-current").addEventListener("click",()=>{selectedFolders.add(browserPath);persistFolderState();renderSelectedFolders();});
document.querySelector("#folder-close").addEventListener("click",()=>{document.querySelector("#folder-browser").hidden=true;});
document.querySelector("#clear-folder-selection").addEventListener("click",()=>{selectedFolders.clear();persistFolderState();renderSelectedFolders();});
scanButton.addEventListener("click",async()=>{if(!selectedTargetLanguages.size){status.textContent="Select at least one target language before scanning.";return;}scanButton.disabled=true;cancelScanButton.hidden=true;status.textContent="Starting scan…";scanResults.replaceChildren();candidateList.replaceChildren(el("p","Previous scan candidates cleared. Waiting for new results…"));try{const paths=selectedFolders.size?[...selectedFolders]:[""];persistFolderState();const r=await fetch("/api/scan",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({paths,languages:[...selectedTargetLanguages]})});const d=await r.json();if(!r.ok)throw Error(d.detail||r.statusText);status.textContent="Scan queued: "+d.total+" media files across "+paths.length+" folder(s).";trackJob(d.job_id);}catch(e){status.textContent="Could not start scan: "+e.message;scanButton.disabled=false;await loadCandidates();}});
async function readJsonResponse(response,context){const body=await response.text();let data;try{data=body?JSON.parse(body):null;}catch{const preview=body.trim().slice(0,180);throw Error(context+" returned non-JSON (HTTP "+response.status+"). "+(preview||"The response body was empty."));}if(!response.ok)throw Error(data?.detail||context+" failed (HTTP "+response.status+").");if(!data||typeof data!=="object")throw Error(context+" returned an empty or invalid response.");return data;}
async function trackJob(id){if(activeTrackedJobId===id)return;activeTrackedJobId=id;const panel=document.querySelector("#job-progress");let failures=0;let stopped=false;activeScanJobId=null;async function poll(){if(stopped)return;try{const r=await fetch("/api/jobs/"+id,{headers:{"Accept":"application/json"}});const j=await readJsonResponse(r,"Job status request");if(j.kind==="scan"&&["queued","running"].includes(j.status)){activeScanJobId=id;cancelScanButton.hidden=false;cancelScanButton.disabled=false;}else if(j.kind==="scan"){activeScanJobId=null;cancelScanButton.hidden=true;}if(typeof j.id!=="number"||typeof j.status!=="string")throw Error("Job status response is missing its job identity or status.");failures=0;panel.replaceChildren();panel.append(el("strong","Job #"+j.id+" · "+j.kind+" · "+j.status));line(panel,"Progress",j.completed+"/"+j.total+" · "+j.succeeded+" succeeded · "+j.failed+" failed");line(panel,"Phase",j.phase||"queued");line(panel,"Current file",j.current_file||"—");if(j.error)panel.append(el("p",j.error,"warning"));if(["completed","failed","cancelled","interrupted"].includes(j.status)){stopped=true;activeTrackedJobId=null;scanButton.disabled=false;cancelScanButton.hidden=true;activeScanJobId=null;status.textContent=j.kind==="scan"?"Scan "+j.status+".":"Injection "+j.status+".";await loadCandidates();return;}setTimeout(poll,1000);}catch(e){failures++;panel.replaceChildren();panel.append(el("p","Job #"+id+" is still being tracked, but its status could not be read: "+e.message,"warning"));if(failures<5){panel.append(el("p","Retrying status check ("+failures+"/5)…"));setTimeout(poll,Math.min(1000*Math.pow(2,failures-1),8000));return;}stopped=true;activeTrackedJobId=null;scanButton.disabled=false;cancelScanButton.hidden=true;activeScanJobId=null;status.textContent="Job #"+id+" may still be running; status tracking paused after repeated request failures.";const retry=el("button","Retry status check");retry.addEventListener("click",()=>{stopped=false;failures=0;retry.remove();poll();});panel.append(retry);}}poll();}
cancelScanButton.addEventListener("click",async()=>{if(activeScanJobId===null)return;cancelScanButton.disabled=true;status.textContent="Requesting scan cancellation…";try{const r=await fetch("/api/jobs/"+activeScanJobId+"/cancel",{method:"POST",headers:{"Accept":"application/json"}});const d=await readJsonResponse(r,"Cancel scan request");status.textContent=d.status==="cancelled"?"Scan cancelled. The current file, if any, was allowed to finish.":"Scan is no longer active ("+d.status+").";cancelScanButton.hidden=true;activeScanJobId=null;}catch(e){status.textContent="Could not cancel scan: "+e.message;cancelScanButton.disabled=false;}});\ndocument.querySelector("#clear-saved-data").addEventListener("click",async()=>{if(!confirm("Permanently clear all saved jobs, scan results, subtitle candidates, patch history, and .bak backup files under /media? Media files themselves will not be modified."))return;const button=document.querySelector("#clear-saved-data"),message=document.querySelector("#clear-data-message");button.disabled=true;message.textContent="Clearing saved data…";try{const r=await fetch("/api/data/clear",{method:"POST",headers:{"Accept":"application/json"}});const d=await readJsonResponse(r,"Clear saved data request");candidateList.replaceChildren(el("p","No saved candidates. Run a new scan to find subtitles."));scanResults.replaceChildren();document.querySelector("#job-progress").replaceChildren(el("p","No saved job progress."));status.textContent="Saved data cleared.";message.textContent="Cleared saved jobs, results, candidates, patch history, and "+d.backup_files_removed+" .bak backup file(s)."+(d.backup_files_failed?" "+d.backup_files_failed+" backup file(s) could not be removed.":"");}catch(e){message.textContent="Could not clear saved data: "+e.message;}finally{button.disabled=false;}});
loadCandidates().catch(e=>candidateList.textContent="Could not load candidates: "+e.message);
async function resumeLatestJob(){
 try{
  const r=await fetch("/api/jobs?limit=20",{headers:{"Accept":"application/json"}});
  const d=await readJsonResponse(r,"Restore job progress");
  const latest=d.jobs?.find(j=>["queued","running"].includes(j.status))||d.jobs?.[0];if(!latest)return;
  if(["queued","running"].includes(latest.status)){
   if(latest.kind==="scan")scanButton.disabled=true;status.textContent="Restoring "+latest.kind+" job #"+latest.id+"…";trackJob(latest.id);return;
  }
  const panel=document.querySelector("#job-progress");panel.replaceChildren();
  panel.append(el("strong","Job #"+latest.id+" · "+latest.kind+" · "+latest.status));
  line(panel,"Progress",latest.completed+"/"+latest.total+" · "+latest.succeeded+" succeeded · "+latest.failed+" failed");
  line(panel,"Phase",latest.phase||"—");line(panel,"Current file",latest.current_file||"—");
  if(latest.error)panel.append(el("p",latest.error,"warning"));
  status.textContent=(latest.kind==="scan"?"Scan ":"Job ")+latest.status+" (restored from saved history).";
 }catch(e){status.textContent="Could not restore previous progress: "+e.message;}
}
resumeLatestJob();
</script></body></html>""")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/languages")
def languages():
    return {"languages": [{"code": code} for code in sorted(LANGUAGE_CODES)]}


@app.get("/api/config")
def config():
    return {
        "media_root": settings.media_root,
        "test_mode": settings.test_mode,
        "opensubtitles_configured": public_provider_settings()["configured"],
        "candidate_staging_enabled": public_provider_settings()["configured"],
    }


@app.get("/api/settings/opensubtitles")
def get_opensubtitles_settings():
    return public_provider_settings()


@app.post("/api/settings/opensubtitles")
def update_opensubtitles_settings(request: OpenSubtitlesSettingsRequest):
    try:
        result = save_provider_settings(
            api_key=request.api_key,
            username=request.username,
            password=request.password,
            max_candidates_per_file=request.max_candidates_per_file,
            log_level=request.log_level,
            clear_account_credentials=request.clear_account_credentials,
        )
        configure_logging(result["log_level"])
        logger.info("logging_level_updated", extra={"event": "logging_level_updated",
                    "phase": "configuration", "log_level": result["log_level"]})
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        logger.exception("provider_settings_save_failed", extra={"event":"provider_settings_save_failed","phase":"configuration","error_type":type(exc).__name__})
        raise HTTPException(status_code=500, detail="Could not securely save OpenSubtitles settings to the persistent data volume.") from exc


@app.post("/api/settings/opensubtitles/test")
def test_opensubtitles_settings():
    try:
        test_provider_settings()
        return {"ok": True, "message": "OpenSubtitles connection verified."}
    except (ValueError, OpenSubtitlesError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/media/directories")
def media_directories(path: str = ""):
    media_root = Path(settings.media_root).resolve(strict=True)
    relative = Path(path or ".")
    if relative.is_absolute() or ".." in relative.parts:
        raise HTTPException(status_code=400, detail="Choose a folder inside the mounted media directory.")
    current = media_root
    for component in relative.parts:
        if component in {"", "."}:
            continue
        current = current / component
        if current.is_symlink():
            raise HTTPException(status_code=400, detail="Symbolic-link folders cannot be browsed.")
    try:
        current = current.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=404, detail="The selected folder does not exist.")
    if not current.is_relative_to(media_root) or not current.is_dir():
        raise HTTPException(status_code=400, detail="Choose a directory inside the mounted media directory.")
    directories = []
    try:
        for child in current.iterdir():
            if child.is_symlink() or not child.is_dir():
                continue
            child_relative = child.relative_to(media_root).as_posix()
            directories.append({"name": child.name, "path": child_relative})
    except OSError as exc:
        raise HTTPException(status_code=403, detail="Could not read this folder.") from exc
    directories.sort(key=lambda item: item["name"].casefold())
    current_relative = current.relative_to(media_root).as_posix()
    parent_relative = None if current == media_root else ("" if current.parent == media_root else current.parent.relative_to(media_root).as_posix())
    return {"path": "" if current_relative == "." else current_relative,
            "display_path": "/media" if current_relative == "." else "/media/" + current_relative,
            "parent_path": parent_relative, "directories": directories}


def candidate_should_be_visible(row: dict) -> bool:
    """Only expose fresh, successfully staged candidates awaiting approval."""
    return (
        row.get("status") == "staged"
        and row.get("review_decision") == "pending"
        and not row.get("is_stale")
        and row.get("patch_status") in {None, "", "not_started"}
    )


def _clear_media_backup_files(media_root: Path) -> tuple[int, int]:
    """Remove regular .bak files under the media root without following symlinks."""
    removed = failed = 0
    for current, directories, filenames in os.walk(media_root, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = [
            name for name in directories
            if not (current_path / name).is_symlink()
        ]
        for filename in filenames:
            backup = current_path / filename
            if backup.suffix.lower() != ".bak" or backup.is_symlink() or not backup.is_file():
                continue
            try:
                backup.unlink()
                removed += 1
            except OSError:
                failed += 1
                logger.warning(
                    "media_backup_cleanup_failed",
                    extra={"event":"media_backup_cleanup_failed","phase":"data_clear"},
                )
    return removed, failed


@app.post("/api/data/clear")
def clear_data():
    try:
        staged_paths = clear_saved_data()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    staging_root = Path(settings.subtitle_staging_path).resolve()
    removed = 0
    for value in staged_paths:
        try:
            path = Path(value).resolve()
            if path.is_relative_to(staging_root) and path.is_file():
                path.unlink()
                removed += 1
        except (OSError, RuntimeError, ValueError):
            logger.warning("saved_candidate_cleanup_failed", extra={"event":"saved_candidate_cleanup_failed","phase":"data_clear"})
    media_root = Path(settings.media_root).resolve(strict=True)
    backup_files_removed, backup_files_failed = _clear_media_backup_files(media_root)
    logger.info(
        "saved_data_cleared",
        extra={"event":"saved_data_cleared","phase":"data_clear","status":"completed",
               "backup_files_removed":backup_files_removed,"backup_files_failed":backup_files_failed},
    )
    return {
        "ok":True,
        "staged_files_removed":removed,
        "backup_files_removed":backup_files_removed,
        "backup_files_failed":backup_files_failed,
    }


@app.get("/api/candidates")
def candidates(all: bool = False):
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
    visible_rows = [row for row in rows if candidate_should_be_visible(row)]
    fields = (
        "id", "media_relative_path", "file_name", "language", "format",
        "content_language", "content_language_confidence", "content_status",
        "release", "moviehash_match", "download_count", "rating",
        "trusted_uploader", "hearing_impaired", "ai_translated", "match_score",
        "match_reasons", "status", "review_decision", "review_updated_at",
        "last_error", "is_stale", "inspection", "patch_status", "patch_started_at", "patch_completed_at",
    )
    output = [{key: row.get(key) for key in fields} for row in visible_rows]
    if all:
        visible_paths = {row["media_relative_path"] for row in output}
        root = Path(settings.media_root).resolve()
        for media in list_media_files():
            path = media["relative_path"]
            if path in visible_paths:
                continue
            media_path = root / path
            try:
                safe_path = media_path.resolve(strict=True)
                stale = not safe_path.is_relative_to(root) or not safe_path.is_file()
                if not stale:
                    stat_result = safe_path.stat()
                    stale = stat_result.st_size != media["size"] or stat_result.st_mtime != media["modified_at"]
            except (OSError, RuntimeError):
                stale = True
            output.append({"is_non_candidate": True, "media_relative_path": path,
                "file_name": Path(path).name, "inspection": media["inspection"],
                "is_stale": stale, "status": "scanned"})
    return {"candidates": output, "count": len(output)}



@app.post("/api/media-files/edit")
def edit_scanned_media(request: MediaEditRequest):
    if request.confirm is not True:
        raise HTTPException(status_code=400, detail="Explicit confirmation is required for media modification.")
    relative = Path(request.media_relative_path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise HTTPException(status_code=400, detail="Unsafe media path.")
    media = next((item for item in list_media_files() if item["relative_path"] == relative.as_posix()), None)
    if media is None:
        raise HTTPException(status_code=404, detail="This file is not in the scanned media inventory.")
    media_root = Path(settings.media_root).resolve(strict=True)
    source = media_root
    try:
        for part in relative.parts:
            source = source / part
            if source.is_symlink():
                raise HTTPException(status_code=400, detail="Symlink components are not allowed in media paths.")
        source = source.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=404, detail="The scanned media file is unavailable.")
    if not source.is_relative_to(media_root) or not source.is_file() or source.suffix.lower() != ".mkv":
        raise HTTPException(status_code=415, detail="Only scanned MKV media files can be edited.")
    current = source.stat()
    if current.st_size != media["size"] or current.st_mtime != media["modified_at"]:
        raise HTTPException(status_code=409, detail="The media file changed after scanning. Rescan before editing.")
    available = {int(track["index"]) for track in (media.get("inspection") or {}).get("subtitle_streams", [])
                 if isinstance(track.get("index"), int) and not isinstance(track.get("index"), bool)}
    indices = request.keep_subtitle_indices
    if any(not isinstance(index, int) or isinstance(index, bool) for index in indices):
        raise HTTPException(status_code=400, detail="Subtitle stream selections must be integer stream indices.")
    if len(indices) != len(set(indices)) or not set(indices).issubset(available):
        raise HTTPException(status_code=409, detail="Selected subtitle tracks do not match the scanned media. Rescan before editing.")
    try:
        languages = normalize_languages(request.target_languages)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job = create_job("inject", total=1, metadata={"existing_tracks_only": True, "media_relative_path": relative.as_posix()})
    start_job(job["id"])
    pattern = f".{source.name}.recovery-*.bak"
    before_backups = set(source.parent.glob(pattern))
    try:
        result = patch_mkv(source, None, approved=True, test_mode=settings.test_mode,
            expected_size=media["size"], expected_mtime=media["modified_at"], keep_subtitle_indices=indices)
        record_media_patch(relative.as_posix(), "patched", result, result.get("backup"))
        details = inspect_media(source)
        stat_result = source.stat()
        record_scan(relative.as_posix(), stat_result.st_size, stat_result.st_mtime, details)
        reset_candidate_reviews_for_media(relative.as_posix())
        reprocess_result = None
        if not indices and request.reprocess_if_empty:
            provider = load_provider_settings()
            if provider["api_key"]:
                try:
                    client = OpenSubtitlesClient(provider["api_key"], provider["username"], provider["password"])
                    reprocess_result = stage_candidates(client, source, media_root, stat_result,
                        details.get("metadata") or {}, languages)
                except OpenSubtitlesError as match_error:
                    reprocess_result = {"status": "provider_error", "error": str(match_error)}
            else:
                reprocess_result = {"status": "not_configured", "found": 0, "staged": 0}
        record_job_item(job["id"], media_path=relative.as_posix(), status="completed",
            phase="verified", patch_status="patched", details=result)
        update_job_progress(job["id"], phase="completed", completed=1, succeeded=1)
        finish_job(job["id"], "completed")
        return {"job_id": job["id"], "status": "completed", "result": result, "reprocess": reprocess_result,
            "message": "Existing subtitle tracks updated; no new subtitle was injected."
                + (" Matching reprocessed: " + str((reprocess_result or {}).get("status", "completed")) + "." if not indices and request.reprocess_if_empty else "")}
    except Exception as exc:
        backups = set(source.parent.glob(pattern)) - before_backups
        backup = str(max(backups, key=lambda path: path.stat().st_mtime_ns)) if backups else None
        error = f"{type(exc).__name__}: {str(exc)[:400]}"
        record_media_patch(relative.as_posix(), "failed", {"error": error, "recovery_path": backup}, backup)
        record_job_item(job["id"], media_path=relative.as_posix(), status="failed",
            phase="existing_track_edit", patch_status="failed", error=error, details={"recovery_path": backup})
        update_job_progress(job["id"], phase="failed", completed=1, failed=1)
        finish_job(job["id"], "failed", error)
        raise HTTPException(status_code=500, detail="Existing subtitle-track edit failed: " + error) from exc

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
    job = create_job("review", total=1, metadata={"decision":request.decision})
    start_job(job["id"])
    record_job_item(job["id"], media_path=row["media_relative_path"], status="completed",
                    phase="review", candidate_id=candidate_id,
                    review_decision=request.decision)
    update_job_progress(job["id"], phase="review", completed=1, succeeded=1)
    finish_job(job["id"], "completed")
    logger.info("candidate_reviewed", extra={"event":"candidate_reviewed",
                "candidate_id":candidate_id,"media_path":row["media_relative_path"],
                "phase":"review","status":request.decision})
    return {**decision, "job_id":job["id"]}


def _run_injection_job(job_id: int, candidate_id: int, row: dict,
                       source: Path, subtitle: Path, keep_subtitle_indices: list[int] | None = None,
                       target_language: str = "en"):
    start_job(job_id)
    update_job_progress(job_id, phase="muxing", current_file=row["media_relative_path"], total=1)
    logger.info("injection_started", extra={"event":"injection_started","job_id":job_id,
                "candidate_id":candidate_id,"media_path":row["media_relative_path"],"phase":"muxing"})
    pattern = f".{source.name}.recovery-*.bak"
    before = set(source.parent.glob(pattern))
    try:
        result = patch_mkv(source, subtitle, approved=True, test_mode=settings.test_mode,
                           expected_size=row["media_size"], expected_mtime=row["media_modified_at"],
                           keep_subtitle_indices=keep_subtitle_indices,
                           target_language=target_language)
        finish_candidate_patch(candidate_id, "patched", result, result.get("backup"))
        record_job_item(job_id, media_path=row["media_relative_path"], status="completed",
                        phase="verified", candidate_id=candidate_id, review_decision="approved",
                        patch_status="patched", details=result)
        update_job_progress(job_id, phase="completed", completed=1, succeeded=1)
        finish_job(job_id, "completed")
        logger.info("injection_completed", extra={"event":"injection_completed","job_id":job_id,
                    "candidate_id":candidate_id,"media_path":row["media_relative_path"],
                    "phase":"verified","status":"patched"})
        return result
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
        return None


@app.post("/api/candidates/{candidate_id}/inject")
def inject_candidate(candidate_id: int, request: InjectRequest, background_tasks: BackgroundTasks = None):
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
    target_language = row["language"].strip().lower()
    if target_language not in LANGUAGE_CODES:
        raise HTTPException(status_code=415, detail="The candidate has an unsupported target language. Rescan with a supported language.")
    if subtitle.suffix.lower() not in {".srt", ".ass", ".ssa", ".sup", ".sub"}:
        raise HTTPException(status_code=415, detail="Unsupported subtitle format.")
    if Path(row["file_name"]).suffix.lower() != subtitle.suffix.lower():
        raise HTTPException(status_code=409, detail="Staged subtitle format does not match the reviewed candidate.")
    inspected = next((item for item in list_candidates(row["media_relative_path"])
                      if item["id"] == candidate_id), None)
    if inspected is None:
        raise HTTPException(status_code=409, detail="Could not verify the inspected subtitle tracks. Rescan before injecting.")
    available_indices = {int(track["index"]) for track in
                         (inspected.get("inspection") or {}).get("subtitle_streams", [])
                         if isinstance(track.get("index"), int)}
    requested_indices = request.keep_subtitle_indices
    if requested_indices is not None:
        if any(not isinstance(index, int) or isinstance(index, bool) for index in requested_indices):
            raise HTTPException(status_code=400, detail="Subtitle stream selections must be integer stream indices.")
        if len(requested_indices) != len(set(requested_indices)) or not set(requested_indices).issubset(available_indices):
            raise HTTPException(status_code=409, detail="The selected existing subtitle tracks no longer match the inspected media. Rescan before injecting.")
    else:
        requested_indices = sorted(available_indices)
    try:
        claimed = claim_candidate_patch(candidate_id, test_mode=settings.test_mode)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if claimed is None:
        raise HTTPException(status_code=404, detail="Candidate not found.")
    job = create_job("inject", total=1, metadata={"candidate_id":candidate_id,
                      "test_mode":bool(settings.test_mode)})
    record_job_item(job["id"], media_path=row["media_relative_path"], status="running",
                    phase="queued", candidate_id=candidate_id, review_decision="approved",
                    patch_status="running")
    if background_tasks is None:
        result = _run_injection_job(job["id"], candidate_id, row, source, subtitle, requested_indices, target_language)
        if result is None:
            raise HTTPException(status_code=500, detail="Subtitle injection failed. Inspect job history and retained recovery files.")
        return {"candidate_id":candidate_id,"media":row["media_relative_path"],
                "test_mode":settings.test_mode,**result}
    background_tasks.add_task(_run_injection_job, job["id"], candidate_id, row, source, subtitle, requested_indices, target_language)
    logger.info("injection_queued", extra={"event":"injection_queued","job_id":job["id"],
                "candidate_id":candidate_id,"media_path":row["media_relative_path"],"phase":"queued"})
    return {"job_id":job["id"],"candidate_id":candidate_id,"status":"queued",
            "test_mode":settings.test_mode}


def _scan_file_identity(stat_result) -> tuple[int, int, int, int]:
    """Identity fields used to invalidate cached inspection results."""
    return (
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_dev,
        stat_result.st_ino,
    )


def _scan_job_is_running(job_id: int) -> bool:
    job_state = get_job(job_id)
    return job_state is not None and job_state["status"] == "running"


def _scan_one_file(job_id: int, root: Path, media_root: Path, path: Path,
                   client: OpenSubtitlesClient | None,
                   target_languages: list[str]) -> dict | None:
    """Inspect and discover candidates for one file; safe to run in a worker thread."""
    media_relative_path = str(path.relative_to(media_root))
    if not _scan_job_is_running(job_id):
        return {
            "media_path": media_relative_path,
            "status": "cancelled",
            "phase": "cancelled",
        }
    if not media_path_is_safe(path, root):
        return {
            "media_path": media_relative_path,
            "status": "failed",
            "phase": "scan",
            "error": "Media file disappeared or moved outside the selected scan root.",
        }
    try:
        stat_result = path.stat()
        details = get_cached_scan(media_relative_path, stat_result)
        if details is not None:
            current_stat = path.stat()
            if _scan_file_identity(current_stat) != _scan_file_identity(stat_result):
                stat_result = current_stat
                details = None
        if details is None:
            with timed_stage("media_inspection", job_id=job_id,
                             media_path=media_relative_path,
                             file_size_bytes=stat_result.st_size):
                details = inspect_media(path)
            inspected_stat = path.stat()
            if _scan_file_identity(inspected_stat) != _scan_file_identity(stat_result):
                raise OSError("Media file changed during inspection; retry the scan.")
            stat_result = inspected_stat
            with timed_stage("scan_record_persistence", job_id=job_id,
                             media_path=media_relative_path):
                record_scan(
                    media_relative_path, stat_result.st_size, stat_result.st_mtime,
                    details, modified_ns=stat_result.st_mtime_ns,
                    device=stat_result.st_dev, inode=stat_result.st_ino,
                )
            logger.info(
                "media_inspection_cache_miss",
                extra={"event": "media_inspection_cache_miss", "job_id": job_id,
                       "media_path": media_relative_path, "phase": "inspection"},
            )
        else:
            logger.info(
                "media_inspection_cache_hit",
                extra={"event": "media_inspection_cache_hit", "job_id": job_id,
                       "media_path": media_relative_path, "phase": "inspection"},
            )

        summary = {"status": "not_configured", "found": 0, "staged": 0, "candidates": []}
        phase = "inspected"
        if target_languages == ["en"] and details["has_verified_english_subtitle"]:
            summary["status"] = "verified_english_present"
            logger.info(
                "candidate_discovery_skipped",
                extra={"event": "candidate_discovery_skipped",
                       "job_id": job_id, "media_path": media_relative_path,
                       "phase": "discovery", "filter_reason": "verified_english_subtitle_present"},
            )
        elif client is not None:
            if not _scan_job_is_running(job_id):
                return {"media_path": media_relative_path, "status": "cancelled",
                        "phase": "cancelled"}
            phase = "discovery"
            try:
                with timed_stage("candidate_staging", job_id=job_id,
                                 media_path=media_relative_path):
                    summary = stage_candidates(
                        client, path, media_root, stat_result,
                        details.get("metadata") or {}, target_languages,
                        should_cancel=lambda: not _scan_job_is_running(job_id),
                    )
            except OpenSubtitlesError as exc:
                logger.warning(
                    "opensubtitles_search_failed",
                    extra={"event": "opensubtitles_search_failed", "job_id": job_id,
                           "media_path": media_relative_path, "phase": "discovery",
                           "error_type": type(exc).__name__},
                )
                summary = {"status": "provider_error", "found": 0, "staged": 0,
                           "candidates": [], "error": str(exc)}
            if summary.get("status") == "cancelled" or not _scan_job_is_running(job_id):
                return {"media_path": media_relative_path, "status": "cancelled",
                        "phase": "cancelled"}
        else:
            logger.info(
                "candidate_discovery_skipped",
                extra={"event": "candidate_discovery_skipped",
                       "job_id": job_id, "media_path": media_relative_path,
                       "phase": "discovery", "filter_reason": "provider_not_configured"},
            )
        return {
            "media_path": media_relative_path,
            "status": "completed",
            "phase": phase,
            "summary": summary,
        }
    except (OSError, subprocess.SubprocessError, ValueError, OpenSubtitlesError) as exc:
        logger.exception(
            "media_scan_failed",
            extra={"event": "media_scan_failed", "job_id": job_id,
                   "media_path": media_relative_path, "phase": "scan",
                   "error_type": type(exc).__name__},
        )
        return {
            "media_path": media_relative_path,
            "status": "failed",
            "phase": "scan",
            "error": f"{type(exc).__name__}: {str(exc)[:400]}",
        }


def _scan_worker(job_id: int, root: Path, paths: list[Path],
                 target_languages: list[str] | None = None):
    start_job(job_id)
    try:
        if not _scan_job_is_running(job_id):
            return
        target_languages = normalize_languages(target_languages)
        media_root = Path(settings.media_root).resolve()
        provider = load_provider_settings()
        client = (
            OpenSubtitlesClient(provider["api_key"], provider["username"], provider["password"])
            if provider["api_key"] else None
        )
        worker_count = max(1, SCAN_WORKERS)
        completed = succeeded = failed = 0
        update_job_progress(job_id, phase="inspecting", total=len(paths))
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="media-scan") as executor:
            for offset in range(0, len(paths), worker_count):
                if not _scan_job_is_running(job_id):
                    logger.info(
                        "scan_job_cancelled",
                        extra={"event": "scan_job_cancelled", "job_id": job_id,
                               "phase": "cancelled", "status": "cancelled"},
                    )
                    return
                batch = paths[offset:offset + worker_count]
                valid_paths = [path for path in batch if media_path_is_safe(path, root)]
                if not valid_paths:
                    continue
                update_job_progress(
                    job_id, phase="inspecting",
                    current_file=valid_paths[0].relative_to(media_root).as_posix(),
                )
                for path in valid_paths:
                    record_job_item(
                        job_id, media_path=path.relative_to(media_root).as_posix(),
                        status="running", phase="inspecting",
                    )
                futures = [
                    executor.submit(
                        _scan_one_file, job_id, root, media_root, path, client, target_languages
                    )
                    for path in valid_paths
                ]
                # Read results in input order: parallel execution does not reorder
                # the scan's persisted job items or progress accounting.
                results = [future.result() for future in futures]
                for result in results:
                    if result is None:
                        continue
                    media_relative_path = result["media_path"]
                    if result["status"] == "completed":
                        summary = result["summary"]
                        record_job_item(
                            job_id, media_path=media_relative_path, status="completed",
                            phase=result["phase"],
                            details={"candidate_status": summary["status"],
                                     "candidates_found": summary["found"],
                                     "candidates_staged": summary["staged"]},
                        )
                        succeeded += 1
                        logger.info(
                            "media_scan_completed",
                            extra={"event": "media_scan_completed", "job_id": job_id,
                                   "media_path": media_relative_path, "phase": result["phase"],
                                   "status": "completed"},
                        )
                    elif result["status"] == "failed":
                        record_job_item(
                            job_id, media_path=media_relative_path, status="failed",
                            phase=result["phase"], error=result["error"],
                        )
                        failed += 1
                    else:
                        record_job_item(
                            job_id, media_path=media_relative_path, status="cancelled",
                            phase="cancelled",
                        )
                    completed += 1
                    update_job_progress(
                        job_id, completed=completed, succeeded=succeeded, failed=failed,
                    )
                if not _scan_job_is_running(job_id):
                    return
        finish_job(job_id, "completed", f"{failed} file(s) failed." if failed else None)
        logger.info(
            "scan_job_completed",
            extra={"event": "scan_job_completed", "job_id": job_id,
                   "phase": "completed", "status": "completed"},
        )
    except Exception as exc:
        finish_job(job_id, "failed", f"{type(exc).__name__}: {str(exc)[:400]}")
        logger.exception(
            "scan_job_failed",
            extra={"event": "scan_job_failed", "job_id": job_id,
                   "phase": "scan", "error_type": type(exc).__name__},
        )


@app.post("/api/scan")
def scan(request: ScanRequest, background_tasks: BackgroundTasks):
    try:
        target_languages = normalize_languages(request.languages)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    media_root = Path(settings.media_root).resolve(strict=True)
    requested_paths = request.paths or [request.path]
    roots = []
    for value in requested_paths:
        requested = Path((value or "").strip() or ".")
        if requested.is_absolute():
            raw = str(requested)
            if raw == "/media":
                requested = Path(".")
            elif raw.startswith("/media/"):
                requested = Path(raw[len("/media/"):])
            else:
                raise HTTPException(status_code=400, detail="Choose folders inside the mounted /media directory.")
        if ".." in requested.parts:
            raise HTTPException(status_code=400, detail="Parent-directory paths are not allowed.")
        candidate = media_root
        for component in requested.parts:
            if component in {"", "."}:
                continue
            candidate = candidate / component
            if candidate.is_symlink():
                raise HTTPException(status_code=400, detail="Symbolic-link folders cannot be scanned.")
        try:
            candidate = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            raise HTTPException(status_code=400, detail="A selected folder does not exist.")
        if not candidate.is_relative_to(media_root) or not candidate.is_dir():
            raise HTTPException(status_code=400, detail="Every selected folder must be inside /media.")
        if candidate not in roots:
            roots.append(candidate)
    if not roots:
        raise HTTPException(status_code=400, detail="Select at least one folder to scan.")
    for staged_path in clear_pending_review_queue():
        try:
            staging_root = Path(settings.subtitle_staging_path).resolve()
            candidate_path = Path(staged_path).resolve()
            if candidate_path.is_relative_to(staging_root) and candidate_path.is_file():
                candidate_path.unlink()
        except (OSError, RuntimeError, ValueError):
            logger.warning("stale_candidate_cleanup_failed", extra={"event":"stale_candidate_cleanup_failed","phase":"scan_reset"})
    try:
        paths = []
        seen = set()
        with timed_stage("scan_inventory", phase="discovery"):
            for selected_root in roots:
                for media_file in scan_media(selected_root):
                    canonical = media_file.resolve(strict=True)
                    if canonical not in seen:
                        seen.add(canonical)
                        paths.append(canonical)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info(
        "scan_inventory_completed",
        extra={"event": "scan_inventory_completed", "phase": "discovery",
               "file_count": len(paths), "status": f"{len(paths)} supported MKV files across {len(roots)} selected folder(s)"},
    )
    job = create_job("scan", total=len(paths), metadata={
        "scan_roots": [str(root.relative_to(media_root)) for root in roots],
        "target_languages": target_languages,
    })
    background_tasks.add_task(_scan_worker, job["id"], media_root, paths, target_languages)
    logger.info("scan_job_queued", extra={"event":"scan_job_queued","job_id":job["id"],
                "phase":"queued","status":"queued"})
    return {"job_id":job["id"],"status":"queued","total":len(paths),"folders":len(roots)}


@app.get("/api/jobs")
def jobs(limit: int = 25):
    return {"jobs":list_jobs(limit)}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["kind"] != "scan":
        raise HTTPException(status_code=409, detail="Only scan jobs can be cancelled.")
    if job["status"] not in {"queued", "running"}:
        raise HTTPException(status_code=409, detail=f"This scan is already {job['status']}.")
    finish_job(job_id, "cancelled", "Cancelled by user.")
    updated = get_job(job_id)
    if updated is None or updated["status"] != "cancelled":
        raise HTTPException(status_code=409, detail="The scan finished before cancellation could be applied.")
    logger.info("scan_job_cancelled", extra={"event":"scan_job_cancelled","job_id":job_id,
                "phase":"cancelled","status":"cancelled"})
    return {"job_id":job_id,"status":"cancelled"}


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: int):
    result = get_job(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return result