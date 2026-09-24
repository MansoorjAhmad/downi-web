"""DOWNI Web — /api/info
POST { "url": "..." }  →  { ok, title, thumbnail, duration, uploader, platform, formats[] }
GET  ?health=1         →  { ok, service }

Protected by X-Downi-Web: 1 header.
"""

import json
import re
import time
import ipaddress
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from yt_dlp import YoutubeDL

_HDR_KEY = "X-Downi-Web"
_HDR_VAL = "1"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_PLATFORM_RE = {
    "youtube":   r"youtube\.com|youtu\.be",
    "instagram": r"instagram\.com",
    "tiktok":    r"tiktok\.com",
    "twitter":   r"twitter\.com|x\.com",
    "facebook":  r"facebook\.com|fb\.watch",
    "reddit":    r"reddit\.com|v\.redd\.it",
    "pinterest": r"pinterest\.com|pin\.it",
}


def _detect_platform(url: str) -> str:
    for name, pattern in _PLATFORM_RE.items():
        if re.search(pattern, url, re.I):
            return name
    return "web"


# Plain media file links are answered without yt-dlp (the generic extractor
# reports no codec info for them and downstream muxed-picking rejects it).
_DIRECT_MEDIA_RE = re.compile(
    r"\.(mp4|m4v|mov|webm|mkv|m4a|mp3|ogg|wav)(\?|$)", re.I
)
_AUDIO_EXT = {"m4a", "mp3", "ogg", "wav"}


# --- Abuse guard: allow-list supported platforms, block private networks ---
# The previous build accepted ANY http(s) URL, which made this endpoint a free
# open proxy. Platform URLs are allow-listed; direct media files stay supported
# from any public HTTPS host, but loopback/private/link-local targets are
# refused (SSRF guard).

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
    return url.lower().startswith("https://") and bool(_DIRECT_MEDIA_RE.search(url.split("#")[0]))


def _direct_media_info(url: str) -> dict:
    path = url.split("?")[0].rstrip("/")
    ext = path.rsplit(".", 1)[-1].lower()
    raw_name = path.rsplit("/", 1)[-1].rsplit(".", 1)[0] or "video"
    is_audio = ext in _AUDIO_EXT
    return {
        "ok": True,
        "title": re.sub(r"[^\x20-\x7E]", " ", raw_name).strip() or "video",
        "thumbnail": "",
        "duration": 0,
        "uploader": "",
        "platform": "direct",
        "formats": [
            {"id": "audio", "label": "Audio File", "badge": ext.upper(), "ext": ext}
            if is_audio else
            {"id": "best", "label": "Direct Video File", "badge": ext.upper(), "ext": ext}
        ],
    }


def _get_info(url: str) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 15,
        "retries": 2,
        "skip_download": True,
        "http_headers": dict(_HEADERS),
    }
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


# Honest failure for YouTube: Google's bot check blocks yt-dlp from
# datacenter IPs, and client-spoofing extractor_args are banned
# (READ_THIS_BEFORE_UPGRADE.md — they broke the Android app the same way).
_YOUTUBE_MSG = (
    "YouTube isn't supported on web yet. "
    "Use the DOWNI Android app for YouTube downloads — it extracts on-device."
)


# --- TikTok direct fast-path (ported from the Android app's proven core) ---
# downloader.py uses tikwm.com for HD no-watermark video + MP3 audio without
# routing TikTok through yt-dlp. Same behavior here; yt-dlp stays as fallback.

_TIKWM_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


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


def _tiktok_info(url: str) -> dict:
    """Metadata response in the /api/info shape, or raises on failure."""
    data = _tiktok_fetch(_expand_tiktok_shortlink(url))
    if data.get("code") != 0:
        raise RuntimeError(data.get("msg") or "TikTok video not available")
    d = data.get("data") or {}
    cover = d.get("cover") or d.get("origin_cover") or ""
    if cover.startswith("/"):
        cover = urllib.parse.urljoin("https://www.tikwm.com", cover)
    author = d.get("author") or {}
    return {
        "ok": True,
        "title": d.get("title") or "TikTok Video",
        "thumbnail": cover,
        "duration": int(d.get("duration") or 0),
        "uploader": author.get("nickname") or author.get("unique_id") or "TikTok Creator",
        "platform": "tiktok",
        "formats": [
            {"id": "best", "label": "HD Video (No Watermark)", "badge": "HD", "ext": "mp4"},
            {"id": "audio", "label": "Audio Track (MP3)", "badge": "MP3", "ext": "mp3"},
        ],
    }


# --- Facebook URL shapes ---------------------------------------------------
# Facebook invents a new URL shape every few months; yt-dlp only groks the
# classics. Normalize to /watch?v=<id> where possible and expand share links.

