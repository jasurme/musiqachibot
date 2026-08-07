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
- [x] **Admin broadcast** — private text/video/video-note/document/media from the configured admin is copied to every active private user
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

`ADMIN_USER_ID` defaults to `7645204689`. Every copyable message sent by that
user in the bot's private chat is an announcement; it is copied without forward
attribution and paced at `BROADCAST_RATE_PER_SECOND=20`. Blocked/deactivated
recipients are removed from later sends. `/delete_my_data` removes a user from
the audience until they interact privately with the bot again.

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
  handlers/          admin broadcast, /start, downloads, search, recognition
  services/          downloader (yt-dlp), audio (ffmpeg), recognizer, search
  db/storage.py      sqlite: active users, locale, sessions + file_id cache
  middlewares/       private-user registration + i18n locale resolution
locales/             uz / ru / en strings
```
