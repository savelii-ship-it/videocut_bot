# ☁️ Текущая облачная схема (развёрнуто 13.11.2026)

Бот живёт в облаке, Mac не нужен. Два независимых инстанса на двух
аккаунтах Render (бесплатный тариф, без карты):

| Инстанс | URL | Аккаунт Render | Регион |
|---|---|---|---|
| `videocut-bot-1` | https://videocut-bot-1.onrender.com | savelii.turenko@gmail.com | Frankfurt |
| `videocut-bot-2` | https://videocut-bot-2.onrender.com | saveliyturenko@gmail.com | Oregon |

Код: https://github.com/savelii-ship-it/videocut_bot (публичный, секретов нет —
всё в env-переменных Render).

## Как работает failover

Оба инстанса крутят polling одновременно. Telegram отдаёт апдейты только
одному (обычно первому) — второй получает `TelegramConflictError` и молча
повторяет попытки каждые ~5 с. Упал/уснул первый → второй подхватывает
автоматически. Лимит 750 ч/мес на аккаунт Render покрывает ровно один
инстанс 24/7 — поэтому два аккаунта.

## Как не засыпают (без UptimeRobot)

Каждый инстанс раз в 4 минуты пингует `/health` другого (env `PING_URLS`).
Взаимные запросы не дают бесплатным инстансам уснуть после 15 минут простоя.

## Управление через API (Render)

- Аккаунт 1: ключ `videocut-deploy` (dashboard → Account Settings → API Keys)
- Аккаунт 2: ключ `videocut-deploy-2`
- Редеплой: `POST /v1/services/{id}/deploys` (autoDeploy выключен — после
  пуша на GitHub деплой запускается вручную через API)
- env: `PUT /v1/services/{id}/env-vars`

## Обновление кода

```bash
git push origin main        # код на GitHub
# затем по одному POST на /deploys каждого сервиса (см. выше)
```

## Локальный запуск (разработка)

```bash
pip install -r requirements.txt
cp .env.example .env   # вписать BOT_TOKEN; WEBAPP_URL — через туннель
python bot.py
```

Проверки: `python test_pipeline.py` (ffmpeg-пайплайн),
`python test_e2e.py` (13 сценариев логики бота).
