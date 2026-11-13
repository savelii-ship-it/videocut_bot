"""Деплой бота на два Hugging Face Space (failover-пара).

Запуск: HF_TOKEN=hf_xxx python3 deploy_hf.py
Токен: https://huggingface.co/settings/tokens → Create new token (Write).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from huggingface_hub import HfApi

load_dotenv()

HF_TOKEN = os.environ.get("HF_TOKEN", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not HF_TOKEN.startswith("hf_"):
    sys.exit("❌ Задай HF_TOKEN=hf_... (https://huggingface.co/settings/tokens)")
if not BOT_TOKEN:
    sys.exit("❌ В .env нет BOT_TOKEN")

ROOT = Path(__file__).parent
FILES = [
    "Dockerfile", ".dockerignore", "bot.py", "processing.py",
    "server.py", "requirements.txt", "webapp/index.html",
]

api = HfApi(token=HF_TOKEN)
me = api.whoami()["name"]
print(f"Аккаунт Hugging Face: {me}")

for n in (1, 2):
    slug = f"videocut-bot-{n}"
    repo_id = f"{me}/{slug}"
    url = f"https://{me}-{slug}.hf.space".lower()
    print(f"\n=== {repo_id} ===")

    api.create_repo(repo_id, repo_type="space", space_sdk="docker",
                    private=False, exist_ok=True)
    print("✅ Space создан (Docker, free CPU)")

    for rel in FILES:
        api.upload_file(
            path_or_fileobj=str(ROOT / rel),
            path_in_repo=rel,
            repo_id=repo_id,
            repo_type="space",
        )
        print(f"  ↑ {rel}")

    api.add_space_secret(repo_id, "BOT_TOKEN", BOT_TOKEN)
    api.add_space_secret(repo_id, "WEBAPP_URL", url)
    api.add_space_secret(repo_id, "HTTP_PORT", "7860")
    print(f"✅ секреты прописаны, мини-апп: {url}")

print("\nСборка образов идёт на стороне HF (3–7 мин).")
print("Жду, пока поднимутся /health…")

import urllib.request  # noqa: E402

deadline = time.time() + 15 * 60
urls = [f"https://{me}-videocut-bot-{n}.hf.space/health".lower() for n in (1, 2)]
pending = set(urls)
while pending and time.time() < deadline:
    for u in list(pending):
        try:
            with urllib.request.urlopen(u, timeout=15) as r:
                if r.status == 200:
                    print(f"✅ ЖИВОЙ: {u}")
                    pending.discard(u)
        except Exception:
            pass
    if pending:
        time.sleep(20)

if pending:
    print("⚠️ Не дождался:", ", ".join(pending))
    print("Глянь вкладку Logs у Space — возможно, сборка ещё идёт.")
else:
    print("\n🎉 Оба инстанса в строю. Failover активен:")
    print("Telegram отдаёт апдейты одному, второй подхватит при падении первого.")
    print("Осталось: UptimeRobot → два монитора на эти /health (шаг 3 в DEPLOY.md).")
