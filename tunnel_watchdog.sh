#!/bin/bash
# Watchdog: держит SSH-туннель localhost.run живым.
# Если туннель падает — переподнимает, обновляет WEBAPP_URL в .env
# и перезапускает бота с новым адресом.
cd "$(dirname "$0")"

start_bot() {
  pkill -f "python3 bot.py" 2>/dev/null
  sleep 2
  nohup python3 bot.py > bot.log 2>&1 &
  echo "[$(date '+%H:%M:%S')] бот перезапущен (pid $!)"
}

while true; do
  ssh -o StrictHostKeyChecking=no \
      -o ServerAliveInterval=15 \
      -o ServerAliveCountMax=2 \
      -o ExitOnForwardFailure=yes \
      -R 80:localhost:8080 nokey@localhost.run > tunnel_live.log 2>&1 &
  SSH_PID=$!

  URL=""
  for i in $(seq 1 30); do
    URL=$(grep -oE "https://[a-zA-Z0-9]+\.app\.lhr\.(life|run)" tunnel_live.log | tail -1)
    [ -n "$URL" ] && break
    sleep 1
  done

  if [ -z "$URL" ]; then
    echo "[$(date '+%H:%M:%S')] туннель не выдал URL, повтор через 10 с"
    kill $SSH_PID 2>/dev/null
    sleep 10
    continue
  fi

  CUR=$(grep '^WEBAPP_URL=' .env | cut -d= -f2)
  if [ "$URL" != "$CUR" ]; then
    echo "[$(date '+%H:%M:%S')] новый адрес: $URL"
    sed -i '' "s|^WEBAPP_URL=.*|WEBAPP_URL=$URL|" .env
    start_bot
  elif ! pgrep -f "python3 bot.py" > /dev/null; then
    start_bot
  fi
  echo "[$(date '+%H:%M:%S')] туннель активен: $URL"

  wait $SSH_PID
  echo "[$(date '+%H:%M:%S')] туннель упал, переподнимаю через 5 с…"
  sleep 5
done
