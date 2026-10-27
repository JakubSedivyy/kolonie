"""Společný základ botů: velikost pozice, správa stop-lossů, provedení obchodu."""
from __future__ import annotations
import logging
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from .. import config
from ..wallet import Wallet, is_short

log = logging.getLogger("colony.agent")


class _SimShort:
    """Krypto shorty se jen simulují - plnění za tržní cenu, skluz a poplatky počítá peněženka."""
    simulated = True

    def short_sell(self, symbol, usd, price):
        from ..broker import Fill
        return Fill(usd / price, price, "sim")

    def cover(self, symbol, qty, price):
        from ..broker import Fill
        return Fill(qty, price, "sim")


SIM = _SimShort()


@dataclass
class Entry:
    stop: float
    target: float | None = None
    trail_pct: float | None = None
    max_hold_min: int | None = None
    reason: str = ""


class BarAgent:
    """Bot, který se rozhoduje na zavřených svíčkách. Stejný kód běží live i v backtestu."""
    name = "bot"
    asset_class = "stock"
    crypto = False
    minutes = 5                 # délka svíčky
    lookback_bars = 300         # kolik svíček historie potřebuje
    risk_per_trade = 0.01       # kolik % peněženky smí ztratit na jednom obchodu
    max_position_frac = 0.30    # max podíl peněženky v jedné pozici
    max_positions = 3
    trades_in_risk_off = True
    benched = False             # laboratoř nenašla ziskovou variantu -> bot odpočívá (nové obchody ne)
    TUNABLE: dict = {}          # parametry, které smí laboratoř měnit

    def __init__(self, wallet: Wallet | None = None):
        self.wallet = wallet or Wallet(self.name, self.asset_class)

    def apply(self, params: dict):
        for k, v in (params or {}).items():
            if k == "last_entry" and isinstance(v, str):
                from datetime import time as _t
                h, m = v.split(":"); v = _t(int(h), int(m))
            setattr(self, k, v)
        return self

    # --- co musí strategie definovat ---------------------------------------------
    def universe(self, ctx) -> list[str]:
        raise NotImplementedError

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return df

    def entry(self, df: pd.DataFrame, i: int, ctx) -> Entry | None:
        return None

    def exit_signal(self, df: pd.DataFrame, i: int, lot) -> str | None:
        return None

    def can_enter(self, ts: pd.Timestamp) -> bool:
        return True

    # --- sázky na pokles (short) ---------------------------------------------------
    can_short = False           # umí bot vydělat i na poklesu?

    def short_entry(self, df: pd.DataFrame, i: int, ctx) -> Entry | None:
        """Entry.stop je NAD cenou, Entry.target POD cenou (skutečné ceny)."""
        return None

    def short_exit_signal(self, df: pd.DataFrame, i: int, lot) -> str | None:
        return None

    @staticmethod
    def check_exit_short(lot, high: float, low: float, close: float, open_: float, now: datetime):
        m = lot.meta
        if high >= m["stop_px"]:
            why = "trailing stop" if m["stop_px"] < m["p0"] else "stop-loss"
            return (max(open_, m["stop_px"]), why)
        if m.get("target_px") and low <= m["target_px"]:
            return (min(open_, m["target_px"]), "cíl zisku")
        if m.get("trail"):
            m["low"] = min(m.get("low", m["p0"]), low)
            m["stop_px"] = min(m["stop_px"], m["low"] * (1 + m["trail"]))
        if lot.max_hold_min:
            held = (now - datetime.fromisoformat(lot.opened)).total_seconds() / 60
            if held >= lot.max_hold_min:
                return (close, "časový limit")
        return None

    def must_flatten(self, ts: pd.Timestamp) -> bool:
        return False

    # --- společná logika ----------------------------------------------------------
    def fees_ok(self, price: float, stop: float) -> bool:
        """Obchod má smysl, jen když je stop aspoň FEE_STOP_MULT x (4x) dál než cena nákupu+prodeje.
        Jinak i úspěšné obchody končí po poplatcích skoro na nule a každý stop bolí dvojnásob
        (to potopilo Trendyho: stopy ~1-2 %, poplatky a skluz 0,6 % za obchod)."""
        c = self.wallet.costs
        if c["fee"] <= 0:                     # akcie na Alpace jsou bez poplatku
            return True
        round_trip = 2 * (c["fee"] + c["slip"])
        return abs(price - stop) / price >= config.FEE_STOP_MULT * round_trip

    def size_usd(self, price: float, stop: float, mood_mult: float = 1.0) -> float:
        w = self.wallet
        eq = w.equity()
        risk = max(price - stop, price * 0.002) / price
        usd = eq * self.risk_per_trade * mood_mult / risk
        usd = min(usd, eq * self.max_position_frac, w.cash * 0.98)
        return usd if usd >= config.MIN_ORDER_USD else 0.0

    def check_exit(self, lot, high: float, low: float, close: float, open_: float, now: datetime):
        """Vrátí (cena, důvod) pokud má pozice skončit, jinak None. Aktualizuje trailing stop."""
        # nejdřív stop z minulé svíčky (konzervativně: nevíme, jestli přišlo dřív maximum nebo minimum)
        if low <= lot.stop:
            why = "trailing stop" if lot.stop > lot.entry else "stop-loss"
            return (min(open_, lot.stop), why)               # při gapu dolů plní za open
        if lot.target and high >= lot.target:
            return (max(open_, lot.target), "cíl zisku")
        if lot.trail_pct:                                     # posun trailing stopu pro další svíčky
            lot.high = max(lot.high, high)
            lot.stop = max(lot.stop, lot.high * (1 - lot.trail_pct))
        if lot.max_hold_min:
            held = (now - datetime.fromisoformat(lot.opened)).total_seconds() / 60
            if held >= lot.max_hold_min:
                return (close, "časový limit")
        return None


