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
    mark_candidate_error, list_candidates, set_candidate_decision,
    get_candidate, claim_candidate_patch, finish_candidate_patch, recover_interrupted_patches,
    create_job, start_job, update_job_progress, record_job_item, finish_job,
    get_job, list_jobs, recover_interrupted_jobs, clear_pending_review_queue, clear_saved_data,
)
from app.media import inspect_media
from app.opensubtitles import OpenSubtitlesClient, OpenSubtitlesError, assess_subtitle_payload
from app.scanner import scan_media
from app.patcher import patch_mkv
from app.provider_settings import (
    load_provider_settings, public_provider_settings, save_provider_settings,
    test_provider_settings,
)

app = FastAPI(title=settings.app_name)


class ScanRequest(BaseModel):
    path: str = "/media"
    paths: list[str] = []


class CandidateDecisionRequest(BaseModel):
    decision: str


class InjectRequest(BaseModel):
    confirm: bool = False


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
                     stat_result, metadata: dict) -> dict:
    """Search and stage the top candidates outside the media mount."""
    relative_media_path = str(path.relative_to(media_root))
    staging_root = Path(settings.subtitle_staging_path).resolve()
    if staging_root == media_root or staging_root.is_relative_to(media_root):
        raise OpenSubtitlesError("Subtitle staging directory must be outside the media library.")
    logger.info("opensubtitles_search_started", extra={"event":"opensubtitles_search_started","media_path":relative_media_path,"phase":"discovery"})
    candidates = client.search(path, metadata)
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
            relative_media_path, stat_result.st_size, stat_result.st_mtime, candidate
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
<section class="panel"><h2>Media scan</h2><p>Browse the mounted media directory and select one or more folders to scan.</p><button id="browse-folders" class="secondary">Browse folders</button><button id="clear-folder-selection" class="secondary" type="button">Clear selection</button><div id="selected-folders" class="selected-folders"><span class="folder-chip">/media (default)</span></div><div id="folder-browser" class="folder-browser" hidden><div class="actions"><button id="folder-up" class="secondary" type="button">Up one level</button><button id="folder-select-current" class="secondary" type="button">Select this folder</button><button id="folder-close" class="secondary" type="button">Close</button></div><p id="folder-current" class="folder-path">/media</p><ul id="folder-list" class="folder-list"></ul><p id="folder-error" class="warning" aria-live="polite"></p></div><button id="scan">Start scan</button><button id="cancel-scan" class="secondary" type="button" hidden disabled>Cancel scan</button><p id="status">Ready.</p><div id="scan-results"></div><div id="job-progress" class="candidate" aria-live="polite">No active job.</div></section>
<section class="panel"><div class="actions" style="justify-content:space-between;align-items:center"><div><h2>Subtitle candidate review</h2><p>Review candidates for the latest scan.</p></div><button id="clear-saved-data" class="secondary" type="button">Clear saved data</button></div><div id="candidate-list">Loading saved candidates…</div><p id="clear-data-message" aria-live="polite"></p></section>
<script>
const scanButton=document.querySelector("#scan"),cancelScanButton=document.querySelector("#cancel-scan"),status=document.querySelector("#status"),scanResults=document.querySelector("#scan-results"),candidateList=document.querySelector("#candidate-list");
let activeScanJobId=null;let activeTrackedJobId=null;
function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function line(parent,label,value){const p=el("p");p.append(el("strong",label+": "),document.createTextNode(value===null||value===undefined?"Unknown":String(value)));parent.append(p);}
function streams(value){if(!Array.isArray(value)||!value.length)return "";return value.map(s=>[s.codec,s.language,s.title].filter(Boolean).join(" · ")||JSON.stringify(s)).join("; ");}
async function loadCandidates(){const r=await fetch("/api/candidates");const d=await r.json();if(!r.ok)throw Error(d.detail||r.statusText);renderCandidates(d.candidates);}
function renderCandidates(items){candidateList.replaceChildren();if(!items.length){candidateList.append(el("p","No saved candidates yet. Run a scan with OpenSubtitles configured."));return;}
for(const c of items){const card=el("article",undefined,"candidate");card.append(el("h3",c.media_relative_path));card.append(el("p","Candidate #"+c.id+" · "+c.file_name));line(card,"Provider language / format",c.language+" / "+c.format);line(card,"Release",c.release||"Not provided");line(card,"Match score",c.match_score);line(card,"Exact file hash match",c.moviehash_match?"Yes":"No");line(card,"Candidate download status",c.status);line(card,"Match rationale",(c.match_reasons||[]).join("; ")||"No rationale supplied");line(card,"Content check",c.content_status+(c.content_language?" · "+c.content_language:"")+(c.content_language_confidence?" · confidence "+Number(c.content_language_confidence).toFixed(2):""));line(card,"Existing audio tracks",streams(c.inspection?.audio_streams));line(card,"Existing subtitle tracks",streams(c.inspection?.subtitle_streams));
if(c.is_stale)card.append(el("p","This candidate is stale: the media file changed after discovery. Rescan before reviewing.","warning"));
card.append(el("p","Review decision: "+c.review_decision+(c.review_updated_at?" · "+c.review_updated_at:"")));
line(card,"Injection status",c.patch_status||"not_started");
if(c.patch_status==="interrupted")card.append(el("p","An earlier injection was interrupted. Inspect the media file and retained recovery copy manually before proceeding.","warning"));
if(c.status==="staged"){const links=el("div",undefined,"actions"),preview=el("a","Preview subtitle"),download=el("a","Download candidate");preview.href="/api/candidates/"+c.id+"/preview";preview.target="_blank";preview.rel="noopener";download.href="/api/candidates/"+c.id+"/download";links.append(preview,download);card.append(links);}else if(c.last_error){card.append(el("p",c.last_error,"warning"));}
if(c.status==="staged"&&c.review_decision==="pending"&&!c.is_stale){const actions=el("div",undefined,"actions"),approve=el("button","Approve & inject"),reject=el("button","Reject this candidate");reject.className="secondary";approve.addEventListener("click",()=>decide(c.id,"approved",approve));reject.addEventListener("click",()=>decide(c.id,"rejected",reject));actions.append(approve,reject);card.append(actions);}
if(c.status==="staged"&&c.review_decision==="approved"&&!c.is_stale&&c.patch_status!=="patched"&&c.patch_status!=="running"&&c.patch_status!=="interrupted"){const injectButton=el("button",c.patch_status==="failed"?"Retry injection":"Inject approved subtitle");injectButton.addEventListener("click",()=>inject(c.id,injectButton));card.append(injectButton);}
candidateList.append(card);}}
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
  const injection=await fetch("/api/candidates/"+id+"/inject",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({confirm:true})});
  const result=await injection.json();if(!injection.ok){await fetch("/api/candidates/"+id+"/decision",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({decision:"pending"})});throw Error(result.detail||injection.statusText);}
  trackJob(result.job_id);
 }else{await loadCandidates();}
}catch(e){alert("Could not "+(decision==="approved"?"approve and inject":"save decision")+": "+e.message);button.disabled=false;await loadCandidates();}
}
async function inject(id,button){const mode=document.querySelector("#mode").textContent;const warning=mode==="TEST MODE"?"TEST MODE will replace this MKV and retain a recovery copy beside it.":"LIVE MODE will replace this MKV; its recovery copy is deleted only after post-replacement verification.";if(!confirm(warning+"\\n\\nInject the explicitly approved subtitle into this exact media file now?"))return;button.disabled=true;try{const r=await fetch("/api/candidates/"+id+"/inject",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({confirm:true})});const d=await r.json();if(!r.ok)throw Error(d.detail||r.statusText);trackJob(d.job_id);}catch(e){alert("Could not start injection: "+e.message);button.disabled=false;await loadCandidates();}}
async function loadProviderSettings(){try{const r=await fetch("/api/settings/opensubtitles"),s=await r.json();if(!r.ok)throw Error(s.detail||r.statusText);document.querySelector("#provider-status").textContent=s.configured?"OpenSubtitles is configured"+(s.has_account_credentials?" with account credentials.":" using the API-key allowance."):"OpenSubtitles is not configured yet.";document.querySelector("#provider-limit").value=s.max_candidates_per_file;document.querySelector("#provider-log-level").value=s.log_level||"INFO";}catch(e){document.querySelector("#provider-status").textContent="Could not load provider settings: "+e.message;}}
document.querySelector("#provider-save").addEventListener("click",async()=>{const button=document.querySelector("#provider-save"),message=document.querySelector("#provider-message");button.disabled=true;message.textContent="Saving securely…";try{const r=await fetch("/api/settings/opensubtitles",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({api_key:document.querySelector("#provider-api-key").value,username:document.querySelector("#provider-username").value,password:document.querySelector("#provider-password").value,max_candidates_per_file:Number(document.querySelector("#provider-limit").value),log_level:document.querySelector("#provider-log-level").value,clear_account_credentials:document.querySelector("#provider-clear-account").checked})});const d=await r.json();if(!r.ok)throw Error(d.detail||r.statusText);document.querySelector("#provider-api-key").value="";document.querySelector("#provider-username").value="";document.querySelector("#provider-password").value="";document.querySelector("#provider-clear-account").checked=false;message.textContent="Settings saved to the persistent data volume. "+(d.configured?"Provider is configured.":"");await loadProviderSettings();const c=await fetch("/api/config").then(x=>x.json());document.querySelector("#provider-status").textContent=c.opensubtitles_configured?"OpenSubtitles is configured.":"OpenSubtitles is not configured.";}catch(e){message.textContent="Could not save settings: "+e.message;}finally{button.disabled=false;}});
document.querySelector("#provider-test").addEventListener("click",async()=>{const button=document.querySelector("#provider-test"),message=document.querySelector("#provider-message");button.disabled=true;message.textContent="Testing OpenSubtitles…";try{const r=await fetch("/api/settings/opensubtitles/test",{method:"POST"}),d=await r.json();if(!r.ok)throw Error(d.detail||r.statusText);message.textContent=d.message;}catch(e){message.textContent="Connection test failed: "+e.message;}finally{button.disabled=false;}});
loadProviderSettings();
fetch("/api/config").then(r=>r.json()).then(c=>{document.querySelector("#mode").textContent=c.test_mode?"TEST MODE":"LIVE MODE";}).catch(()=>{document.querySelector("#mode").textContent="unknown";});
let browserPath="";let selectedFolders=new Set();
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
scanButton.addEventListener("click",async()=>{scanButton.disabled=true;cancelScanButton.hidden=true;status.textContent="Starting scan…";scanResults.replaceChildren();candidateList.replaceChildren(el("p","Previous scan candidates cleared. Waiting for new results…"));try{const paths=selectedFolders.size?[...selectedFolders]:[""];persistFolderState();const r=await fetch("/api/scan",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({paths})});const d=await r.json();if(!r.ok)throw Error(d.detail||r.statusText);status.textContent="Scan queued: "+d.total+" media files across "+paths.length+" folder(s).";trackJob(d.job_id);}catch(e){status.textContent="Could not start scan: "+e.message;scanButton.disabled=false;await loadCandidates();}});
async function readJsonResponse(response,context){const body=await response.text();let data;try{data=body?JSON.parse(body):null;}catch{const preview=body.trim().slice(0,180);throw Error(context+" returned non-JSON (HTTP "+response.status+"). "+(preview||"The response body was empty."));}if(!response.ok)throw Error(data?.detail||context+" failed (HTTP "+response.status+").");if(!data||typeof data!=="object")throw Error(context+" returned an empty or invalid response.");return data;}
async function trackJob(id){if(activeTrackedJobId===id)return;activeTrackedJobId=id;const panel=document.querySelector("#job-progress");let failures=0;let stopped=false;activeScanJobId=null;async function poll(){if(stopped)return;try{const r=await fetch("/api/jobs/"+id,{headers:{"Accept":"application/json"}});const j=await readJsonResponse(r,"Job status request");if(j.kind==="scan"&&["queued","running"].includes(j.status)){activeScanJobId=id;cancelScanButton.hidden=false;cancelScanButton.disabled=false;}else if(j.kind==="scan"){activeScanJobId=null;cancelScanButton.hidden=true;}if(typeof j.id!=="number"||typeof j.status!=="string")throw Error("Job status response is missing its job identity or status.");failures=0;panel.replaceChildren();panel.append(el("strong","Job #"+j.id+" · "+j.kind+" · "+j.status));line(panel,"Progress",j.completed+"/"+j.total+" · "+j.succeeded+" succeeded · "+j.failed+" failed");line(panel,"Phase",j.phase||"queued");line(panel,"Current file",j.current_file||"—");if(j.error)panel.append(el("p",j.error,"warning"));if(["completed","failed","cancelled","interrupted"].includes(j.status)){stopped=true;activeTrackedJobId=null;scanButton.disabled=false;cancelScanButton.hidden=true;activeScanJobId=null;status.textContent=j.kind==="scan"?"Scan "+j.status+".":"Injection "+j.status+".";await loadCandidates();return;}setTimeout(poll,1000);}catch(e){failures++;panel.replaceChildren();panel.append(el("p","Job #"+id+" is still being tracked, but its status could not be read: "+e.message,"warning"));if(failures<5){panel.append(el("p","Retrying status check ("+failures+"/5)…"));setTimeout(poll,Math.min(1000*Math.pow(2,failures-1),8000));return;}stopped=true;activeTrackedJobId=null;scanButton.disabled=false;cancelScanButton.hidden=true;activeScanJobId=null;status.textContent="Job #"+id+" may still be running; status tracking paused after repeated request failures.";const retry=el("button","Retry status check");retry.addEventListener("click",()=>{stopped=false;failures=0;retry.remove();poll();});panel.append(retry);}}poll();}
cancelScanButton.addEventListener("click",async()=>{if(activeScanJobId===null)return;cancelScanButton.disabled=true;status.textContent="Requesting scan cancellation…";try{const r=await fetch("/api/jobs/"+activeScanJobId+"/cancel",{method:"POST",headers:{"Accept":"application/json"}});const d=await readJsonResponse(r,"Cancel scan request");status.textContent=d.status==="cancelled"?"Scan cancelled. The current file, if any, was allowed to finish.":"Scan is no longer active ("+d.status+").";cancelScanButton.hidden=true;activeScanJobId=null;}catch(e){status.textContent="Could not cancel scan: "+e.message;cancelScanButton.disabled=false;}});\ndocument.querySelector("#clear-saved-data").addEventListener("click",async()=>{if(!confirm("Permanently clear all saved jobs, scan results, subtitle candidates, and patch history? This does not modify media files."))return;const button=document.querySelector("#clear-saved-data"),message=document.querySelector("#clear-data-message");button.disabled=true;message.textContent="Clearing saved data…";try{const r=await fetch("/api/data/clear",{method:"POST",headers:{"Accept":"application/json"}});const d=await readJsonResponse(r,"Clear saved data request");candidateList.replaceChildren(el("p","No saved candidates. Run a new scan to find subtitles."));scanResults.replaceChildren();document.querySelector("#job-progress").replaceChildren(el("p","No saved job progress."));status.textContent="Saved data cleared.";message.textContent="Cleared saved jobs, results, candidates, and patch history.";}catch(e){message.textContent="Could not clear saved data: "+e.message;}finally{button.disabled=false;}});
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
    logger.info("saved_data_cleared", extra={"event":"saved_data_cleared","phase":"data_clear","status":"completed"})
    return {"ok":True,"staged_files_removed":removed}


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
    rows = [row for row in rows if candidate_should_be_visible(row)]
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
    record_job_item(job["id"], media_path=row["media_relative_path"], status="running",
                    phase="queued", candidate_id=candidate_id, review_decision="approved",
                    patch_status="running")
    if background_tasks is None:
        result = _run_injection_job(job["id"], candidate_id, row, source, subtitle)
        if result is None:
            raise HTTPException(status_code=500, detail="Subtitle injection failed. Inspect job history and retained recovery files.")
        return {"candidate_id":candidate_id,"media":row["media_relative_path"],
                "test_mode":settings.test_mode,**result}
    background_tasks.add_task(_run_injection_job, job["id"], candidate_id, row, source, subtitle)
    logger.info("injection_queued", extra={"event":"injection_queued","job_id":job["id"],
                "candidate_id":candidate_id,"media_path":row["media_relative_path"],"phase":"queued"})
    return {"job_id":job["id"],"candidate_id":candidate_id,"status":"queued",
            "test_mode":settings.test_mode}


