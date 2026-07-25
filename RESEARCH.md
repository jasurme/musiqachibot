# Building a Music-Finder Telegram Bot (like @Oxangbot) — Research & Build Plan

> **Historical design note:** this captures early options, not the exact current
> implementation. `README.md`, `.env.example`, and `DEPLOY.md` are the operational
> source of truth. Never share bot tokens, cookies, or proxy credentials.

> Goal: recreate a bot that (1) finds music by name / artist / lyrics, (2) recognizes
> music from voice messages, videos, audio & video-notes (Shazam-style), and
> (3) downloads videos + their audio from Instagram, TikTok, YouTube.

---

## 1. Feature breakdown (from the reference bot)

| # | User sends | Bot does | Core tech |
|---|------------|----------|-----------|
| A | Text: song name / artist | Search → download full track → send as audio | Search API + `yt-dlp` + `ffmpeg` |
| B | Text: a lyric snippet | Lyrics search → identify song → download → send | Genius/Musixmatch + `yt-dlp` |
| C | Voice message / audio / video / video-note | Extract audio → **recognize** (fingerprint) → download full song → send | `ffmpeg` + AudD/ACRCloud/Shazamio |
| D | Instagram / TikTok / YouTube link | Download video **and** its music, send both | `yt-dlp` + `ffmpeg` |

All four features converge on **two shared building blocks**:
1. **A "find the audio" step** — turn "artist + title" into an actual audio file (searches YouTube via `yt-dlp` and downloads best audio).
2. **A "recognize" step** — turn a chunk of audio into "artist + title" (audio fingerprinting).

Build those two blocks well and every feature is a thin wrapper around them.

---

## 2. Architecture at a glance

```
              ┌─────────────────────────────────────────────┐
  Telegram ──►│  Bot process (aiogram 3, async)              │
   user       │   • routers: text / media / url             │
              │   • rate limiting, i18n (uz/ru/en)          │
              └───────┬───────────────┬──────────────┬──────┘
                      │               │              │
              ┌───────▼──────┐ ┌──────▼──────┐ ┌─────▼───────┐
              │ Search+DL    │ │ Recognize   │ │ URL download│
              │ yt-dlp+ffmpeg│ │ AudD/ACR/   │ │ yt-dlp      │
              │              │ │ Shazamio    │ │             │
              └───────┬──────┘ └──────┬──────┘ └─────┬───────┘
                      │               │              │
              ┌───────▼───────────────▼──────────────▼───────┐
              │  Cache (SQLite/Postgres + file cache)          │
              │  + Local Bot API (>20 MB in / >50 MB out)      │
              └────────────────────────────────────────────────┘
```

Everything runs on **one VPS**. This is *not* a serverless-friendly workload —
downloads are long-running, need `ffmpeg`, disk space, and a persistent process.

---

## 3. Tech-stack decision (with rationale)

