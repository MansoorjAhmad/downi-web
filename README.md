# DOWNI Web ⚡

> **Grab any video. One tap. Zero clutter.**  
> Official web app for iOS, Android, and desktop browsers.

Live: **https://getdowni.vercel.app**

Vanilla JS + Tailwind (fully local — no CDN), an installable PWA with an offline app shell, and Python serverless endpoints powered by `yt-dlp`.

---

## 🚀 Features

- **Every major platform, server-side**: TikTok (HD, no watermark, fast mirror path with rate-limit retry), Instagram (public reels & posts), Facebook (public videos, share links normalized), X/Twitter (fast-path mirror → direct MP4), Pinterest, plus any public **direct media link** (HTTPS).
- **Honest about limits**: YouTube isn't available on web (client spoofing is permanently banned — see `READ_THIS_BEFORE_UPGRADE.md` in the Android repo; the free **DOWNI Android app** handles YouTube on-device). Instagram/Facebook/X posts that are private or login-walled can't be grabbed by any anonymous tool — you'll get one plain sentence saying so, never raw extractor logs.
- **Facebook link-shapes**: modern share links (`fb.watch`, `/share/v/…`, `m.facebook.com`, `/watch/<page>/<id>/`) are normalized to the canonical form before extraction.
- **Quality picker**: up to 1080p where a platform serves a muxed stream, plus 720p / 480p / audio-only.
- **Standard downloads**: files go straight to the browser's default download folder — no save dialog, no folder picking, every browser. A dedicated save sheet handles iOS.
- **Offline-ready PWA**: installable, light/dark theme (system default), local icons/fonts/runtime cached by a service worker.
- **Honest limits**: max proxied file 300 MB; Vercel function limits apply (300 s max duration).

## 🛡️ Security & abuse limits

- Platform allow-list + HTTPS-only direct links + private-network (SSRF) block on both endpoints — this protects the server from abuse targets, it never limits your users.
- The `X-Downi-Web` header is a *soft* anti-scrape signal, not a secret (it is visible in the page source).
- No rate limiting, by design — every download is welcome.

## 🛠️ Deploy to Vercel

1. Push this repository to GitHub (`MansoorjAhmad/downi-web`).
2. Go to [Vercel Dashboard](https://vercel.com/new) → Import the repository.
3. Set the domain in Settings → Domains to `getdowni.vercel.app`.
4. Deploy — `/api/info` and `/api/stream` become Python functions automatically.

## 🔢 Versioning

The web app has its own semver line (currently **v2.1.1**), decoupled from the Android app's versions.

