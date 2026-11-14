#!/bin/sh
# Стартуем POT-провайдер (генератор proof-of-origin токенов для YouTube)
# и самого бота в одном контейнере. Рецепт запуска — из официального
# deno-образа bgutil-ytdlp-pot-provider.
deno run --allow-env --allow-net \
  --allow-ffi=/opt/pot/server/node_modules \
  --allow-read=/opt/pot/server/node_modules \
  /opt/pot/server/src/main.ts &
cd /app && exec python bot.py
