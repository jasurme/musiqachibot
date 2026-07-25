"""ffmpeg helper — turn a video into a Telegram round video-note.

Video notes must be square, ≤ 60 s. We crop the centred square, scale it down,
and cap the duration.
"""
import os

from bot.services import downloader
from bot.services.audio import _run_ffmpeg


async def make_video_note(src_path: str, out_dir: str, size: int = 480,
                          max_seconds: int = 60,
                          max_bytes: int | None = None) -> str:
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(src_path))[0]
    out = os.path.join(out_dir, base + ".note.mp4")
    # crop centred square (min of width/height), then scale to size×size
    vf = f"crop='min(iw,ih)':'min(iw,ih)',scale={size}:{size}"
    size_limit = ["-fs", str(int(max_bytes))] if max_bytes is not None else []
    await _run_ffmpeg(
        ["ffmpeg", "-nostdin", "-y", "-i", src_path, "-t", str(max_seconds),
         "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
         *size_limit, out],
        timeout=300,
    )
    downloader._assert_output_size(out, max_bytes)
    if max_bytes is not None and os.path.getsize(out) >= int(max_bytes) * 0.98:
        # -fs may return success with a truncated video note; reserve a small
        # delivery margin instead of sending a partial circle.
        raise downloader.DownloadTooLarge(
            "converted output exceeds configured size limit"
        )
    return out
