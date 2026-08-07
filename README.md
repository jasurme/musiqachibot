# 🎵 Musiqa Bot

A Telegram music bot: find songs by name/artist, recognize music from
voice/video/audio (Shazam-style), and download videos + audio from Instagram,
TikTok, YouTube, Facebook and X. UZ / RU / EN.

See [RESEARCH.md](RESEARCH.md) for the full design and rationale.

## Quick start (local dev)

```bash
# 1. Install Python 3.12, ffmpeg and Deno, then create the venv
make setup          # or see commands below

# 2. Put your BotFather token in .env
#    BOT_TOKEN=123456:ABC...

# 3. Run
make run            # or: .venv/bin/python -m bot.main
```

Manual equivalent:
```bash
python3.12 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install --require-hashes -r requirements.lock
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m bot.main
```

`requirements.txt` is the human-maintained direct dependency list. Production
Docker builds install the fully pinned, hashed `requirements.lock`; regenerate
it deliberately with `uv pip compile requirements.txt --universal
--generate-hashes --output-file requirements.lock`, then rerun all smoke tests.

`ffmpeg` must be installed (`brew install ffmpeg`) for media processing. Current
YouTube support also needs [Deno](https://deno.com/) for yt-dlp's JavaScript
challenge solver (`brew install deno` on macOS). The Docker image includes both.

## Build status — all core features DONE ✅

- [x] **/start** welcome menu + language switch (uz / ru / en, persisted per user)
- [x] **Feature A** — name/artist → five direct results → tap → Telegram-ready M4A/MP3
- [x] **Lyrics** button for recognized tracks (lyrics.ovh, free/no-key)
- [x] **Feature C** — voice/audio/video/video-note → **recognize** (Shazamio) → album art + "Song title/Artist" header + results list + Lyrics/Video buttons
- [x] **Feature D** — social-media link → **quality picker** (360/480/720/1080/Audio)
- [x] **Confirmed admin video broadcast** — choose normal/circle, preview converted media, confirm, then fan out with one instant Top Music button
- [x] **Durable admin announcements** — immediate non-command messages and files resume from their SQLite cursor after a restart
- [x] **/top_music** — persisted Top Music chart with direct track buttons, refreshed off-path every 48 hours and announced automatically when it changes
- [x] **file_id/search/recognition caches** + bounded global/per-user concurrent jobs
- [x] Killable, concurrency-limited provider workers with byte/duration/deadline guards
- [x] Shazamio recognition with optional **AudD fallback** (`AUDD_TOKEN`)
- [x] Owner-bound callback sessions, 7/30-day retention, `/privacy`, and `/delete_my_data`
- [x] **Local Bot API server topology** — opt-in shared-volume Compose stack for incoming >20 MB or outgoing >50 MB

Search and media delivery use YouTube via yt-dlp; Shazamio needs no API key.
Provider behavior changes over time, so the pinned yt-dlp release and deployment
smoke test should be reviewed regularly.

## Tests

```bash
make test        # fast offline tests (dispatcher-level, network faked)
make test-net    # real-internet smoke tests (search/download/recognize/lyrics/Instagram)
# Make total YouTube blockage fail instead of skip:
STRICT_NETWORK_TESTS=1 make test-net
```

**Speed note:** every delivered track's Telegram `file_id` is cached in SQLite.
First fetch = download+convert+upload; **every repeat is usually near-instant**
(re-sends the file_id, no source download/upload). The actual first-fetch speed
depends on the provider, egress, media format and Telegram.

Cookies are used only to help yt-dlp reach otherwise public media when a source
bot-checks the server. Private, members-only, premium, and login-only media is
rejected even if the shared cookie account can access it.

`ADMIN_USER_ID` defaults to `7645204689`. A video or circle from that private
chat first gets normal/circle choices and an explicit confirmation; converted
media is uploaded once and its Telegram `file_id` is reused for the fan-out.
Other copyable admin messages keep the immediate announcement behavior.
Every announcement is persisted before its first recipient, so a Railway
restart resumes the unsent remainder. Slash commands still run normally and
are never broadcast. Announcements have no forward attribution and are paced at
`BROADCAST_RATE_PER_SECOND=20`. Blocked/deactivated recipients are removed from
later sends. `/delete_my_data` removes a user from the audience until they
interact privately with the bot again.

The media button and `/top_music` read only a complete SQLite snapshot, so no
chart lookup happens on the user request path. A background task checks Apple’s
official Uzbekistan Top Songs feed every `TOP_MUSIC_REFRESH_HOURS=48`, resolves
all ten download choices before atomically activating a changed chart, retains
the last-good snapshot on any error, and resumes a pending chart announcement
after a restart. Users receive ten full-width track buttons and can start an
exact-track download with one tap. Selecting an audio that has not previously
been sent still performs the normal one-time download/upload; Telegram's
`file_id` cache makes later sends instant. Backend source details are not shown
in the Telegram response.

## Railway / cloud YouTube setup

The image includes Deno, `yt-dlp-ejs`, and `curl_cffi` browser impersonation,
but those are extractor/runtime requirements, not an anti-bot bypass. YouTube
can still reject Railway's
datacenter IP with `Sign in to confirm you’re not a bot`. When YouTube requires
login/CAPTCHA state, try a fresh Netscape export in Railway as
`YTDLP_COOKIES_CONTENT`; if that egress remains blocked, set `YTDLP_PROXY` to an
authorized stable proxy with unblocked egress. Neither method guarantees access.
Cookies are login credentials—use a low-privilege throwaway account and follow
the exact procedure in [DEPLOY.md](DEPLOY.md). Startup logs report only whether
cookies/proxy are configured; their values are never printed.

The Local Bot API wiring and filesystem isolation are container-tested. Before
raising the default 20/50 MB limits in production, run a credentialed canary with
one inbound file above 20 MB and one outbound file above 50 MB.

> No auto-reload: after editing code, `make stop && make start` (or restart `make run`).

## Layout

```
bot/
  main.py            entrypoint (dispatcher, routers, middleware)
  config.py          env loading
  i18n.py            translation lookup
  handlers/          confirmed broadcasts, Top 10, downloads, search, recognition
  services/          charts/fan-out, downloader, ffmpeg, recognizer, search
  db/storage.py      sqlite: users, Top 10 state, drafts, sessions + file_id cache
  middlewares/       private-user registration + i18n locale resolution
locales/             uz / ru / en strings
```
