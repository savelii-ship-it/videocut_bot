"""Ядро нарезки видео: пробинг, раскладка на части, склейка с баннером.

Использует ffmpeg/ffprobe. Все тяжёлые операции — через asyncio subprocess.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Поиск ffmpeg/ffprobe: сначала системные, потом static-ffmpeg
# ---------------------------------------------------------------------------

def _find_binary(name: str) -> str:
    sys_path = shutil.which(name)
    if sys_path:
        return sys_path
    try:
        import static_ffmpeg  # type: ignore

        ffmpeg, ffprobe = static_ffmpeg.run.get_or_fetch_platform_executables_else_raise()
        return ffmpeg if name == "ffmpeg" else ffprobe
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            f"{name} не найден. Установите ffmpeg в систему или `pip install static-ffmpeg`."
        ) from exc


FFMPEG = _find_binary("ffmpeg")
FFPROBE = _find_binary("ffprobe")


class ProcessingError(Exception):
    """Ошибка обработки видео."""


@dataclass
class VideoInfo:
    duration: float
    width: int
    height: int
    has_audio: bool


async def _run(cmd: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode, (out + err).decode("utf-8", errors="replace")


async def probe(path: str | Path) -> VideoInfo:
    """Длительность, размер кадра, наличие звука."""
    code, out = await _run(
        [
            FFPROBE, "-v", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(path),
        ]
    )
    if code != 0:
        raise ProcessingError(f"ffprobe не смог прочитать файл: {out[-300:]}")
    # _run склеивает stdout+stderr — достаём JSON надёжно
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise ProcessingError("ffprobe вернул пустой ответ")
    data = json.loads(m.group(0))

    duration = float(data.get("format", {}).get("duration", 0))
    width = height = 0
    has_audio = False
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not width:
            width = int(s.get("width", 0))
            height = int(s.get("height", 0))
        elif s.get("codec_type") == "audio":
            has_audio = True
    if duration <= 0:
        raise ProcessingError("Не удалось определить длительность видео")
    if not width or not height:
        raise ProcessingError("В файле нет видеопотока")
    return VideoInfo(duration=duration, width=width, height=height, has_audio=has_audio)


def split_range(start: float, end: float, part_len: float) -> list[tuple[float, float]]:
    """Режем [start, end] на части по part_len секунд."""
    if end <= start:
        raise ProcessingError("Конец отрезка должен быть позже начала")
    if part_len <= 0:
        raise ProcessingError("Длина части должна быть больше нуля")
    parts: list[tuple[float, float]] = []
    t = start
    while t < end - 0.05:  # не плодим куски короче 50 мс
        parts.append((t, min(t + part_len, end)))
        t += part_len
    return parts


async def detect_scene_cuts(path: str | Path, start: float, end: float,
                            threshold: float = 0.3) -> list[float]:
    """«Умный» режим: ищем смены сцен внутри отрезка (scdet/showinfo)."""
    code, out = await _run(
        [
            FFMPEG, "-hide_banner",
            "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
            "-i", str(path),
            "-vf", f"select='gt(scene,{threshold})',showinfo",
            "-f", "null", "-",
        ]
    )
    times = [float(t) + start for t in re.findall(r"pts_time:([\d.]+)", out)]
    return times


def smart_split(parts: list[tuple[float, float]],
                scene_cuts: list[float],
                tolerance: float = 2.0) -> list[tuple[float, float]]:
    """Сдвигаем внутренние границы частей к ближайшей смене сцены (±tolerance с)."""
    if not scene_cuts or len(parts) < 2:
        return parts
    bounds = [parts[0][0]]
    for i in range(len(parts) - 1):
        ideal = parts[i][1]
        near = [c for c in scene_cuts if abs(c - ideal) <= tolerance]
        bounds.append(min(near, key=lambda c: abs(c - ideal)) if near else ideal)
    bounds.append(parts[-1][1])
    result = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]
    return [p for p in result if p[1] - p[0] > 0.05]


async def cut_part(src: str | Path, dst: str | Path,
                   start: float, end: float,
                   banner: str | Path | None = None,
                   video_width: int | None = None) -> None:
    """Вырезаем кусок [start, end] с перекодированием; опционально кладём баннер
    на всю ширину по вертикальному центру."""
    dst = Path(dst)
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
        "-i", str(src),
    ]
    if banner:
        if not video_width:
            video_width = (await probe(src)).width
        is_video_banner = Path(banner).suffix.lower() in {".mp4", ".mov", ".webm", ".gif", ".mkv"}
        if is_video_banner:
            # Видео-баннер: зацикливаем на длину части
            cmd += ["-stream_loop", "-1", "-i", str(banner)]
        else:
            cmd += ["-i", str(banner)]
        # Баннер: масштаб до полной ширины кадра, позиция — центр по вертикали
        overlay = "overlay=0:(main_h-overlay_h)/2"
        if is_video_banner:
            overlay += ":shortest=1"
        fc = f"[1:v]scale={video_width}:-1[bnr];[0:v][bnr]{overlay}[v]"
        cmd += [
            "-filter_complex", fc,
            "-map", "[v]", "-map", "0:a?",
        ]
    else:
        cmd += ["-map", "0:v", "-map", "0:a?"]
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(dst),
    ]
    code, out = await _run(cmd)
    if code != 0 or not dst.exists() or dst.stat().st_size == 0:
        raise ProcessingError(f"ffmpeg упал на отрезке {start:.1f}–{end:.1f}: {out[-300:]}")


async def process_video(src: str | Path, out_dir: str | Path,
                        start: float, end: float, part_len: float,
                        banner: str | Path | None = None,
                        smart: bool = False,
                        on_progress=None) -> list[Path]:
    """Полный пайплайн: отрезок → части (+баннер) → список файлов."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    info = await probe(src)
    end = min(end, info.duration)
    parts = split_range(start, end, part_len)
    if smart:
        cuts = await detect_scene_cuts(src, start, end)
        parts = smart_split(parts, cuts)

    results: list[Path] = []
    total = len(parts)
    for i, (s, e) in enumerate(parts, 1):
        dst = out_dir / f"part_{i:03d}.mp4"
        await cut_part(src, dst, s, e, banner, video_width=info.width)
        results.append(dst)
        if on_progress:
            await on_progress(i, total)
    return results


# ---------------------------------------------------------------------------
# Утилиты времени
# ---------------------------------------------------------------------------

def parse_time(text: str) -> float:
    """'90' | '1:30' | '00:01:30' → секунды."""
    text = text.strip()
    chunks = text.split(":")
    if not 1 <= len(chunks) <= 3:
        raise ValueError(f"Некорректное время: {text!r}")
    try:
        nums = [float(c) for c in chunks]
    except ValueError:
        raise ValueError(f"Некорректное время: {text!r}") from None
    while len(nums) < 3:
        nums.insert(0, 0.0)
    h, m, s = nums
    if m >= 60 or s >= 60 or h < 0:
        raise ValueError(f"Некорректное время: {text!r}")
    return h * 3600 + m * 60 + s


def parse_range(text: str) -> tuple[float, float]:
    """'0:05-1:30' → (5.0, 90.0)."""
    parts = re.split(r"\s*[-–—]\s*", text.strip())
    if len(parts) != 2:
        raise ValueError("Формат: начало-конец, например 0:05-1:30")
    start, end = parse_time(parts[0]), parse_time(parts[1])
    if end <= start:
        raise ValueError("Конец должен быть позже начала")
    return start, end


def fmt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(int(m), 60)
    return f"{h:02d}:{m:02d}:{s:05.2f}" if h else f"{m:02d}:{s:05.2f}"
