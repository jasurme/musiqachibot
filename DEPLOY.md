# Deploying to Railway

The repo ships a **Dockerfile** (Python 3.12 + ffmpeg + Deno) and `railway.toml`,
so Railway uses the reviewed Docker build — no Nixpacks/Railpack guesswork. The
Python dependency tree is version-and-hash locked, Debian packages come from the
base image's dated snapshot, and the entrypoint drops root/capabilities before
the bot processes any user media.

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
| `ADMIN_USER_ID` | recommended | `7645204689`; owns broadcasts and the private `/total_users` audience report |
| `BROADCAST_RATE_PER_SECOND` | optional | `20`; do not exceed the validated maximum of `25` |
| `TOP_MUSIC_REFRESH_HOURS` | optional | `48`; refresh the persisted Uzbekistan Top 10 off-path |
| `TOP_MUSIC_RETRY_MINUTES` | optional | `30`; initial bounded retry after a chart/provider failure |
| `MAX_FILE_MB` | optional | `50` |
| `MAX_INPUT_MB` | optional | `20` (standard Bot API incoming-file limit) |
| `PRIVACY_POLICY_URL` | required for public launch | operator-specific HTTPS policy shown by `/privacy`; configure in BotFather too |
| `YTDLP_COOKIES_CONTENT` | conditional | fresh Netscape cookie export when YouTube requires login/CAPTCHA state; see §4 |
| `YTDLP_PROXY` | sometimes needed | authorized stable proxy whose egress is not blocked |
| `INSTAGRAM_PROXY` | rarely needed | Instagram-only authorized stable proxy |
| `YTDLP_CONCURRENCY` | optional | `3` |
| `HEAVY_JOB_CONCURRENCY` | optional | `3`; global cap across user download/conversion/recognition jobs |
| `YTDLP_SLEEP_REQUESTS` | optional | `0`; increase only if measured rate limits justify the latency |
| `YTDLP_JOB_TIMEOUT_SECONDS` | optional | `600`; hard queue + provider + conversion deadline |
| `YTDLP_METADATA_TIMEOUT_SECONDS` | optional | `90`; hard search/metadata deadline |
| `DOWNLOAD_MAX_SECONDS` | optional | `1800` |
| `AUDD_TOKEN` | optional | paid recognition fallback after Shazamio |

⚠️ **Never** put the token in the repo — it's only ever an env var. `.env` is gitignored.

## 3. Add a Volume (so data survives redeploys)
Railway's filesystem is **wiped on every redeploy**. Without a volume, the SQLite
DB resets each deploy → language prefs, the `file_id` cache, and button sessions
(the "havola eskirdi" fix) are all lost.

1. Service → **Settings → Volumes → New Volume**, mount path **`/data`**.
2. Set `DB_PATH=/data/musiqa.db`.

That's it — the app writes its DB there and it persists.

The same database stores the last complete Top 10, all weekly music and mood
snapshots, ranking history, durable campaign cursors, admin video confirmation
drafts, and every administrator-announcement cursor.
Without the volume, the stored lists must be rebuilt after every deployment and
partially delivered announcements cannot resume.

### Admin video announcements and Top 10

When `ADMIN_USER_ID` sends a private normal video or circle, the bot does not
send it immediately. Choose **normal video** or **circle**, then press the
confirmation button. Cross-format conversion downloads at most
`MAX_INPUT_MB`; same-format Telegram copies do not need that download. Every
delivered video gets one callback button that opens the already stored Top
Music chart as ten direct track-download buttons.

The Top 10 source is Apple’s official unauthenticated Uzbekistan Top Songs RSS
feed. Refresh/search work runs in the background; opening `/top_music` or its
media button performs no provider request. Only a complete ten-track snapshot
replaces the previous one.

The bot also prepares New Music, Rising, Discoveries, and five ten-track mood
lists off the request path. The three proactive categories are sent at most
once in their Asia/Tashkent weekly slots (Monday, conditional Wednesday, and
Friday) to every active user and resume from SQLite after a restart. Mood lists
refresh weekly but are opened on demand, preventing five extra broadcasts.
The first reliable Rising comparison needs about one week of retained chart
history. Keep one Railway replica and retain `overlapSeconds=0` so one scheduler
owns SQLite campaign leases and Telegram polling.

## 4. ⚠️ Fix Railway's YouTube bot-check

The image includes the two current yt-dlp prerequisites: the matching
`yt-dlp-ejs` package and Deno. They execute YouTube's JavaScript challenges;
they are not an anti-bot bypass and cannot change Railway's IP reputation. The
production error in this incident was explicit: `Sign in to confirm you’re not a bot`.

When YouTube requires login/CAPTCHA state, use this exact upstream-recommended
cookie export procedure. Cookies are not a guaranteed fix for a blocked IP:

1. Use a dedicated, low-privilege/throwaway Google account. yt-dlp warns that an
   account used for downloading can be temporarily or permanently restricted.
2. Open one private/incognito window and sign in to YouTube.
3. In that same tab, open `https://www.youtube.com/robots.txt`.
4. Export only YouTube cookies with a reputable local exporter in Mozilla/Netscape
   format. The first line must be `# Netscape HTTP Cookie File` (or
   `# HTTP Cookie File`). Close the private window and do not reopen that session.
5. Railway → service → **Variables** → add `YTDLP_COOKIES_CONTENT`, paste the
   complete multiline file, and redeploy. Do not use `YTDLP_COOKIES_FILE` on
   Railway unless you separately mount/create that file.
