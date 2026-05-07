"""Скачивание медиа из Telegram + кадры из видео через ffmpeg."""
from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
import subprocess
import tempfile
from pathlib import Path

from telethon.tl.custom import Message

VIDEO_FRAMES = int(os.getenv("VIDEO_FRAMES", "4"))


def _b64_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    if mime.startswith("video/"):
        mime = "image/jpeg"
    b = base64.b64encode(path.read_bytes()).decode()
    return f"data:{mime};base64,{b}"


async def _extract_frames(video: Path, n: int) -> list[Path]:
    """Равномерно n кадров через ffmpeg."""
    out_dir = Path(tempfile.mkdtemp(prefix="aitg_frames_"))
    # длительность
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(video),
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        dur = float(out.strip() or 0)
    except ValueError:
        dur = 0
    if dur <= 0:
        return []
    frames: list[Path] = []
    for i in range(n):
        ts = dur * (i + 0.5) / n
        f = out_dir / f"f{i}.jpg"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-ss", f"{ts:.2f}", "-i", str(video),
            "-frames:v", "1", "-q:v", "3", "-vf", "scale=768:-2", str(f),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        await proc.wait()
        if f.exists():
            frames.append(f)
    return frames


async def _convert_to_mp3(audio: Path) -> Path | None:
    """Конвертирует аудио в mp3 через ffmpeg."""
    out = audio.with_suffix(".mp3")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(audio), "-ar", "16000", "-ac", "1",
        "-b:a", "64k", str(out),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    await proc.wait()
    return out if out.exists() else None


async def message_to_image_parts(msg: Message) -> list[dict]:
    """Превращает медиа из сообщения в openai-style image_url content parts."""
    if not msg or not msg.media:
        return []
    tmp = Path(tempfile.mkdtemp(prefix="aitg_dl_"))
    path = await msg.download_media(file=str(tmp))
    if not path:
        return []
    p = Path(path)
    mime = mimetypes.guess_type(p.name)[0] or ""

    if mime.startswith("video/") or p.suffix.lower() in {".mp4", ".mov", ".webm", ".gif"}:
        # анимированные стикеры (.tgs/.webm) и видео — режем на кадры
        frames = await _extract_frames(p, VIDEO_FRAMES)
        return [
            {"type": "image_url", "image_url": {"url": _b64_data_url(f)}}
            for f in frames
        ]
    if mime.startswith("image/") or p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
        return [{"type": "image_url", "image_url": {"url": _b64_data_url(p)}}]
    if mime.startswith("audio/") or p.suffix.lower() in {".ogg", ".oga", ".mp3", ".wav", ".m4a", ".opus", ".flac"}:
        mp3 = await _convert_to_mp3(p)
        if mp3:
            b = base64.b64encode(mp3.read_bytes()).decode()
            return [{"type": "input_audio", "input_audio": {"data": b, "format": "mp3"}}]
    # неизвестный тип — игнорим
    return []
