#!/usr/bin/env python3
"""Personal YouTube and Instagram downloader. Shared yt-dlp options plus a small CLI.

Usage:
    python ytd.py URL [URL ...]            # video, up to 1080p
    python ytd.py -a URL                   # audio only (m4a, ~128 kbps)
    python ytd.py -a --low URL             # audio only, small file (~48 kbps)
    python ytd.py -q 720 URL               # cap video height
    python ytd.py --login                  # sign in once, in ytd's own Chrome window

Instagram links, and private, members-only or age-restricted YouTube videos, use the
login in ytd's own Chrome profile (see with_login and LOGIN_PROFILE).
"""

import argparse
import functools
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.cookies import YoutubeDLCookieJar, extract_cookies_from_browser

DEFAULT_OUT = Path("~/Downloads/ytd")
INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
# ytd's own Chrome profile, signed in to YouTube and Instagram once (ytd.py --login).
# macOS keeps other programs out of Chrome's main profile, and a profile used only
# here also keeps everyday browsing from rotating (and so breaking) its cookies.
LOGIN_PROFILE = Path("~/Library/Application Support/ytd/chrome").expanduser()
CHROME_PROFILE = os.environ.get("YTD_CHROME_PROFILE", str(LOGIN_PROFILE))
LOGIN_PAGES = (
    "https://accounts.google.com/ServiceLogin?continue=https%3A%2F%2Fwww.youtube.com%2F",
    "https://www.instagram.com/accounts/login/",
)


# YouTube serves two AAC streams: itag 140 (~128 kbps) and itag 139 (~48 kbps), and Opus
# ones (itag 251 ~130 kbps, 250/249 smaller) in WebM. Each is taken as-is, so no level is
# re-encoded: the Opus stream only moves from WebM to an .opus (Ogg) file. The last
# fallbacks cover sites such as Instagram that often have no audio-only stream: the audio
# is then taken from the video.
AUDIO_FORMATS = {
    "high": "bestaudio[ext=m4a]/bestaudio/best",
    "low": "bestaudio[ext=m4a][abr<=64]/worstaudio[ext=m4a]/worstaudio/worst",
    "opus": "bestaudio[acodec=opus]/bestaudio/best",
}

# The file each audio level ends up as.
AUDIO_CODECS = {"high": "m4a", "low": "m4a", "opus": "opus"}


class LoginMissing(RuntimeError):
    pass


class InstagramCooldown(LoginMissing):
    """Instagram is refusing the account's requests for now; ytd waits it out."""


YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be",
}

# Per site: the cookie domain kept, the cookies that prove a login, the message when absent.
LOGINS = {
    "instagram": (
        "instagram.com", ({"sessionid"},),
        "ytd'nin Chrome penceresinde Instagram oturumu yok. “Giriş” düğmesiyle açılan "
        "pencerede instagram.com'a giriş yap, sonra tekrar dene.",
    ),
    # The same test yt-dlp makes: LOGIN_INFO plus one of the SAPISID cookies.
    "youtube": (
        "youtube.com", ({"LOGIN_INFO"}, {"SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"}),
        "Bu video YouTube hesabı istiyor ama ytd'nin Chrome penceresinde YouTube oturumu yok. "
        "“Giriş” düğmesiyle açılan pencerede YouTube'a giriş yap, sonra tekrar dene.",
    ),
}

# YouTube errors an account can fix: private, members-only, age-restricted, the bot check.
# yt-dlp ends most of them with "Use --cookies-from-browser or --cookies …".
YOUTUBE_LOGIN_HINTS = (
    "private video", "members-only", "join this channel", "sign in to confirm", "--cookies",
)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def needs_login(url: str) -> bool:
    """Instagram always wants the login; YouTube only for some videos (see with_login)."""
    return _host(url) in INSTAGRAM_HOSTS


def is_youtube(url: str) -> bool:
    return _host(url) in YOUTUBE_HOSTS


def wants_youtube_login(url: str, errors: list[str]) -> bool:
    return is_youtube(url) and any(
        hint in e.lower() for e in errors for hint in YOUTUBE_LOGIN_HINTS
    )


_jars: dict[str, YoutubeDLCookieJar] = {}
_jar_lock = threading.Lock()


def login_cookies(site: str) -> YoutubeDLCookieJar:
    """[site]'s cookies from the user's Chrome, kept in memory only and never
    written to disk. Chrome encrypts them with a key in the macOS keychain, so the
    first read asks for permission; caching keeps that to once per process (the
    server exits when idle, so a fresh read follows the next start). Only the
    site's own cookies are kept; every other site's cookies are dropped."""
    domain, required, missing = LOGINS[site]
    with _jar_lock:
        if site not in _jars:
            if not Path(CHROME_PROFILE).exists():
                raise LoginMissing(missing)
            try:
                everything = extract_cookies_from_browser("chrome", CHROME_PROFILE)
            except Exception as exc:  # noqa: BLE001 - the reason goes to the UI as text
                raise LoginMissing(f"Chrome çerezleri okunamadı: {exc}") from exc
            jar = YoutubeDLCookieJar()
            for cookie in everything:
                if cookie.domain.lstrip(".").endswith(domain):
                    jar.set_cookie(cookie)
            have = {c.name for c in jar if c.value}
            if not all(have & names for names in required):
                raise LoginMissing(missing)
            _jars[site] = jar
        return _jars[site]


