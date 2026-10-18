#!/usr/bin/env python3
"""Transcript of a YouTube video without downloading the video.

1. Subtitles the uploader wrote (punctuated, instant) are used when the video
   has them in its own language.
2. Otherwise only the audio is fetched (the small ~48 kbps stream, to a temp
   folder) and transcribed on this Mac with Whisper (mlx-whisper, the same
   install the Ses Bölücü helper uses); the audio is then deleted.

Writes "<title> [<id>].txt" (plain text) and ".srt" (timestamps, to find
the part worth cutting) into ~/Downloads/ytd/<channel>/ — the same name a
download of that video gets. transcribe_file() does the same for a file
already downloaded, next to it.

Usage:
    python transcript.py URL                # captions if any, else Whisper
    python transcript.py --whisper URL      # always Whisper
    python transcript.py FILE.mp4           # Whisper on a downloaded file
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

import yt_dlp

from ytd import AUDIO_FORMATS, DEFAULT_OUT, build_options

WHISPER = Path(
    "~/Library/Application Support/ses-bolucu/venv/bin/mlx_whisper"
).expanduser()
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"

Progress = Callable[..., None]


def _vtt_cues(vtt: str) -> list[tuple[str, str, str]]:
    """(start, end, text) per cue; rolling duplicate lines removed."""
    cues: list[tuple[str, str, str]] = []
    seen_last = ""
    for block in re.split(r"\n\s*\n", vtt.replace("\r", "")):
        lines = block.strip().split("\n")
        timing = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if timing is None:
            continue
        start, end = (t.strip().split(" ")[0] for t in lines[timing].split("-->"))
        text = " ".join(
            html.unescape(re.sub(r"<[^>]+>", "", l)).strip()
            for l in lines[timing + 1 :]
        ).strip()
        if not text or text == seen_last:
            continue
        # Auto-style rolling captions repeat the previous line first.
        if seen_last and text.startswith(seen_last):
            text = text[len(seen_last) :].strip()
        seen_last = " ".join(
            html.unescape(re.sub(r"<[^>]+>", "", l)).strip()
            for l in lines[timing + 1 :]
        ).strip()
        if text:
            cues.append((start, end, text))
    return cues


def _srt_time(t: str) -> str:
    parts = t.split(":")
    if len(parts) == 2:
        parts.insert(0, "00")
    h, m, s = parts
    return f"{int(h):02d}:{int(m):02d}:{s.replace('.', ',')}"


def _write_from_vtt(vtt_path: Path, base: Path) -> list[Path]:
    cues = _vtt_cues(vtt_path.read_text(encoding="utf-8", errors="replace"))
    txt = base.with_suffix(".txt")
    srt = base.with_suffix(".srt")
    txt.write_text(" ".join(c[2] for c in cues).strip() + "\n", encoding="utf-8")
    srt.write_text(
        "\n".join(
            f"{i}\n{_srt_time(s)} --> {_srt_time(e)}\n{t}\n"
            for i, (s, e, t) in enumerate(cues, 1)
        ),
        encoding="utf-8",
    )
    return [txt, srt]


def _creator_subtitle_lang(info: dict) -> str | None:
    """A subtitle track the uploader made, preferring the video's language."""
    subs = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
    if not subs:
        return None
    lang = info.get("language")
    if lang:
        for key in subs:
            if key == lang or key.split("-")[0] == lang.split("-")[0]:
                return key
    return next(iter(subs))


BASE_OPTS = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True}


