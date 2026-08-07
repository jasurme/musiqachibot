FROM denoland/deno:bin-2.9.3@sha256:eb93e70bd53efec4be113d9974107840756551bc1a59a8258892d7bbe5fb4ab0 AS deno

# Controlled application runtime for Railway (or any Docker host).
# Python 3.12 is required by the current audio stack.
FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

# ffmpeg handles media conversion. Deno runs yt-dlp's sandboxed YouTube EJS
# challenge solver and is copied from the official multi-architecture image.
COPY --from=deno /deno /usr/local/bin/deno

# Use the Debian snapshot recorded by the pinned base image instead of mutable
# package mirrors, so ffmpeg resolves to the same build on every rebuild.
RUN sed -i \
        's|URIs: http://deb.debian.org/debian$|URIs: http://snapshot.debian.org/archive/debian/20260713T000000Z|' \
        /etc/apt/sources.list.d/debian.sources \
    && sed -i \
        's|URIs: http://deb.debian.org/debian-security$|URIs: http://snapshot.debian.org/archive/debian-security/20260713T000000Z|' \
        /etc/apt/sources.list.d/debian.sources \
    && echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99snapshot \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY . .

# Fail the build instead of deploying a container that can search YouTube but
# cannot solve the JavaScript challenges needed to download media.
RUN deno --version \
    && python -c "import yt_dlp_ejs" \
    && python -c "import curl_cffi" \
    && groupadd --system --gid 101 musiqa \
    && useradd --system --uid 101 --gid 101 --home-dir /app \
        --no-create-home --shell /usr/sbin/nologin musiqa \
    && install -d -o 101 -g 101 /data /data/downloads \
    && chmod 0755 /app/docker-entrypoint.sh

ENV DB_PATH=/data/musiqa.db \
    DOWNLOAD_DIR=/data/downloads \
    DENO_DIR=/tmp/deno \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# The entrypoint briefly prepares a root-owned volume, then execs a capless,
# non-root tini as PID 1 so terminated yt-dlp/ffmpeg/Deno descendants are reaped.
# Long-polling worker — no web server / port needed.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "-m", "bot.main"]