def open_login_window() -> None:
    """ytd's Chrome profile in its own window, on the YouTube and Instagram sign-in pages."""
    LOGIN_PROFILE.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["open", "-na", "Google Chrome", "--args", f"--user-data-dir={LOGIN_PROFILE}",
         "--no-first-run", "--no-default-browser-check", *LOGIN_PAGES],
        check=False,
    )
    forget_login_cookies()  # the next try reads the fresh login


def forget_login_cookies() -> None:
    with _jar_lock:
        _jars.clear()


class Downloader(yt_dlp.YoutubeDL):
    """YoutubeDL that keeps the errors it reported (with ignoreerrors they are
    not raised) and can take a ready cookie jar instead of loading one itself."""

    def __init__(self, params: dict, cookiejar: YoutubeDLCookieJar | None = None):
        self._given_jar = cookiejar
        self.errors: list[str] = []
        super().__init__(params)

    @functools.cached_property
    def cookiejar(self):
        return self._given_jar if self._given_jar is not None else super().cookiejar

    def report_error(self, message, *args, **kwargs):
        self.errors.append(str(message))
        return super().report_error(message, *args, **kwargs)


_login_urls: set[str] = set()

# Instagram errors an account can fix (the post is only served to signed-in users).
INSTAGRAM_LOGIN_HINTS = (
    "empty media response", "login required", "log in", "--cookies",
    "isn't available to everyone", "rate-limit reached",
)
# What Instagram sends the account once it has had enough automated requests:
# an empty or HTML page where JSON should be, 429s, a checkpoint.
INSTAGRAM_BLOCK_HINTS = (
    "failed to parse json", "please wait a few minutes", "429", "too many requests",
    "checkpoint", "challenge_required", "feedback_required",
)


def wants_instagram_login(url: str, errors: list[str]) -> bool:
    return needs_login(url) and any(
        hint in e.lower() for e in errors for hint in INSTAGRAM_LOGIN_HINTS
    )


class _InstagramAccountGate:
    """Paces the requests made with the Instagram account and remembers, across
    runs, when Instagram last pushed back. Signed-in requests are what Instagram
    rations; anonymous ones never pass through here.

    - at most PER_HOUR signed-in actions in any 60 minutes, at least GAP seconds apart;
    - once Instagram refuses the account, no signed-in request for COOLDOWN hours
      (each further try would only extend its block), anonymous ones still go."""

    PER_HOUR = 30
    GAP = 8.0
    COOLDOWN = 3 * 3600
    STATE = LOGIN_PROFILE.parent / "instagram_account.json"

    def __init__(self):
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            return json.loads(self.STATE.read_text())
        except Exception:  # noqa: BLE001 - missing or broken state means a fresh start
            return {"recent": [], "blocked_until": 0}

    def _save(self, state: dict) -> None:
        self.STATE.parent.mkdir(parents=True, exist_ok=True)
        self.STATE.write_text(json.dumps(state))

    def _cooldown_message(self, until: float) -> str:
        left = max(1, round((until - time.time()) / 60))
        return (f"Instagram hesabın isteklerini geçici olarak kesti; ytd hesabı yormamak için "
                f"{left} dakika hesapla istek atmayacak. Hesapsız açılan gönderiler bu sürede "
                f"de iner. Telefonda Instagram bir uyarı/doğrulama gösteriyorsa onaylamak "
                f"süreyi kısaltır.")

    def before(self) -> None:
        """Waits for the signed-in request's turn, or raises InstagramCooldown."""
        with self._lock:
            state = self._load()
            now = time.time()
            if state.get("blocked_until", 0) > now:
                raise InstagramCooldown(self._cooldown_message(state["blocked_until"]))
            recent = [t for t in state.get("recent", []) if now - t < 3600]
            wait = 0.0
            if len(recent) >= self.PER_HOUR:
                wait = recent[0] + 3600 - now
            if recent:
                wait = max(wait, recent[-1] + self.GAP - now)
            if wait > 0:
                time.sleep(wait)
            recent.append(time.time())
            state["recent"] = recent
            self._save(state)

    def after(self, errors: list[str]) -> None:
        """Starts the cooldown when the account's request was refused."""
        if any(h in e.lower() for e in errors for h in INSTAGRAM_BLOCK_HINTS):
            with self._lock:
                state = self._load()
                state["blocked_until"] = time.time() + self.COOLDOWN
                self._save(state)
            raise InstagramCooldown(self._cooldown_message(time.time() + self.COOLDOWN))


