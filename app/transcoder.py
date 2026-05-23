import asyncio
import os
import time
from dataclasses import dataclass
from typing import Optional

# Each profile is a flat list of FFmpeg args that fully replaces the
# manual video_bitrate / audio_bitrate / preset trio.
PROFILE_ARGS: dict[str, list[str]] = {
    "ai_video": [
        "-vf",
        "scale='if(gte(iw,ih),min(1920,iw),-2)':'if(lt(iw,ih),min(1920,ih),-2)',fps=10,mpdecimate",
        "-c:v", "libx264",
        "-crf", "20",
        "-preset", "veryslow",
        "-tune", "stillimage",
        "-fps_mode", "vfr",
        "-c:a", "aac",
        "-ac", "1",
        "-ar", "16000",
        "-b:a", "32k",
    ],
}

VALID_PROFILES: frozenset[str] = frozenset(PROFILE_ARGS)


@dataclass
class TranscodeOptions:
    output_format: str = "mp4"
    # Manual mode
    video_bitrate: str = "1M"
    audio_bitrate: str = "128k"
    preset: str = "fast"
    # Profile mode — when set, the three fields above are ignored
    profile: Optional[str] = None


@dataclass
class TranscodeResult:
    input_size: int
    output_size: int
    duration_seconds: float

    @property
    def compression_ratio(self) -> float:
        if self.input_size == 0:
            return 0.0
        return round(1 - (self.output_size / self.input_size), 4)

    @property
    def size_reduction_pct(self) -> float:
        return round(self.compression_ratio * 100, 2)


def _build_ffmpeg_cmd(input_path: str, output_path: str, options: TranscodeOptions) -> list[str]:
    if options.profile:
        encoding_args = PROFILE_ARGS[options.profile]
    else:
        encoding_args = [
            "-c:v", "libx264",
            "-b:v", options.video_bitrate,
            "-c:a", "aac",
            "-b:a", options.audio_bitrate,
            "-preset", options.preset,
            "-movflags", "+faststart",  # place moov atom at start for streaming
        ]
    return ["ffmpeg", "-i", input_path, *encoding_args, "-y", output_path]


async def _get_duration(input_path: str) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        input_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    try:
        return float(stdout.decode().strip())
    except ValueError:
        return 0.0


async def transcode_video(
    input_path: str,
    output_path: str,
    options: TranscodeOptions,
    progress_queue: asyncio.Queue | None = None,
    on_process=None,
) -> TranscodeResult:
    start = time.monotonic()
    input_size = os.path.getsize(input_path)

    total_duration = await _get_duration(input_path) if progress_queue is not None else 0.0

    cmd = _build_ffmpeg_cmd(input_path, output_path, options)
    # -progress pipe:1 emits machine-readable key=value blocks to stdout
    cmd = [cmd[0], "-progress", "pipe:1"] + cmd[1:]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    if on_process is not None:
        on_process(proc)

    # Drain stderr in background to prevent the OS pipe buffer from blocking FFmpeg
    stderr_task = asyncio.create_task(proc.stderr.read())

    block: dict[str, str] = {}
    async for raw_line in proc.stdout:
        line = raw_line.decode(errors="replace").strip()
        if "=" in line:
            key, _, val = line.partition("=")
            block[key] = val.strip()
        if block.get("progress") in ("continue", "end"):
            if progress_queue is not None and total_duration > 0:
                try:
                    out_us = int(block.get("out_time_us") or 0)
                except ValueError:
                    out_us = 0
                elapsed = time.monotonic() - start
                pct = min(100.0, out_us / 1_000_000 / total_duration * 100)
                eta = round(elapsed / pct * (100 - pct), 1) if pct > 0 else None
                try:
                    progress_queue.put_nowait({
                        "type": "progress",
                        "elapsed_sec": round(elapsed, 1),
                        "pct": round(pct, 1),
                        "eta_sec": eta,
                    })
                except asyncio.QueueFull:
                    pass
            block = {}

    await proc.wait()
    stderr_data = await stderr_task

    if proc.returncode != 0:
        raise RuntimeError(stderr_data.decode(errors="replace"))

    output_size = os.path.getsize(output_path)
    return TranscodeResult(
        input_size=input_size,
        output_size=output_size,
        duration_seconds=round(time.monotonic() - start, 2),
    )
