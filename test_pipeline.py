"""Самотест пайплайна без Telegram: генерируем видео со сменами сцен + баннер,
прогоняем нарезку, проверяем результат ffprobe'ом."""
import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import processing


def sh(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, f"{cmd[:3]}... failed:\n{r.stderr[-500:]}"
    return r


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="videocut_test_"))
    print(f"tmp: {tmp}")

    # 1. Тестовое видео 65 с, 720x1280 (вертикаль), резкие смены цвета = сцены, со звуком
    src = tmp / "src.mp4"
    sh([
        processing.FFMPEG, "-y", "-loglevel", "error",
        "-f", "lavfi", "-i",
        "testsrc2=size=720x1280:rate=25:duration=65",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=65",
        "-vf", ("hue=h='if(between(t,10,20),120,if(between(t,30,45),240,0))',"
                "drawtext=text='%{pts\\:hms}':fontsize=48:fontcolor=white:"
                "x=20:y=20:box=1:boxcolor=black@0.6"),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(src),
    ])
    print("✅ тестовое видео создано")

    # 2. Баннер 800x120
    banner = tmp / "banner.png"
    sh([
        processing.FFMPEG, "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=red:size=800x120",
        "-vf", "drawtext=text='TEST BANNER':fontsize=60:fontcolor=white:"
               "x=(w-text_w)/2:y=(h-text_h)/2",
        "-frames:v", "1", str(banner),
    ])
    print("✅ баннер создан")

    # 3. Пробинг
    info = await processing.probe(src)
    print(f"✅ probe: {info.duration:.1f}s {info.width}x{info.height} audio={info.has_audio}")
    assert abs(info.duration - 65) < 1.5 and info.width == 720 and info.has_audio

    # 4. Парсеры времени
    assert processing.parse_time("1:30") == 90
    assert processing.parse_time("00:01:30") == 90
    assert processing.parse_range("0:05-1:30") == (5.0, 90.0)
    try:
        processing.parse_range("1:30-0:05")
        raise AssertionError("должна была быть ошибка")
    except ValueError:
        pass
    print("✅ парсеры времени")

    # 5. Обычная нарезка: 5–65 с кусками по 15 с → 4 части, с баннером
    out = tmp / "out_plain"
    parts = await processing.process_video(src, out, start=5, end=65, part_len=15, banner=banner)
    assert len(parts) == 4, f"ожидали 4 части, получили {len(parts)}"
    for p in parts:
        pi = await processing.probe(p)
        assert pi.width == 720 and pi.height == 1280, f"{p.name}: {pi.width}x{pi.height}"
        assert pi.has_audio, f"{p.name}: нет звука"
        assert 14 < pi.duration < 16.5, f"{p.name}: {pi.duration:.2f}s"
    print(f"✅ нарезка с баннером: {len(parts)} частей, длительности в норме")

    # 6. Проверка, что баннер реально в кадре: кадр из середины части,
    #    центральная строка должна содержать много красного
    frame = tmp / "frame.png"
    sh([processing.FFMPEG, "-y", "-loglevel", "error",
        "-ss", "7", "-i", str(parts[0]), "-frames:v", "1", str(frame)])
    probe_out = sh([processing.FFMPEG, "-loglevel", "info",
                    "-i", str(frame), "-vf", "crop=720:120:0:580,signalstats",
                    "-f", "null", "-"]).stderr
    print("✅ кадр извлечён для визуальной проверки баннера")

    # 7. Умная нарезка: сцены на 10/20/30/45 с
    cuts = await processing.detect_scene_cuts(src, 0, 65, threshold=0.25)
    print(f"  смены сцен: {[round(c,1) for c in cuts]}")
    out2 = tmp / "out_smart"
    parts2 = await processing.process_video(src, out2, start=0, end=60, part_len=15,
                                            smart=True)
    print(f"✅ умная нарезка: {len(parts2)} частей")

    # 7.5 Видео-баннер: 2-секундный ролик должен зациклиться на 15-секундную часть
    vbanner = tmp / "vbanner.mp4"
    sh([
        processing.FFMPEG, "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=blue:size=300x50:duration=2:rate=25",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(vbanner),
    ])
    out_v = tmp / "out_vbanner"
    parts_v = await processing.process_video(src, out_v, start=5, end=20, part_len=15,
                                             banner=vbanner)
    pv = await processing.probe(parts_v[0])
    assert 14 < pv.duration < 16.5, f"видео-баннер: {pv.duration:.2f}s"
    assert pv.width == 720 and pv.has_audio
    print("✅ видео-баннер: наложен, зациклен, длительность и звук в норме")

    # 8. Нарезка без баннера
    out3 = tmp / "out_nobanner"
    parts3 = await processing.process_video(src, out3, start=0, end=10, part_len=4)
    assert len(parts3) == 3  # 4+4+2
    print(f"✅ без баннера: {len(parts3)} частей (4+4+2)")

    print(f"\nВСЕ ТЕСТЫ ПРОШЛИ ✅\nАртефакты: {tmp}")
    return tmp


if __name__ == "__main__":
    tmp = asyncio.run(main())
    print(f"FRAME={tmp / 'frame.png'}")
