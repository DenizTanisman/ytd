#!/usr/bin/env python3
"""Local web UI for the personal YouTube downloader. Binds to 127.0.0.1 only.

Under launchd (see install-autostart.sh) the port is held by launchd itself: the first
request starts this process on demand, and it exits again after IDLE_SECONDS without
requests or running downloads. Run directly (python server.py) it stays up.
"""

import asyncio
import ctypes
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse

import uvicorn
import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ytd import DEFAULT_OUT, build_options

HOST = "127.0.0.1"
PORT = 8765
ALLOWED_HOST_HEADERS = {f"{HOST}:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOST_HEADERS}
ALLOWED_URL_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtu.be",
}
OUT_DIR = DEFAULT_OUT.expanduser().resolve()
STATIC_DIR = Path(__file__).parent / "static"
ACTIVE = {"queued", "downloading", "processing"}
STATE_FILE = OUT_DIR / ".jobs.json"
MAX_SAVED_JOBS = 100
IDLE_SECONDS = int(os.environ.get("YTD_IDLE_SECONDS", "300"))
LAUNCHD_SOCKET = "Listeners"
UPDATE_STAMP = Path(sys.prefix) / ".last-update"

executor = ThreadPoolExecutor(max_workers=2)
jobs: dict[str, dict] = {}
lock = threading.RLock()
last_activity = time.monotonic()
server: uvicorn.Server | None = None
on_demand = False


def touch() -> None:
    global last_activity
    last_activity = time.monotonic()


async def idle_watch() -> None:
    while server and not server.should_exit:
        await asyncio.sleep(10)
        with lock:
            busy = any(j["status"] in ACTIVE for j in jobs.values())
        if busy:
            touch()
        elif time.monotonic() - last_activity > IDLE_SECONDS:
            print("Idle, shutting down until the next request", flush=True)
            server.should_exit = True


@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(idle_watch()) if on_demand else None
    yield
    if task:
        task.cancel()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


class DownloadRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    mode: Literal["video", "audio", "transcript"] = "video"
    max_height: Literal[360, 480, 720, 1080, 1440, 2160] = 1080
    audio_quality: Literal["high", "low"] = "high"
    # Transcript: the uploader's subtitles when there are any, else Whisper.
    transcript_source: Literal["auto", "whisper"] = "auto"
    # Video / audio download: also save the transcript next to each file,
    # under the same name (.txt and .srt).
    with_transcript: bool = False
    playlist: bool = True


class PreviewRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    playlist: bool = True


@app.middleware("http")
async def guard(request: Request, call_next):
    # Host check blocks DNS rebinding. Origin check blocks cross-site POSTs from other tabs.
    if request.headers.get("host") not in ALLOWED_HOST_HEADERS:
        return JSONResponse({"detail": "Forbidden host"}, status_code=403)
    if request.method != "GET" and request.headers.get("origin") not in ALLOWED_ORIGINS:
        return JSONResponse({"detail": "Forbidden origin"}, status_code=403)
    # Status polls from an open tab are not use; only real actions keep the server awake.
    if not (request.method == "GET" and request.url.path == "/api/downloads"):
        touch()
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        # Always revalidate the page so an open tab never runs stale polling code.
        response.headers["Cache-Control"] = "no-cache"
    return response