class Executor:
    """Provádí obchody přes brokera a zapisuje je do peněženky."""

    def __init__(self, broker, notify=None):
        self.broker = broker
        self.notify = notify or (lambda msg: None)

    @property
    def slip(self):
        # simulace i paper: přičti skluz, ať papír není růžovější než realita.
        # live: cena plnění je skutečná, skluz už v ní je.
        return not config.IS_LIVE or getattr(self.broker, "simulated", False)

    def enter(self, agent: BarAgent, symbol: str, price: float, e: Entry, usd: float, now: datetime, meta=None,
              short=False):
        w = agent.wallet
        fee_buf = 1 + w.costs["fee"] + w.costs["slip"]
        usd = min(usd, w.cash / fee_buf)
        if usd < config.MIN_ORDER_USD:
            return None
        if short:
            return self._enter_short(agent, symbol, price, e, usd, now, meta)
        try:
            f = self.broker.buy_usd(symbol, usd, price, crypto=agent.crypto)
        except Exception as ex:
            log.warning("%s buy %s failed: %s", agent.name, symbol, ex)
            return None
        if not f or f.qty <= 0:
            return None
        # posuň stop/cíl o rozdíl mezi signální cenou a skutečným plněním
        shift = f.price / price
        lot = w.open(symbol, f.qty, f.price, stop=e.stop * shift,
                     target=e.target * shift if e.target else None, trail_pct=e.trail_pct,
                     max_hold_min=e.max_hold_min, reason=e.reason, now=now, apply_slippage=self.slip, meta=meta)
        self.notify(f"🟢 {agent.name} KOUPIL {symbol} za ${lot.cost:.2f} @ {lot.entry:.6g} ({e.reason})")
        return lot

    def _enter_short(self, agent, symbol, price, e, usd, now, meta):
        """Short: akcie skutečně přes Alpaku (paper), krypto jen simulovaně (Alpaca krypto short neumí)."""
        w = agent.wallet
        sim = agent.crypto
        broker = SIM if sim else self.broker
        try:
            f = broker.short_sell(symbol, usd, price)
        except Exception as ex:
            log.warning("%s short %s failed: %s", agent.name, symbol, ex)
            return None
        if not f or f.qty <= 0:
            return None
        shift = f.price / price
        m = dict(meta or {}, side="short", sim=sim, stop_px=e.stop * shift, trail=e.trail_pct,
                 target_px=e.target * shift if e.target else None, low=f.price)
        lot = w.open(symbol, f.qty, f.price, stop=0.0, trail_pct=None, max_hold_min=e.max_hold_min,
                     reason=e.reason, now=now, apply_slippage=self.slip, meta=m)
        lot.stop = 2 * lot.meta["p0"] - m["stop_px"]          # stop v "zrcadlových" cenách (jen pro přehled)
        self.notify(f"🐻 {agent.name}{' (simulace)' if sim else ''} SHORT {symbol} za ${lot.cost:.2f} @ {f.price:.6g}, "
                    f"stop {m['stop_px']:.6g} ({e.reason})")
        return lot

    def exit(self, agent: BarAgent, symbol: str, price: float, why: str, now: datetime):
        w = agent.wallet
        lot = w.lots.get(symbol)
        if not lot:
            return None
        if is_short(lot):
            broker = SIM if lot.meta.get("sim") else self.broker
            try:
                f = broker.cover(symbol, lot.qty, price)
            except Exception as ex:
                log.warning("%s cover %s failed: %s", agent.name, symbol, ex)
                return None
            if f is None:
                log.warning("%s cover %s nevyplněn, zkusím znovu", agent.name, symbol)
                return None
            t = w.close(symbol, f.price if f.qty > 0 else price, why, now=now, apply_slippage=self.slip)
            self.notify(f"{'✅' if t['pnl'] > 0 else '🔻'} {agent.name} UZAVŘEL SHORT {symbol}: "
                        f"{t['pnl']:+.2f} $ ({t['ret']*100:+.1f} %) - {why}")
            return t
        try:
            f = self.broker.sell(symbol, lot.qty, price, crypto=agent.crypto)
        except Exception as ex:
            log.warning("%s sell %s failed: %s", agent.name, symbol, ex)
            return None
        if f is None:
            # příkaz odmítnut / nevyplněn -> pozice pořád na účtu, zkusíme to při dalším průchodu
            log.warning("%s sell %s nevyplněn, zkusím znovu", agent.name, symbol)
            return None
        fill_price = f.price if f.qty > 0 else price      # qty 0 = na účtu už nic není, jen srovnat účetnictví
        t = w.close(symbol, fill_price, why, now=now, apply_slippage=self.slip)
        icon = "✅" if t["pnl"] > 0 else "🔻"
        self.notify(f"{icon} {agent.name} PRODAL {symbol}: {t['pnl']:+.2f} $ ({t['ret']*100:+.1f} %) - {why}")
        return t
