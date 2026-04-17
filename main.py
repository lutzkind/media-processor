import asyncio
import mimetypes
import os
import secrets
import uuid
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import Cookie, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

app = FastAPI(title="Media Processor API")

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/data/uploads"))
BASE_URL = os.environ.get("BASE_URL", "https://media.luxeillum.com").rstrip("/")
API_KEY = os.environ.get("API_KEY", "")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff"}

for d in ["_named", "_thumb", "_composite"]:
    (UPLOAD_DIR / d).mkdir(parents=True, exist_ok=True)


# ── auth ─────────────────────────────────────────────────────────────────────

def require_api_key(x_api_key: str = Header(default="")):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401, "Invalid API key")


# ── upload ───────────────────────────────────────────────────────────────────

@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    folder: Optional[str] = Form(None),
    name: Optional[str] = Form(None),  # store as a named/pre-set asset (e.g. "faceintro", "video")
    _=Depends(require_api_key),
):
    """
    Upload a file. Returns {public_id, url, secure_url} — same shape as Cloudinary upload response.

    - `name`: if provided, store as a named asset (accessible by overlay_name / splice_name in /composite).
    - `folder`: if provided, store under that subfolder (e.g. "prospect").
    """
    ext = Path(file.filename or "file").suffix.lower() or ".bin"

    if name:
        save_dir = UPLOAD_DIR / "_named"
        # Remove any previous version of this named asset
        for old in save_dir.glob(f"{name}.*"):
            old.unlink(missing_ok=True)
        public_id = f"_named/{name}"
        file_path = save_dir / f"{name}{ext}"
    else:
        raw_id = str(uuid.uuid4())
        if folder:
            save_dir = UPLOAD_DIR / folder
            save_dir.mkdir(parents=True, exist_ok=True)
            public_id = f"{folder}/{raw_id}"
        else:
            save_dir = UPLOAD_DIR
            public_id = raw_id
        file_path = save_dir / f"{raw_id}{ext}"

    async with aiofiles.open(file_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            await f.write(chunk)

    url = f"{BASE_URL}/files/{public_id}{ext}"
    return {"public_id": public_id, "url": url, "secure_url": url, "format": ext.lstrip(".")}


# ── thumbnail ────────────────────────────────────────────────────────────────

@app.get("/thumb/{public_id:path}.jpg")
async def get_thumbnail(
    public_id: str,
    w: int = Query(1280),
    h: int = Query(720),
    c: str = Query("fill"),    # crop mode: fill | crop | scale
    g: str = Query("center"),  # gravity:  north | south | center | northeast | northwest
    t: float = Query(0.0),     # seek offset in seconds
):
    """Extract a JPEG thumbnail from a video or image file."""
    file_path = _find_file(public_id)
    if not file_path:
        raise HTTPException(404, f"Asset '{public_id}' not found")

    # Deterministic cache key
    cache_name = f"{public_id.replace('/', '_')}_{w}x{h}_{c}_{g}_{t:.1f}.jpg"
    thumb_path = UPLOAD_DIR / "_thumb" / cache_name

    if not thumb_path.exists():
        vf = _build_scale_filter(w, h, c, g)
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(t),
            "-i", str(file_path),
            "-frames:v", "1",
            "-vf", vf,
            "-q:v", "2",
            str(thumb_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            raise HTTPException(500, f"Thumbnail error: {stderr.decode()[-600:]}")

    return FileResponse(thumb_path, media_type="image/jpeg")


# ── composite ────────────────────────────────────────────────────────────────

class CompositeRequest(BaseModel):
    base_id: str                  # public_id of the screenshot / base video
    overlay_name: str = "faceintro"  # named asset for circular face overlay
    splice_name: str = "video"       # named asset concatenated after the intro
    overlay_w: int = 206
    overlay_h: int = 206
    overlay_x: int = 62           # px offset from SE edge
    overlay_y: int = 36
    output_w: int = 1280
    output_h: int = 720


@app.post("/composite")
async def create_composite(req: CompositeRequest, _=Depends(require_api_key)):
    """
    Build a two-part composite video:
      Part 1 — base image or base video with the overlay video displayed
                as a circular picture-in-picture (SE corner).
      Part 2 — splice video appended with no overlay.
    """
    base_path = _find_file(req.base_id)
    overlay_path = _find_file_named(req.overlay_name)
    splice_path = _find_file_named(req.splice_name)

    if not base_path:
        raise HTTPException(404, f"Base asset '{req.base_id}' not found")
    if not overlay_path:
        raise HTTPException(404, f"Named asset '{req.overlay_name}' not found — upload it via POST /upload with name={req.overlay_name}")
    if not splice_path:
        raise HTTPException(404, f"Named asset '{req.splice_name}' not found — upload it via POST /upload with name={req.splice_name}")

    out_id = str(uuid.uuid4())
    tmp_part1 = UPLOAD_DIR / "_composite" / f"{out_id}_p1.mp4"
    out_path = UPLOAD_DIR / "_composite" / f"{out_id}.mp4"

    ow, oh = req.overlay_w, req.overlay_h
    cx, cy, r = ow // 2, oh // 2, ow // 2
    W, H = req.output_w, req.output_h
    # Overlay position: inset from SE corner
    pos_x = f"W-{ow}-{req.overlay_x}"
    pos_y = f"H-{oh}-{req.overlay_y}"

    base_is_image = base_path.suffix.lower() in IMAGE_EXTENSIONS

    # ── Part 1: base media + circular overlay ──────────────────────────────
    # Commas inside geq expressions must be escaped as \, for FFmpeg's filter parser
    filter_p1 = (
        f"[0:v]scale={W}:{H},setsar=1,setpts=PTS-STARTPTS[bg];"
        f"[1:v]crop='min(iw,ih)':'min(iw,ih)':'(iw-min(iw,ih))/2':'(ih-min(iw,ih))/2',"
        f"scale={ow}:{oh},setpts=PTS-STARTPTS,format=rgba,"
        f"geq="
        f"r='r(X\\,Y)':"
        f"g='g(X\\,Y)':"
        f"b='b(X\\,Y)':"
        f"a='255*lte(sqrt(pow(X-{cx}\\,2)+pow(Y-{cy}\\,2))\\,{r})'[circle];"
        f"[bg][circle]overlay={pos_x}:{pos_y}:eof_action=pass[out]"
    )

    cmd1 = ["ffmpeg", "-y"]
    if base_is_image:
        cmd1.extend(["-loop", "1"])
    cmd1.extend([
        "-i", str(base_path),
        "-i", str(overlay_path),
        "-filter_complex", filter_p1,
        "-map", "[out]",
        "-map", "1:a?",
    ])
    # Stop when the composed video stream ends.
    cmd1.append("-shortest")
    cmd1.extend([
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(tmp_part1),
    ])

    proc1 = await asyncio.create_subprocess_exec(
        *cmd1, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr1 = await asyncio.wait_for(proc1.communicate(), timeout=300)
    if proc1.returncode != 0:
        raise HTTPException(500, f"Part 1 error: {stderr1.decode()[-600:]}")

    # ── Part 2: concatenate part1 + splice video ───────────────────────────
    filter_concat = (
        f"[0:v]setpts=PTS-STARTPTS[v0];"
        f"[1:v]scale={W}:{H},setsar=1,setpts=PTS-STARTPTS[v1];"
        f"[v0][0:a][v1][1:a]concat=n=2:v=1:a=1[v][a]"
    )

    cmd2 = [
        "ffmpeg", "-y",
        "-i", str(tmp_part1),
        "-i", str(splice_path),
        "-filter_complex", filter_concat,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out_path),
    ]

    proc2 = await asyncio.create_subprocess_exec(
        *cmd2, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr2 = await asyncio.wait_for(proc2.communicate(), timeout=300)
    tmp_part1.unlink(missing_ok=True)
    if proc2.returncode != 0:
        raise HTTPException(500, f"Concat error: {stderr2.decode()[-600:]}")

    url = f"{BASE_URL}/files/_composite/{out_id}.mp4"
    return {"public_id": f"_composite/{out_id}", "url": url, "secure_url": url}


# ── delete ───────────────────────────────────────────────────────────────────

@app.delete("/asset/{public_id:path}")
async def delete_asset(public_id: str, _=Depends(require_api_key)):
    file_path = _find_file(public_id)
    if not file_path:
        raise HTTPException(404, "Asset not found")
    file_path.unlink(missing_ok=True)
    # Evict thumbnail cache entries for this asset
    prefix = public_id.replace("/", "_")
    for cached in (UPLOAD_DIR / "_thumb").glob(f"{prefix}_*"):
        cached.unlink(missing_ok=True)
    return {"result": "ok", "public_id": public_id}


# ── serve ────────────────────────────────────────────────────────────────────

@app.get("/files/{file_path:path}")
async def serve_file(file_path: str):
    full_path = UPLOAD_DIR / file_path
    if not full_path.exists() or not full_path.is_file():
        raise HTTPException(404, "File not found")
    mime, _ = mimetypes.guess_type(str(full_path))
    return FileResponse(full_path, media_type=mime or "application/octet-stream")


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── session store (in-memory, single-node) ───────────────────────────────────
# Maps session token → True. Cleared on restart; fine for a single-user tool.
_sessions: set[str] = set()
DASHBOARD_USERNAME = os.environ.get("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", API_KEY or "changeme")


def _is_authenticated(session: Optional[str] = Cookie(default=None)) -> bool:
    return session is not None and session in _sessions


# ── login ─────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(error: str = ""):
    err_html = f'<p class="error">{error}</p>' if error else ""
    return HTMLResponse(_LOGIN_HTML.replace("<!-- ERROR -->", err_html))


@app.post("/login")
async def login_submit(username: str = Form(...), password: str = Form(...)):
    if username == DASHBOARD_USERNAME and password == DASHBOARD_PASSWORD:
        token = secrets.token_hex(32)
        _sessions.add(token)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("session", token, httponly=True, samesite="lax", max_age=86400 * 30)
        return resp
    return RedirectResponse("/login?error=Invalid+credentials", status_code=303)


@app.post("/logout")
async def logout(session: Optional[str] = Cookie(default=None)):
    if session:
        _sessions.discard(session)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session")
    return resp


# ── dashboard ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(authenticated: bool = Depends(_is_authenticated)):
    if not authenticated:
        return RedirectResponse("/login", status_code=303)

    def fmt_size(b: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if b < 1024:
                return f"{b:.1f} {unit}"
            b /= 1024  # type: ignore[assignment]
        return f"{b:.1f} TB"

    def dir_size(path: Path) -> int:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())

    # ── named assets ─────────────────────────────────────────────────────────
    named_dir = UPLOAD_DIR / "_named"
    named_assets = []
    for f in (sorted(named_dir.iterdir(), key=lambda x: x.name) if named_dir.exists() else []):
        if f.is_file():
            stat = f.stat()
            named_assets.append({"name": f.stem, "ext": f.suffix, "size": fmt_size(stat.st_size)})

    expected = {"faceintro", "video"}
    found_names = {a["name"] for a in named_assets}
    missing = expected - found_names

    # ── all uploads, track folders ────────────────────────────────────────────
    all_files = []
    folder_counts: dict = {}  # folder_key → file count
    for root, dirs, files_in in os.walk(UPLOAD_DIR):
        dirs[:] = [d for d in dirs if not d.startswith("_")]
        rel_root = Path(root).relative_to(UPLOAD_DIR)
        folder_key = "" if str(rel_root) == "." else str(rel_root)
        fc = 0
        for fn in files_in:
            fp = Path(root) / fn
            stat = fp.stat()
            rel = fp.relative_to(UPLOAD_DIR)
            ext = fp.suffix.lower()
            is_video = ext in (".mp4", ".webm", ".mov", ".avi", ".mkv")
            is_image = ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")
            pid = str(rel.with_suffix(""))   # public_id without extension
            url = f"{BASE_URL}/files/{rel}"
            all_files.append({
                "path": str(rel), "name": fn, "folder": folder_key,
                "size": fmt_size(stat.st_size), "mtime": stat.st_mtime,
                "url": url, "pid": pid, "ext": ext.lstrip("."),
                "is_video": is_video, "is_image": is_image,
            })
            fc += 1
        folder_counts[folder_key] = folder_counts.get(folder_key, 0) + fc
    all_files.sort(key=lambda x: x["mtime"], reverse=True)

    # ── composites ───────────────────────────────────────────────────────────
    comp_dir = UPLOAD_DIR / "_composite"
    composites = []
    if comp_dir.exists():
        for f in sorted(comp_dir.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True)[:48]:
            stat = f.stat()
            url = f"{BASE_URL}/files/_composite/{f.name}"
            pid = f"_composite/{f.stem}"
            composites.append({"name": f.name, "size": fmt_size(stat.st_size), "url": url, "pid": pid})

    total_bytes = dir_size(UPLOAD_DIR) if UPLOAD_DIR.exists() else 0

    # ── build folder sidebar ──────────────────────────────────────────────────
    folder_nav = '<a class="nav-item active" href="#" onclick="filterFolder(\'\',this)"><span class="ni">&#128248;</span>All Files<span class="folder-count">' + str(len(all_files)) + '</span></a>'
    for fk in sorted(folder_counts):
        if fk == "":
            continue
        label = fk
        cnt = folder_counts[fk]
        escaped = fk.replace("'", "\\'")
        folder_nav += f'<a class="nav-item" href="#" onclick="filterFolder(\'{escaped}\',this)"><span class="ni">&#128193;</span>{label}<span class="folder-count">{cnt}</span></a>'

    # ── folder options for upload modal ───────────────────────────────────────
    folder_opts = '<option value="">Root</option>'
    for fk in sorted(folder_counts):
        if fk:
            folder_opts += f'<option value="{fk}">{fk}</option>'
    folder_opts += '<option value="__new__">+ New folder…</option>'

    # ── named asset rows ──────────────────────────────────────────────────────
    named_rows = ""
    for name in sorted(missing):
        named_rows += f"""<div class="asset-row missing-asset">
          <div class="asset-icon">&#128249;</div>
          <div class="asset-meta"><span class="asset-name">{name}</span><span class="asset-tag missing-tag">Missing</span></div>
          <form method="post" action="/ui/upload-named" enctype="multipart/form-data" class="asset-form">
            <input type="hidden" name="name" value="{name}">
            <label class="file-label">Choose file<input type="file" name="file" required></label>
            <button type="submit" class="btn-primary btn-sm">Upload</button>
          </form></div>"""
    for a in named_assets:
        named_rows += f"""<div class="asset-row">
          <div class="asset-icon">&#127910;</div>
          <div class="asset-meta"><span class="asset-name">{a['name']}{a['ext']}</span><span class="asset-tag ok-tag">Ready</span><span class="asset-size">{a['size']}</span></div>
          <form method="post" action="/ui/upload-named" enctype="multipart/form-data" class="asset-form">
            <input type="hidden" name="name" value="{a['name']}">
            <label class="file-label">Replace<input type="file" name="file" required></label>
            <button type="submit" class="btn-outline btn-sm">Replace</button>
          </form></div>"""

    # ── media cards ───────────────────────────────────────────────────────────
    def media_card(f: dict) -> str:
        badge = f['ext'].upper() if f['ext'] else "FILE"
        if f["is_video"]:
            preview = f'<video src="{f["url"]}" class="thumb-media" muted preload="metadata"></video>'
        elif f["is_image"]:
            preview = f'<img src="{f["url"]}" class="thumb-media" loading="lazy" alt="">'
        else:
            preview = '<div class="thumb-file">&#128196;</div>'
        short = (f["name"][:24] + "…") if len(f["name"]) > 24 else f["name"]
        url_esc = f["url"].replace('"', '&quot;')
        pid_esc = f["pid"].replace('"', '&quot;').replace("'", "\\'")
        url_js  = f["url"].replace("'", "\\'")
        return (
            f'<div class="media-card" data-folder="{f["folder"]}" data-name="{f["name"].lower()}">'
            f'<div class="thumb-wrap">'
            f'{preview}'
            f'<span class="media-badge">{badge}</span>'
            f'<div class="card-overlay">'
            f'<a href="{url_esc}" target="_blank" class="ov-btn ov-open" title="Open">&#10697;</a>'
            f'<button class="ov-btn ov-url" onclick="cp(\'{url_js}\',this)" title="Copy URL">URL</button>'
            f'<button class="ov-btn ov-id"  onclick="cp(\'{pid_esc}\',this)" title="Copy public_id">ID</button>'
            f'<button class="ov-btn ov-del" onclick="del(\'{pid_esc}\',this)" title="Delete">&#128465;</button>'
            f'</div></div>'
            f'<div class="media-info"><span class="media-name" title="{f["name"]}">{short}</span>'
            f'<span class="media-meta">{f["folder"] or "root"} &middot; {f["size"]}</span></div></div>'
        )

    media_cards = "".join(media_card(f) for f in all_files) or '<p class="empty-state">No uploads yet</p>'

    def comp_card(c: dict) -> str:
        short = (c["name"][:24] + "…") if len(c["name"]) > 24 else c["name"]
        url_js = c["url"].replace("'", "\\'")
        pid_esc = c["pid"].replace("'", "\\'")
        return (
            f'<div class="media-card" data-folder="_composite" data-name="{c["name"].lower()}">'
            f'<div class="thumb-wrap">'
            f'<video src="{c["url"]}" class="thumb-media" muted preload="metadata"></video>'
            f'<span class="media-badge">composite</span>'
            f'<div class="card-overlay">'
            f'<a href="{c["url"]}" target="_blank" class="ov-btn ov-open" title="Open">&#10697;</a>'
            f'<button class="ov-btn ov-url" onclick="cp(\'{url_js}\',this)" title="Copy URL">URL</button>'
            f'<button class="ov-btn ov-id"  onclick="cp(\'{pid_esc}\',this)" title="Copy public_id">ID</button>'
            f'<button class="ov-btn ov-del" onclick="del(\'{pid_esc}\',this)" title="Delete">&#128465;</button>'
            f'</div></div>'
            f'<div class="media-info"><span class="media-name" title="{c["name"]}">{short}</span>'
            f'<span class="media-meta">composite &middot; {c["size"]}</span></div></div>'
        )

    comp_cards = "".join(comp_card(c) for c in composites) or '<p class="empty-state">No composites yet</p>'

    return HTMLResponse(
        _DASHBOARD_HTML
        .replace("%%NAMED_ROWS%%", named_rows)
        .replace("%%MEDIA_CARDS%%", media_cards)
        .replace("%%COMP_CARDS%%", comp_cards)
        .replace("%%TOTAL%%", fmt_size(total_bytes))
        .replace("%%UPLOADS%%", str(len(all_files)))
        .replace("%%COMPS%%", str(len(composites)))
        .replace("%%FOLDER_NAV%%", folder_nav)
        .replace("%%FOLDER_OPTS%%", folder_opts)
    )


# ── UI helpers (session-auth) ─────────────────────────────────────────────────

@app.post("/ui/upload-named")
async def ui_upload_named(
    file: UploadFile = File(...),
    name: str = Form(...),
    authenticated: bool = Depends(_is_authenticated),
):
    if not authenticated:
        raise HTTPException(401)
    ext = Path(file.filename or "file").suffix.lower() or ".bin"
    save_dir = UPLOAD_DIR / "_named"
    for old in save_dir.glob(f"{name}.*"):
        old.unlink(missing_ok=True)
    file_path = save_dir / f"{name}{ext}"
    async with aiofiles.open(file_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            await f.write(chunk)
    return RedirectResponse("/", status_code=303)


@app.post("/ui/upload-file")
async def ui_upload_file(
    file: UploadFile = File(...),
    folder: str = Form(""),
    authenticated: bool = Depends(_is_authenticated),
):
    if not authenticated:
        raise HTTPException(401)
    ext = Path(file.filename or "file").suffix.lower() or ".bin"
    raw_id = str(uuid.uuid4())
    clean_folder = folder.strip().strip("/")
    if clean_folder:
        save_dir = UPLOAD_DIR / clean_folder
        save_dir.mkdir(parents=True, exist_ok=True)
        public_id = f"{clean_folder}/{raw_id}"
    else:
        save_dir = UPLOAD_DIR
        public_id = raw_id
    file_path = save_dir / f"{raw_id}{ext}"
    async with aiofiles.open(file_path, "wb") as fh:
        while chunk := await file.read(1024 * 1024):
            await fh.write(chunk)
    url = f"{BASE_URL}/files/{public_id}{ext}"
    return {"public_id": public_id, "url": url, "secure_url": url}


@app.post("/ui/delete-file")
async def ui_delete_file(
    request: Request,
    authenticated: bool = Depends(_is_authenticated),
):
    if not authenticated:
        raise HTTPException(401)
    body = await request.json()
    public_id = body.get("public_id", "")
    fp = _find_file(public_id)
    if not fp or not fp.is_file():
        raise HTTPException(404, "File not found")
    fp.unlink()
    return {"deleted": public_id}


# ── HTML templates ─────────────────────────────────────────────────────────────

_LOGIN_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Media Processor — Sign in</title>
  <style>
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
    body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         background:#f0f4f8;min-height:100vh;display:flex;flex-direction:column;
         align-items:center;justify-content:center}
    .logo-row{display:flex;align-items:center;gap:10px;margin-bottom:28px}
    .logo-icon{width:40px;height:40px;background:linear-gradient(135deg,#3448c5,#5b6df8);
               border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:20px}
    .logo-text{font-size:22px;font-weight:700;color:#1a202c}
    .card{background:#fff;border-radius:12px;
          box-shadow:0 4px 24px rgba(0,0,0,.10),0 1px 4px rgba(0,0,0,.06);
          padding:40px 44px 36px;width:420px}
    .card h2{font-size:22px;font-weight:700;color:#1a202c;text-align:center;margin-bottom:28px}
    label{display:block;font-size:13px;font-weight:600;color:#4a5568;margin-bottom:6px;margin-top:18px}
    input[type=text],input[type=password]{width:100%;padding:10px 14px;border:1.5px solid #cbd5e0;
                         border-radius:7px;font-size:15px;color:#1a202c;outline:none;transition:border-color .15s}
    input[type=text]:focus,input[type=password]:focus{border-color:#3448c5;box-shadow:0 0 0 3px rgba(52,72,197,.12)}
    button[type=submit]{margin-top:24px;width:100%;background:#3448c5;color:#fff;border:none;
                        border-radius:7px;padding:12px;font-size:14px;font-weight:700;
                        letter-spacing:.04em;cursor:pointer;text-transform:uppercase;transition:background .15s}
    button[type=submit]:hover{background:#2a3aaa}
    .error{background:#fff5f5;border:1px solid #feb2b2;color:#c53030;border-radius:7px;
           padding:10px 14px;font-size:13px;text-align:center;margin-bottom:8px}
    .hint{text-align:center;margin-top:18px;font-size:12px;color:#a0aec0}
  </style>
</head>
<body>
  <div class="logo-row">
    <div class="logo-icon">&#127916;</div>
    <span class="logo-text">Media Processor</span>
  </div>
  <div class="card">
    <h2>Sign in to your account</h2>
    <!-- ERROR -->
    <form method="post" action="/login">
      <label for="un">Username</label>
      <input type="text" id="un" name="username" autofocus placeholder="Enter your username" autocomplete="username">
      <label for="pw">Password</label>
      <input type="password" id="pw" name="password" placeholder="Enter your password" autocomplete="current-password">
      <button type="submit">Sign In</button>
    </form>
    <p class="hint">Self-hosted media service</p>
  </div>
</body>
</html>"""

_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Media Processor</title>
  <style>
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
    body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         background:#f7f8fc;color:#2d3748;display:flex;min-height:100vh}

    /* ── Sidebar ── */
    .sidebar{width:230px;min-height:100vh;background:#1c2035;display:flex;
             flex-direction:column;flex-shrink:0;position:sticky;top:0;height:100vh;overflow-y:auto}
    .sidebar-logo{padding:20px 18px 16px;display:flex;align-items:center;gap:10px;
                  border-bottom:1px solid #2d3254}
    .sidebar-logo .icon{width:32px;height:32px;background:linear-gradient(135deg,#3448c5,#5b6df8);
                        border-radius:8px;display:flex;align-items:center;justify-content:center;
                        font-size:16px;flex-shrink:0}
    .sidebar-logo span{font-size:14px;font-weight:700;color:#fff;line-height:1.2}
    .sidebar-logo small{display:block;font-size:10px;color:#8892b0;font-weight:400}
    nav{flex:1;padding:10px 0}
    .nav-section{padding:12px 18px 4px;font-size:10px;font-weight:700;color:#4a5568;
                 text-transform:uppercase;letter-spacing:.08em}
    .nav-item{display:flex;align-items:center;gap:8px;padding:8px 18px;color:#a0aec0;
              font-size:13px;font-weight:500;cursor:pointer;text-decoration:none;
              border-left:3px solid transparent;transition:all .12s;white-space:nowrap;overflow:hidden}
    .nav-item:hover{color:#fff;background:rgba(255,255,255,.05)}
    .nav-item.active{color:#fff;background:rgba(84,100,255,.18);border-left-color:#5464ff}
    .nav-item .ni{font-size:14px;width:18px;text-align:center;flex-shrink:0}
    .folder-count{margin-left:auto;background:rgba(255,255,255,.1);border-radius:10px;
                  padding:1px 7px;font-size:10px;font-weight:700;flex-shrink:0}
    .sidebar-footer{padding:14px 18px;border-top:1px solid #2d3254}
    .sidebar-footer form button{width:100%;background:transparent;border:1px solid #3d4470;
                                color:#8892b0;border-radius:6px;padding:7px;font-size:12px;
                                cursor:pointer;transition:all .12s}
    .sidebar-footer form button:hover{background:#2d3254;color:#fff}

    /* ── Main ── */
    .main{flex:1;display:flex;flex-direction:column;min-width:0}
    .topbar{background:#fff;border-bottom:1px solid #e2e8f0;padding:0 24px;height:56px;
            display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:10}
    .topbar h1{font-size:16px;font-weight:700;color:#1a202c;margin-right:auto}
    .search-wrap{position:relative}
    .search-wrap input{padding:7px 10px 7px 32px;border:1.5px solid #e2e8f0;border-radius:7px;
                       font-size:13px;color:#2d3748;outline:none;width:220px;transition:border-color .15s}
    .search-wrap input:focus{border-color:#3448c5}
    .search-wrap::before{content:"\\1F50D";position:absolute;left:9px;top:50%;transform:translateY(-50%);
                          font-size:13px;pointer-events:none;color:#a0aec0}
    .btn-upload{background:#3448c5;color:#fff;border:none;border-radius:7px;padding:8px 16px;
                font-size:13px;font-weight:700;cursor:pointer;white-space:nowrap;transition:background .12s}
    .btn-upload:hover{background:#2a3aaa}
    .api-chip{background:#f7f8fc;border:1px solid #e2e8f0;border-radius:20px;padding:4px 12px;
              font-size:12px;color:#718096}
    .api-chip a{color:#3448c5;text-decoration:none;font-weight:600}

    /* ── Content ── */
    .content{padding:24px;flex:1}

    /* ── Stats ── */
    .stats-row{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:24px}
    .stat-card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;
               padding:18px 20px;display:flex;align-items:center;gap:14px}
    .stat-icon{width:42px;height:42px;border-radius:10px;display:flex;align-items:center;
               justify-content:center;font-size:18px;flex-shrink:0}
    .stat-icon.blue{background:#ebf0ff}
    .stat-icon.green{background:#e6ffed}
    .stat-icon.purple{background:#f3e8ff}
    .stat-label{font-size:11px;color:#718096;font-weight:500;margin-bottom:2px}
    .stat-value{font-size:20px;font-weight:700;color:#1a202c}

    /* ── Named assets ── */
    .asset-row{background:#fff;border:1px solid #e2e8f0;border-radius:10px;
               padding:12px 16px;margin-bottom:8px;display:flex;align-items:center;gap:12px}
    .asset-row.missing-asset{border-color:#fed7d7;background:#fffafa}
    .asset-icon{font-size:20px;width:28px;text-align:center;flex-shrink:0}
    .asset-meta{flex:1;display:flex;align-items:center;gap:8px;min-width:0;flex-wrap:wrap}
    .asset-name{font-size:13px;font-weight:600;color:#1a202c}
    .asset-size{font-size:11px;color:#a0aec0}
    .asset-tag{padding:2px 8px;border-radius:999px;font-size:10px;font-weight:700}
    .ok-tag{background:#e6ffed;color:#276749}
    .missing-tag{background:#fff5f5;color:#c53030}
    .asset-form{display:flex;align-items:center;gap:8px;flex-shrink:0;flex-wrap:wrap}
    .file-label{font-size:12px;color:#718096;cursor:pointer;background:#f7f8fc;
                border:1px solid #e2e8f0;border-radius:6px;padding:5px 10px;white-space:nowrap}
    .file-label input[type=file]{display:none}
    .btn-primary{background:#3448c5;color:#fff;border:none;border-radius:7px;
                 padding:7px 14px;font-size:12px;font-weight:700;cursor:pointer;
                 white-space:nowrap;transition:background .12s}
    .btn-primary:hover{background:#2a3aaa}
    .btn-outline{background:transparent;color:#3448c5;border:1.5px solid #3448c5;border-radius:7px;
                 padding:6px 14px;font-size:12px;font-weight:700;cursor:pointer;
                 white-space:nowrap;transition:all .12s}
    .btn-outline:hover{background:#ebf0ff}

    /* ── Media grid ── */
    .media-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px}
    .media-card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden;
                transition:box-shadow .12s,transform .12s;position:relative}
    .media-card:hover{box-shadow:0 4px 18px rgba(0,0,0,.10);transform:translateY(-1px)}
    .media-card.hidden{display:none}
    .thumb-wrap{width:100%;aspect-ratio:16/9;background:#f0f4f8;position:relative;overflow:hidden;
                display:flex;align-items:center;justify-content:center}
    .thumb-media{width:100%;height:100%;object-fit:cover;display:block}
    .thumb-file{color:#a0aec0;font-size:30px}
    .media-badge{position:absolute;bottom:5px;left:6px;background:rgba(0,0,0,.55);color:#fff;
                 font-size:9px;font-weight:700;padding:2px 5px;border-radius:4px;text-transform:uppercase}
    .card-overlay{position:absolute;inset:0;background:rgba(20,25,50,.72);
                  display:flex;align-items:center;justify-content:center;gap:6px;
                  opacity:0;transition:opacity .15s;flex-wrap:wrap;padding:6px}
    .thumb-wrap:hover .card-overlay{opacity:1}
    .ov-btn{background:rgba(255,255,255,.18);color:#fff;border:1px solid rgba(255,255,255,.3);
            border-radius:6px;padding:5px 9px;font-size:11px;font-weight:700;cursor:pointer;
            text-decoration:none;transition:background .1s;white-space:nowrap}
    .ov-btn:hover{background:rgba(255,255,255,.32)}
    .ov-del{background:rgba(220,50,50,.4);border-color:rgba(220,50,50,.6)}
    .ov-del:hover{background:rgba(220,50,50,.7)}
    .media-info{padding:7px 9px}
    .media-name{display:block;font-size:11px;color:#4a5568;font-weight:600;
                overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .media-meta{display:block;font-size:10px;color:#a0aec0;margin-top:1px}
    .empty-state{color:#a0aec0;font-size:14px;padding:32px 0;text-align:center;grid-column:1/-1}
    a{text-decoration:none;color:inherit}

    /* ── Section ── */
    .section{margin-bottom:28px}
    .section-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
    .section-title{font-size:14px;font-weight:700;color:#1a202c}
    .section-sub{font-size:12px;color:#a0aec0;margin-left:8px;font-weight:400}
    .tab-panel{display:none}
    .tab-panel.active{display:block}

    /* ── Upload modal ── */
    .modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:100;
              align-items:center;justify-content:center}
    .modal-bg.open{display:flex}
    .modal{background:#fff;border-radius:14px;padding:28px 32px;width:420px;max-width:95vw;
           box-shadow:0 20px 60px rgba(0,0,0,.25)}
    .modal h2{font-size:17px;font-weight:700;color:#1a202c;margin-bottom:20px}
    .drop-zone{border:2px dashed #cbd5e0;border-radius:10px;padding:32px;text-align:center;
               cursor:pointer;transition:border-color .15s,background .15s;margin-bottom:16px}
    .drop-zone.drag-over{border-color:#3448c5;background:#f0f4ff}
    .drop-zone p{font-size:13px;color:#718096;margin-top:6px}
    .drop-zone .dz-icon{font-size:32px}
    .modal label{display:block;font-size:12px;font-weight:600;color:#4a5568;margin-bottom:5px;margin-top:14px}
    .modal select,.modal input[type=text]{width:100%;padding:8px 10px;border:1.5px solid #cbd5e0;
                                          border-radius:7px;font-size:13px;outline:none;
                                          transition:border-color .15s}
    .modal select:focus,.modal input[type=text]:focus{border-color:#3448c5}
    .modal-footer{display:flex;gap:10px;margin-top:20px;justify-content:flex-end}
    .btn-cancel{background:transparent;border:1.5px solid #e2e8f0;color:#718096;border-radius:7px;
                padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer}
    .btn-cancel:hover{background:#f7f8fc}
    .progress-bar-wrap{height:6px;background:#e2e8f0;border-radius:3px;margin-top:12px;overflow:hidden;display:none}
    .progress-bar{height:100%;background:#3448c5;border-radius:3px;width:0%;transition:width .2s}
    #up-file-list{font-size:12px;color:#718096;margin-top:6px;min-height:18px}

    /* ── Toast ── */
    .toast{position:fixed;bottom:24px;right:24px;background:#1c2035;color:#fff;border-radius:8px;
           padding:10px 18px;font-size:13px;font-weight:600;z-index:200;
           opacity:0;transform:translateY(8px);transition:all .2s;pointer-events:none}
    .toast.show{opacity:1;transform:translateY(0)}
    .toast.err{background:#c53030}
  </style>
</head>
<body>

<!-- ── Sidebar ── -->
<aside class="sidebar">
  <div class="sidebar-logo">
    <div class="icon">&#127916;</div>
    <div><span>Media Processor</span><small>media.luxeillum.com</small></div>
  </div>
  <nav>
    <div class="nav-section">Media Library</div>
    %%FOLDER_NAV%%
    <div class="nav-section" style="margin-top:6px">Sections</div>
    <a class="nav-item" href="#" onclick="showSection('composites',this)">
      <span class="ni">&#127910;</span>Composites<span class="folder-count">%%COMPS%%</span>
    </a>
    <a class="nav-item" href="#" onclick="showSection('named',this)">
      <span class="ni">&#128279;</span>Named Assets
    </a>
    <div class="nav-section" style="margin-top:6px">Developer</div>
    <a class="nav-item" href="/docs" target="_blank">
      <span class="ni">&#128196;</span>API Docs
    </a>
  </nav>
  <div class="sidebar-footer">
    <form method="post" action="/logout"><button type="submit">Sign out</button></form>
  </div>
</aside>

<!-- ── Main ── -->
<div class="main">
  <div class="topbar">
    <h1 id="page-title">Media Library</h1>
    <div class="search-wrap">
      <input type="text" id="search" placeholder="Search files…" oninput="filterSearch(this.value)">
    </div>
    <button class="btn-upload" onclick="openUpload()">&#8679; Upload</button>
    <span class="api-chip">&#128190; %%TOTAL%% &nbsp;|&nbsp; <a href="/docs" target="_blank">API</a></span>
  </div>

  <div class="content">
    <div class="stats-row">
      <div class="stat-card">
        <div class="stat-icon blue">&#128190;</div>
        <div><div class="stat-label">Total Storage</div><div class="stat-value">%%TOTAL%%</div></div>
      </div>
      <div class="stat-card">
        <div class="stat-icon green">&#128247;</div>
        <div><div class="stat-label">Uploaded Files</div><div class="stat-value">%%UPLOADS%%</div></div>
      </div>
      <div class="stat-card">
        <div class="stat-icon purple">&#127910;</div>
        <div><div class="stat-label">Composites</div><div class="stat-value">%%COMPS%%</div></div>
      </div>
    </div>

    <!-- Media Library -->
    <div id="tab-uploads" class="tab-panel active section">
      <div class="section-header">
        <span class="section-title">All Files <span class="section-sub" id="file-count">%%UPLOADS%% files</span></span>
      </div>
      <div class="media-grid" id="media-grid">%%MEDIA_CARDS%%</div>
    </div>

    <!-- Composites -->
    <div id="tab-composites" class="tab-panel section">
      <div class="section-header">
        <span class="section-title">Composites <span class="section-sub">%%COMPS%% videos</span></span>
      </div>
      <div class="media-grid">%%COMP_CARDS%%</div>
    </div>

    <!-- Named Assets -->
    <div id="tab-named" class="tab-panel section">
      <div class="section-header">
        <span class="section-title">Named Assets
          <span class="section-sub">Pre-loaded assets for the composite operation</span>
        </span>
      </div>
      %%NAMED_ROWS%%
    </div>
  </div>
</div>

<!-- ── Upload Modal ── -->
<div class="modal-bg" id="upload-modal" onclick="maybeClose(event)">
  <div class="modal">
    <h2>&#8679; Upload Files</h2>
    <div class="drop-zone" id="drop-zone" onclick="document.getElementById('file-input').click()">
      <div class="dz-icon">&#128190;</div>
      <strong>Click or drag &amp; drop files here</strong>
      <p>Any file type &middot; Multiple files supported</p>
    </div>
    <input type="file" id="file-input" multiple style="display:none" onchange="queueFiles(this.files)">
    <div id="up-file-list"></div>
    <label for="folder-select">Folder</label>
    <select id="folder-select" onchange="checkNewFolder(this)">%%FOLDER_OPTS%%</select>
    <input type="text" id="new-folder-input" placeholder="e.g. prospect" style="display:none;margin-top:8px">
    <div class="progress-bar-wrap" id="prog-wrap"><div class="progress-bar" id="prog-bar"></div></div>
    <div class="modal-footer">
      <button class="btn-cancel" onclick="closeUpload()">Cancel</button>
      <button class="btn-upload" id="do-upload" onclick="startUpload()">Upload</button>
    </div>
  </div>
</div>

<!-- ── Toast ── -->
<div class="toast" id="toast"></div>

<script>
// ── Tab / section switching ───────────────────────────────────────────────────
let currentFolder = '';

function showSection(name, el) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  if (el) el.classList.add('active');
  const titles = {uploads:'Media Library', composites:'Composites', named:'Named Assets'};
  document.getElementById('page-title').textContent = titles[name] || 'Media Processor';
  return false;
}

function filterFolder(folder, el) {
  currentFolder = folder;
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.getElementById('tab-uploads').classList.add('active');
  if (el) el.classList.add('active');
  document.getElementById('page-title').textContent = folder ? folder + '/' : 'All Files';
  applyFilters();
  return false;
}

function applyFilters() {
  const q = document.getElementById('search').value.toLowerCase();
  let vis = 0;
  document.querySelectorAll('#media-grid .media-card').forEach(c => {
    const matchF = !currentFolder || c.dataset.folder === currentFolder;
    const matchQ = !q || c.dataset.name.includes(q);
    const hide = !(matchF && matchQ);
    c.classList.toggle('hidden', hide);
    if (!hide) vis++;
  });
  const sub = document.getElementById('file-count');
  if (sub) sub.textContent = vis + ' file' + (vis !== 1 ? 's' : '');
}

function filterSearch(q) { applyFilters(); }

// ── Copy / delete ─────────────────────────────────────────────────────────────
function cp(text, btn) {
  navigator.clipboard.writeText(text).then(() => toast('Copied!'));
}

function del(pid, btn) {
  if (!confirm('Delete "' + pid + '"?')) return;
  fetch('/ui/delete-file', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({public_id: pid})
  }).then(r => {
    if (r.ok) {
      const card = btn.closest('.media-card');
      card.style.transition = 'opacity .2s';
      card.style.opacity = '0';
      setTimeout(() => { card.remove(); applyFilters(); }, 200);
      toast('Deleted');
    } else {
      r.json().then(d => toast(d.detail || 'Delete failed', true));
    }
  }).catch(() => toast('Delete failed', true));
}

// ── Upload modal ──────────────────────────────────────────────────────────────
let uploadQueue = [];

function openUpload() {
  document.getElementById('upload-modal').classList.add('open');
  uploadQueue = [];
  document.getElementById('up-file-list').textContent = '';
  document.getElementById('prog-wrap').style.display = 'none';
  document.getElementById('prog-bar').style.width = '0%';
}
function closeUpload() { document.getElementById('upload-modal').classList.remove('open'); }
function maybeClose(e) { if (e.target.id === 'upload-modal') closeUpload(); }

function checkNewFolder(sel) {
  document.getElementById('new-folder-input').style.display =
    sel.value === '__new__' ? 'block' : 'none';
}

function queueFiles(files) {
  uploadQueue = Array.from(files);
  document.getElementById('up-file-list').textContent =
    uploadQueue.map(f => f.name).join(', ').substring(0, 120) +
    (uploadQueue.length > 3 ? ` (+${uploadQueue.length - 3} more)` : '');
}

// drag & drop
const dz = document.getElementById('drop-zone');
dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag-over'); });
dz.addEventListener('dragleave', () => dz.classList.remove('drag-over'));
dz.addEventListener('drop', e => {
  e.preventDefault(); dz.classList.remove('drag-over');
  queueFiles(e.dataTransfer.files);
});

async function startUpload() {
  if (!uploadQueue.length) { toast('Select at least one file', true); return; }
  const selEl = document.getElementById('folder-select');
  let folder = selEl.value === '__new__'
    ? document.getElementById('new-folder-input').value.trim()
    : selEl.value;

  const btn = document.getElementById('do-upload');
  btn.disabled = true; btn.textContent = 'Uploading…';
  const wrap = document.getElementById('prog-wrap');
  const bar  = document.getElementById('prog-bar');
  wrap.style.display = 'block';

  let done = 0;
  for (const file of uploadQueue) {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('folder', folder);
    try {
      const r = await fetch('/ui/upload-file', { method: 'POST', body: fd });
      if (!r.ok) { const d = await r.json(); toast(d.detail || 'Upload failed', true); }
    } catch { toast('Upload failed', true); }
    done++;
    bar.style.width = (done / uploadQueue.length * 100) + '%';
  }

  btn.disabled = false; btn.textContent = 'Upload';
  toast(done + ' file' + (done !== 1 ? 's' : '') + ' uploaded — reloading…');
  setTimeout(() => location.reload(), 900);
}

// ── Toast ─────────────────────────────────────────────────────────────────────
let _tid;
function toast(msg, err) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast show' + (err ? ' err' : '');
  clearTimeout(_tid);
  _tid = setTimeout(() => el.classList.remove('show'), 2400);
}

// ── Named asset file-label update ─────────────────────────────────────────────
document.addEventListener('change', e => {
  if (e.target.type === 'file') {
    const lbl = e.target.closest('label');
    if (lbl) lbl.childNodes[0].textContent = e.target.files[0]?.name.substring(0,18) || 'Choose';
  }
});
</script>
</body>
</html>"""
# ── helpers ──────────────────────────────────────────────────────────────────

def _find_file(public_id: Optional[str]) -> Optional[Path]:
    if not public_id:
        return None
    candidates = [UPLOAD_DIR / public_id]  # already has extension
    for ext in (".mp4", ".jpg", ".jpeg", ".png", ".gif", ".webm", ".mov", ".avi", ".mkv"):
        candidates.append(UPLOAD_DIR / f"{public_id}{ext}")
    for p in candidates:
        if p.exists() and p.is_file():
            return p
    return None


def _find_file_named(name: str) -> Optional[Path]:
    named_dir = UPLOAD_DIR / "_named"
    for p in named_dir.glob(f"{name}.*"):
        if p.is_file():
            return p
    return None


def _build_scale_filter(w: int, h: int, c: str, g: str) -> str:
    if c == "crop":
        gravity_crop = {
            "north":     f"crop={w}:{h}:(iw-{w})/2:0",
            "south":     f"crop={w}:{h}:(iw-{w})/2:ih-{h}",
            "northeast": f"crop={w}:{h}:iw-{w}:0",
            "northwest": f"crop={w}:{h}:0:0",
        }.get(g, f"crop={w}:{h}:(iw-{w})/2:(ih-{h})/2")
        return f"scale={w}:-2,{gravity_crop}"
    if c == "fill":
        return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    return f"scale={w}:{h}"
