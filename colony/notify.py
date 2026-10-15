"""Telegram: zprávy o obchodech a příkazy z mobilu (/status, /pauza, /pokracuj, /prodej_vse)."""
from __future__ import annotations
import logging

import requests

from . import config

log = logging.getLogger("colony.telegram")


class Telegram:
    def __init__(self):
        self.enabled = bool(config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_ID)
        self.base = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}"
        self.offset = 0

    def send(self, text: str):
        log.info(text)
        if not self.enabled:
            return
        try:
            requests.post(self.base + "/sendMessage", timeout=10,
                          data={"chat_id": config.TELEGRAM_CHAT_ID, "text": text[:4000],
                                "disable_web_page_preview": "true"})
        except Exception as e:
            log.warning("telegram send failed: %s", e)

    def commands(self) -> list[str]:
        """Příkazy jen z tvého chatu - nikdo cizí kolonii neovládne."""
        if not self.enabled:
            return []
        try:
            r = requests.get(self.base + "/getUpdates", timeout=10,
                             params={"offset": self.offset, "timeout": 0}).json()
        except Exception:
            return []
        out = []
        for u in r.get("result", []):
            self.offset = u["update_id"] + 1
            m = u.get("message") or {}
            if str((m.get("chat") or {}).get("id")) == str(config.TELEGRAM_CHAT_ID):
                out.append((m.get("text") or "").strip().split("@")[0].lower())
        return out
