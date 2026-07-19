# 🎵 Musiqa Bot

A Telegram music bot: find songs by name/artist/lyrics, recognize music from
voice/video/audio (Shazam-style), and download videos + audio from Instagram,
TikTok and YouTube. UZ / RU / EN.

See [RESEARCH.md](RESEARCH.md) for the full design and rationale.

## Quick start (local dev)

```bash
# 1. Create the venv (Python 3.12) and install deps
make setup          # or see commands below

# 2. Put your BotFather token in .env
#    BOT_TOKEN=123456:ABC...

# 3. Run
make run            # or: .venv/bin/python -m bot.main
```

Manual equivalent:
```bash
/opt/homebrew/opt/python@3.12/libexec/bin/python3 -m venv .venv
.venv/bin/pip install -U pip -r requirements.txt
.venv/bin/python -m bot.main
```

`ffmpeg` must be installed (`brew install ffmpeg`) — used for audio extraction.

## Build status — all core features DONE ✅

- [x] **/start** welcome menu + language switch (uz / ru / en, persisted per user)
- [x] **Feature A** — name/artist → numbered results list + ◀️▶️ paging → tap → mp3
- [x] **Feature B** — **Lyrics** button (lyrics.ovh, free/no-key)
- [x] **Feature C** — voice/audio/video/video-note → **recognize** (Shazamio) → album art + "Song title/Artist" header + results list + Lyrics/Video buttons
- [x] **Feature D** — IG/TikTok/YouTube link → **quality picker** (360/480/720/Audio)
- [x] **file_id cache** (instant repeats) + `👉 @<bot>` signature on every delivery
- [ ] **Local Bot API server** (files > 50 MB) — ready in `docker-compose.yml`, opt-in

All features use YouTube via yt-dlp — **no API keys needed**. Search coverage for
Uzbek artists matches the reference bot exactly.

## Tests

```bash
make test        # 43 fast offline tests (dispatcher-level, network faked) — ~0.2s
make test-net    # 5 real-internet smoke tests (search/download/recognize/lyrics)
```

**Speed note:** every delivered track's Telegram `file_id` is cached in SQLite.
First fetch = download+convert+upload (a few seconds); **every repeat is instant**
(re-sends the file_id, no download/upload). A server is faster than localhost for
the first fetch (better uplink to Telegram), but the *instant* feel of big bots is
this cache — see below.

> Age-restricted YouTube videos need cookies (`--cookies`) or that one track fails
> with "Sign in to confirm your age".

> No auto-reload: after editing code, `make stop && make start` (or restart `make run`).

## Layout

```
bot/
  main.py            entrypoint (dispatcher, routers, middleware)
  config.py          env loading
  i18n.py            translation lookup
  handlers/          /start, url download, text search, media recognize
  services/          downloader (yt-dlp), audio (ffmpeg), recognizer, search
  db/storage.py      sqlite: user locale + file_id cache
  middlewares/       i18n locale resolution
locales/             uz / ru / en strings
```
