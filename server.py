"""HTTP-сервер мини-аппа: отдаёт index.html и видео по одноразовым токенам
(с поддержкой Range-запросов для перемотки в плеере)."""
from __future__ import annotations

import re
import secrets
from pathlib import Path

from aiohttp import web

WEBAPP_DIR = Path(__file__).parent / "webapp"

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


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/video/{token}", video)
    return app
