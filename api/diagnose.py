"""DOWNI Web — /api/diagnose

Mirrors the Android app's engine diagnostics (downloader.py → diagnose()):
probe the checks that actually matter for server-side extraction and report
exactly what is reachable from this deployment, so a failure is never silent.

GET → { ok, results: [{ check, ok, ms, error }], yt_dlp, impersonate }
Protected by X-Downi-Web: 1 header.
"""

import json
import time
import importlib.util
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler
from yt_dlp import YoutubeDL

_HDR_KEY = "X-Downi-Web"
_HDR_VAL = "1"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Public probe posts, kept current. A deleted probe must not report a healthy
# engine as broken — each check reports its own error text verbatim instead of
# guessing, so the panel always tells the truth about this deployment.
_PROBES = (
    ("Pinterest extraction", "https://www.pinterest.com/pin/my-favourite-video-of-the-day-in-2025--17592254792009169/"),
    ("Instagram extraction", "https://www.instagram.com/reel/DcZTAe4jKBp"),
    ("Facebook extraction", "https://www.facebook.com/gov.sg/videos/10154383743583686/"),
)


def _impersonate_available() -> bool:
    try:
        return bool(importlib.util.find_spec("curl_cffi"))
    except Exception:
        return False


def _ydl_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "http_headers": {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
    }
    if _impersonate_available():
        opts["impersonate"] = "chrome"
    return opts


def _extract(url: str):
    with YoutubeDL(_ydl_opts()) as ydl:
        return ydl.extract_info(url, download=False)


def _downloadable(url: str):
    """Metadata alone can't prove the download path works — resolve a stream."""
    info = _extract(url)
    with YoutubeDL({**_ydl_opts(), "format": "b/best"}) as ydl:
        picked = ydl.extract_info(url, download=False)
    if not (picked.get("url") or picked.get("formats")):
        raise RuntimeError("no playable stream resolved")
    return info


def _head(url: str):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": _UA})
    urllib.request.urlopen(req, timeout=10).read(0)


def _tiktok_api():
    data = urllib.parse.urlencode(
        {"url": "https://www.tiktok.com/@tiktok/video/7106594312292453675", "hd": 1}
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://www.tikwm.com/api/",
        data=data,
        headers={"User-Agent": _UA, "Referer": "https://www.tikwm.com/"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        json.loads(resp.read().decode("utf-8", errors="ignore"))


def _xfxtwitter():
    req = urllib.request.Request(
        "https://api.fxtwitter.com/status/719944021058060289",
        headers={"User-Agent": _UA, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        json.loads(resp.read().decode("utf-8", errors="ignore"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", f"Content-Type, {_HDR_KEY}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._json(200, {"ok": True})

    def do_GET(self):
        if self.headers.get(_HDR_KEY) != _HDR_VAL:
            self._json(403, {"ok": False, "error": "Forbidden"})
            return

        results = []

        def check(name, fn):
            t0 = time.time()
            try:
                fn()
                results.append({"check": name, "ok": True, "ms": int((time.time() - t0) * 1000), "error": ""})
            except Exception as exc:
                results.append({"check": name, "ok": False, "ms": int((time.time() - t0) * 1000), "error": str(exc)[:180]})

        check("Internet reachability", lambda: _head("https://www.google.com/generate_204"))
        check("Engine download path", lambda: _downloadable(_PROBES[0][1]))
        for name, url in _PROBES[1:]:
            check(name, lambda u=url: _extract(u))
        check("TikTok mirror (tikwm)", _tiktok_api)
        check("X mirror (fxtwitter)", _xfxtwitter)

        try:
            from yt_dlp.version import __version__ as ydl_version
        except Exception:
            ydl_version = "unknown"

        self._json(200, {
            "ok": True,
            "results": results,
            "yt_dlp": ydl_version,
            "impersonate": _impersonate_available(),
        })


handler = Handler
