"""ffmpeg helpers — extract audio, trim samples for recognition."""
import asyncio
import os
import subprocess


async def extract_audio(video_path: str, out_dir: str) -> str:
    """Extract the full audio track of a video to an mp3."""
    return await asyncio.to_thread(_extract_audio_sync, video_path, out_dir)


def _extract_audio_sync(video_path: str, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(video_path))[0]
    out = os.path.join(out_dir, base + ".mp3")
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-vn",
         "-acodec", "libmp3lame", "-q:a", "2", out],
        check=True, capture_output=True,
    )
    return out


async def make_sample(src_path: str, out_dir: str, seconds: int = 15) -> str:
    """Trim a short mono sample for recognition (Feature C)."""
    return await asyncio.to_thread(_make_sample_sync, src_path, out_dir, seconds)


def _make_sample_sync(src_path: str, out_dir: str, seconds: int) -> str:
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(src_path))[0]
    out = os.path.join(out_dir, base + f".sample.mp3")
    subprocess.run(
        ["ffmpeg", "-y", "-i", src_path, "-t", str(seconds),
         "-ac", "1", "-acodec", "libmp3lame", "-q:a", "5", out],
        check=True, capture_output=True,
    )
    return out
