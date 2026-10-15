"""Zjistí tvoje TELEGRAM_CHAT_ID.
1) V Telegramu napiš @BotFather -> /newbot -> dostaneš token, vlož ho do .env jako TELEGRAM_TOKEN
2) Napiš svému novému botovi libovolnou zprávu (např. "ahoj")
3) Spusť:  python telegram_setup.py"""
import os
import requests
from dotenv import load_dotenv

load_dotenv()
tok = os.getenv("TELEGRAM_TOKEN")
if not tok:
    raise SystemExit("Nejdřív dej TELEGRAM_TOKEN do souboru .env")
r = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=10).json()
chats = {(u.get("message") or {}).get("chat", {}).get("id") for u in r.get("result", [])} - {None}
if not chats:
    raise SystemExit("Bot zatím nedostal žádnou zprávu. Napiš mu v Telegramu 'ahoj' a spusť znovu.")
for c in chats:
    print(f"TELEGRAM_CHAT_ID={c}")
print("Tenhle řádek vlož do .env")
