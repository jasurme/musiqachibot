# Deploying to Railway

The repo ships a **Dockerfile** (Python 3.12 + ffmpeg) and `railway.toml`, so
Railway builds it deterministically — no Nixpacks/Railpack guesswork.

## 1. Create the service
1. Railway → **New Project → Deploy from GitHub repo** → pick `jasurme/musiqachibot`.
2. Railway detects the Dockerfile and builds it. It runs as a **worker** —
   this is a long-polling bot, so it needs **no port / no public domain**.

## 2. Environment variables (Settings → Variables)
| Variable | Required | Value |
|----------|----------|-------|
| `BOT_TOKEN` | ✅ | your token from @BotFather |
| `DB_PATH` | recommended | `/data/musiqa.db` (see Volume below) |
| `DEFAULT_LOCALE` | optional | `uz` |
| `MAX_FILE_MB` | optional | `50` |
| `YTDLP_COOKIES_FILE` | see §4 | e.g. `/app/cookies.txt` |
| `YTDLP_PROXY` | see §4 | residential proxy URL |

⚠️ **Never** put the token in the repo — it's only ever an env var. `.env` is gitignored.

## 3. Add a Volume (so data survives redeploys)
Railway's filesystem is **wiped on every redeploy**. Without a volume, the SQLite
DB resets each deploy → language prefs, the `file_id` cache, and button sessions
(the "havola eskirdi" fix) are all lost.

1. Service → **Settings → Volumes → New Volume**, mount path **`/data`**.
2. Set `DB_PATH=/data/musiqa.db`.

That's it — the app writes its DB there and it persists.

## 4. ⚠️ YouTube blocks datacenter IPs — read this
This is the #1 gotcha. From cloud hosts (Railway/AWS/etc.) YouTube frequently
rejects yt-dlp with *"Sign in to confirm you're not a bot"* / HTTP 403.
Reported success: **datacenter ~20–40%** vs **residential ~85–95%**.

So **search (Feature A) and audio download may be flaky on Railway** unless you add:
- **Cookies** (biggest easy win): export a `cookies.txt` (Netscape format, via a
  browser extension) from a throwaway Google account, add it to the service (e.g.
  a Railway *file* mount or bake it in), and set `YTDLP_COOKIES_FILE=/app/cookies.txt`.
- **A residential proxy** (highest impact): set `YTDLP_PROXY=http://user:pass@host:port`.
- Keep `yt-dlp` fresh — it's already unpinned in `requirements.txt` so redeploys pull the latest.

Instagram/TikTok are less aggressive but can also need cookies for private/rate-limited content.
Both `YTDLP_COOKIES_FILE` and `YTDLP_PROXY` are already wired into every yt-dlp call.

## 5. File-size limit
Standard Bot API caps uploads at **50 MB** (`MAX_FILE_MB=50`). Big 1080p videos
can exceed that and will report "too big". To lift it to ~2 GB you'd run a
**Local Bot API server** as a second Railway service and set `LOCAL_BOT_API_URL` —
optional, most songs/clips are well under 50 MB.

## 6. Verify
Deploy logs should show:
```
Bot @<name> (id=...) starting long polling...
aiogram.dispatcher: Run polling ...
```
Then message the bot. If searches fail with 403 → do §4 (cookies/proxy).

## Redeploys
Push to `main` → Railway auto-builds and restarts. The volume (DB) persists.