def _from_subtitles(info: dict, base: Path, tmp_dir: Path, report: Progress) -> list[Path] | None:
    """Writes base.txt/.srt from the uploader's subtitles; None when there
    are none."""
    lang = _creator_subtitle_lang(info)
    if not lang:
        return None
    report(status="processing", stage="altyazı")
    opts = {
        **BASE_OPTS,
        "skip_download": True,
        "writesubtitles": True,
        "subtitleslangs": [lang],
        "subtitlesformat": "vtt",
        "outtmpl": {"default": str(tmp_dir / "sub.%(ext)s")},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([info["webpage_url"]])
    vtt = next(tmp_dir.glob("*.vtt"), None)
    return _write_from_vtt(vtt, base) if vtt else None


def _from_whisper(media: Path, base: Path, tmp_dir: Path, report: Progress) -> list[Path]:
    """Writes base.txt/.srt by running Whisper on [media] (any audio or
    video file ffmpeg reads)."""
    if not WHISPER.exists():
        raise RuntimeError("Whisper (mlx-whisper) bu Mac'te bulunamadı")
    report(status="processing", stage="whisper", progress=None, speed=None, eta=None)
    # One pass writes every format; txt and srt are kept.
    # Music and silence make Whisper loop on its last sentence. Not feeding
    # it its own previous text and skipping hallucinations over silence
    # (needs word timestamps) stops most of it; _clean drops what is left.
    subprocess.run(
        [str(WHISPER), str(media), "--model", WHISPER_MODEL,
         "--output-format", "srt", "--output-dir", str(tmp_dir),
         "--output-name", "out", "--verbose", "False",
         "--condition-on-previous-text", "False",
         "--word-timestamps", "True",
         "--hallucination-silence-threshold", "2"],
        check=True, capture_output=True,
    )
    made = tmp_dir / "out.srt"
    if not made.exists():
        raise RuntimeError("Whisper metin üretemedi")
    cues = _clean(_srt_cues(made.read_text(encoding="utf-8", errors="replace")))
    return _write_cues(cues, base)


def _srt_cues(srt: str) -> list[tuple[str, str, str]]:
    cues = []
    for block in re.split(r"\n\s*\n", srt.replace("\r", "").strip()):
        lines = block.strip().split("\n")
        timing = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if timing is None:
            continue
        start, end = (t.strip() for t in lines[timing].split("-->"))
        cues.append((start, end, " ".join(l.strip() for l in lines[timing + 1:]).strip()))
    return cues


def _norm(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.lower()).strip()


def _clean(cues: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Drops empty cues and runs of the same sentence (Whisper's loops),
    keeping the first of each run."""
    out: list[tuple[str, str, str]] = []
    for start, end, text in cues:
        if not _norm(text):
            continue
        if out and _norm(out[-1][2]) == _norm(text):
            continue
        out.append((start, end, text))
    return out


def _write_cues(cues: list[tuple[str, str, str]], base: Path) -> list[Path]:
    txt = base.with_name(base.name + ".txt")
    srt = base.with_name(base.name + ".srt")
    txt.write_text("\n".join(t for _, _, t in cues) + "\n", encoding="utf-8")
    srt.write_text(
        "\n".join(f"{i}\n{s} --> {e}\n{t}\n" for i, (s, e, t) in enumerate(cues, 1)),
        encoding="utf-8",
    )
    return [txt, srt]


def _info(url: str) -> dict:
    with yt_dlp.YoutubeDL({**BASE_OPTS, "skip_download": True}) as ydl:
        return ydl.extract_info(url, download=False)


def transcribe_file(
    media: Path,
    url: str | None = None,
    force_whisper: bool = False,
    progress: Progress | None = None,
) -> list[Path]:
    """Transcript of an already downloaded file, saved next to it under the
    same name (".../Title [id].mp4" → ".../Title [id].txt" and ".srt").
    The uploader's subtitles are used when [url] has them, otherwise
    Whisper reads the file itself — nothing is downloaded again."""
    report = progress or (lambda **_: None)
    media = Path(media)
    base = media.with_name(media.stem)
    with tempfile.TemporaryDirectory(prefix="ytd-transcript-") as tmp:
        tmp_dir = Path(tmp)
        if url and not force_whisper:
            try:
                made = _from_subtitles(_info(url), base, tmp_dir, report)
            except yt_dlp.utils.DownloadError:
                made = None  # subtitles unreachable: Whisper still works
            if made:
                return made
        return _from_whisper(media, base, tmp_dir, report)


def transcribe(
    url: str,
    out_dir: Path = DEFAULT_OUT,
    force_whisper: bool = False,
    progress: Progress | None = None,
) -> list[Path]:
    """Transcript only (no video kept). Files get the name a download of the
    same video would get, so a later download sits next to its text."""
    report = progress or (lambda **_: None)
    out_dir = Path(out_dir).expanduser()
    info = _info(url)
    report(
        title=info.get("title"),
        channel=info.get("channel") or info.get("uploader"),
        thumbnail=info.get("thumbnail"),
    )
    # ytd's own naming: <out>/<channel>/<title> [<id>]
    with yt_dlp.YoutubeDL(build_options(out_dir)) as ydl:
        named = Path(ydl.prepare_filename(info))
    base = named.with_name(named.stem)
    base.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ytd-transcript-") as tmp:
        tmp_dir = Path(tmp)
        if not force_whisper:
            made = _from_subtitles(info, base, tmp_dir, report)
            if made:
                return made

        # No usable subtitles: the audio only (small stream), then Whisper.
        def on_progress(d: dict) -> None:
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                report(
                    status="downloading",
                    stage="ses",
                    progress=round(done * 100 / total, 1) if total else None,
                    speed=d.get("speed"),
                    eta=d.get("eta"),
                )

        opts = {
            **BASE_OPTS,
            "format": AUDIO_FORMATS["low"],
            "outtmpl": {"default": str(tmp_dir / "audio.%(ext)s")},
            "progress_hooks": [on_progress],
            "retries": 5,
            "fragment_retries": 5,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([info["webpage_url"]])
        audio = next(tmp_dir.glob("audio.*"), None)
        if audio is None:
            raise RuntimeError("Ses indirilemedi")
        return _from_whisper(audio, base, tmp_dir, report)


def main() -> None:
    parser = argparse.ArgumentParser(description="Transcript of a YouTube video.")
    parser.add_argument("items", nargs="+", help="YouTube links, or downloaded files")
    parser.add_argument("--whisper", action="store_true", help="always transcribe the audio")
    parser.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    say = lambda **k: k.get("stage") and print("…", k["stage"])  # noqa: E731
    for item in args.items:
        if Path(item).expanduser().is_file():
            made = transcribe_file(Path(item).expanduser(), None, True, progress=say)
        else:
            made = transcribe(item, args.out, args.whisper, progress=say)
        for path in made:
            print(path)


if __name__ == "__main__":
    main()
