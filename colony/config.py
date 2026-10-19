"""Všechna nastavení kolonie na jednom místě."""
import os
from dotenv import load_dotenv

load_dotenv()

# --- klíče (soubor .env) -------------------------------------------------------
ALPACA_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- režim -----------------------------------------------------------------------
# "paper" = simulované peníze na Alpace. "live" = skutečné peníze.
# Live se zapne JEN když je zároveň I_UNDERSTAND_REAL_MONEY=yes. Degen bot je vždy simulace.
TRADING_MODE = os.getenv("TRADING_MODE", "paper").lower()
LIVE_CONFIRMED = os.getenv("I_UNDERSTAND_REAL_MONEY", "").lower() == "yes"
IS_LIVE = TRADING_MODE == "live" and LIVE_CONFIRMED
# Na papíře obchodují všichni boti (i ti, které laboratoř nedoporučuje) - chceme vidět živé výsledky.
# Odpočinek podle laboratoře platí jen pro reálné peníze, nebo když LAB_BENCH_ON_PAPER=yes.
LAB_BENCH_ON_PAPER = os.getenv("LAB_BENCH_ON_PAPER", "no").lower() == "yes"
# Při náladě "risk_off" (BTC i S&P dolů) by většina botů nenakupovala. Na papíře chceme data, takže obchodují dál
# s menšími sázkami (MOOD_MULT). U reálných peněz platí přísné pravidlo, pokud RISK_OFF_TRADING=yes nezmění.
RISK_OFF_TRADING = os.getenv("RISK_OFF_TRADING", "no" if IS_LIVE else "yes").lower() == "yes"

# --- pravidla přežití ----------------------------------------------------------------
START_CAPITAL = 1000.0          # každý bot začíná s $1000
DEATH_EQUITY = 800.0            # pod $800 (-20 %) bot umírá
PROFIT_DEADLINE_DAYS = 30       # když po 30 dnech nemá víc než na začátku, umírá
DAILY_LOSS_LIMIT = 0.05         # -5 % za den = "zraněný", do konce dne nesmí obchodovat
RENT_PER_DAY = 0.05             # "nájem" za server (~5 €/měsíc rozpočítané mezi boty)
COLONY_KILL_SWITCH = 0.85       # když celá kolonie klesne pod 85 % startu, všichni se uspí

# --- realistické náklady (aby papír nelhal) ----------------------------------------------
COSTS = {
    # fee = poplatek z objemu, slip = skluz ceny při plnění (každým směrem)
    "stock":  {"fee": 0.0,    "slip": 0.0005},
    "crypto": {"fee": 0.0025, "slip": 0.0005},   # Alpaca crypto taker fee ~0.25 %
    "meme":   {"fee": 0.0025, "slip": 0.0020},
    "degen":  {"fee": 0.0100, "slip": 0.0300},   # DEX swap + MEV + mělká likvidita
    "dexmeme": {"fee": 0.0050, "slip": 0.0100},  # zavedené meme coiny na DEXu: hlubší pooly, menší skluz
    "short":  {"fee": 0.0006, "slip": 0.0005},   # simulované shorty krypta (perp burza, taker 0,06 %)
    "predict": {"fee": 0.0100, "slip": 0.0},     # Polymarket: kupuje za ask, prodává za bid + rezerva na poplatky
}

MIN_ORDER_USD = 10.0
LOOP_SECONDS = 30               # jak často kolonie "dýchá"
STATE_DIR = os.getenv("STATE_DIR", "state")


STOP_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VYPNUTO")


def is_stopped(path: str = None) -> bool:
    """Kolonie je vypnutá majitelem, když v kořeni repozitáře existuje soubor VYPNUTO."""
    return os.path.exists(path or STOP_FILE)
