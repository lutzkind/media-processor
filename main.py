import asyncio
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="Media Processor API")

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/data/uploads"))
BASE_URL = os.environ.get("BASE_URL", "https://media.luxeillum.com").rstrip("/")
API_KEY = os.environ.get("API_KEY", "")

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
    base_id: str                  # public_id of the screenshot / base image
    overlay_name: str = "faceintro"  # named asset for circular face overlay
    splice_name: str = "video"       # named asset concatenated after the intro
    overlay_w: int = 300
    overlay_h: int = 300
    overlay_x: int = 60           # px offset from SE edge
    overlay_y: int = 60
    output_w: int = 1280
    output_h: int = 720


@app.post("/composite")
async def create_composite(req: CompositeRequest, _=Depends(require_api_key)):
    """
    Build a two-part composite video:
      Part 1 — screenshot looped for the duration of the overlay video,
                with the overlay video displayed as a circular picture-in-picture (SE corner).
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

    # ── Part 1: screenshot + circular overlay ──────────────────────────────
    # Commas inside geq expressions must be escaped as \, for FFmpeg's filter parser
    filter_p1 = (
        f"[0:v]scale={W}:{H},setsar=1,setpts=PTS-STARTPTS[bg];"
        f"[1:v]scale={ow}:{oh},setpts=PTS-STARTPTS,format=rgba,"
        f"geq="
        f"r='r(X\\,Y)':"
        f"g='g(X\\,Y)':"
        f"b='b(X\\,Y)':"
        f"a='255*lte(sqrt(pow(X-{cx}\\,2)+pow(Y-{cy}\\,2))\\,{r})'[circle];"
        f"[bg][circle]overlay={pos_x}:{pos_y}[out]"
    )

    cmd1 = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(base_path),   # loop the screenshot
        "-i", str(overlay_path),               # face intro video
        "-filter_complex", filter_p1,
        "-map", "[out]",
        "-map", "1:a?",
        "-shortest",                           # stop when overlay video ends
        "-c:v", "libx264", "-preset", "fast", "-crf", "22",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(tmp_part1),
    ]

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
