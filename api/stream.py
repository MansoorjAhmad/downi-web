"""DOWNI Web — /api/stream
POST { "url": "...", "formatId": "720" }
→  Proxies the video/audio file with Content-Disposition so browser saves it.

Protected by X-Downi-Web: 1 header.
Falls back to a JSON { cdnUrl, filename } response for iOS Safari (caller opens it).
"""

import json
import re
import threading
import time
import urllib.request
import urllib.error
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler
from yt_dlp import YoutubeDL

_HDR_KEY = "X-Downi-Web"
_HDR_VAL = "1"
_CHUNK = 65536  # 64 KB

# --- Per-IP sliding-window rate limiter --------------------------------
# Per warm instance (see note in info.py). Stream is the expensive path —
# it proxies whole files server-side — so its limit is tighter than info's.
_RATE_LIMIT = 10       # requests per window per IP
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


def _sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name or "video")
    return name[:80].strip() or "video"


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

    return None, None, {}


def _resolve(url: str, fmt_id: str):
    """Returns (cdn_url, ext, title, http_headers)."""
    selector = _FORMAT_SELECTORS.get(fmt_id.lower(), _FORMAT_SELECTORS["best"])
    is_audio = fmt_id.lower() in ("audio", "mp3", "m4a")

    attempts = [
        (selector, {"youtube": {"player_client": ["android_vr"]}}),
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
        self._json(200, {"ok": True, "service": "downi-web-stream"})

    def do_POST(self):
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
            fmt_id = (data.get("formatId") or "best").strip()
            ios_mode = data.get("ios", False)  # if True, just return cdn URL for client to open

            if not url.startswith("http"):
                self._json(400, {"ok": False, "error": "Invalid URL"})
                return

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

                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break

        except Exception as exc:
            msg = str(exc)[:350]
            self._json(502, {"ok": False, "error": msg})


handler = Handler
