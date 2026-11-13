FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    HTTP_PORT=7860 \
    DATA_DIR=/data

WORKDIR /app

# deno — JS-рантайм, нужен yt-dlp для YouTube challenge
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip ca-certificates \
    && curl -sL -o /tmp/deno.zip \
       https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip \
    && unzip -o /tmp/deno.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/deno \
    && rm -f /tmp/deno.zip \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --force-reinstall --no-deps \
       "yt-dlp @ https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/latest/download/yt-dlp.tar.gz" \
    && python -c "import static_ffmpeg; static_ffmpeg.run.get_or_fetch_platform_executables_else_raise()"

COPY . .

EXPOSE 7860
CMD ["python", "bot.py"]
