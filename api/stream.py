"""DOWNI Web — /api/stream
POST { "url": "...", "formatId": "720" }
→  Proxies the video/audio file with Content-Disposition so browser saves it.

Protected by X-Downi-Web: 1 header.
Falls back to a JSON { cdnUrl, filename } response for iOS Safari (caller opens it).
"""

import json
import re
import time
import ipaddress
import urllib.parse
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler
from yt_dlp import YoutubeDL

_HDR_KEY = "X-Downi-Web"
_HDR_VAL = "1"
_CHUNK = 65536  # 64 KB

# Serverless responses are buffered with hard size/duration limits — huge
# files would die mid-transfer. Fail honestly instead.
_MAX_PROXY_BYTES = 300 * 1024 * 1024

_YDL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_FORMAT_SELECTORS = {
    "best":  "b/best",
    "1080":  "b[height<=1080]/b/best",
    "720":   "b[height<=720]/b/best",
    "480":   "b[height<=480]/b/best",
    "audio": "ba/b",
}

_MIME = {
    "mp4": "video/mp4",
    "webm": "video/webm",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "mkv": "video/x-matroska",
}

# Honest failure for YouTube — see the note in info.py. Client-spoofing
# extractor_args are banned (READ_THIS_BEFORE_UPGRADE.md).
_YOUTUBE_MSG = (
    "YouTube isn't supported on web yet. "
    "Use the DOWNI Android app for YouTube downloads — it extracts on-device."
)


def _is_youtube(url: str) -> bool:
    return bool(re.search(r"youtube\.com|youtu\.be", url, re.I))


# --- TikTok direct fast-path (ported from the Android app's proven core) ---
# tikwm.com gives HD no-watermark MP4 + MP3 audio; yt-dlp stays as fallback.

_TIKWM_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _is_tiktok(url: str) -> bool:
    return "tiktok.com" in url.lower()


def _expand_tiktok_shortlink(url: str) -> str:
    if not re.search(r"vt\.tiktok\.com|vm\.tiktok\.com|tiktok\.com/t/", url, re.I):
        return url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _TIKWM_UA})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.geturl()
    except Exception:
        return url