def validate_url(raw: str) -> str:
    parsed = urlparse(raw.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or host not in ALLOWED_URL_HOSTS:
        raise HTTPException(status_code=400, detail="Sadece YouTube linkleri kabul edilir")
    return parsed.geturl()


def single_video_url(url: str) -> str:
    """Drop the list= parameter so a watch?v=…&list=… link fetches only that video."""
    parsed = urlparse(url)
    video_id = parse_qs(parsed.query).get("v", [None])[0]
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    return url


def pick_thumbnail(info: dict) -> str | None:
    if info.get("thumbnail"):
        return info["thumbnail"]
    thumbs = info.get("thumbnails") or []
    if thumbs:
        return thumbs[-1].get("url")
    entries = [e for e in (info.get("entries") or []) if e]
    return pick_thumbnail(entries[0]) if entries else None


def update(job_id: str, **fields) -> None:
    with lock:
        jobs[job_id].update(fields)


def save_jobs() -> None:
    """Persist the job list so history survives the idle shutdowns."""
    with lock:
        data = list(jobs.values())[-MAX_SAVED_JOBS:]
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        tmp.replace(STATE_FILE)


def load_jobs() -> None:
    try:
        data = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return
    with lock:
        for job in data:
            if job.get("status") in ACTIVE:
                job.update(status="error", speed=None, eta=None,
                           error="Sunucu kapandığı için indirme yarıda kaldı")
            jobs[job["id"]] = job


def public(job: dict) -> dict:
    view = {k: v for k, v in job.items() if k != "files"}
    view["files"] = [{"index": i, "name": Path(f).name} for i, f in enumerate(job["files"])]
    return view


def resolve_file(job_id: str, index: int) -> Path:
    with lock:
        job = jobs.get(job_id)
        files = list(job["files"]) if job else []
    if index < 0 or index >= len(files):
        raise HTTPException(status_code=404, detail="File not found")
    path = Path(files[index]).resolve()
    if not path.is_relative_to(OUT_DIR) or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return path


def run_job(job_id: str, url: str, mode: str, max_height: int, audio_quality: str,
            with_transcript: bool = False) -> None:
    def on_progress(d: dict) -> None:
        info = d.get("info_dict") or {}
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            update(
                job_id,
                status="downloading",
                title=info.get("title"),
                channel=info.get("channel") or info.get("uploader"),
                thumbnail=info.get("thumbnail"),
                progress=round(done * 100 / total, 1) if total else None,
                speed=d.get("speed"),
                eta=d.get("eta"),
                item=info.get("playlist_index"),
                items=info.get("n_entries"),
            )
        elif d["status"] == "finished":
            update(job_id, status="processing", progress=100, speed=None, eta=None)

    def on_done(filepath: str) -> None:
        with lock:
            jobs[job_id]["files"].append(str(Path(filepath).resolve()))

    opts = build_options(OUT_DIR, mode == "audio", max_height, audio_quality)
    opts.update(progress_hooks=[on_progress], post_hooks=[on_done], quiet=True, noprogress=True)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            code = ydl.download([url])
        with lock:
            has_files = bool(jobs[job_id]["files"])
        transcript_error = None
        already = False
        if not has_files and with_transcript:
            # Downloaded before (skipped by the archive): transcribe that file.
            found = existing_downloads(url)
            if found:
                with lock:
                    jobs[job_id]["files"].extend(found)
                has_files = already = True
        if has_files and with_transcript:
            # Still ACTIVE ("processing") the whole time, so the idle watch
            # keeps the server up through long Whisper runs.
            transcript_error = transcribe_files(job_id)
        if has_files:
            errors = [e for e in (None if code == 0 or already else "Bazı öğeler indirilemedi",
                                  transcript_error) if e]
            update(job_id, status="done", progress=100, stage=None, item=None, items=None,
                   error=" · ".join(errors) or None)
        else:
            update(job_id, status="error",
                   error="Hiçbir şey inmedi. Daha önce indirilmiş ya da erişilemiyor.")
    except Exception as exc:  # noqa: BLE001 - surface a short message to the UI
        update(job_id, status="error", error=str(exc)[:300])
    save_jobs()


VIDEO_ID = re.compile(r"\[([\w-]{11})\]$")
TEXT_EXTS = (".txt", ".srt")
MEDIA_EXTS = (".mp4", ".m4a", ".webm", ".mkv", ".mp3", ".opus", ".ogg", ".wav")


def is_text(path: str | Path) -> bool:
    return Path(path).suffix.lower() in TEXT_EXTS


def text_siblings(media: Path) -> list[Path]:
    """Where a file's transcript lives: same name, .txt and .srt."""
    return [media.with_name(media.stem + ext) for ext in TEXT_EXTS]


def video_id(url: str) -> str | None:
    parsed = urlparse(url)
    if (parsed.hostname or "").endswith("youtu.be"):
        return parsed.path.strip("/") or None
    return parse_qs(parsed.query).get("v", [None])[0]


def existing_downloads(url: str) -> list[str]:
    """Files already in the download folder for this video (the archive
    makes yt-dlp skip it)."""
    vid = video_id(url)
    if not vid:
        return []
    return sorted(
        str(p.resolve()) for p in OUT_DIR.rglob(f"*[[]{vid}[]]*")
        if p.is_file() and p.suffix.lower() in MEDIA_EXTS
    )


def transcribe_files(job_id: str) -> str | None:
    """Writes "<file>.txt/.srt" next to every downloaded file, one by one.
    Returns a short error when some could not be transcribed."""
    from transcript import transcribe_file  # lazy: keeps the cold start fast

    with lock:
        media = [f for f in jobs[job_id]["files"] if not is_text(f)]
    failed = 0
    for i, path in enumerate(media, 1):
        update(job_id, status="processing", stage="altyazı", progress=None,
               item=i if len(media) > 1 else None, items=len(media) if len(media) > 1 else None)
        siblings = text_siblings(Path(path))
        if all(f.exists() for f in siblings):
            made = siblings  # already transcribed (earlier, or by hand)
        else:
            match = VIDEO_ID.search(Path(path).stem.removesuffix(" (low)"))
            url = f"https://www.youtube.com/watch?v={match.group(1)}" if match else None

            def on_progress(**fields) -> None:
                if fields.get("stage"):
                    update(job_id, stage=fields["stage"])

            try:
                made = transcribe_file(Path(path), url, progress=on_progress)
            except Exception:  # noqa: BLE001 - one bad file must not stop the rest
                failed += 1
                continue
        with lock:
            files = jobs[job_id]["files"]
            files.extend(str(Path(f).resolve()) for f in made if str(Path(f).resolve()) not in files)
    if failed:
        return f"{failed} dosyanın metni çıkarılamadı"
    return None


def run_transcript_job(job_id: str, url: str, force_whisper: bool) -> None:
    """Transcript instead of a download (transcript.py). The job stays in an
    ACTIVE status the whole time, Whisper included, so the idle watch keeps
    the server up."""
    from transcript import transcribe  # lazy: keeps the page's cold start fast

    keep = {"status", "progress", "speed", "eta", "title", "channel", "thumbnail"}

    def on_progress(**fields) -> None:
        stage = fields.get("stage")
        if stage:
            # What actually made the text: the subtitles or Whisper.
            fields["quality"] = "whisper" if stage in ("ses", "whisper") else "altyazı"
            fields["stage"] = stage
        update(job_id, **{k: v for k, v in fields.items() if k in keep | {"quality", "stage"}})

    try:
        update(job_id, status="processing")
        for path in transcribe(url, OUT_DIR, force_whisper, progress=on_progress):
            with lock:
                jobs[job_id]["files"].append(str(Path(path).resolve()))
        update(job_id, status="done", progress=100, speed=None, eta=None, stage=None, error=None)
    except Exception as exc:  # noqa: BLE001 - surface a short message to the UI
        update(job_id, status="error", speed=None, eta=None, stage=None, error=str(exc)[:300])
    save_jobs()


@app.post("/api/preview")
def preview(req: PreviewRequest) -> dict:
    url = validate_url(req.url)
    if not req.playlist:
        url = single_video_url(url)
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "extract_flat": "in_playlist", "noplaylist": not req.playlist}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=str(exc)[:300]) from exc
    is_list = info.get("_type") == "playlist"
    return {
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "duration": info.get("duration"),
        "thumbnail": pick_thumbnail(info),
        "playlist": is_list,
        "count": (info.get("playlist_count") or len(info.get("entries") or [])) if is_list else None,
    }


