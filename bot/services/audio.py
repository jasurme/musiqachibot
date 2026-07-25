"""Killable ffmpeg helpers for extraction and recognition samples."""
import asyncio
import os
import subprocess

from bot.services import downloader


async def _run_ffmpeg(command: list[str], timeout: float) -> None:
    """Run one ffmpeg process group with bounded, cancellation-safe cleanup."""
    if downloader._CLOSING:
        raise downloader.ProviderBusy("media provider queue is busy")
    owner_task = asyncio.current_task()
    already_registered = owner_task in downloader._PROVIDER_TASKS
    if owner_task is not None:
        downloader._PROVIDER_TASKS.add(owner_task)
    process = None
    try:
        spawn_kwargs = {"start_new_session": True} if os.name == "posix" else {}
        spawn_task = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                **spawn_kwargs,
            )
        )
        process = await downloader._await_worker_spawn(spawn_task, min(timeout, 10))
        downloader._ACTIVE_PROCESSES[process.pid] = process
        if downloader._CLOSING:
            await downloader._kill_process_group(process)
            raise downloader.ProviderBusy("media provider queue is busy")
        _, stderr = await downloader._communicate_with_deadline(process, timeout)
    finally:
        if process is not None:
            downloader._ACTIVE_PROCESSES.pop(process.pid, None)
        if owner_task is not None and not already_registered:
            downloader._PROVIDER_TASKS.discard(owner_task)
    if process.returncode:
        raise subprocess.CalledProcessError(
            process.returncode, command, stderr=stderr
        )


async def extract_audio(video_path: str, out_dir: str) -> str:
    """Extract the full audio track of a video to an mp3."""
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(video_path))[0]
    out = os.path.join(out_dir, base + ".mp3")
    await _run_ffmpeg(
        ["ffmpeg", "-nostdin", "-y", "-i", video_path, "-vn",
         "-acodec", "libmp3lame", "-q:a", "2", out],
        timeout=300,
    )
    return out


async def make_sample(
    src_path: str, out_dir: str, seconds: int = 15, start_seconds: int = 0,
) -> str:
    """Trim a short mono sample for recognition (Feature C)."""
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(src_path))[0]
    out = os.path.join(out_dir, base + f".{start_seconds}.sample.mp3")
    seek = ["-ss", str(start_seconds)] if start_seconds > 0 else []
    await _run_ffmpeg(
        ["ffmpeg", "-nostdin", "-y", *seek, "-i", src_path, "-t", str(seconds),
         "-ac", "1", "-acodec", "libmp3lame", "-q:a", "5", out],
        timeout=120,
    )
    return out
