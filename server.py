"""HTTP-сервер мини-аппа: отдаёт index.html и видео по одноразовым токенам
(с поддержкой Range-запросов для перемотки в плеере).
Плюс /internal/* — обмен пользовательскими файлами между инстансами-репликами."""
from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

from aiohttp import web

WEBAPP_DIR = Path(__file__).parent / "webapp"
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))

# token -> Path видеофайла
_tokens: dict[str, Path] = {}


def register_video(path: Path) -> str:
    token = secrets.token_urlsafe(16)
    _tokens[token] = path
    return token


def revoke_video(token: str) -> None:
    _tokens.pop(token, None)


async def index(request: web.Request) -> web.Response:
    return web.FileResponse(WEBAPP_DIR / "index.html")


async def health(request: web.Request) -> web.Response:
    # Для аптайм-мониторов и health-check'ов хостингов
    return web.Response(text="ok")


_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


async def video(request: web.Request) -> web.StreamResponse:
    token = request.match_info["token"]
    path = _tokens.get(token)
    if not path or not path.exists():
        raise web.HTTPNotFound(text="unknown or expired token")

    size = path.stat().st_size
    range_header = request.headers.get("Range")
    if not range_header:
        return web.FileResponse(
            path,
            headers={"Content-Type": "video/mp4", "Accept-Ranges": "bytes"},
        )

    m = _RANGE_RE.fullmatch(range_header.strip())
    if not m:
        raise web.HTTPBadRequest(text="bad range")
    start_s, end_s = m.groups()
    start = int(start_s) if start_s else 0
    end = int(end_s) if end_s else size - 1
    end = min(end, size - 1)
    if start > end:
        raise web.HTTPRequestRangeNotSatisfiable()
    length = end - start + 1

    response = web.StreamResponse(
        status=206,
        headers={
            "Content-Type": "video/mp4",
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        },
    )
    await response.prepare(request)
    with path.open("rb") as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(256 * 1024, remaining))
            if not chunk:
                break
            await response.write(chunk)
            remaining -= len(chunk)
    await response.write_eof()
    return response


# ---------------------------------------------------------------------------
# Репликация пользовательских файлов между инстансами (polling-фейловер)
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"[\w.\-*]+")


def _secret_ok(request: web.Request) -> bool:
    secret = os.getenv("INTERNAL_SECRET", "")
    return bool(secret) and request.headers.get("X-Internal-Secret") == secret


def _target_dir(request: web.Request) -> Path:
    uid = request.query.get("uid", "")
    if not uid.isdigit():
        raise web.HTTPBadRequest(text="bad uid")
    d = DATA_DIR / uid
    d.mkdir(parents=True, exist_ok=True)
    return d


async def internal_push(request: web.Request) -> web.Response:
    """Принять файл от другого инстанса: POST /internal/push?uid=&name= (тело = байты)."""
    if not _secret_ok(request):
        raise web.HTTPForbidden()
    name = request.query.get("name", "")
    if not _NAME_RE.fullmatch(name) or "*" in name:
        raise web.HTTPBadRequest(text="bad name")
    data = await request.read()
    if len(data) > 50 * 1024 * 1024:
        raise web.HTTPRequestEntityTooLarge(max_size=50 * 1024 * 1024, actual_size=len(data))
    (_target_dir(request) / name).write_bytes(data)
    return web.Response(text="ok")


async def internal_delete(request: web.Request) -> web.Response:
    """Удалить файлы по glob-паттерну: POST /internal/delete?uid=&pattern=banner.*"""
    if not _secret_ok(request):
        raise web.HTTPForbidden()
    pattern = request.query.get("pattern", "")
    if not _NAME_RE.fullmatch(pattern):
        raise web.HTTPBadRequest(text="bad pattern")
    d = _target_dir(request)
    for p in d.glob(pattern):
        if p.is_file():
            p.unlink()
    return web.Response(text="ok")


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/video/{token}", video)
    app.router.add_post("/internal/push", internal_push)
    app.router.add_post("/internal/delete", internal_delete)
    return app