@app.post("/api/downloads", status_code=202)
def create_download(req: DownloadRequest) -> dict:
    url = validate_url(req.url)
    if not req.playlist or req.mode == "transcript":
        url = single_video_url(url)
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id, "url": url, "mode": req.mode, "status": "queued",
        "quality": (
            f"{req.max_height}p" if req.mode == "video"
            else req.audio_quality if req.mode == "audio"
            else req.transcript_source
        ),
        "title": None, "channel": None, "thumbnail": None,
        "progress": 0, "speed": None, "eta": None,
        "item": None, "items": None, "files": [], "error": None,
        "transcript": req.mode == "transcript" or req.with_transcript,
    }
    with lock:
        jobs[job_id] = job
    save_jobs()
    if req.mode == "transcript":
        executor.submit(run_transcript_job, job_id, url, req.transcript_source == "whisper")
    else:
        executor.submit(run_job, job_id, url, req.mode, req.max_height, req.audio_quality,
                        req.with_transcript)
    return public(job)


@app.get("/api/downloads")
def list_downloads() -> list[dict]:
    with lock:
        return [public(j) for j in reversed(list(jobs.values()))]


@app.delete("/api/downloads", status_code=204)
def clear_finished() -> None:
    with lock:
        for job_id in [k for k, j in jobs.items() if j["status"] not in ACTIVE]:
            del jobs[job_id]
    save_jobs()


