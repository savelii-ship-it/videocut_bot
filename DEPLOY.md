# ☁️ Перенос бота в облако (бесплатно, без привязки к твоему Mac)

Архитектура failover: **две копии бота на двух независимых хостингах**.
Telegram отдаёт апдейты только одному поллеру — второй молча ждёт.
Упал первый → второй подхватывает сам, без вмешательства. У каждого
свой публичный адрес мини-аппа (зашит в его `WEBAPP_URL`).

Что понадобится от тебя: одна регистрация (почта или GitHub). Карта не нужна.

---

## Хостинг 1 и 2: Hugging Face Spaces (бесплатно, 2 vCPU / 16 ГБ на каждый)

Повтори шаги дважды — для `videocut-bot-1` и `videocut-bot-2`.

1. Зарегистрируйся на https://huggingface.co (кнопка Sign Up, можно через GitHub).
2. https://huggingface.co/new-space →
   - Name: `videocut-bot-1`
   - SDK: **Docker** → шаблон **Blank**
   - Hardware: **CPU basic (free)**
3. В созданном Space открой вкладку **Files** → **Add file** → **Upload files**
   и залей всё содержимое папки `videocut_bot` (Dockerfile, bot.py, processing.py,
   server.py, webapp/, requirements.txt).
4. Вкладка **Settings** → **Variables and secrets** → **New secret**:
   - `BOT_TOKEN` = токен от @BotFather
   - `WEBAPP_URL` = `https://ТВОЙ_ЛОГИН-videocut-bot-1.hf.space`
     (именно так, через дефис; адрес можно скопировать из кнопки «⋯ → Embed this Space»)
   - `HTTP_PORT` = `7860`
5. Space сам соберёт образ и запустит бота (вкладка **Logs** — там будет
   `Run polling for bot @ClipHubot`).

Проверка: открой `https://ТВОЙ_ЛОГИН-videocut-bot-1.hf.space/health` → `ok`.

## Чтобы Spaces не засыпали (бесплатный UptimeRobot)

Бесплатный Space засыпает после ~48 ч без HTTP-запросов. Будильник:

1. https://uptimerobot.com → Sign Up (бесплатно, без карты).
2. **Add New Monitor** → HTTP(s), URL: `https://ТВОЙ_ЛОГИН-videocut-bot-1.hf.space/health`,
   интервал 5 мин.
3. То же для второго Space.

Бонус: UptimeRobot пришлёт письмо, если оба хостинга лягут одновременно.

---

## Альтернатива: свой/любой VPS (если появится)

```bash
docker build -t videocut_bot .
docker run -d --restart always \
  -e BOT_TOKEN=... -e WEBAPP_URL=https://твой-домен -e HTTP_PORT=8080 \
  -p 8080:8080 --name videocut videocut_bot
```

---

## Замечания

- Данные (баннеры, настройки) живут в контейнере и сбрасываются при пересборке
  Space — на бесплатном тарифе это нормально, бот переспросит баннер.
- Оба Space качают ffmpeg/deno при сборке образа — в рантайме ничего не догружается.
- Лимиты: обработка видео CPU-bound; 2 vCPU хватает на нарезку 720p/1080p в
  реальном темпе (проверено: 4 части по 15 с ≈ 10–15 с).
