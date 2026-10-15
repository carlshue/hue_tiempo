#!/usr/bin/env python3
"""Genera el mapa y lo manda a Telegram. Pensado para ejecutarse a diario (cron de Railway).

Variables de entorno: AEMET_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import os
import subprocess
import sys
from pathlib import Path

import requests

HERE = Path(__file__).parent
salida = HERE / "tiempo_murcia.png"
subprocess.run([sys.executable, str(HERE / "mapa_tiempo_murcia.py"), "--refresh", "--salida", str(salida)], check=True)

token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
with open(salida, "rb") as f:
    r = requests.post(f"https://api.telegram.org/bot{token}/sendDocument" if os.environ.get("COMO_ARCHIVO") else
                      f"https://api.telegram.org/bot{token}/sendPhoto",
                      data={"chat_id": chat, "caption": "El tiempo en Murcia"},
                      files={("document" if os.environ.get("COMO_ARCHIVO") else "photo"): f}, timeout=60)
r.raise_for_status()
print("Enviado a Telegram")
