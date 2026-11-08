#!/usr/bin/env python3
"""ytd as an MCP server: transcripts and downloads for Claude.

Order for a transcript (get_transcript, method="auto"):
  1. A transcript this Mac already made for the video (~/Downloads/ytd) is returned.
  2. Web: captions read straight from YouTube, nothing downloaded: the uploader's
     subtitles, else YouTube's own automatic captions in the video's language.
     Seconds, no disk, no Whisper; automatic captions have no punctuation.
  3. Only when the web has nothing (Instagram, videos without captions): the Mac is
     checked (free disk, power, Whisper present) and, if fit, the small audio stream
     is fetched to a temp folder and transcribed here with Whisper.

download() checks the disk against the video's estimated size before fetching it.
Long work runs as a background job (job_status); the Mac is kept awake while it runs.

    .venv/bin/python mcp_server.py          # stdio, for Claude Code / Claude Desktop
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# Claude Desktop starts servers with a bare PATH; yt-dlp needs ffmpeg from Homebrew.
os.environ["PATH"] = os.pathsep.join(
    ["/opt/homebrew/bin", "/usr/local/bin", os.environ.get("PATH", "/usr/bin:/bin")]
)

import yt_dlp  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402

import transcript as tr  # noqa: E402
import ytd  # noqa: E402

OUT = ytd.DEFAULT_OUT.expanduser()
# Free space that must remain after a download, in GB.
MIN_FREE_GB = float(os.environ.get("YTD_MIN_FREE_GB", "5"))
# On battery below this percentage, Whisper and big downloads are refused.
MIN_BATTERY = int(os.environ.get("YTD_MIN_BATTERY", "20"))
QUIET = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True}
GB = 1024**3


# ---------------------------------------------------------------- machine check


def _power() -> dict:
    out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True).stdout
    pct = re.search(r"(\d+)%", out)
    low_power = re.search(
        r"lowpowermode\s+1",
        subprocess.run(["pmset", "-g"], capture_output=True, text=True).stdout,
    )
    return {
        "on_ac": "AC Power" in out,
        "battery_percent": int(pct.group(1)) if pct else None,
        "low_power_mode": bool(low_power),
    }


def _free_bytes() -> int:
    target = OUT if OUT.exists() else OUT.parent
    return shutil.disk_usage(target).free


def machine_status() -> dict:
    p = _power()
    return {
        "free_disk_gb": round(_free_bytes() / GB, 1),
        "min_free_gb_kept": MIN_FREE_GB,
        **p,
        "whisper_installed": tr.WHISPER.exists(),
        "ffmpeg_installed": shutil.which("ffmpeg") is not None,
        "login_profile_present": Path(ytd.CHROME_PROFILE).exists(),
        "jobs_running_or_queued": sum(j["status"] in ("queued", "running") for j in _jobs.values()),
        "output_folder": str(OUT),
    }


def preflight(need_bytes: int, heavy: bool, whisper: bool) -> list[str]:
    """Reasons the Mac is not fit for the job now; empty when it is."""
    problems = []
    reserved = sum(j.get("need_bytes", 0) for j in _jobs.values() if j["status"] == "queued")
    free = _free_bytes()
    left = free - need_bytes - reserved
    if left < MIN_FREE_GB * GB:
        problems.append(
            f"Yer yetmiyor: boş {free / GB:.1f} GB, bu iş için ~{need_bytes / GB:.2f} GB gerek"
            f"{f' (+ sıradaki işler {reserved / GB:.1f} GB)' if reserved else ''}; "
            f"sonrasında en az {MIN_FREE_GB:g} GB boş kalmalı."
        )
    if heavy:
        p = _power()
        if not p["on_ac"] and (p["battery_percent"] or 0) < MIN_BATTERY:
            problems.append(
                f"Mac pilde ve şarj %{p['battery_percent']}: %{MIN_BATTERY} altında uzun iş "
                "başlatılmıyor. Şarja tak, sonra tekrar dene."
            )
    if whisper and not tr.WHISPER.exists():
        problems.append(f"Whisper (mlx-whisper) bulunamadı: {tr.WHISPER}")
    if not shutil.which("ffmpeg"):
        problems.append("ffmpeg bulunamadı (brew install ffmpeg).")
    return problems


# ---------------------------------------------------------------- video info


def _info(url: str, fmt: str | None = None) -> dict:
    opts = {**QUIET, "skip_download": True, **({"format": fmt} if fmt else {})}
    info = ytd.with_login(url, opts, lambda ydl: ydl.extract_info(url, download=False))
    if not info:
        raise RuntimeError("Video bilgisi alınamadı.")
    if info.get("_type") == "playlist" or info.get("entries") is not None:
        raise RuntimeError("Bu bir oynatma listesi; MCP tek video alır. Listeler için ytd arayüzünü kullan.")
    return info


def _estimate_bytes(info: dict, fallback_kbps: int) -> int:
    total = 0
    duration = info.get("duration") or 0
    for f in info.get("requested_formats") or [info]:
        size = f.get("filesize") or f.get("filesize_approx")
        if not size and f.get("tbr") and duration:
            size = f["tbr"] * 125 * duration
        if not size:
            return int(duration * fallback_kbps * 125) if duration else 2 * GB
        total += size
    return int(total)


def _base(info: dict) -> Path:
    """<out>/<channel>/<title> [<id>], the name ytd gives this video's files."""
    with yt_dlp.YoutubeDL({**ytd.build_options(OUT), **QUIET}) as ydl:
        named = Path(ydl.prepare_filename(info))
    return named.with_name(named.stem)


