#!/usr/bin/env python3
"""Personal YouTube downloader. Shared yt-dlp options plus a small CLI.

Usage:
    python ytd.py URL [URL ...]            # video, up to 1080p
    python ytd.py -a URL                   # audio only (m4a, ~128 kbps)
    python ytd.py -a --low URL             # audio only, small file (~48 kbps)
    python ytd.py -q 720 URL               # cap video height
"""

import argparse
from pathlib import Path

import yt_dlp

DEFAULT_OUT = Path("~/Downloads/ytd")


# YouTube serves two AAC streams: itag 140 (~128 kbps) and itag 139 (~48 kbps). Both are
# taken as-is, so neither level is re-encoded.
AUDIO_FORMATS = {
    "high": "bestaudio[ext=m4a]/bestaudio/best",
    "low": "bestaudio[ext=m4a][abr<=64]/worstaudio[ext=m4a]/worstaudio",
}


def build_options(
    out_dir: Path, audio_only: bool = False, max_height: int = 1080, audio_quality: str = "high"
) -> dict:
    out_dir = Path(out_dir).expanduser()
    # The low-quality copy gets its own name and archive so it never collides with the high one.
    suffix = " (low)" if audio_only and audio_quality == "low" else ""
    archive = f".archive-audio-{audio_quality}.txt" if audio_only else ".archive-video.txt"
    opts = {
        "outtmpl": {
            "default": str(out_dir / "%(channel,uploader|Unknown)s" / f"%(title).180B [%(id)s]{suffix}.%(ext)s"),
        },
        "windowsfilenames": True,
        # Skips items already fetched; the archive file lives next to the downloads.
        "download_archive": str(out_dir / archive),
        "ignoreerrors": "only_download",
        "concurrent_fragment_downloads": 4,
        "retries": 5,
        "fragment_retries": 5,
        "noplaylist": False,
    }
    if audio_only:
        opts.update(
            format=AUDIO_FORMATS[audio_quality],
            postprocessors=[
                {"key": "FFmpegExtractAudio", "preferredcodec": "m4a"},
                {"key": "FFmpegMetadata"},
                {"key": "EmbedThumbnail"},
            ],
            writethumbnail=True,
        )
    else:
        h = int(max_height)
        opts.update(
            # Prefer H.264/AAC in mp4 so files play in QuickTime and the browser.
            format=(
                f"bv*[height<={h}][vcodec^=avc1]+ba[ext=m4a]/"
                f"bv*[height<={h}]+ba/b[height<={h}]/b"
            ),
            merge_output_format="mp4",
            postprocessors=[{"key": "FFmpegMetadata"}],
        )
    return opts


def main() -> None:
    parser = argparse.ArgumentParser(description="Download YouTube videos or audio.")
    parser.add_argument("urls", nargs="+")
    parser.add_argument("-a", "--audio", action="store_true", help="audio only (m4a)")
    parser.add_argument("--low", action="store_true", help="smaller audio (~48 kbps)")
    parser.add_argument("-q", "--quality", type=int, default=1080, help="max video height")
    parser.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT, help="output folder")
    args = parser.parse_args()

    out = args.out.expanduser()
    out.mkdir(parents=True, exist_ok=True)
    with yt_dlp.YoutubeDL(build_options(out, args.audio, args.quality, "low" if args.low else "high")) as ydl:
        raise SystemExit(ydl.download(args.urls))


if __name__ == "__main__":
    main()
