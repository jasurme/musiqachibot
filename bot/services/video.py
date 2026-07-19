"""ffmpeg helper — turn a video into a Telegram round video-note.

Video notes must be square, ≤ 60 s. We crop the centred square, scale it down,
and cap the duration.
"""
import asyncio
import os
import subprocess


async def make_video_note(src_path: str, out_dir: str, size: int = 480,
                          max_seconds: int = 60) -> str:
    return await asyncio.to_thread(_make_note_sync, src_path, out_dir, size, max_seconds)


def _make_note_sync(src_path: str, out_dir: str, size: int, max_seconds: int) -> str:
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(src_path))[0]
    out = os.path.join(out_dir, base + ".note.mp4")
    # crop centred square (min of width/height), then scale to size×size
    vf = f"crop='min(iw,ih)':'min(iw,ih)',scale={size}:{size}"
    subprocess.run(
        ["ffmpeg", "-y", "-i", src_path, "-t", str(max_seconds),
         "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out],
        check=True, capture_output=True,
    )
    return out
