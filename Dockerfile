FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    HTTP_PORT=7860 \
    DATA_DIR=/data \
    DENO_NO_PROMPT=1 \
    DENO_NO_UPDATE_CHECK=1

WORKDIR /app

# deno — JS-рантайм, нужен yt-dlp (EJS) и POT-провайдеру;
# node нужен только для установки node_modules (нативные prebuilds canvas);
# libcairo и др. — рантайм-зависимости canvas
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       curl unzip ca-certificates gnupg \
       libcairo2 libpango-1.0-0 libglib2.0-0 libjpeg62-turbo libgif7 librsvg2-2 \
    && curl -sL -o /tmp/deno.zip \
       https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip \
    && unzip -o /tmp/deno.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/deno \
    && rm -f /tmp/deno.zip \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# POT-провайдер: генерирует proof-of-origin токены, без которых YouTube
# не отдаёт видео датацентровым IP (HTTP-сервер на 127.0.0.1:4416).
# node_modules ставим через npm (там prebuilt-бинарники canvas),
# а запускаем под deno — как в официальном образе провайдера.
RUN curl -sL -o /tmp/pot.tar.gz \
       https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/2.0.2.tar.gz \
    && tar -xzf /tmp/pot.tar.gz -C /opt \
    && mv /opt/bgutil-ytdlp-pot-provider-2.0.2 /opt/pot \
    && rm /tmp/pot.tar.gz \
    && cd /opt/pot/server \
    && npm ci --omit=dev --no-audit --no-fund \
    && deno cache --frozen src/main.ts

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --force-reinstall --no-deps \
       "yt-dlp @ https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/latest/download/yt-dlp.tar.gz" \
    && python -c "import static_ffmpeg; static_ffmpeg.run.get_or_fetch_platform_executables_else_raise()"

COPY . .

EXPOSE 7860
CMD ["sh", "entrypoint.sh"]
