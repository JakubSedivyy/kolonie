"""Spuštění kolonie:  python run.py"""
import logging
import sys

from colony import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])

if not (config.ALPACA_KEY and config.ALPACA_SECRET):
    sys.exit("Chybí ALPACA_API_KEY / ALPACA_SECRET_KEY v souboru .env")
if config.TRADING_MODE == "live" and not config.LIVE_CONFIRMED:
    sys.exit("TRADING_MODE=live vyžaduje i I_UNDERSTAND_REAL_MONEY=yes v .env")

from colony.live import Colony

Colony().run()