instagram_gate = _InstagramAccountGate()


def _with_instagram(url: str, opts: dict, action):
    """Anonymous first (most public reels and posts open without an account);
    the Chrome login only for what Instagram shows signed-in users alone, and
    then paced by instagram_gate."""
    if url not in _login_urls:
        try:
            with Downloader(opts) as ydl:
                result = action(ydl)
            if not wants_instagram_login(url, ydl.errors):
                return result
        except yt_dlp.utils.DownloadError as exc:
            if not wants_instagram_login(url, [str(exc)]):
                raise
        _login_urls.add(url)
    jar = login_cookies("instagram")
    instagram_gate.before()
    try:
        with Downloader(opts, jar) as ydl:
            result = action(ydl)
        instagram_gate.after(ydl.errors)
        return result
    except yt_dlp.utils.DownloadError as exc:
        instagram_gate.after([str(exc)])
        raise


def with_login(url: str, opts: dict, action):
    """action(ydl) for [url]. Both sites are tried without any cookies first, so
    public videos never touch the account. YouTube gets the Chrome login only
    when it asks for an account (private or members-only video, age check);
    Instagram only for posts it shows signed-in users alone, and then paced so
    the account is not cut off (see _InstagramAccountGate)."""
    if needs_login(url):
        return _with_instagram(url, opts, action)
    if url not in _login_urls:
        try:
            with Downloader(opts) as ydl:
                result = action(ydl)
            if not wants_youtube_login(url, ydl.errors):
                return result
        except yt_dlp.utils.DownloadError as exc:
            if not wants_youtube_login(url, [str(exc)]):
                raise
        _login_urls.add(url)  # later steps for this video skip the anonymous try
    with Downloader(opts, login_cookies("youtube")) as ydl:
        return action(ydl)


def build_options(
    out_dir: Path, audio_only: bool = False, max_height: int = 1080, audio_quality: str = "high",
    video_codec: str = "av1",
) -> dict:
    out_dir = Path(out_dir).expanduser()
    # The low-quality copy gets its own name and archive so it never collides with the high one.
    suffix = " (low)" if audio_only and audio_quality == "low" else ""
    archive = (
        f".archive-audio-{audio_quality}.txt" if audio_only
        else ".archive-video-av1.txt" if video_codec == "av1"
        else ".archive-video.txt"
    )
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
                {"key": "FFmpegExtractAudio", "preferredcodec": AUDIO_CODECS[audio_quality]},
                {"key": "FFmpegMetadata"},
                {"key": "EmbedThumbnail"},
            ],
            writethumbnail=True,
        )
    elif video_codec == "av1":
        h = int(max_height)
        opts.update(
            # The efficient pick: AV1 (else VP9) with the Opus audio, both as YouTube
            # serves them — about a third of H.264's size at the same height. MKV holds
            # them as they are; plays in IINA / VLC / Chrome (not every QuickTime).
            format=(
                f"bv*[height<={h}][vcodec^=av01]+ba[acodec=opus]/"
                f"bv*[height<={h}][vcodec^=vp09]+ba[acodec=opus]/"
                f"bv*[height<={h}]+ba/b[height<={h}]/b"
            ),
            merge_output_format="mkv",
            postprocessors=[{"key": "FFmpegMetadata"}],
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
    parser = argparse.ArgumentParser(description="Download YouTube or Instagram videos or audio.")
    parser.add_argument("urls", nargs="*")
    parser.add_argument("--login", action="store_true",
                        help="open ytd's Chrome window to sign in to YouTube / Instagram")
    parser.add_argument("-a", "--audio", action="store_true", help="audio only (m4a)")
    parser.add_argument("--low", action="store_true", help="smaller audio (~48 kbps)")
    parser.add_argument("--opus", action="store_true", help="audio as Opus (~130 kbps, not re-encoded)")
    parser.add_argument("-q", "--quality", type=int, default=1080, help="max video height")
    parser.add_argument("--h264", action="store_true",
                        help="video as H.264 + AAC in MP4 (plays in QuickTime; at most 1080p). "
                             "Default: AV1 (else VP9) + Opus in MKV, half the size, up to 2160p")
    parser.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT, help="output folder")
    args = parser.parse_args()
    if args.login:
        open_login_window()
        return
    if not args.urls:
        parser.error("a URL is needed (or --login)")

    out = args.out.expanduser()
    out.mkdir(parents=True, exist_ok=True)
    level = "opus" if args.opus else "low" if args.low else "high"
    opts = build_options(out, args.audio or args.opus, args.quality, level,
                         "h264" if args.h264 else "av1")
    code = 0
    for url in args.urls:
        code = with_login(url, opts, lambda ydl: ydl.download([url])) or code
    raise SystemExit(code)


if __name__ == "__main__":
    main()