def _normalize_facebook(url: str) -> str:
    u = url.strip()
    # m./mobile./web. subdomains → canonical www host
    u = re.sub(
        r"^(https?://)(?:m|mobile|web)\.facebook\.com",
        r"\1www.facebook.com", u, flags=re.I,
    )
    # Share/short links: follow redirects to the canonical target.
    if re.search(r"fb\.watch/|facebook\.com/share/", u, re.I):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": _TIKWM_UA})
            with urllib.request.urlopen(req, timeout=10) as resp:
                u = resp.geturl() or u
        except Exception:
            pass  # keep the original; yt-dlp may still cope
    # /watch/<page>/<id>/ → /watch?v=<id>  (the shape yt-dlp understands)
    m = re.search(r"facebook\.com/watch/[^/?]+/(\d{6,})", u, re.I)
    if m:
        return f"https://www.facebook.com/watch/?v={m.group(1)}"
    return u


# --- Honest errors ---------------------------------------------------------
# Raw yt-dlp output (extractor prefixes, CLI flags like --cookies-from-
# browser, FAQ links) must never reach a phone screen. Map the failure class
# to one human sentence; when a platform is blocking our datacenter IPs, say
# so and point to the on-device app — same doctrine as YouTube.

_BLOCK_MSGS = {
    "instagram": "Instagram is blocking web downloads right now. The free DOWNI Android app grabs Instagram on-device — no blocks.",
    "facebook": "Facebook is blocking web downloads right now. The free DOWNI Android app grabs Facebook videos on-device.",
    "twitter": "X is fighting web downloads right now. The free DOWNI Android app grabs it on-device.",
    "reddit": "Reddit is fighting web downloads right now. The free DOWNI Android app grabs it on-device.",
}
_BLOCK_HINTS = (
    "login", "logged-in", "cookies", "empty media", "rate-limit",
    "rate limit", "403", "forbidden", "blocked", "checkpoint",
    "cannot parse data",
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
    # Last resort: cleaned text, cut at a word boundary instead of mid-word.
    if len(msg) > 180:
        msg = msg[:180].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return msg or "This link could not be grabbed right now."


def _build_formats(info: dict) -> list:
    raw_formats = info.get("formats") or []

    # Collect available muxed heights
    heights = sorted(
        {
            f.get("height")
            for f in raw_formats
            if f.get("height")
            and f.get("vcodec") not in (None, "none")
            and f.get("acodec") not in (None, "none")
        },
        reverse=True,
    )

    candidates = [("best", "Best Quality", "BEST", "mp4")]
    for h, label, badge in [(1080, "1080p Full HD", "1080p"), (720, "720p HD", "720p"), (480, "480p", "480p")]:
        if any(hh >= h for hh in heights):
            candidates.append((str(h), label, badge, "mp4"))

    candidates.append(("audio", "Audio Only (MP3)", "MP3", "m4a"))

    seen = set()
    result = []
    for id_, label, badge, ext in candidates:
        if id_ not in seen:
            seen.add(id_)
            result.append({"id": id_, "label": label, "badge": badge, "ext": ext})
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # silence default logging

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
        self._json(200, {"ok": True, "service": "downi-web-info", "version": "2.1.1"})

    def do_POST(self):
        # Auth check
        if self.headers.get(_HDR_KEY) != _HDR_VAL:
            self._json(403, {"ok": False, "error": "Forbidden"})
            return

        platform = "web"  # may be refined below; needed by the error handler
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b"{}"
            data = json.loads(body.decode() or "{}")

            url = (data.get("url") or "").strip()
            if not url.startswith("http"):
                self._json(400, {"ok": False, "error": "Please paste a valid video link starting with https://"})
                return

            if not _url_allowed(url):
                self._json(400, {"ok": False, "error": "This link isn't supported on web. TikTok, Pinterest and direct media links work here; the DOWNI Android app handles YouTube, Instagram, Facebook & X."})
                return

            platform = _detect_platform(url)
            if platform == "youtube":
                self._json(502, {"ok": False, "error": _YOUTUBE_MSG})
                return

            if platform == "facebook":
                url = _normalize_facebook(url)

            if _DIRECT_MEDIA_RE.search(url.split("#")[0]):
                self._json(200, _direct_media_info(url))
                return

            if platform == "tiktok":
                try:
                    self._json(200, _tiktok_info(url))
                    return
                except Exception:
                    pass  # fall through to yt-dlp

            info = _get_info(url)
            formats = _build_formats(info)

            # Best thumbnail — prefer a reasonably sized one
            thumbnail = info.get("thumbnail") or ""
            thumbnails = info.get("thumbnails") or []
            if thumbnails:
                # Pick the highest-res thumbnail that has a URL
                best = max(
                    (t for t in thumbnails if t.get("url")),
                    key=lambda t: (t.get("width") or 0) * (t.get("height") or 0),
                    default=None,
                )
                if best:
                    thumbnail = best["url"]

            self._json(200, {
                "ok": True,
                "title": info.get("title") or "Video",
                "thumbnail": thumbnail,
                "duration": info.get("duration") or 0,
                "uploader": info.get("uploader") or info.get("channel") or "",
                "platform": platform,
                "formats": formats,
            })

        except Exception as exc:
            self._json(502, {"ok": False, "error": _friendly_error(str(exc), platform)})


handler = Handler
