"""DOWNI Web — /api/info
POST { "url": "..." }  →  { ok, title, thumbnail, duration, uploader, platform, formats[] }
GET  ?health=1         →  { ok, service }

Protected by X-Downi-Web: 1 header.
"""

import json
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler
from yt_dlp import YoutubeDL

_HDR_KEY = "X-Downi-Web"
_HDR_VAL = "1"

# --- Per-IP sliding-window rate limiter --------------------------------
# Vercel Python functions are ephemeral, so this window is per warm
# instance, not global. It blunts single-source hammering and scraping;
# a shared store (e.g. Upstash Redis) or Vercel WAF is the next step if
# distributed abuse from rotating IPs shows up.
_RATE_LIMIT = 30       # requests per window per IP
_RATE_WINDOW = 60.0    # seconds
_MAX_TRACKED_IPS = 10000
_rate_lock = threading.Lock()
_rate_hits = defaultdict(deque)


def _client_ip(handler) -> str:
    for header in ("x-forwarded-for", "x-real-ip", "x-vercel-forwarded-for"):
        value = handler.headers.get(header)
        if value:
            return value.split(",")[0].strip()
    return "unknown"


def _rate_limit_allow(handler) -> bool:
    ip = _client_ip(handler)
    now = time.monotonic()
    with _rate_lock:
        if len(_rate_hits) > _MAX_TRACKED_IPS:
            stale = [k for k, v in _rate_hits.items() if not v or now - v[-1] > _RATE_WINDOW]
            for k in stale:
                del _rate_hits[k]
        hits = _rate_hits[ip]
        while hits and now - hits[0] > _RATE_WINDOW:
            hits.popleft()
        if len(hits) >= _RATE_LIMIT:
            return False
        hits.append(now)
        return True

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
}


def _detect_platform(url: str) -> str:
    for name, pattern in _PLATFORM_RE.items():
        if re.search(pattern, url, re.I):
            return name
    return "web"


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


def _tiktok_fetch(url: str) -> dict:
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
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


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

    def _json(self, code: int, data: dict, extra_headers=None):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", f"Content-Type, {_HDR_KEY}")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._json(200, {"ok": True})

    def do_GET(self):
        self._json(200, {"ok": True, "service": "downi-web-info", "version": "2.6.3"})

    def do_POST(self):
        # Auth check
        if self.headers.get(_HDR_KEY) != _HDR_VAL:
            self._json(403, {"ok": False, "error": "Forbidden"})
            return

        # Per-IP rate limit
        if not _rate_limit_allow(self):
            self._json(
                429,
                {"ok": False, "error": "Too many requests. Wait a minute and try again."},
                extra_headers={"Retry-After": "60"},
            )
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b"{}"
            data = json.loads(body.decode() or "{}")

            url = (data.get("url") or "").strip()
            if not url.startswith("http"):
                self._json(400, {"ok": False, "error": "Please paste a valid video link starting with https://"})
                return

            platform = _detect_platform(url)
            if platform == "youtube":
                self._json(502, {"ok": False, "error": _YOUTUBE_MSG})
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
            msg = str(exc)[:350]
            # Friendly error messages
            if "private" in msg.lower() or "login" in msg.lower():
                msg = "This video is private or requires login — it can't be downloaded."
            elif "not available" in msg.lower():
                msg = "This video is not available in your region or has been removed."
            elif "unsupported url" in msg.lower():
                msg = "This link isn't supported yet. Try YouTube, Instagram, TikTok, or Twitter."
            self._json(502, {"ok": False, "error": msg})


handler = Handler
