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
- **Standard downloads**: files go straight to the browser's default download folder — no save dialog, no folder picking, every browser. iOS Safari saves natively to Files → Downloads through short-lived signed links.
- **Offline-ready PWA**: installable, light/dark theme (system default), local icons/fonts/runtime cached by a service worker.
- **Honest limits**: max proxied file 300 MB; Vercel function limits apply (300 s max duration).

## 🔗 APK parity

The web app is a true sibling of the Android app, not a lookalike:

- **Same design system** — identical tokens, local Tailwind build, fonts and icon language, the same `Grab / Queue / Vault / Settings` navigation, and the same components: full-screen in-app player (double-tap seek, speed, swipe-down), network diagnostics panel, accent picker (cyan / violet / emerald / rose), AMOLED black, vault layout + search + sort, toast actions with **Undo**, last-grab chip, clipboard thumbnail preview, and honest quality notes.
- **Same engine** — yt-dlp 2026.8.19 driven by the Android app's lane cascade (see `downloader.py` in the mobile repo), with one server-side twist: lanes prefer progressive HTTP files and refuse HLS manifests, because the phone downloads HLS natively while this proxy streams a single URL.
- **Same honest errors** — one plain sentence per failure (private / login-walled / removed / no video), never raw extractor logs.
- **`/api/diagnose`** mirrors the app's `diagnose()`: reachability, engine download path, Instagram/Facebook extraction, TikTok mirror, X mirror, and an honest *optional* TLS-impersonation probe.
- **Web-only by necessity** — standard browser downloads (straight to the default folder, no picker), signed iOS download links, PWA install, and no APK self-updater (a browser can't install APKs). DowniDrop (Android's share-to-DOWNI) stays an app-only feature.

## 🛡️ Security & abuse limits

- Platform allow-list + HTTPS-only direct links + private-network (SSRF) block on both endpoints — this protects the server from abuse targets, it never limits your users.
- The `X-Downi-Web` header is a *soft* anti-scrape signal, not a secret (it is visible in the page source). The signed iOS download links (`/api/stream?t=…`, HMAC-signed, 30-minute expiry) are the same class of soft signal — the platform allow-list and SSRF checks that ran when the token was minted are the real guards.
- No rate limiting, by design — every download is welcome.

## 🛠️ Deploy to Vercel

1. Push this repository to GitHub (`MansoorjAhmad/downi-web`).
2. Go to [Vercel Dashboard](https://vercel.com/new) → Import the repository.
3. Set the domain in Settings → Domains to `getdowni.vercel.app`.
4. Deploy — `/api/info` and `/api/stream` become Python functions automatically.

## 🔢 Versioning

The web app has its own semver line (currently **v2.1.1**), decoupled from the Android app's versions.