6. Confirm startup logs contain `yt-dlp auth configuration: cookies=True`; this
   confirms a readable file was loaded, not that YouTube will accept the session.

Delete `YTDLP_PLAYER_CLIENT` when cookies are configured. The application ignores
that override in authenticated mode so yt-dlp can dynamically select clients that
support cookies; forcing Android/TV/browser clients can hide usable audio formats.

YouTube may rotate cookies when that browser session is reopened. If fresh cookies
still fail, the Railway IP itself may be challenged; configure `YTDLP_PROXY` with
an authorized, stable endpoint whose egress is not blocked. A proxy category alone
does not guarantee success, and extraction and media downloads must use the same
session/egress. Never commit cookies or proxy credentials, and never use an account
that can access private/member media—the bot must not expose that access to users.

Some YouTube clients can separately require a PO token. Deno/EJS does not create
one. If the log explicitly reports a PO-token requirement, follow yt-dlp's current
[PO Token Guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide) and use a
reviewed provider plugin; do not paste an arbitrary token workaround into code.

For `make api`, use `YTDLP_COOKIES_CONTENT`. The Compose stack intentionally
does not mount the host path from `YTDLP_COOKIES_FILE`; add a deliberate
read-only secret mount if file-based local cookies are required.

Official references: [yt-dlp YouTube cookie guidance](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies),
[cookie format FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp),
and [EJS setup](https://github.com/yt-dlp/yt-dlp/wiki/EJS).

`yt-dlp[default,curl-cffi]` is pinned to the tested 2026.07.04 upstream security
release; `curl_cffi` enables yt-dlp's recommended browser impersonation for
sites such as Instagram. This repository and image have not received a formal
security audit. Update the
pin deliberately after running offline, strict-network, and Docker smoke tests.

### Instagram on Railway

Portrait reels use their shorter edge as the quality (for example, `720×1280`
is 720p). The bot uses yt-dlp's orientation-neutral `res` sorting, so these no
longer fail as unavailable formats. Public reels normally require no account.
If logs instead show an Instagram login redirect, empty-media response, 403, or
rate limit, Railway's egress is being challenged. First retry later. If it is
persistent, configure an authorized `INSTAGRAM_PROXY`. Instagram account
cookies are deliberately unsupported so a shared account cannot expose private
or followed-only posts. Private/login-only posts remain rejected.

## 5. File-size limit
The standard Bot API caps incoming `getFile` downloads at **20 MB**
(`MAX_INPUT_MB=20`) and outgoing uploads at **50 MB** (`MAX_FILE_MB=50`). Larger
media is rejected before work starts where Telegram reports its size; external
downloads also have byte/duration guards, capped transcoding, and a final size
check. Each yt-dlp job
runs in a disposable, concurrency-limited subprocess. At
`YTDLP_JOB_TIMEOUT_SECONDS` the complete process group (including ffmpeg/Deno)
is killed and its private staging directory is removed, so a hung provider cannot
permanently consume the bot's worker pool.

`docker-compose.yml` provides an opt-in local stack in which the bot and Bot API
server share the latter's state volume. This is required because `--local`
returns absolute file paths. Before switching an existing bot, follow Telegram's
official `logOut` migration procedure; then run `make api` and raise both limits
conservatively (never above the Local Bot API upload cap of 2000 MB).

The shared-volume topology and restricted runtime are tested, but the repository
cannot perform a credentialed Telegram canary. Verify one inbound file above
20 MB and one outbound file above 50 MB before raising production limits.

A URL to an independent Railway Bot API service is not enough: Railway services
cannot use this Compose shared-volume layout. The default Railway deployment
therefore intentionally stays on the standard 20/50 MB limits unless both
processes are redesigned into one service with shared persistent storage.

## 6. Verify
Deploy logs should show:
```
Bot @<name> (id=...) starting long polling...
aiogram.dispatcher: Run polling ...
```
Then message the bot. If searches fail with 403 → do §4 (cookies/proxy).

Run `STRICT_NETWORK_TESTS=1 make test-net` in a development/CI checkout to catch
extractor regressions. It cannot prove Railway's IP reputation. After each
Railway deployment, request the known incident video once through the bot and
verify the logs; the production image intentionally excludes tests and dev tools.

## Redeploys
Push to `main` → Railway auto-builds and restarts. The volume (DB) persists.

Keep one replica: Railway volumes cannot be attached to multiple replicas, and
SQLite is intentionally the single-worker store. Enable volume backups and test
a restore before treating the file-id/session cache as durable.

## Maintenance and launch controls

The base-image digest and Debian snapshot date are intentionally frozen for
reproducibility; that also freezes OS fixes. On a regular schedule and whenever
a relevant security update ships:

1. Update the Python/Deno image digests and Debian snapshot date deliberately.
2. Rebuild with no stale application cache and run `make test`, strict network
   smoke tests, the exact incident-video canary, and the restricted-container
   checks (non-root UID, zero capabilities, read-only app filesystem).
3. Run your registry/platform vulnerability scan and review every changed package.
4. Deploy to staging, then production with `overlapSeconds=0`.

Before public launch, publish an operator-specific privacy policy, data-deletion
contact, rights-only terms, and takedown process in BotFather. Disclose the use of
Telegram, Railway, source sites, Shazam/AudD, lyrics.ovh, and any configured proxy.
Use a cookie account with no private/member/premium entitlements. Obtain legal
advice for the jurisdictions where the bot is offered; the software does not grant
rights to download or redistribute third-party media.