@app.post("/api/downloads/{job_id}/files/{index}/transcript", status_code=202)
def transcribe_one(job_id: str, index: int) -> dict:
    """"Transkript çıkar" on a finished download's file."""
    path = resolve_file(job_id, index)
    with lock:  # check and claim together, so a double click cannot start two runs
        if jobs[job_id]["status"] in ACTIVE:
            raise HTTPException(status_code=409, detail="Bu iş zaten sürüyor")
        update(job_id, status="processing", stage="altyazı", error=None, transcript=True)
    save_jobs()
    executor.submit(run_file_transcript, job_id, path)
    with lock:
        return public(jobs[job_id])


def run_file_transcript(job_id: str, path: Path) -> None:
    """Transcript of one file of a finished job; the job is ACTIVE meanwhile."""
    with lock:
        others = [f for f in jobs[job_id]["files"] if f != str(path)]
        jobs[job_id]["files"] = [str(path)]
    error = transcribe_files(job_id)
    with lock:
        made = jobs[job_id]["files"][1:]
        jobs[job_id]["files"] = others + [str(path)] + [f for f in made if f not in others]
    update(job_id, status="done", stage=None, item=None, items=None, error=error)
    save_jobs()


@app.get("/api/downloads/{job_id}/files/{index}")
def open_file(job_id: str, index: int) -> FileResponse:
    path = resolve_file(job_id, index)
    # Transcripts: say UTF-8, or the browser garbles ç, ğ, ş …
    media = "text/plain; charset=utf-8" if path.suffix in (".txt", ".srt") else None
    return FileResponse(path, media_type=media, content_disposition_type="inline")


@app.post("/api/downloads/{job_id}/files/{index}/reveal", status_code=204)
def reveal_file(job_id: str, index: int) -> None:
    path = resolve_file(job_id, index)
    # Argument list, no shell. The path is already confined to OUT_DIR.
    subprocess.run(["open", "-R", str(path)], check=False)


@app.post("/api/reveal", status_code=204)
def reveal_root() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(["open", str(OUT_DIR)], check=False)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def launchd_sockets(name: str) -> list[socket.socket]:
    """Sockets launchd opened for this job, or [] when not started by launchd."""
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    fn = libc.launch_activate_socket
    fn.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_int)),
                   ctypes.POINTER(ctypes.c_size_t)]
    fn.restype = ctypes.c_int
    fds = ctypes.POINTER(ctypes.c_int)()
    count = ctypes.c_size_t(0)
    if fn(name.encode(), ctypes.byref(fds), ctypes.byref(count)) != 0:
        return []
    socks = [socket.socket(fileno=fds[i]) for i in range(count.value)]
    libc.free.argtypes = [ctypes.c_void_p]
    libc.free(fds)
    return socks


def update_ytdlp_if_stale() -> None:
    # YouTube changes often. Updating after shutdown means nobody waits for pip.
    if UPDATE_STAMP.exists() and time.time() - UPDATE_STAMP.stat().st_mtime < 86400:
        return
    cmd = [sys.executable, "-m", "pip", "install", "-q", "--upgrade", "yt-dlp[default]"]
    if subprocess.run(cmd, check=False, timeout=600).returncode == 0:
        UPDATE_STAMP.touch()


if __name__ == "__main__":
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    load_jobs()
    socks = launchd_sockets(LAUNCHD_SOCKET)
    on_demand = bool(socks)
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=PORT, log_level="warning"))
    if on_demand:
        print(f"Started on demand, idle timeout {IDLE_SECONDS}s", flush=True)
        server.run(sockets=socks)
        update_ytdlp_if_stale()
    else:
        print(f"Open http://{HOST}:{PORT}")
        server.run()
