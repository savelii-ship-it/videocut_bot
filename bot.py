"""Telegram-бот для нарезки видео на части с опциональным баннером.

Сценарий:
  1. Пользователь присылает видео.
  2. Выбирает временной отрезок — в мини-аппе или вручную (0:05-1:30).
  3. Выбирает длину части и «умную» нарезку по сценам.
  4. Баннер (если загружен через /banner) кладётся на всю ширину по центру.
  5. Бот присылает все части обратно.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from dotenv import load_dotenv

import processing
import server

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")  # https-URL сервера мини-аппа
# Render и подобные отдают порт через PORT; локально — HTTP_PORT или 8080
HTTP_PORT = int(os.getenv("HTTP_PORT") or os.getenv("PORT", "8080"))
DATA_DIR = Path(os.getenv("DATA_DIR", "data")).resolve()
# Кросс-пинг резервного инстанса: бесплатные хостинги не усыпляют друг друга
PING_URLS = [u.strip() for u in os.getenv("PING_URLS", "").split(",") if u.strip()]
MAX_TG_BYTES = 49 * 1024 * 1024        # лимит отправки ботом ~50 МБ
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # лимит скачивания через Bot API

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("videocut")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


class CutStates(StatesGroup):
    waiting_range = State()        # ручной ввод отрезка
    waiting_partlen = State()      # ручной ввод длины части
    waiting_banner = State()       # ждём картинку/видео-баннер
    waiting_cookies = State()      # ждём cookies.txt для YouTube
    settings_len = State()         # своя длина части в настройках


# ---------------------------------------------------------------------------
# Настройки пользователя (settings.json рядом с данными)
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS = {"part_len": 30.0, "use_banner": True, "smart": False, "quick": False}


def settings_path(user_id: int) -> Path:
    return user_dir(user_id) / "settings.json"


def load_settings(user_id: int) -> dict:
    p = settings_path(user_id)
    if p.exists():
        try:
            return {**DEFAULT_SETTINGS, **json.loads(p.read_text())}
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(user_id: int, settings: dict) -> None:
    settings_path(user_id).write_text(json.dumps(settings, ensure_ascii=False))


def _settings_text(s: dict) -> str:
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"• Длина части: <b>{s['part_len']:g} с</b>\n"
        f"• Баннер: <b>{'вкл' if s['use_banner'] else 'выкл'}</b>\n"
        f"• Умная нарезка: <b>{'вкл' if s['smart'] else 'выкл'}</b>\n"
        f"• Быстрый режим: <b>{'вкл ⚡' if s['quick'] else 'выкл'}</b>\n\n"
        + ("В быстром режиме после видео/ссылки спрашиваю только отрезок — "
           "остальное берётся отсюда." if s["quick"] else
           "Включи быстрый режим — буду спрашивать только отрезок.")
    )


def _settings_kb(s: dict) -> InlineKeyboardMarkup:
    def mark(val: float) -> str:
        return "✅ " if s["part_len"] == val else ""

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{mark(15)}15 с", callback_data="s_len_15"),
         InlineKeyboardButton(text=f"{mark(30)}30 с", callback_data="s_len_30"),
         InlineKeyboardButton(text=f"{mark(60)}60 с", callback_data="s_len_60")],
        [InlineKeyboardButton(text="✏️ Своя длина", callback_data="s_len_custom")],
        [InlineKeyboardButton(
            text=f"🖼 Баннер: {'вкл' if s['use_banner'] else 'выкл'}",
            callback_data="s_toggle_banner")],
        [InlineKeyboardButton(
            text=f"🧠 Умная нарезка: {'вкл' if s['smart'] else 'выкл'}",
            callback_data="s_toggle_smart")],
        [InlineKeyboardButton(
            text=f"⚡ Быстрый режим: {'вкл' if s['quick'] else 'выкл'}",
            callback_data="s_toggle_quick")],
    ])


@dp.message(Command("settings"))
async def cmd_settings(message: Message) -> None:
    s = load_settings(message.from_user.id)
    await message.answer(_settings_text(s), reply_markup=_settings_kb(s))


@dp.callback_query(F.data.startswith("s_"))
async def settings_cb(cb: CallbackQuery, state: FSMContext) -> None:
    uid = cb.from_user.id
    s = load_settings(uid)
    action = cb.data[2:]
    if action == "len_custom":
        await state.set_state(CutStates.settings_len)
        await cb.message.answer("Введи длину части в секундах (1–3600):")
        await cb.answer()
        return
    if action.startswith("len_"):
        s["part_len"] = float(action[4:])
    elif action == "toggle_banner":
        s["use_banner"] = not s["use_banner"]
    elif action == "toggle_smart":
        s["smart"] = not s["smart"]
    elif action == "toggle_quick":
        s["quick"] = not s["quick"]
    save_settings(uid, s)
    await cb.message.edit_text(_settings_text(s), reply_markup=_settings_kb(s))
    await cb.answer()


@dp.message(CutStates.settings_len)
async def settings_custom_len(message: Message, state: FSMContext) -> None:
    try:
        val = float(message.text.replace(",", ".").strip())
        if not 1 <= val <= 3600:
            raise ValueError
    except ValueError:
        await message.answer("❌ Число от 1 до 3600, например <code>25</code>")
        return
    s = load_settings(message.from_user.id)
    s["part_len"] = val
    save_settings(message.from_user.id, s)
    await state.set_state(None)
    await message.answer(f"Сохранено: {val:g} с ✅", reply_markup=_settings_kb(s))


def user_dir(user_id: int) -> Path:
    d = DATA_DIR / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


BANNER_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".gif", ".webm")


def banner_path(user_id: int) -> Path | None:
    """Файл баннера пользователя (картинка или видео), None если не задан."""
    d = user_dir(user_id)
    for ext in BANNER_EXTS:
        p = d / f"banner{ext}"
        if p.exists():
            return p
    return None


# ---------------------------------------------------------------------------
# Базовые команды
# ---------------------------------------------------------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "✂️ <b>Видеонарезчик</b>\n\n"
        "1. Пришли мне видео или ссылку на него (YouTube и др.)\n"
        "2. Выбери временной отрезок — в мини-аппе или вручную\n"
        "3. Укажи длину части (15/30/60 с…)\n"
        "4. Получи все части обратно\n\n"
        "По желанию: /banner — картинка или видео/GIF, которые встанут "
        "на всю ширину посередине каждой части. /delbanner — убрать.\n"
        "/settings — длина части, баннер и ⚡ быстрый режим (только отрезок — и готово).\n"
        "Если YouTube ругается «подтвердите, что вы не бот» — /cookies."
    )


@dp.message(Command("banner"))
async def cmd_banner(message: Message, state: FSMContext) -> None:
    await state.set_state(CutStates.waiting_banner)
    await message.answer(
        "Пришли картинку (PNG/JPG) или видео/GIF — это станет баннером "
        "на всю ширину по центру. /cancel — отмена."
    )


@dp.message(Command("delbanner"))
async def cmd_delbanner(message: Message) -> None:
    p = banner_path(message.from_user.id)
    if p:
        p.unlink()
        await message.answer("Баннер удалён 🗑")
    else:
        await message.answer("Баннера и не было.")


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())


@dp.message(CutStates.waiting_banner, F.photo | F.video | F.animation | F.document)
async def got_banner(message: Message, state: FSMContext) -> None:
    # Картинка, видео или GIF/анимация — всё годится для баннера
    if message.photo:
        file_id, ext = message.photo[-1].file_id, ".png"
    elif message.animation:
        file_id, ext = message.animation.file_id, ".mp4"
    elif message.video:
        file_id, ext = message.video.file_id, ".mp4"
    else:
        doc = message.document
        mime = doc.mime_type or ""
        if not (mime.startswith("image/") or mime.startswith("video/")):
            await message.answer("Это не картинка и не видео. Пришли PNG/JPG/видео/GIF.")
            return
        ext = Path(doc.file_name or "banner").suffix or (".png" if mime.startswith("image/") else ".mp4")
        if ext.lower() not in BANNER_EXTS:
            ext = ".mp4" if mime.startswith("video/") else ".png"
        file_id = doc.file_id

    # старый баннер (другого формата) убираем
    old = banner_path(message.from_user.id)
    if old:
        old.unlink()
    dest = user_dir(message.from_user.id) / f"banner{ext.lower()}"
    file = await bot.get_file(file_id)
    await bot.download_file(file.file_path, dest)
    # set_state(None), а не clear(): не стираем данные о загруженном видео
    await state.set_state(None)
    kind = "Видео-баннер" if dest.suffix == ".mp4" else "Баннер"
    await message.answer(f"{kind} сохранён ✅ Теперь пришли видео (или режь уже загруженное).")


# ---------------------------------------------------------------------------
# Приём видео
# ---------------------------------------------------------------------------

async def _save_video(message: Message, file_id: str, file_size: int | None) -> Path | None:
    if file_size and file_size > MAX_DOWNLOAD_BYTES:
        await message.answer(
            "⚠️ Файл больше 20 МБ — через обычный Bot API я его не скачаю. "
            "Сожми видео или подними локальный Bot API сервер."
        )
        return None
    job_id = uuid.uuid4().hex[:12]
    job_dir = user_dir(message.from_user.id) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    src = job_dir / "input.mp4"
    file = await bot.get_file(file_id)
    await bot.download_file(file.file_path, src)
    try:
        info = await processing.probe(src)
    except processing.ProcessingError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        await message.answer(f"Не смог прочитать видео: {e}")
        return None
    return src


def _webapp_keyboard(src: Path, duration: float, has_banner: bool) -> ReplyKeyboardMarkup:
    token = server.register_video(src)
    url = f"{WEBAPP_URL}/?token={token}&dur={duration:.2f}&banner={int(has_banner)}"
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="✂️ Выбрать отрезок в мини-аппе",
                                  web_app=WebAppInfo(url=url))]],
        resize_keyboard=True,
    )


async def _offer_cut(message: Message, state: FSMContext, src: Path) -> None:
    info = await processing.probe(src)
    await state.update_data(src=str(src), duration=info.duration)
    has_banner = banner_path(message.from_user.id) is not None

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⌨️ Ввести время вручную", callback_data="manual_range")
    ]])
    text = (
        f"📹 Видео получено: <b>{processing.fmt_time(info.duration)}</b>, "
        f"{info.width}×{info.height}"
        + ("\n🖼 Баннер: загружен" if has_banner else "\n🖼 Баннер: нет (/banner)")
        + "\n\nВыбери отрезок в мини-аппе или введи вручную."
    )
    if WEBAPP_URL:
        await message.answer(text, reply_markup=_webapp_keyboard(src, info.duration, has_banner))
        await message.answer("Или:", reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)


@dp.message(StateFilter(None), F.video)
async def got_video(message: Message, state: FSMContext) -> None:
    src = await _save_video(message, message.video.file_id, message.video.file_size)
    if src:
        await _offer_cut(message, state, src)


@dp.message(StateFilter(None), F.document)
async def got_document(message: Message, state: FSMContext) -> None:
    doc = message.document
    if not (doc.mime_type or "").startswith("video/"):
        await message.answer("Это не видео. Пришли видеофайл.")
        return
    src = await _save_video(message, doc.file_id, doc.file_size)
    if src:
        await _offer_cut(message, state, src)


# ---------------------------------------------------------------------------
# Ссылка на видео (YouTube и др. — через yt-dlp)
# ---------------------------------------------------------------------------

URL_RE = re.compile(r"https?://[^\s]+")
MAX_URL_BYTES = 2 * 1024 * 1024 * 1024  # не тянем файлы больше 2 ГБ


def cookies_path(user_id: int) -> Path:
    return user_dir(user_id) / "cookies.txt"


@dp.message(Command("cookies"))
async def cmd_cookies(message: Message, state: FSMContext) -> None:
    await state.set_state(CutStates.waiting_cookies)
    await message.answer(
        "YouTube иногда просит «подтвердите, что вы не бот» — лечится куками.\n\n"
        "1. В Chrome поставь расширение <b>Get cookies.txt LOCALLY</b>\n"
        "2. Зайди на youtube.com (залогиненным) и экспортируй cookies.txt\n"
        "3. Пришли файл сюда\n\n"
        "/delcookies — удалить, /cancel — отмена."
    )


@dp.message(Command("delcookies"))
async def cmd_delcookies(message: Message) -> None:
    p = cookies_path(message.from_user.id)
    if p.exists():
        p.unlink()
        await message.answer("Cookies удалены 🗑")
    else:
        await message.answer("Cookies и не было.")


@dp.message(CutStates.waiting_cookies, F.document)
async def got_cookies(message: Message, state: FSMContext) -> None:
    doc = message.document
    if doc.file_size and doc.file_size > 1024 * 1024:
        await message.answer("❌ Файл слишком большой для cookies.txt")
        return
    dest = cookies_path(message.from_user.id)
    file = await bot.get_file(doc.file_id)
    await bot.download_file(file.file_path, dest)
    content = dest.read_text(errors="replace")
    if "youtube.com" not in content and "google.com" not in content:
        dest.unlink(missing_ok=True)
        await message.answer("❌ В файле нет кук YouTube. Экспортируй с youtube.com и пришли ещё раз.")
        return
    await state.set_state(None)
    await message.answer("Cookies сохранены ✅ Теперь кидай ссылку на YouTube.")


async def download_by_url(url: str, dest: Path, cookies: Path | None = None) -> None:
    env = dict(os.environ)
    # deno нужен yt-dlp для JS-challenge YouTube
    env["PATH"] = f"{Path.home()}/.local/bin:" + env.get("PATH", "")
    cmd = [
        sys.executable, "-m", "yt_dlp", "--no-playlist",
        "--ffmpeg-location", str(Path(processing.FFMPEG).parent),
        "-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "--merge-output-format", "mp4",
        "--no-progress", "--no-warnings",
    ]
    if cookies and cookies.exists():
        cmd += ["--cookies", str(cookies)]
    cmd += ["-o", str(dest), url]
    proc = await asyncio.create_subprocess_exec(
        *cmd, env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        tail = out.decode("utf-8", errors="replace")[-400:]
        if "Sign in to confirm" in tail:
            raise processing.ProcessingError(
                "YouTube просит подтверждение «не бот». Пришли куки через /cookies — и скачаю."
            )
        raise processing.ProcessingError(f"не удалось скачать по ссылке: {tail}")


@dp.message(StateFilter(None), F.text.regexp(URL_RE))
async def got_url(message: Message, state: FSMContext) -> None:
    url = URL_RE.search(message.text).group(0)
    status = await message.answer("⬇️ Скачиваю видео по ссылке… (YouTube может качать медленно)")

    job_id = uuid.uuid4().hex[:12]
    job_dir = user_dir(message.from_user.id) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    dest = job_dir / "input.mp4"
    try:
        await download_by_url(url, dest, cookies=cookies_path(message.from_user.id))
    except processing.ProcessingError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        await status.edit_text(f"❌ {e}")
        return

    if dest.stat().st_size > MAX_URL_BYTES:
        shutil.rmtree(job_dir, ignore_errors=True)
        await status.edit_text("❌ Видео больше 2 ГБ — такое не потяну.")
        return
    try:
        await processing.probe(dest)
    except processing.ProcessingError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        await status.edit_text(f"❌ Скачалось что-то не то: {e}")
        return
    await status.delete()
    await _offer_cut(message, state, dest)


# ---------------------------------------------------------------------------
# Ручной ввод отрезка и длины части
# ---------------------------------------------------------------------------

@dp.callback_query(F.data == "manual_range")
async def manual_range(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CutStates.waiting_range)
    await cb.message.answer(
        "Введи отрезок в формате <code>начало-конец</code>.\n"
        "Например: <code>0:05-1:30</code> или <code>00:00:10-00:02:00</code>",
        reply_markup=ReplyKeyboardRemove(),
    )
    await cb.answer()


@dp.message(CutStates.waiting_range)
async def got_range(message: Message, state: FSMContext) -> None:
    try:
        start, end = processing.parse_range(message.text)
    except ValueError as e:
        await message.answer(f"❌ {e}\nПопробуй ещё раз, например <code>0:05-1:30</code>")
        return
    data = await state.get_data()
    duration = float(data.get("duration", 0))
    if duration and start >= duration:
        await message.answer(f"❌ Начало за пределами видео ({processing.fmt_time(duration)}).")
        return
    end = min(end, duration) if duration else end
    await state.update_data(start=start, end=end)

    s = load_settings(message.from_user.id)
    if s["quick"]:
        # Быстрый режим: длина/баннер/умная — из настроек, без вопросов
        await state.update_data(
            part_len=s["part_len"], smart=s["smart"],
            use_banner=s["use_banner"] and banner_path(message.from_user.id) is not None,
        )
        await state.set_state(None)
        await _run_job(message, state)
        return
    await state.set_state(CutStates.waiting_partlen)
    await _ask_partlen(message, start, end)


async def _ask_partlen(message: Message, start: float, end: float) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="15 с", callback_data="len_15"),
         InlineKeyboardButton(text="30 с", callback_data="len_30"),
         InlineKeyboardButton(text="60 с", callback_data="len_60")],
        [InlineKeyboardButton(text="✏️ Своя длина", callback_data="len_custom")],
        [InlineKeyboardButton(text="🧠 Умная нарезка (30 с)", callback_data="len_smart_30")],
    ])
    await message.answer(
        f"Отрезок: <b>{processing.fmt_time(start)} → {processing.fmt_time(end)}</b>\n"
        "На части какой длины резать?",
        reply_markup=kb,
    )


@dp.message(CutStates.waiting_partlen)
async def got_custom_len(message: Message, state: FSMContext) -> None:
    try:
        part_len = float(message.text.replace(",", ".").strip())
        if not 1 <= part_len <= 3600:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введи число секунд от 1 до 3600, например <code>25</code>")
        return
    await state.update_data(part_len=part_len, smart=False)
    await state.set_state(None)
    await _ask_banner_choice(message, state)


@dp.callback_query(F.data.startswith("len_"))
async def got_len(cb: CallbackQuery, state: FSMContext) -> None:
    val = cb.data[4:]
    if val == "custom":
        await cb.message.answer("Введи длину части в секундах (например <code>25</code>):")
        await cb.answer()
        return  # остаёмся в CutStates.waiting_partlen
    smart = val.startswith("smart_")
    part_len = float(val.split("_")[-1])
    await state.update_data(part_len=part_len, smart=smart)
    await state.set_state(None)
    await cb.answer()
    await _ask_banner_choice(cb.message, state)


# ---------------------------------------------------------------------------
# Баннер и запуск
# ---------------------------------------------------------------------------

async def _ask_banner_choice(message: Message, state: FSMContext) -> None:
    # chat.id == user.id в личке; у cb.message from_user — это бот, поэтому chat.id надёжнее
    if banner_path(message.chat.id) is not None:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🖼 С баннером", callback_data="banner_yes"),
            InlineKeyboardButton(text="🚫 Без баннера", callback_data="banner_no"),
        ]])
        await message.answer("Вставить твой баннер посередине на всю ширину?", reply_markup=kb)
    else:
        await state.update_data(use_banner=False)
        await _run_job(message, state)


@dp.callback_query(F.data.in_({"banner_yes", "banner_no"}))
async def got_banner_choice(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(use_banner=cb.data == "banner_yes")
    await cb.answer()
    await _run_job(cb.message, state)


# ---------------------------------------------------------------------------
# Данные из мини-аппа
# ---------------------------------------------------------------------------

@dp.message(F.web_app_data)
async def got_webapp_data(message: Message, state: FSMContext) -> None:
    try:
        payload = json.loads(message.web_app_data.data)
        start = float(payload["start"])
        end = float(payload["end"])
        part_len = float(payload["part_len"])
        use_banner = bool(payload.get("banner"))
        smart = bool(payload.get("smart"))
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        await message.answer("❌ Мини-апп прислал некорректные данные. Попробуй вручную.")
        return

    data = await state.get_data()
    src = data.get("src")
    if not src or not Path(src).exists():
        await message.answer("❌ Видео потерялось. Пришли его ещё раз.")
        return
    if end <= start:
        await message.answer("❌ Конец отрезка должен быть позже начала.")
        return

    if use_banner and banner_path(message.from_user.id) is None:
        use_banner = False

    await state.update_data(start=start, end=end, part_len=part_len,
                            smart=smart, use_banner=use_banner)
    await message.answer("Принято ✅ Запускаю нарезку…", reply_markup=ReplyKeyboardRemove())
    await _run_job(message, state)


# ---------------------------------------------------------------------------
# Обработка и отправка
# ---------------------------------------------------------------------------

async def _run_job(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    src = data.get("src")
    start = float(data.get("start", 0))
    end = float(data.get("end", 0))
    part_len = float(data.get("part_len", 30))
    smart = bool(data.get("smart"))
    use_banner = bool(data.get("use_banner"))
    await state.clear()

    if not src or not Path(src).exists():
        await message.answer("❌ Видео не найдено. Пришли его ещё раз.")
        return

    src_path = Path(src)
    out_dir = src_path.parent / "out"
    banner = banner_path(message.chat.id) if use_banner else None
    if banner and not banner.exists():
        banner = None

    progress_msg = await message.answer("⏳ Готовлю…")

    async def on_progress(done: int, total: int) -> None:
        try:
            await progress_msg.edit_text(f"⏳ Нарезаю: {done}/{total} частей…")
        except Exception:
            pass

    try:
        parts = await processing.process_video(
            src_path, out_dir, start, end, part_len,
            banner=banner, smart=smart, on_progress=on_progress,
        )
    except processing.ProcessingError as e:
        await progress_msg.edit_text(f"❌ Ошибка обработки: {e}")
        shutil.rmtree(src_path.parent, ignore_errors=True)
        return

    await progress_msg.edit_text(f"📤 Отправляю {len(parts)} частей…")
    sent, failed = 0, 0
    for i, part in enumerate(parts, 1):
        size = part.stat().st_size
        if size > MAX_TG_BYTES:
            failed += 1
            await message.answer(
                f"⚠️ Часть {i} весит {size / 1024 / 1024:.1f} МБ — больше лимита Telegram (~50 МБ). "
                "Попробуй короче отрезок или меньше длину части."
            )
            continue
        try:
            await message.answer_video(
                FSInputFile(part),
                caption=f"🎬 Часть {i}/{len(parts)}",
                supports_streaming=True,
            )
            sent += 1
        except Exception as e:
            failed += 1
            log.exception("send failed")
            await message.answer(f"⚠️ Не смог отправить часть {i}: {e}")

    await progress_msg.edit_text(
        f"✅ Готово: отправлено {sent} из {len(parts)} частей."
        + (f" Не отправлено: {failed}." if failed else "")
    )
    shutil.rmtree(src_path.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Точка входа: бот (polling) + HTTP-сервер мини-аппа в одном процессе
# ---------------------------------------------------------------------------

async def keepalive_loop() -> None:
    """Пингуем резервный инстанс раз в 4 мин — бесплатные хостинги не засыпают."""
    import aiohttp as _ah

    async with _ah.ClientSession(timeout=_ah.ClientTimeout(total=15)) as s:
        while True:
            for u in PING_URLS:
                try:
                    async with s.get(u) as r:
                        log.debug("keepalive %s -> %s", u, r.status)
                except Exception as e:
                    log.warning("keepalive %s упал: %s", u, e)
            await asyncio.sleep(240)


async def main() -> None:
    runner = None
    if WEBAPP_URL:
        app = server.create_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
        await site.start()
        log.info("Mini app server: http://0.0.0.0:%s (public: %s)", HTTP_PORT, WEBAPP_URL)
    else:
        log.warning("WEBAPP_URL не задан — мини-апп отключён, только ручной ввод.")

    keepalive = None
    if PING_URLS:
        keepalive = asyncio.create_task(keepalive_loop())
        log.info("Keepalive пинги: %s", ", ".join(PING_URLS))

    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        if keepalive:
            keepalive.cancel()
        if runner:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
