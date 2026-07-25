#!/bin/sh
set -eu

# Railway/Docker volumes are initially root-owned. Prepare only this bot's
# dedicated data paths, then permanently drop root and all Linux capabilities
# before Python, yt-dlp, ffmpeg, or Deno sees untrusted media.
if [ "$(id -u)" = "0" ]; then
    # Create children while root still owns the mount, then chown them together.
    # With a minimal capability set, chowning /data first would remove root's
    # ordinary write permission before /data/downloads can be created.
    install -d /data /data/downloads
    chown 101:101 /data /data/downloads
    chown -R 101:101 /data/downloads
    find /data -maxdepth 1 -type f \
        \( -name '*.db' -o -name '*.db-shm' -o -name '*.db-wal' \) \
        -exec chown 101:101 {} +
    exec setpriv \
        --no-new-privs \
        --reuid=101 --regid=101 --init-groups \
        --inh-caps=-all --ambient-caps=-all --bounding-set=-all \
        -- /usr/bin/tini -- "$@"
fi

exec setpriv --no-new-privs -- /usr/bin/tini -- "$@"
