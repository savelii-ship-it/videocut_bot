"""End-to-end тест логики бота без сети: поддельные апдейты Telegram
прогоняются через настоящий диспетчер aiogram, API-вызовы записывает
MockedSession. Проверяем весь путь: /start → видео → /banner+фото →
данные мини-аппа → ffmpeg-нарезка → отправка частей."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "123456789:AAEaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
os.environ["WEBAPP_URL"] = "https://test.example.com"
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="videocut_e2e_data_")

sys.path.insert(0, str(Path(__file__).parent))

from aiogram.types import Message, Update  # noqa: E402

import bot as botmod  # noqa: E402
import processing  # noqa: E402


class FakeBot:
    """Минимальный мок Bot: записывает все API-вызовы, сеть не трогает."""

    def __init__(self):
        self.id = 999
        self.token = os.environ["BOT_TOKEN"]
        self.requests: list = []

    async def __call__(self, method, request_timeout=None):
        self.requests.append(method)
        name = type(method).__name__
        if name in ("SendMessage", "SendVideo", "EditMessageText"):
            data = {"message_id": len(self.requests), "date": 1_800_000_000,
                    "chat": {"id": getattr(method, "chat_id", CHAT), "type": "private"}}
            if name in ("SendMessage", "EditMessageText"):
                data["text"] = getattr(method, "text", "")
            return Message.model_validate(data, context={"bot": self})
        return True

CHAT = 12345
TMP = Path(tempfile.mkdtemp(prefix="videocut_e2e_"))

# --- фикстуры: видео и баннер ---------------------------------------------
SRC = TMP / "src.mp4"
BANNER_IMG = TMP / "banner.png"


def make_fixtures():
    import subprocess
    subprocess.run([
        processing.FFMPEG, "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(SRC),
    ], check=True)
    subprocess.run([
        processing.FFMPEG, "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=red:size=600x80",
        "-frames:v", "1", str(BANNER_IMG),
    ], check=True)


# --- подмена сетевых вызовов ------------------------------------------------

def patch_bot(mock: FakeBot):
    botmod.bot = mock

    async def fake_get_file(file_id):
        kind = "photo" if "photo" in str(file_id) else "video"
        return SimpleNamespace(file_path=f"fake/{kind}.mp4", file_size=SRC.stat().st_size)

    async def fake_download_file(file_path, destination, timeout=None, chunk_size=None, seek=None):
        if "photo" in str(file_path):
            shutil.copy(BANNER_IMG, destination)
        else:
            shutil.copy(SRC, destination)
        return None

    botmod.bot.get_file = fake_get_file
    botmod.bot.download_file = fake_download_file


def upd(update_id: int, message: dict) -> Update:
    base = {
        "message_id": update_id,
        "from": {"id": CHAT, "is_bot": False, "first_name": "Tester"},
        "chat": {"id": CHAT, "type": "private"},
        "date": 1_800_000_000,
    }
    base.update(message)
    return Update.model_validate({"update_id": update_id, "message": base})


def sent_methods(mock: FakeBot) -> list[str]:
    return [type(m).__name__ for m in mock.requests]


async def main() -> None:
    make_fixtures()
    mock = FakeBot()
    patch_bot(mock)
    dp = botmod.dp

    # 1. /start
    await dp.feed_update(mock, upd(1, {
        "text": "/start",
        "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
    }))
    assert "SendMessage" in sent_methods(mock), sent_methods(mock)
    print("✅ /start → приветствие отправлено")

    # 2. Пользователь присылает видео
    before = len(mock.requests)
    await dp.feed_update(mock, upd(2, {
        "video": {"file_id": "vid_1", "file_unique_id": "u1", "width": 640,
                  "height": 360, "duration": 30, "file_size": SRC.stat().st_size,
                  "mime_type": "video/mp4"},
    }))
    new = sent_methods(mock)[before:]
    assert new.count("SendMessage") >= 2, new  # инфо о видео + кнопки
    reqs = mock.requests[before:]
    kb_texts = " ".join(str(m) for m in reqs)
    assert "web_app" in kb_texts, "нет кнопки мини-аппа"
    assert "token=" in kb_texts, "в URL мини-аппа нет токена"
    print("✅ видео принято → ответ с кнопкой мини-аппа (web_app + token)")

    # 3. /banner + фото
    await dp.feed_update(mock, upd(3, {
        "text": "/banner",
        "entities": [{"type": "bot_command", "offset": 0, "length": 7}],
    }))
    await dp.feed_update(mock, upd(4, {
        "photo": [{"file_id": "photo_1", "file_unique_id": "up1",
                   "width": 600, "height": 80, "file_size": BANNER_IMG.stat().st_size}],
    }))
    assert botmod.banner_path(CHAT) is not None, "баннер не сохранился"
    assert botmod.banner_path(CHAT).suffix == ".png"
    print("✅ /banner + фото → баннер сохранён")

    # 3.5 /banner + ВИДЕО → видео-баннер (заменяет картинку)
    await dp.feed_update(mock, upd(40, {
        "text": "/banner",
        "entities": [{"type": "bot_command", "offset": 0, "length": 7}],
    }))
    await dp.feed_update(mock, upd(41, {
        "video": {"file_id": "vid_banner", "file_unique_id": "ub1", "width": 300,
                  "height": 50, "duration": 2, "file_size": SRC.stat().st_size,
                  "mime_type": "video/mp4"},
    }))
    bp = botmod.banner_path(CHAT)
    assert bp is not None and bp.suffix == ".mp4", f"видео-баннер не сохранился: {bp}"
    print("✅ /banner + видео → видео-баннер сохранён (banner.mp4)")
    # возвращаем картинку-баннер для следующего сценария
    await dp.feed_update(mock, upd(42, {
        "text": "/banner",
        "entities": [{"type": "bot_command", "offset": 0, "length": 7}],
    }))
    await dp.feed_update(mock, upd(43, {
        "photo": [{"file_id": "photo_2", "file_unique_id": "up2",
                   "width": 600, "height": 80, "file_size": BANNER_IMG.stat().st_size}],
    }))
    assert botmod.banner_path(CHAT).suffix == ".png"
    print("✅ замена видео-баннера картинкой работает")

    # 4. Данные из мини-аппа: отрезок 2–28 с, части по 10 с, с баннером
    before = len(mock.requests)
    payload = {"start": 2.0, "end": 28.0, "part_len": 10.0,
               "smart": False, "banner": True}
    await dp.feed_update(mock, upd(5, {
        "web_app_data": {"data": json.dumps(payload), "button_text": "✂️ Нарезать"},
    }))
    new = sent_methods(mock)[before:]
    videos = new.count("SendVideo")
    assert videos == 3, f"ожидали 3 части, отправлено: {videos} ({new})"
    print(f"✅ мини-апп → нарезка с баннером → {videos} части отправлены")

    # 5. То же без баннера, умная нарезка
    before = len(mock.requests)
    # видео надо прислать заново (job-директория чистится после прогона)
    await dp.feed_update(mock, upd(6, {
        "video": {"file_id": "vid_2", "file_unique_id": "u2", "width": 640,
                  "height": 360, "duration": 30, "file_size": SRC.stat().st_size,
                  "mime_type": "video/mp4"},
    }))
    payload = {"start": 0.0, "end": 30.0, "part_len": 12.0,
               "smart": True, "banner": False}
    await dp.feed_update(mock, upd(7, {
        "web_app_data": {"data": json.dumps(payload), "button_text": "✂️ Нарезать"},
    }))
    new = sent_methods(mock)[before:]
    videos = new.count("SendVideo")
    assert videos >= 3, f"умная нарезка: {videos} частей ({new})"
    print(f"✅ умная нарезка без баннера → {videos} части")

    # 6. Ручной ввод: отрезок + длина + выбор баннера
    await dp.feed_update(mock, upd(8, {
        "video": {"file_id": "vid_3", "file_unique_id": "u3", "width": 640,
                  "height": 360, "duration": 30, "file_size": SRC.stat().st_size,
                  "mime_type": "video/mp4"},
    }))
    # нажимаем «ввести вручную»
    cb = {
        "id": "cb1", "from": {"id": CHAT, "is_bot": False, "first_name": "T"},
        "chat_instance": "ci", "data": "manual_range",
        "message": {"message_id": 100, "from": {"id": 999, "is_bot": True, "first_name": "B"},
                    "chat": {"id": CHAT, "type": "private"}, "date": 1_800_000_000,
                    "text": "x"},
    }
    await dp.feed_update(mock, Update.model_validate({"update_id": 9, "callback_query": cb}))
    await dp.feed_update(mock, upd(10, {"text": "0:05-0:25"}))
    cb2 = dict(cb, id="cb2", data="len_10")
    await dp.feed_update(mock, Update.model_validate({"update_id": 11, "callback_query": cb2}))
    cb3 = dict(cb, id="cb3", data="banner_yes")
    before = len(mock.requests)
    await dp.feed_update(mock, Update.model_validate({"update_id": 12, "callback_query": cb3}))
    new = sent_methods(mock)[before:]
    videos = new.count("SendVideo")
    assert videos == 2, f"ручной ввод: {videos} частей ({new})"
    print(f"✅ ручной ввод 0:05-0:25 по 10 с с баннером → {videos} части")

    # 7. Некорректный отрезок — бот должен ответить ошибкой, не упасть
    await dp.feed_update(mock, upd(13, {
        "video": {"file_id": "vid_4", "file_unique_id": "u4", "width": 640,
                  "height": 360, "duration": 30, "file_size": SRC.stat().st_size,
                  "mime_type": "video/mp4"},
    }))
    cb4 = dict(cb, id="cb4", data="manual_range")
    await dp.feed_update(mock, Update.model_validate({"update_id": 14, "callback_query": cb4}))
    before = len(mock.requests)
    await dp.feed_update(mock, upd(15, {"text": "привет-мир"}))
    new = sent_methods(mock)[before:]
    assert "SendMessage" in new and "SendVideo" not in new
    print("✅ мусорный ввод → вежливая ошибка, падений нет")

    # сбрасываем состояние ручного ввода после сценария 7
    await dp.feed_update(mock, upd(150, {
        "text": "/cancel",
        "entities": [{"type": "bot_command", "offset": 0, "length": 7}],
    }))

    # 8. Ссылка на видео (скачивание мокаем, остальное — по-настоящему)
    async def fake_download(url, dest, cookies=None):
        shutil.copy(SRC, dest)
    botmod.download_by_url = fake_download
    before = len(mock.requests)
    await dp.feed_update(mock, upd(16, {"text": "https://www.youtube.com/watch?v=test123"}))
    new = sent_methods(mock)[before:]
    kb = " ".join(str(m) for m in mock.requests[before:])
    assert "web_app" in kb, f"после ссылки нет кнопки мини-аппа: {new}"
    print("✅ ссылка на видео → скачано и предложена нарезка")

    # 9. Битая ссылка → понятная ошибка
    async def fail_download(url, dest, cookies=None):
        raise botmod.processing.ProcessingError("не удалось скачать по ссылке: 403")
    botmod.download_by_url = fail_download
    before = len(mock.requests)
    await dp.feed_update(mock, upd(17, {"text": "https://example.com/broken"}))
    new = sent_methods(mock)[before:]
    assert new.count("EditMessageText") == 1 and "SendVideo" not in new, new
    print("✅ битая ссылка → ошибка без падения")

    # 10. /settings + быстрый режим: после отрезка — сразу нарезка
    await dp.feed_update(mock, upd(18, {
        "text": "/settings",
        "entities": [{"type": "bot_command", "offset": 0, "length": 9}],
    }))
    cbs = dict(cb, id="cbs1", data="s_len_15")
    await dp.feed_update(mock, Update.model_validate({"update_id": 19, "callback_query": cbs}))
    cbs2 = dict(cb, id="cbs2", data="s_toggle_quick")
    await dp.feed_update(mock, Update.model_validate({"update_id": 20, "callback_query": cbs2}))
    s = botmod.load_settings(CHAT)
    assert s["part_len"] == 15.0 and s["quick"] is True, s
    print("✅ /settings: длина 15 с + быстрый режим сохранились")

    await dp.feed_update(mock, upd(21, {
        "video": {"file_id": "vid_5", "file_unique_id": "u5", "width": 640,
                  "height": 360, "duration": 30, "file_size": SRC.stat().st_size,
                  "mime_type": "video/mp4"},
    }))
    cb5 = dict(cb, id="cb5", data="manual_range")
    await dp.feed_update(mock, Update.model_validate({"update_id": 22, "callback_query": cb5}))
    before = len(mock.requests)
    await dp.feed_update(mock, upd(23, {"text": "0:05-0:35"}))
    new = sent_methods(mock)[before:]
    videos = new.count("SendVideo")
    assert videos == 2, f"быстрый режим: {videos} частей ({new})"
    print("✅ быстрый режим: отрезок → сразу 2 части по 15 с, без лишних вопросов")

    # откат настроек, чтобы не влиять на другие прогоны
    cbs3 = dict(cb, id="cbs3", data="s_toggle_quick")
    await dp.feed_update(mock, Update.model_validate({"update_id": 24, "callback_query": cbs3}))

    # 11. /cookies: сохранение и валидация кук YouTube
    cookies_fixture = TMP / "cookies.txt"
    cookies_fixture.write_text(
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t9999999999\tSID\ttest\n"
    )
    real_download = botmod.bot.download_file

    async def dl_cookies(file_path, destination, **kw):
        shutil.copy(cookies_fixture, destination)
    botmod.bot.download_file = dl_cookies
    await dp.feed_update(mock, upd(25, {
        "text": "/cookies",
        "entities": [{"type": "bot_command", "offset": 0, "length": 8}],
    }))
    await dp.feed_update(mock, upd(26, {
        "document": {"file_id": "doc_cookies", "file_unique_id": "ud1",
                     "file_name": "cookies.txt", "mime_type": "text/plain",
                     "file_size": 100},
    }))
    assert botmod.cookies_path(CHAT).exists(), "cookies не сохранились"
    print("✅ /cookies: файл сохранён и проверен")
    botmod.bot.download_file = real_download

    print("\nE2E: ВСЕ СЦЕНАРИИ ПРОШЛИ ✅")


if __name__ == "__main__":
    asyncio.run(main())