def _scan_worker(job_id: int, root: Path, paths: list[Path]):
    start_job(job_id)
    try:
        job_state = get_job(job_id)
        if job_state is None or job_state["status"] != "running":
            return
        update_job_progress(job_id, phase="inspecting", total=len(paths))
        client = None
        provider = load_provider_settings()
        if provider["api_key"]:
            client = OpenSubtitlesClient(provider["api_key"], provider["username"], provider["password"])
        completed = succeeded = failed = 0
        for path in paths:
            job_state = get_job(job_id)
            if job_state is None or job_state["status"] != "running":
                logger.info("scan_job_cancelled", extra={"event":"scan_job_cancelled","job_id":job_id,"phase":"cancelled","status":"cancelled"})
                return
            if not media_path_is_safe(path, root):
                continue
            relative_path = str(path.relative_to(root))
            media_relative_path = str(path.relative_to(Path(settings.media_root).resolve()))
            update_job_progress(job_id, phase="inspecting", current_file=media_relative_path)
            record_job_item(job_id, media_path=media_relative_path, status="running", phase="inspecting")
            try:
                details = inspect_media(path)
                stat_result = path.stat()
                record_scan(media_relative_path, stat_result.st_size, stat_result.st_mtime, details)
                summary = {"status":"not_configured","found":0,"staged":0,"candidates":[]}
                phase = "inspected"
                if details["has_verified_english_subtitle"]:
                    summary["status"] = "verified_english_present"
                    logger.info(
                        "candidate_discovery_skipped",
                        extra={"event": "candidate_discovery_skipped",
                               "job_id": job_id, "media_path": media_relative_path,
                               "phase": "discovery", "filter_reason": "verified_english_subtitle_present"},
                    )
                elif client is not None and get_job(job_id)["status"] == "running":
                    phase = "discovery"
                    update_job_progress(job_id, phase=phase, current_file=media_relative_path)
                    try:
                        summary = stage_candidates(client, path, Path(settings.media_root).resolve(),
                                                   stat_result, details.get("metadata") or {})
                    except OpenSubtitlesError as exc:
                        logger.warning("opensubtitles_search_failed", extra={"event":"opensubtitles_search_failed","job_id":job_id,"media_path":media_relative_path,"phase":"discovery","error_type":type(exc).__name__})
                        summary = {"status":"provider_error","found":0,"staged":0,
                                   "candidates":[],"error":str(exc)}
                elif client is None:
                    logger.info(
                        "candidate_discovery_skipped",
                        extra={"event": "candidate_discovery_skipped",
                               "job_id": job_id, "media_path": media_relative_path,
                               "phase": "discovery", "filter_reason": "provider_not_configured"},
                    )
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
               "provider_count": len(paths), "status": f"{len(paths)} supported MKV files across {len(roots)} selected folder(s)"},
    )
    job = create_job("scan", total=len(paths), metadata={"scan_roots":[str(root.relative_to(media_root)) for root in roots]})
    background_tasks.add_task(_scan_worker, job["id"], media_root, paths)
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