| Concern | Choice | Why |
|---------|--------|-----|
| Language | **Python 3.12** (not 3.14) | Every library below has wheels for 3.12; 3.14 is brand-new and `yt-dlp`/audio libs may lag. Use `pyenv`/venv. |
| Bot framework | **aiogram 3.x** | Fully async → handles many concurrent downloads/recognitions cleanly. Ideal when each request does slow I/O. `python-telegram-bot` is fine too but aiogram is the standard for this bot type. |
| Downloading | **yt-dlp** | Supports YouTube, TikTok, Instagram + 1800 sites; actively maintained (daily updates). |
| Audio processing | **ffmpeg** (system binary) | Extract audio from video, trim samples, convert to mp3, tag. |
| Recognition | **Shazamio** (free) → **AudD**/**ACRCloud** (paid, production) | Start free, upgrade for reliability/volume. |
| Metadata / lyrics | **Spotify Web API** + **Genius API** | Spotify = clean title/artist/cover search; Genius = lyric search. Both free. |
| Big files (>50 MB) | **Local Bot API server** (`telegram-bot-api` in Docker) | Allows unlimited bot downloads and uploads up to **2000 MB**. |
| Storage/cache | **SQLite** (start) → **Postgres** (scale) | Cache `file_id`s so re-sending a known song is instant & free. |
| Task queue (later) | **Redis + arq/Celery** | Offload heavy downloads so the bot loop stays responsive at scale. |
| Deploy | **VPS** (Ubuntu) + **systemd** or **Docker Compose** | Persistent process, cron for cleanup. |

---

## 4. The single most important constraint: Telegram file limits

The **standard** Bot API is capped:
- **Download** a file the user sent → **max 20 MB**
- **Upload** a file to the user → **max 50 MB**

That breaks feature C (a user's video could be >20 MB) and feature D (a YouTube
video is often >50 MB).

**Fix: run your own Local Bot API server.** Self-hosting `telegram-bot-api`
allows downloads without a size limit and uploads up to **2000 MB**. You point aiogram at `http://localhost:8081`
instead of `api.telegram.org`.

Steps:
1. Get `api_id` + `api_hash` from https://my.telegram.org (separate from the BotFather token).
2. Run the server (Docker image `aiogram/telegram-bot-api` or build `tdlib/telegram-bot-api`).
3. In aiogram, set the session base URL to your local server.

Do this **early** — retrofitting it later means re-testing every file path.

---

## 5. Feature-by-feature implementation

### Feature A/B — find music by name, artist, or lyrics

Flow:
```
text ──► classify (is it a URL? a lyric? a title?) 
     ──► resolve to canonical "artist – title"
           • title/artist  → Spotify search API (best match, gets cover art)
           • lyric snippet  → Genius search API → song's artist+title
     ──► yt-dlp "ytsearch1:<artist title audio>"  (download best audio)
     ──► ffmpeg → mp3, embed cover + ID3 tags
     ──► send_audio(...)  → cache the returned file_id by (artist,title)
```
Notes:
- **Where the audio comes from:** these bots almost always pull audio from
  **YouTube** via `yt-dlp` search (`ytsearch1:`), then convert to mp3. There is no
  legal public "download the studio track" API — Spotify/Apple only stream.
- **Cache aggressively:** once you've sent a song, store Telegram's `file_id`.
  Next time anyone asks for it, resend the `file_id` — no download, instant, free.

### Feature C — recognize music (the Shazam feature)

Flow:
```
voice / audio / video / video_note
   ──► getFile → download (local Bot API if >20 MB)
   ──► ffmpeg: extract/normalize a 10–20 s audio sample (mono, 16k/44.1k)
   ──► send sample to recognition API
   ──► get {artist, title}
   ──► reuse Feature A's download step → send full track
```
Recognition options (pick one, keep the interface swappable):

| Service | Cost | Auth | Notes |
|---------|------|------|-------|
| **Shazamio** (Python lib, unofficial Shazam) | Free | none | Great to start; unofficial → can break, rate-limited, no SLA. |
| **AudD** | 300 free reqs, then **$5 / 1000** | just an API token | Simplest API. Accepts a **direct URL** *or* file upload; `return=spotify,apple_music`. ~160 M song DB. |
| **ACRCloud** | Tiered (free dev tier) | HMAC signing | Bigger for broadcast/custom DBs; slightly more setup. |

Do not pass a Telegram file URL to a third party: it contains the bot token.
Download locally, trim a short sample, and upload only that sample to AudD.

### Feature D — download from Instagram / TikTok / YouTube

Flow:
```
url ──► yt-dlp detects the extractor automatically
    ──► download video (best mp4 ≤ target size)
    ──► also extract audio → mp3 (the "and its music" part)
    ──► send_video(video) + send_audio(mp3)
```
Real-world gotchas:
- **Instagram** frequently blocks datacenter IPs and may need **cookies**
  (`--cookies cookies.txt`) or a residential **proxy**. Plan for this.
- **TikTok** watermark vs no-watermark — yt-dlp handles most cases; verify format.
- **Size:** enforce a max (e.g. `-f "best[filesize<1900M]"`) and rely on the
  local Bot API server for the 50 MB→2 GB headroom.
- **Age/region-locked YouTube** → cookies again.
- Keep `yt-dlp` current through reviewed, pinned upgrades followed by offline,
  network and container smoke tests; do not mutate production dependencies at startup.

---

## 6. External accounts / API keys you'll need

| Service | Purpose | Free tier? | Where |
|---------|---------|-----------|-------|
| **BotFather** | Bot token | Yes | Telegram @BotFather |
| **my.telegram.org** | `api_id`/`api_hash` for local Bot API server | Yes | https://my.telegram.org |
| **Spotify Developer** | Search metadata + cover art | Yes | developer.spotify.com |
| **Genius** | Lyric search | Yes | genius.com/developers |
| **AudD** *(or ACRCloud)* | Music recognition (production) | 300 free / dev tier | audd.io / acrcloud.com |
| Proxy / residential IP *(maybe)* | Instagram/YouTube reliability | Paid | any proxy provider |

You can build the **entire MVP with zero paid keys** using Shazamio + Spotify +
Genius + yt-dlp. Add AudD when you need reliable recognition at volume.

---

## 7. Step-by-step build order (do them in this sequence)

1. **Skeleton bot** — aiogram 3, `/start` sends the welcome menu (like the screenshots), echo handler. Confirm the token works.
2. **Local Bot API server** — stand it up in Docker, point aiogram at it, verify you can receive a >20 MB file.
3. **Feature D (URL download)** — easiest visible win. Detect URLs, `yt-dlp` download video+audio, send back. Add size guards.
4. **Feature A (name/artist search)** — Spotify search → `yt-dlp ytsearch1` → mp3 → send. Add the `file_id` cache.
5. **Feature B (lyrics)** — Genius search → feed result into Feature A's pipeline.
6. **Feature C (recognition)** — ffmpeg sample extraction → Shazamio → reuse Feature A to deliver the full track. Handle voice/audio/video/video-note types.
7. **Hardening** — rate limiting, per-user throttling, error messages (uz/ru/en), logging, temp-file cleanup, DB cache, admin/stats, reviewed `yt-dlp` upgrades.
8. **Scale (only if needed)** — move heavy work to a Redis/arq worker queue; Postgres; multiple workers.

---

## 8. Suggested project layout

```
musiqa_bot/
├─ bot/
│  ├─ __init__.py
│  ├─ main.py                # aiogram entrypoint, dispatcher, local-API session
│  ├─ config.py              # env vars: BOT_TOKEN, API_ID, keys...
│  ├─ handlers/
│  │  ├─ start.py            # /start welcome menu
│  │  ├─ text_search.py      # feature A/B
│  │  ├─ media_recognize.py  # feature C
│  │  └─ url_download.py      # feature D
│  ├─ services/
│  │  ├─ search.py           # Spotify + Genius resolve
│  │  ├─ downloader.py       # yt-dlp wrapper (audio + video)
│  │  ├─ recognizer.py       # swappable: shazamio | audd | acrcloud
│  │  └─ audio.py            # ffmpeg helpers (sample, convert, tag)
│  ├─ db/
│  │  ├─ models.py           # song cache, users
│  │  └─ cache.py            # file_id lookup
│  └─ middlewares/           # throttling, i18n, logging
├─ locales/                  # uz / ru / en strings
├─ requirements.txt
├─ .env.example              # never commit real .env
├─ docker-compose.yml        # bot + local telegram-bot-api + (redis/postgres)
└─ README.md
```

Historical starter dependency sketch (do not copy for deployment; the current
reviewed pins and EJS support are in `requirements.txt`):
```
aiogram
yt-dlp[default]  # also requires an external JS runtime such as Deno
shazamio
spotipy            # Spotify Web API client
lyricsgenius       # Genius API client
mutagen            # ID3 tagging
aiohttp
python-dotenv
# ffmpeg is a SYSTEM package, not pip: `brew install ffmpeg` / `apt install ffmpeg`
```

---

## 9. Deployment

- **VPS** (Ubuntu 22.04, ≥2 GB RAM, generous disk for temp files). Serverless
  won't work well (long downloads, ffmpeg, local Bot API server, persistent process).
- A future scaled deployment could use separate bot, Local Bot API, Redis and
  Postgres services. The repository's current Compose file only starts the
  host-local Telegram Bot API service.
- Run the bot under **systemd** or Docker `restart: always`.
- **Webhook vs polling**: start with **long polling** (simplest, no public HTTPS
  needed). Switch to webhooks only at higher volume.
- **Maintenance**: verify temp cleanup and schedule reviewed dependency updates.

---

## 10. Legal / ToS reality check (read before shipping)

- Downloading from YouTube/Instagram/TikTok generally **violates their ToS**, and
  redistributing copyrighted music can infringe copyright. Many similar bots
  operate in a grey area and can be taken down.
- Personal or educational use is not a blanket legal exception. Understand the
  rules and legal exposure in the relevant jurisdiction before operating the bot;
  public services can be restricted or removed after abuse/DMCA complaints.
- Recognition APIs (AudD/ACRCloud) are fully legitimate to use.
- Keep API keys and cookies **out of git** (`.env`, secrets manager).

---

## 11. Rough monthly cost

| Item | Cost |
|------|------|
| VPS | ~$5–20 |
| Recognition (Shazamio) | $0 |
| Recognition (AudD, if used) | $5 per 1,000 recognitions |
| Spotify / Genius | $0 |
| Proxy (only if Instagram/YT need it) | ~$0–30 |
| **MVP total** | **~$5–20/mo** |

---

## 12. Decisions (locked 2026-07-19)

1. **Languages** — **UZ + RU + EN**. Full i18n from the start (`locales/` with uz/ru/en). Default uz; let users switch, and/or auto-pick from Telegram `language_code`.
2. **Recognition provider** — **Shazamio (free)** for the MVP, behind a swappable `recognizer` interface so we can drop in AudD later without touching handlers.
3. **Audio source** — YouTube via `yt-dlp` (`ytsearch1:`) — the practical default.
4. **Hosting** — **Local first**: develop on the Mac now, deploy to a VPS later.
   - ⚠️ Even locally, the **Local Bot API server** is still required for files >20/50 MB — run it in Docker on the Mac too (Steps 2). Small-file features work without it, so we can defer it until Feature D/C if desired.
   - Use **Python 3.12 via venv/pyenv** for dev (not the system 3.14).
5. **Scale** — single process to start (long polling); add Redis/arq worker queue only if volume demands it.

---

### Current next step
Deploy the tested Docker image, configure Railway secrets directly, and run a
small production-origin canary. Never share the BotFather token, cookies or proxy
credentials in chat.

*Sources consulted:* aiogram docs & PyPI; AudD docs (`docs.audd.io`) & AudD-vs-ACRCloud;
yt-dlp guides; Telegram Bot API + local `telegram-bot-api` (tdlib); Genius/Musixmatch/Spotify bot examples.