def _tiktok_fetch(url: str, _retried: bool = False) -> dict:
    data = urllib.parse.urlencode(
        {"url": url, "count": 12, "cursor": 0, "web": 1, "hd": 1}
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://www.tikwm.com/api/",
        data=data,
        headers={
            "User-Agent": _TIKWM_UA,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": "https://www.tikwm.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        parsed = json.loads(resp.read().decode("utf-8", errors="ignore"))
    # tikwm's free tier allows 1 request/second globally, and every web user
    # shares that one budget through this endpoint. Hitting the limit is not a
    # failure — wait out the window and retry exactly once.
    if (
        not _retried
        and parsed.get("code") != 0
        and "limit" in str(parsed.get("msg", "")).lower()
    ):
        time.sleep(1.3)
        return _tiktok_fetch(url, _retried=True)
    return parsed


def _tiktok_resolve(url: str, fmt_id: str):
    """Returns (cdn_url, ext, title, http_headers) via tikwm, app-core cascade."""
    is_audio = fmt_id.lower() in ("audio", "mp3", "m4a")
    data = _tiktok_fetch(_expand_tiktok_shortlink(url))
    if data.get("code") != 0:
        raise RuntimeError(data.get("msg") or "TikTok video not available")
    item = data.get("data") or {}
    stream_url = (
        item.get("music") if is_audio
        else (item.get("hdplay") or item.get("play") or item.get("wmplay"))
    )
    if not stream_url:
        raise RuntimeError("No downloadable stream found in TikTok response")
    if stream_url.startswith("/"):
        stream_url = urllib.parse.urljoin("https://www.tikwm.com", stream_url)

    title = item.get("title") or ("tiktok_" + str(item.get("id") or "video"))
    ext = "mp3" if is_audio else "mp4"
    # TikTok CDN requires a browser Referer — same header the Android app sends.
    headers = {"User-Agent": _TIKWM_UA, "Referer": "https://www.tiktok.com/"}
    return stream_url, ext, f"{_sanitize_filename(title)[:50]}-{item.get('id', 'video')}", headers


def _sanitize_filename(name: str) -> str:
    # Keep printable ASCII only: HTTP headers are latin-1, so a title with
    # emoji/curly quotes would crash Content-Disposition otherwise.
    name = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "", name or "video")
    name = re.sub(r"[^\x20-\x7E]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:80].strip() or "video"


# Plain media file links (.mp4/.m4a/...) are proxied directly — yt-dlp's
# generic extractor reports no codecs for them and _pick_muxed rejects them.
_DIRECT_MEDIA_RE = re.compile(
    r"\.(mp4|m4v|mov|webm|mkv|m4a|mp3|ogg|wav)(\?|$)", re.I
)


def _is_direct_media(url: str) -> bool:
    return bool(_DIRECT_MEDIA_RE.search(url.split("#")[0]))


# --- Abuse guard: allow-list supported platforms, block private networks ---
# This endpoint proxies arbitrary downloads, so an unrestricted `url` param
# would turn it into an open fetch proxy. Same policy as api/info.py.

_PLATFORM_HOSTS = (
    "youtube.com", "youtu.be",
    "instagram.com",
    "tiktok.com",
    "twitter.com", "x.com",
    "facebook.com", "fb.watch", "fb.gg",
    "reddit.com", "v.redd.it",
    "pinterest.com", "pin.it",
)


def _hostname(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _is_platform_host(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in _PLATFORM_HOSTS)


def _is_private_host(host: str) -> bool:
    if not host or host == "localhost" or host.endswith((".local", ".internal", ".lan")):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False  # normal hostname, not an IP literal


def _url_allowed(url: str) -> bool:
    host = _hostname(url)
    if _is_private_host(host):
        return False
    if _is_platform_host(host):
        return True
    # Direct media file links stay supported from any public HTTPS host.
    return url.lower().startswith("https://") and _is_direct_media(url)


# --- Facebook URL shapes + honest errors (mirrors api/info.py) -------------
# These two serverless functions can't share a module on this deploy target,
# so the helpers are intentionally duplicated — keep them in sync.

_PLATFORM_DETECT_RE = {
    "instagram": r"instagram\.com",
    "tiktok":    r"tiktok\.com",
    "twitter":   r"twitter\.com|x\.com",
    "facebook":  r"facebook\.com|fb\.watch",
    "reddit":    r"reddit\.com|v\.redd\.it",
    "pinterest": r"pinterest\.com|pin\.it",
}


def _detect_platform(url: str) -> str:
    for name, pattern in _PLATFORM_DETECT_RE.items():
        if re.search(pattern, url, re.I):
            return name
    return "web"


def _normalize_facebook(url: str) -> str:
    u = url.strip()
    u = re.sub(
        r"^(https?://)(?:m|mobile|web)\.facebook\.com",
        r"\1www.facebook.com", u, flags=re.I,
    )
    if re.search(r"fb\.watch/|facebook\.com/share/", u, re.I):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": _TIKWM_UA})
            with urllib.request.urlopen(req, timeout=10) as resp:
                u = resp.geturl() or u
        except Exception:
            pass  # keep the original; yt-dlp may still cope
    m = re.search(r"facebook\.com/watch/[^/?]+/(\d{6,})", u, re.I)
    if m:
        return f"https://www.facebook.com/watch/?v={m.group(1)}"
    return u


_BLOCK_MSGS = {
    "instagram": "Instagram is blocking web downloads right now. The free DOWNI Android app grabs Instagram on-device — no blocks.",
    "facebook": "Facebook is blocking web downloads right now. The free DOWNI Android app grabs Facebook videos on-device.",
    "twitter": "X is fighting web downloads right now. The free DOWNI Android app grabs it on-device.",
    "reddit": "Reddit is fighting web downloads right now. The free DOWNI Android app grabs it on-device.",
}
_BLOCK_HINTS = (
    "login", "logged-in", "cookies", "empty media", "rate-limit",
    "rate limit", "403", "forbidden", "blocked", "checkpoint",
)


def _friendly_error(raw: str, platform: str) -> str:
    msg = re.sub(r"^ERROR:\s*\[[^\]]*\]\s*[^:]*:\s*", "", (raw or "").strip())
    low = msg.lower()
    if "no video could be found" in low:
        return "No video found in this post — it may be a photo or text post."
    if "private" in low:
        return "This post is private — it can't be grabbed."
    if any(h in low for h in _BLOCK_HINTS):
        return _BLOCK_MSGS.get(
            platform,
            "This platform is blocking web downloads right now. The free DOWNI Android app grabs it on-device.",
        )
    if "not available" in low or "removed" in low or "404" in low:
        return "This post is unavailable in your region or has been removed."
    if "unsupported url" in low:
        return (
            "This link isn't supported on web. TikTok, Pinterest and direct "
            "media links work here; the DOWNI Android app handles the rest."
        )
    if "timed out" in low or "timeout" in low:
        return "The platform took too long to answer — try again in a moment."
    if len(msg) > 180:
        msg = msg[:180].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return msg or "This link could not be grabbed right now."


def _pick_muxed(info: dict):
    """Return (url, ext, http_headers) for the best muxed stream."""
    url = info.get("url")
    if url and (
        (info.get("vcodec") not in (None, "none") and info.get("acodec") not in (None, "none"))
        or not info.get("formats")
    ):
        return url, info.get("ext") or "mp4", info.get("http_headers") or {}

    muxed = [
        f for f in (info.get("formats") or [])
        if f.get("url")
        and f.get("vcodec") not in (None, "none")
        and f.get("acodec") not in (None, "none")
    ]
    if muxed:
        best = max(muxed, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        return best["url"], best.get("ext") or "mp4", best.get("http_headers") or {}

    # Tier 2: datacenter IPs sometimes make Instagram return DASH streams
    # (video and audio separate) with no muxed variant at all. A video-only
    # file beats a hard failure — no ffmpeg is available server-side to merge.
    video_only = [
        f for f in (info.get("formats") or [])
        if f.get("url") and f.get("vcodec") not in (None, "none")
    ]
    if video_only:
        best = max(video_only, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        return best["url"], best.get("ext") or "mp4", best.get("http_headers") or {}

    return None, None, {}


def _resolve(url: str, fmt_id: str):
    """Returns (cdn_url, ext, title, http_headers)."""
    selector = _FORMAT_SELECTORS.get(fmt_id.lower(), _FORMAT_SELECTORS["best"])
    is_audio = fmt_id.lower() in ("audio", "mp3", "m4a")

    attempts = [
        (selector, None),
        ("b/best", None),
    ]

    last_err = None
    for sel, extractor_args in attempts:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 15,
            "retries": 2,
            "format": sel,
            "http_headers": dict(_YDL_HEADERS),
        }
        if extractor_args:
            opts["extractor_args"] = extractor_args

        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:
            last_err = e
            continue

        title = info.get("title") or "video"

        if is_audio:
            cdn = info.get("url")
            ext = info.get("ext") or "m4a"
            hdrs = info.get("http_headers") or {}
            if cdn:
                return cdn, ext, title, hdrs
            # scan audio-only formats
            audio_fmts = [
                f for f in (info.get("formats") or [])
                if f.get("url") and f.get("acodec") not in (None, "none")
            ]
            if audio_fmts:
                best = max(audio_fmts, key=lambda f: f.get("tbr") or 0)
                return best["url"], best.get("ext") or "m4a", title, best.get("http_headers") or {}
            last_err = RuntimeError("no audio stream found")
            continue

        cdn, ext, hdrs = _pick_muxed(info)
        if cdn:
            return cdn, ext, title, hdrs
        last_err = RuntimeError("no muxed video+audio stream found")

    raise RuntimeError(str(last_err) or "Could not resolve this link")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", f"Content-Type, {_HDR_KEY}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._json(200, {"ok": True})

    def do_GET(self):
        self._json(200, {"ok": True, "service": "downi-web-stream"})

    def do_POST(self):
        if self.headers.get(_HDR_KEY) != _HDR_VAL:
            self._json(403, {"ok": False, "error": "Forbidden"})
            return

        response_started = False
        platform = "web"  # refined after URL parse; needed by the error handler
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b"{}"
            data = json.loads(body.decode() or "{}")

            url = (data.get("url") or "").strip()
            fmt_id = (data.get("formatId") or "best").strip()
            ios_mode = data.get("ios", False)  # if True, just return cdn URL for client to open

            if not url.startswith("http"):
                self._json(400, {"ok": False, "error": "Invalid URL"})
                return

            if not _url_allowed(url):
                self._json(400, {"ok": False, "error": "This link isn't supported on web. TikTok, Pinterest and direct media links work here; the DOWNI Android app handles YouTube, Instagram, Facebook & X."})
                return

            if _is_youtube(url):
                self._json(502, {"ok": False, "error": _YOUTUBE_MSG})
                return

            platform = _detect_platform(url)
            if platform == "facebook":
                url = _normalize_facebook(url)

            if _is_direct_media(url):
                # Plain media link: proxy it as-is, no extraction needed.
                path = url.split("?")[0].rstrip("/")
                ext = path.rsplit(".", 1)[-1].lower()
                title = _sanitize_filename(path.rsplit("/", 1)[-1].rsplit(".", 1)[0]) or "video"
                cdn_url, ext, title, cdn_headers = url, ext, title, {}
            elif _is_tiktok(url):
                try:
                    cdn_url, ext, title, cdn_headers = _tiktok_resolve(url, fmt_id)
                except Exception:
                    cdn_url, ext, title, cdn_headers = _resolve(url, fmt_id)  # yt-dlp fallback
            else:
                cdn_url, ext, title, cdn_headers = _resolve(url, fmt_id)

            filename = f"{_sanitize_filename(title)}.{ext}"
            content_type = _MIME.get(ext, "application/octet-stream")

            # iOS mode: return CDN URL for the browser to open directly
            if ios_mode:
                self._json(200, {
                    "ok": True,
                    "cdnUrl": cdn_url,
                    "filename": filename,
                    "ext": ext,
                })
                return

            # Proxy the file through the server
            req_headers = dict(_YDL_HEADERS)
            req_headers.update(cdn_headers)

            req = urllib.request.Request(cdn_url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=8) as resp:
                file_size = resp.headers.get("Content-Length", "")
                content_range = resp.headers.get("Content-Range", "")

                if file_size and int(file_size) > _MAX_PROXY_BYTES:
                    self._json(413, {
                        "ok": False,
                        "error": "This video is too large to download through the web app. Try a shorter clip, or use the DOWNI Android app.",
                    })
                    return

                response_started = True
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{filename}"'
                )
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store")
                if file_size:
                    self.send_header("Content-Length", file_size)
                if content_range:
                    self.send_header("Content-Range", content_range)
                self.end_headers()

                written = 0
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > _MAX_PROXY_BYTES:
                        # No length was advertised (or it lied) — cut the
                        # connection rather than buffering forever.
                        self.close_connection = True
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break

        except Exception as exc:
            if response_started:
                # Headers/body already went out; a second HTTP response would
                # corrupt the transfer. Dropping the connection is the only
                # honest signal (the client sees a truncated download).
                self._headers_buffer = []
                self.close_connection = True
                return
            self._json(502, {"ok": False, "error": _friendly_error(str(exc), platform)})


handler = Handler
