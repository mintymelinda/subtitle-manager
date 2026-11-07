from pathlib import Path
import subprocess
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from app.config import settings
from app.database import initialize, record_scan
from app.media import inspect_media
from app.scanner import scan_media

app = FastAPI(title=settings.app_name)


@app.on_event("startup")
def startup():
    initialize()


def media_path_is_safe(path: Path, root: Path) -> bool:
    try:
        return path.resolve(strict=True).is_relative_to(root.resolve(strict=True))
    except (OSError, RuntimeError):
        return False


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
<div class="notice"><strong>Safety:</strong> scanning never changes media. Every patch requires explicit per-file approval.
Test mode is enabled by default and is limited to one approved file.</div>
<section class="panel"><h2>Media scan</h2><p id="root"></p>
<button id="scan">Start scan</button><p id="status">Ready.</p><pre id="results"></pre></section>
<script>
const scan=document.querySelector('#scan'),status=document.querySelector('#status'),results=document.querySelector('#results');
fetch('/api/config').then(r=>r.json()).then(c=>{document.querySelector('#root').textContent='Configured media root: '+c.media_root;});
scan.addEventListener('click',async()=>{scan.disabled=true;status.textContent='Scanning…';results.textContent='';
try{const r=await fetch('/api/scan',{method:'POST'});const data=await r.json();if(!r.ok)throw Error(data.detail||r.statusText);
status.textContent='Scan complete: '+data.files.length+' media files inspected.';results.textContent=JSON.stringify(data.files,null,2);
}catch(e){status.textContent='Scan failed: '+e.message;}finally{scan.disabled=false;}});
</script></body></html>""")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/config")
def config():
    return {"media_root": settings.media_root, "test_mode": settings.test_mode}


@app.post("/api/scan")
def scan():
    root = Path(settings.media_root)
    try:
        paths = list(scan_media(root))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    inspected = []
    for path in paths:
        if not media_path_is_safe(path, root):
            continue
        try:
            details = inspect_media(path)
            stat = path.stat()
            relative_path = str(path.relative_to(root.resolve()))
            record_scan(relative_path, stat.st_size, stat.st_mtime)
            inspected.append({
                "path": relative_path,
                "size": details["size"],
                "duration": details["duration"],
                "video_streams": details["video_streams"],
                "audio_streams": details["audio_streams"],
                "subtitle_streams": details["subtitle_streams"],
                "has_verified_english_subtitle": details["has_verified_english_subtitle"],
                "subtitle_content_assessment": details["subtitle_content_assessment"],
                "needs_review": not details["has_verified_english_subtitle"]
            })
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            inspected.append({"path": str(path.relative_to(root.resolve())), "error": str(exc)})
    return {"files": inspected, "count": len(inspected), "test_mode": settings.test_mode}
