"""Grok (xAI) - čte X (Twitter) v reálném čase. Hlídá denní rozpočet, aby nikdy nepřetáhl strop.

Ceny (říjen 2026, odhad pro výpočet - skutečnou útratu vidíš v console.x.ai):
  X Search: $5 / 1000 přečtených příspěvků, $10 / 1000 profilů
  model:    ~$2 / 1M vstupních tokenů, ~$6 / 1M výstupních
"""
from __future__ import annotations
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import requests

from . import config

log = logging.getLogger("colony.grok")
URL = "https://api.x.ai/v1/responses"
PRICE_IN, PRICE_OUT = 2.0 / 1e6, 6.0 / 1e6
PRICE_POST, PRICE_USER = 5.0 / 1000, 10.0 / 1000
PRICE_WEB = 10.0 / 1000          # odhad za 1 webové hledání


def extract_json(text: str):
    """Grok občas obalí JSON do textu nebo ```json``` - vytáhni první {...} blok."""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    cand = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    try:
        return json.loads(cand)
    except Exception:
        return None


class Grok:
    def __init__(self, session=None):
        self.key = os.getenv("XAI_API_KEY", "")
        self.budget = float(os.getenv("XAI_DAILY_BUDGET", "1.0"))
        env_model = os.getenv("XAI_MODEL", "")
        self.models = [env_model] if env_model else ["grok-4.3", "grok-4.7"]
        self.s = session or requests.Session()
        self.path = os.path.join(config.STATE_DIR, "grok_budget.json")
        self.day, self.spent, self.calls, self.last_error = "", 0.0, 0, ""
        self._load()

    @property
    def enabled(self):
        return bool(self.key)

    def _load(self):
        try:
            d = json.load(open(self.path))
            self.day, self.spent, self.calls = d["day"], d["spent"], d.get("calls", 0)
        except Exception:
            pass

    def _save(self):
        os.makedirs(config.STATE_DIR, exist_ok=True)
        json.dump({"day": self.day, "spent": self.spent, "calls": self.calls}, open(self.path, "w"))

    def _roll(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.day != today:
            self.day, self.spent, self.calls = today, 0.0, 0

    def remaining(self):
        self._roll()
        return max(0.0, self.budget - self.spent)

    def status(self):
        self._roll()
        return {"enabled": self.enabled, "budget": self.budget, "spent": round(self.spent, 3),
                "calls": self.calls, "last_error": self.last_error}

    def ask(self, prompt: str, x_search=True, hours=8, max_turns=2, reserve=0.30, web_search=False):
        """Vrátí text odpovědi, nebo None (bez klíče / došel rozpočet / chyba).
        reserve = kolik si musí nechat v rozpočtu, aby vůbec začal (hrubý odhad ceny 1 dotazu)."""
        if not self.enabled or self.remaining() < reserve:
            return None
        body = {"input": [{"role": "user", "content": prompt}], "max_turns": max_turns}
        if x_search:
            frm = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d")
            body["tools"] = [{"type": "x_search", "from_date": frm}]
        if web_search:
            body.setdefault("tools", []).append({"type": "web_search"})
        for model in list(self.models):
            body["model"] = model
            try:
                r = self.s.post(URL, json=body, timeout=180,
                                headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
            except Exception as e:
                self.last_error = str(e)[:200]
                return None
            if r.status_code in (400, 404) and "model" in r.text.lower() and len(self.models) > 1:
                log.warning("grok model %s nedostupný, zkouším další", model)
                self.models.remove(model)
                continue
            if r.status_code != 200:
                self.last_error = f"HTTP {r.status_code}: {r.text[:200]}"
                log.warning("grok: %s", self.last_error)
                return None
            j = r.json()
            self._account(j)
            self.last_error = ""
            return self._text(j)
        return None

    def _account(self, j):
        u = j.get("usage") or {}
        tools = u.get("server_side_tool_usage_details") or {}
        cost = (u.get("input_tokens", 0) * PRICE_IN + u.get("output_tokens", 0) * PRICE_OUT
                + tools.get("x_posts_fetched", 0) * PRICE_POST + tools.get("x_users_fetched", 0) * PRICE_USER
                + tools.get("web_search_calls", 0) * PRICE_WEB)
        if not u:                       # bez údajů o spotřebě počítej konzervativně
            cost = 0.25
        self._roll()
        self.spent += cost
        self.calls += 1
        self._save()

    @staticmethod
    def _text(j):
        if isinstance(j.get("output_text"), str):
            return j["output_text"]
        parts = []
        for item in j.get("output", []) or []:
            if item.get("type") == "message":
                for c in item.get("content", []) or []:
                    if c.get("type") in ("output_text", "text") and c.get("text"):
                        parts.append(c["text"])
        return "\n".join(parts) or None
