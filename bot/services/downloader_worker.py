"""Private subprocess entry point for bounded yt-dlp jobs.

The bot passes request/response file paths only. Provider URLs, proxy settings,
cookies, and Telegram credentials are never placed in process arguments.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
from collections.abc import Mapping, Sequence

import yt_dlp

from bot.services import downloader


def _jsonable(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"provider result contains unsupported type {type(value).__name__}")


def _operation(name: str):
    if name == "extract_meta":
        return downloader._extract_meta_sync
    if name == "download_quality":
        return downloader._download_quality_sync
    if name == "download_audio":
        return downloader._download_audio_sync
    if name == "search":
        # Imported lazily to avoid downloader <-> search import initialization.
        from bot.services.search import _search_sync

        return _search_sync
    raise ValueError("unknown provider worker operation")


def _write_response(path: str, payload: dict) -> None:
    temporary = path + ".tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    request_path, response_path = sys.argv[1:]
    try:
        with open(request_path, encoding="utf-8") as stream:
            request = json.load(stream)
        operation = request.get("operation") if isinstance(request, dict) else None
        args = request.get("args") if isinstance(request, dict) else None
        if not isinstance(operation, str) or not isinstance(args, list):
            raise ValueError("invalid provider worker request")
        result = _operation(operation)(*args)
        payload = {"ok": True, "result": _jsonable(result)}
    except BaseException as exc:
        if isinstance(exc, yt_dlp.utils.DownloadError):
            error_type = "DownloadError"
        else:
            error_type = type(exc).__name__
        payload = {
            "ok": False,
            "error_type": error_type,
            "message": downloader.safe_error_message(exc),
        }
    _write_response(response_path, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