def _summary(info: dict) -> dict:
    return {
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "duration_s": info.get("duration"),
        "url": info.get("webpage_url"),
    }


# ---------------------------------------------------------------- web captions


def _ms_srt(ms: int) -> str:
    s, ms = divmod(int(ms), 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _json3_cues(data: dict) -> list[tuple[int, int, str]]:
    cues = []
    for ev in data.get("events", []):
        text = "".join(s.get("utf8", "") for s in ev.get("segs") or []).replace("\n", " ").strip()
        if text:
            start = int(ev.get("tStartMs", 0))
            cues.append((start, start + int(ev.get("dDurationMs", 0)), text))
    return cues


def _web_track(info: dict) -> tuple[str, str] | None:
    """(language key, "creator" | "auto") of the best caption track, or None."""
    lang = tr._creator_subtitle_lang(info)
    if lang:
        return lang, "creator"
    auto = info.get("automatic_captions") or {}
    want = (info.get("language") or "").split("-")[0]
    orig = [k for k in auto if k.endswith("-orig")]
    for key in orig:
        if not want or key.split("-")[0] == want:
            return key, "auto"
    for key in auto:
        if want and key.split("-")[0] == want and key.count("-") <= 1:
            return key, "auto"
    return (orig[0], "auto") if orig else None


def web_transcript(info: dict) -> dict | None:
    """Captions read from the site (no media downloaded), saved as .txt/.srt.
    None when the video has no usable captions."""
    track = _web_track(info)
    if not track:
        return None
    lang, kind = track
    url = info["webpage_url"]
    with tempfile.TemporaryDirectory(prefix="ytd-mcp-") as tmp:
        opts = {
            **QUIET,
            "skip_download": True,
            "writesubtitles": kind == "creator",
            "writeautomaticsub": kind == "auto",
            "subtitleslangs": [lang],
            "subtitlesformat": "json3/vtt",
            "outtmpl": {"default": str(Path(tmp) / "s.%(ext)s")},
        }
        ytd.with_login(url, opts, lambda ydl: ydl.download([url]))
        j3 = next(Path(tmp).glob("*.json3"), None)
        vtt = next(Path(tmp).glob("*.vtt"), None)
        if j3:
            cues = _json3_cues(json.loads(j3.read_text(encoding="utf-8")))
        elif vtt:
            cues = [
                (_vtt_ms(s), _vtt_ms(e), t)
                for s, e, t in tr._vtt_cues(vtt.read_text(encoding="utf-8", errors="replace"))
            ]
        else:
            return None
    if not cues:
        return None
    base = _base(info)
    if kind == "auto":
        base = base.with_name(base.name + " (otomatik altyazı)")
    base.parent.mkdir(parents=True, exist_ok=True)
    txt, srt = base.with_name(base.name + ".txt"), base.with_name(base.name + ".srt")
    body = " ".join(t for _, _, t in cues)
    txt.write_text((textwrap.fill(body, 100) if kind == "auto" else "\n".join(t for *_, t in cues)) + "\n",
                   encoding="utf-8")
    srt.write_text(
        "\n".join(f"{i}\n{_ms_srt(s)} --> {_ms_srt(e)}\n{t}\n" for i, (s, e, t) in enumerate(cues, 1)),
        encoding="utf-8",
    )
    return {
        "source": "web: yükleyenin altyazısı" if kind == "creator"
        else "web: YouTube otomatik altyazısı (noktalama eksik, isimlerde hata olabilir)",
        "language": lang,
        "files": [str(txt), str(srt)],
    }


def _vtt_ms(t: str) -> int:
    parts = [float(p) for p in t.replace(",", ".").split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    return int((parts[0] * 3600 + parts[1] * 60 + parts[2]) * 1000)


def existing_transcript(info: dict) -> dict | None:
    """A transcript this Mac already wrote for the video (Whisper or creator subs)."""
    base = _base(info)
    txt, srt = base.with_name(base.name + ".txt"), base.with_name(base.name + ".srt")
    if txt.exists():
        return {"source": "bu Mac'te daha önce çıkarılmış transkript",
                "files": [str(txt)] + ([str(srt)] if srt.exists() else [])}
    return None


# ---------------------------------------------------------------- reading results


def _read_text(result: dict, timestamps: bool, offset: int, max_chars: int) -> str:
    files = [Path(f) for f in result.get("files", [])]
    srt = next((f for f in files if f.suffix == ".srt" and f.exists()), None)
    txt = next((f for f in files if f.suffix == ".txt" and f.exists()), None)
    if timestamps and srt:
        lines = []
        for block in re.split(r"\n\s*\n", srt.read_text(encoding="utf-8").strip()):
            rows = block.split("\n")
            if len(rows) >= 3 and "-->" in rows[1]:
                h, m, s = rows[1].split(" --> ")[0].split(",")[0].split(":")
                stamp = f"{int(h)}:{m}:{s}" if int(h) else f"{int(m)}:{s}"
                lines.append(f"[{stamp}] {' '.join(rows[2:])}")
        text = "\n".join(lines)
    else:
        text = txt.read_text(encoding="utf-8") if txt else ""
    chunk = text[offset: offset + max_chars]
    end = offset + len(chunk)
    tail = (f"\n\n[… devamı var: toplam {len(text)} karakter. Sonraki kısım için offset={end}]"
            if end < len(text) else "")
    return chunk + tail


def _render(result: dict, timestamps: bool, offset: int, max_chars: int) -> str:
    head = {k: v for k, v in result.items() if k not in ("files",)}
    head["files"] = result.get("files")
    return json.dumps(head, ensure_ascii=False, indent=1) + "\n\n" + _read_text(
        result, timestamps, offset, max_chars)


# ---------------------------------------------------------------- jobs

_jobs: dict[str, dict] = {}
_events: dict[str, threading.Event] = {}
_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ytd-job")  # one heavy job at a time
_awake_lock = threading.Lock()
_awake: subprocess.Popen | None = None
_active = 0


def _stay_awake(on: bool) -> None:
    """caffeinate keeps the Mac from idle-sleeping while a job runs."""
    global _awake, _active
    with _awake_lock:
        _active += 1 if on else -1
        if _active > 0 and _awake is None:
            _awake = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        elif _active == 0 and _awake is not None:
            _awake.terminate()
            _awake = None


def submit(kind: str, url: str | None, need_bytes: int, work, meta: dict) -> str:
    job_id = uuid.uuid4().hex[:8]
    job = {"id": job_id, "kind": kind, "url": url, "status": "queued", "stage": None,
           "progress": None, "need_bytes": need_bytes, "created": time.time(), **meta}
    _jobs[job_id] = job
    _events[job_id] = threading.Event()

    def report(**k: Any) -> None:
        if k.get("stage"):
            job["stage"] = k["stage"]
        if "progress" in k:
            job["progress"] = k["progress"]

    def run() -> None:
        job["status"], job["started"] = "running", time.time()
        _stay_awake(True)
        try:
            job["result"] = work(report)
            job["status"] = "done"
        except ytd.LoginMissing as exc:
            job["status"], job["error"] = "error", f"{exc} (Giriş: ~/Projects/ytd/.venv/bin/python ytd.py --login)"
        except Exception as exc:  # noqa: BLE001 - shown to Claude as text
            job["status"], job["error"] = "error", f"{type(exc).__name__}: {exc}"
        finally:
            job["finished"] = time.time()
            _stay_awake(False)
            _events[job_id].set()

    _pool.submit(run)
    return job_id


async def _wait(job_id: str, seconds: float) -> None:
    if seconds > 0:
        await asyncio.to_thread(_events[job_id].wait, seconds)


def _job_view(job: dict) -> dict:
    view = {k: v for k, v in job.items() if k not in ("result", "need_bytes", "created", "started", "finished")}
    if job.get("started"):
        view["elapsed_s"] = round((job.get("finished") or time.time()) - job["started"])
    return view


def _job_reply(job: dict, timestamps: bool, offset: int, max_chars: int) -> str:
    if job["status"] == "done":
        r = job["result"]
        if r.get("files") and any(f.endswith(".txt") for f in r["files"]):
            return _render(r, timestamps, offset, max_chars)
        return json.dumps({**_job_view(job), **r}, ensure_ascii=False, indent=1)
    if job["status"] == "error":
        return json.dumps(_job_view(job), ensure_ascii=False, indent=1)
    return json.dumps(_job_view(job), ensure_ascii=False, indent=1) + (
        "\n\nİş arka planda sürüyor. Birkaç dakika sonra job_status ile "
        f'job_id="{job["id"]}" (wait_seconds=60) çağır.'
    )


# ---------------------------------------------------------------- MCP

INSTRUCTIONS = """ytd: YouTube ve Instagram videoları için transkript ve indirme; kullanıcının Mac'inde çalışır.
- Transkript için get_transcript kullan. Önce web'den altyazıyı dener (indirme yok, saniyeler sürer);
  yoksa Mac'i kontrol edip sesi indirir ve Whisper ile yazıya döker (arka plan işi, dakikalar sürebilir).
  Daha doğru metin gerekirse (otomatik altyazıda noktalama yoktur) method="whisper" ver.
- Videoyu ya da sesi dosya olarak istemek için download kullan; önce disk yeri kontrol edilir.
- Arka plan işleri job_id döndürür; job_status(job_id, wait_seconds=60) ile bekle ve sonucu al.
- system_check Mac'in durumunu (boş disk, güç, Whisper) gösterir.
- Yalnızca kullanıcının indirme hakkı olan içerik için kullan. Mac kapalıysa ya da uykudaysa bu araçlar çalışmaz."""

mcp = MCPServer("ytd", instructions=INSTRUCTIONS)


@mcp.tool()
async def system_check() -> str:
    """Mac'in şu an indirmeye / Whisper'a uygun olup olmadığı: boş disk, güç (şarj/pil), düşük güç modu,
    Whisper ve ffmpeg kurulu mu, sıradaki işler."""
    return json.dumps(await asyncio.to_thread(machine_status), ensure_ascii=False, indent=1)


@mcp.tool()
async def get_transcript(
    url: str,
    method: str = "auto",
    timestamps: bool = False,
    wait_seconds: int = 50,
    offset: int = 0,
    max_chars: int = 40000,
) -> str:
    """Bir YouTube ya da Instagram videosunun transkripti.

    method:
      "auto"    — bu Mac'te önceden çıkarılmış varsa onu; yoksa web'den altyazı (indirme yok);
                  o da yoksa Mac kontrol edilip ses indirilir ve Whisper çalışır.
      "web"     — yalnızca web altyazısı; yoksa hata döner, hiçbir şey indirilmez.
      "whisper" — altyazıya bakmadan, Mac kontrolünden sonra Whisper (en doğru, noktalamalı metin).
    timestamps: true ise satır başına [dk:sn] zaman damgası.
    wait_seconds: Whisper işi için en çok bu kadar beklenir; bitmezse job_id döner.
    offset / max_chars: uzun metni parça parça okumak için.
    Dosyalar ~/Downloads/ytd/<kanal>/ altına .txt ve .srt olarak da kaydedilir."""
    if method not in ("auto", "web", "whisper"):
        return 'method "auto", "web" ya da "whisper" olmalı.'
    try:
        info = await asyncio.to_thread(_info, url, ytd.AUDIO_FORMATS["low"])
    except ytd.LoginMissing as exc:
        return f"Giriş gerekiyor: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"Video açılamadı: {exc}"

    web_note = None
    if method == "auto":
        found = await asyncio.to_thread(existing_transcript, info)
        if found:
            return _render({**_summary(info), **found}, timestamps, offset, max_chars)
    if method in ("auto", "web"):
        try:
            found = await asyncio.to_thread(web_transcript, info)
        except Exception as exc:  # noqa: BLE001 - e.g. YouTube rate limit on captions
            found, web_note = None, f"web altyazısı alınamadı: {exc}"
        if found:
            return _render({**_summary(info), **found}, timestamps, offset, max_chars)
        web_note = web_note or "web'de bu video için altyazı yok"
        if method == "web":
            return f"{web_note}. Whisper için method=\"whisper\" (ya da \"auto\") ile tekrar çağır."

    need = int(_estimate_bytes(info, 64) * 1.5)
    problems = await asyncio.to_thread(preflight, need, True, True)
    if problems:
        return json.dumps({**_summary(info), "web": web_note, "mac_uygun_degil": problems,
                           "durum": await asyncio.to_thread(machine_status)}, ensure_ascii=False, indent=1)

    page_url = info["webpage_url"]
    job_id = submit(
        "transcript", page_url, need,
        lambda report: {**_summary(info), "source": "Whisper (bu Mac'te, ses geçici indirildi)",
                        "web": web_note,
                        "files": [str(p) for p in tr.transcribe(page_url, OUT, True, progress=report)]},
        {"title": info.get("title")},
    )
    await _wait(job_id, wait_seconds)
    return _job_reply(_jobs[job_id], timestamps, offset, max_chars)


def _downloaded_path(info: dict | None, video_id: str, exts: tuple[str, ...]) -> str | None:
    for d in (info or {}).get("requested_downloads") or []:
        if d.get("filepath") and Path(d["filepath"]).exists():
            return d["filepath"]
    tag = f"[{video_id}]"
    hits = [p for p in OUT.rglob("*") if tag in p.name and p.suffix.lstrip(".") in exts]
    return str(max(hits, key=lambda p: p.stat().st_mtime)) if hits else None


@mcp.tool()
async def download(
    url: str,
    kind: str = "video",
    max_height: int = 1080,
    h264: bool = False,
    with_transcript: bool = False,
    wait_seconds: int = 20,
) -> str:
    """Videoyu ya da sesini bu Mac'e indirir (~/Downloads/ytd/<kanal>/). Önce tahmini boyut alınır ve
    boş disk, güç durumu kontrol edilir; uygun değilse indirmez ve nedenini söyler.

    kind: "video" (AV1+Opus MKV; h264=true ise QuickTime'da açılan MP4) ya da "audio" (M4A).
    max_height: en yüksek çözünürlük (720, 1080, 2160 ...).
    with_transcript: indirince yanına transkript de çıkarır (altyazı yoksa Whisper).
    İndirme arka planda sürer; bitmezse job_id döner, job_status ile izlenir."""
    if kind not in ("video", "audio"):
        return 'kind "video" ya da "audio" olmalı.'
    opts = ytd.build_options(OUT, kind == "audio", max_height, "high", "h264" if h264 else "av1")
    try:
        info = await asyncio.to_thread(_info, url, opts["format"])
    except ytd.LoginMissing as exc:
        return f"Giriş gerekiyor: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"Video açılamadı: {exc}"

    size = _estimate_bytes(info, 4000 if kind == "video" else 160)
    need = int(size * (2.0 if kind == "video" else 1.3))  # parts + merged file
    problems = await asyncio.to_thread(
        preflight, need, size > 300 * 1024**2 or with_transcript, with_transcript)
    if problems:
        return json.dumps({**_summary(info), "tahmini_boyut_mb": round(size / 1024**2),
                           "mac_uygun_degil": problems,
                           "durum": await asyncio.to_thread(machine_status)}, ensure_ascii=False, indent=1)

    page_url, vid = info["webpage_url"], info.get("id", "")
    exts = ("m4a",) if kind == "audio" else ("mkv", "mp4", "webm")

    def work(report) -> dict:
        def hook(d: dict) -> None:
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                report(stage="indiriliyor",
                       progress=round(d.get("downloaded_bytes", 0) * 100 / total, 1) if total else None)

        run_opts = {**opts, **QUIET, "progress_hooks": [hook]}
        got = ytd.with_login(page_url, run_opts, lambda ydl: ydl.extract_info(page_url, download=True))
        path = _downloaded_path(got, vid, exts)
        if not path:
            raise RuntimeError("İndirme bitti ama dosya bulunamadı (hata ya da erişim sorunu).")
        result = {**_summary(info), "file": path,
                  "size_mb": round(Path(path).stat().st_size / 1024**2, 1)}
        if with_transcript:
            report(stage="transkript", progress=None)
            result["files"] = [str(p) for p in tr.transcribe_file(Path(path), page_url, progress=report)]
            result["source"] = "indirilen dosyadan (altyazı ya da Whisper)"
        return result

    job_id = submit("download", page_url, need, work,
                    {"title": info.get("title"), "estimated_mb": round(size / 1024**2)})
    await _wait(job_id, wait_seconds)
    return _job_reply(_jobs[job_id], False, 0, 40000)


@mcp.tool()
async def transcribe_local_file(path: str, wait_seconds: int = 50, timestamps: bool = False) -> str:
    """Bu Mac'teki bir ses/video dosyasını (ör. indirilmiş bir video) Whisper ile yazıya döker;
    .txt/.srt dosyanın yanına yazılır."""
    media = Path(path).expanduser()
    if not media.is_file():
        return f"Dosya yok: {media}"
    problems = await asyncio.to_thread(preflight, 50 * 1024**2, True, True)
    if problems:
        return json.dumps({"mac_uygun_degil": problems}, ensure_ascii=False, indent=1)
    job_id = submit("transcript", None, 0,
                    lambda report: {"source": "Whisper (yerel dosya)", "file": str(media),
                                    "files": [str(p) for p in tr.transcribe_file(media, None, True, report)]},
                    {"title": media.name})
    await _wait(job_id, wait_seconds)
    return _job_reply(_jobs[job_id], timestamps, 0, 40000)


@mcp.tool()
async def open_on_mac(path: str, reveal: bool = False) -> str:
    """ytd'nin kaydettiği bir dosyayı (transkript .txt/.srt, indirilen video/ses) kullanıcının Mac'inde
    açar: metin TextEdit'te, video oynatıcıda. reveal=true ise Finder'da dosyayı seçili gösterir.
    Kullanıcı metni ya da videoyu kendisi görmek istediğinde kullan."""
    target = Path(path).expanduser().resolve()
    if not target.is_relative_to(OUT.resolve()):
        return f"Yalnızca {OUT} altındaki dosyalar açılır."
    if not target.exists():
        return f"Dosya yok: {target}"
    cmd = ["open", "-R", str(target)] if reveal else (
        ["open", "-e", str(target)] if target.suffix in (".txt", ".srt") else ["open", str(target)])
    await asyncio.to_thread(subprocess.run, cmd, check=False)
    return f"Mac'te açıldı: {target}"


@mcp.tool()
async def job_status(
    job_id: str = "",
    wait_seconds: int = 0,
    timestamps: bool = False,
    offset: int = 0,
    max_chars: int = 40000,
) -> str:
    """Arka plan işinin durumu; bittiyse sonucu (transkript metni ya da dosya yolu).
    wait_seconds kadar bitmesini bekler (en çok 120). job_id boşsa bu oturumdaki tüm işleri listeler."""
    if not job_id:
        return json.dumps([_job_view(j) for j in _jobs.values()], ensure_ascii=False, indent=1) or "[]"
    job = _jobs.get(job_id)
    if not job:
        return f"Böyle bir iş yok: {job_id} (sunucu yeniden başladıysa işler silinmiş olabilir)."
    await _wait(job_id, min(max(wait_seconds, 0), 120))
    return _job_reply(job, timestamps, offset, max_chars)


if __name__ == "__main__":
    mcp.run()
