"""STÍN - simulace krátkých pozic (short) na kryptu: vydělává, když coin padá.
Alpaca short na krypto neumožňuje, proto je to simulace se skutečnými cenami a náklady burzy
s perpetual kontrakty (poplatek 0,06 % + skluz + funding 0,01 % za 8 h). Páka 1x.

Účetnictví: short je v peněžence vedený jako "zrcadlový" coin s cenou 2*P0 - P
(P0 = cena při otevření). Když coin spadne o 10 %, zrcadlo vyroste o 10 % - přesně zisk shortu."""
from __future__ import annotations
import logging
from datetime import datetime, timedelta

import pandas as pd

from .. import config
from ..indicators import ema, atr, rsi
from ..wallet import Wallet
from ..memory import hour_bucket, num_bucket

log = logging.getLogger("colony.shadow")


from ..wallet import mirror  # noqa: F401  (zrcadlová cena shortu - účetnictví dělá peněženka)


class ShadowAgent:
    name = "Stín"
    asset_class = "short"
    crypto = True
    minutes = 60
    max_positions = 3
    risk_per_trade = 0.01
    max_position_frac = 0.30
    atr_mult = 2.5
    max_hold_h = 72
    funding_8h = 0.0001
    memory = None
    kronika = None

    def __init__(self, data, wallet: Wallet | None = None):
        self.data = data
        self.wallet = wallet or Wallet(self.name, self.asset_class)
        self.last_bar = ""

    def _log(self, sym, p, stop_pct, taken, skip, why, feats, now):
        if self.kronika:
            try:
                self.kronika.signal(self.name, sym, p, taken=taken, skip_reason=skip, stop=p * (1 + stop_pct),
                                    horizon_min=self.max_hold_h * 60, reason=why, features=feats, now=now,
                                    meta={"minutes": 60, "crypto": True, "short": True})
            except Exception as e:
                log.warning("kronika: %s", e)

    # ------------------------------------------------------------------ signál
    @staticmethod
    def prepare(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["ema20"], df["ema50"] = ema(df["close"], 20), ema(df["close"], 50)
        df["atr"] = atr(df, 14)
        df["rsi"] = rsi(df["close"], 14)
        df["ret24"] = df["close"] / df["close"].shift(24) - 1
        df["ema50_slope"] = df["ema50"] / df["ema50"].shift(12) - 1
        return df

    def signal(self, df: pd.DataFrame):
        """Vrátí (stop_pct, důvod) když má smysl shortovat poslední svíčku."""
        if len(df) < 60:
            return None
        r = df.iloc[-1]
        if not (r["close"] < r["ema20"] < r["ema50"]):        # jasný sestupný trend
            return None
        if not (-0.15 <= r["ret24"] <= -0.02) or r["rsi"] < 28:  # padá, ale ještě není vyprodaný
            return None
        if r["ema50_slope"] >= 0 or r["close"] >= r["open"]:     # dlouhý trend musí klesat a svíčka být červená
            return None                                            # (po prudkém pádu se často odráží nahoru)
        stop_pct = min(0.10, max(0.04, self.atr_mult * r["atr"] / r["close"]))
        return stop_pct, f"sestupný trend, {r['ret24']*100:.1f} % za 24 h"

    # ------------------------------------------------------------------ hlavní tah
    def tick(self, now: datetime, mood: str, notify, universe: list[str], paused=False):
        w = self.wallet
        if w.status == "DEAD":
            return
        boundary = pd.Timestamp(now).floor("60min")
        if self.last_bar == boundary.isoformat() or (now - boundary.to_pydatetime()).total_seconds() < 20:
            return
        self.last_bar = boundary.isoformat()
        syms = list(dict.fromkeys(list(universe) + list(w.lots)))
        if not syms:
            return
        try:
            bars = self.data.bars(syms, 60, now - timedelta(days=14), crypto=True)
        except Exception as e:
            log.warning("stín bars: %s", e)
            return
        prices = {}
        for sym, raw in bars.items():
            raw = raw[raw.index + pd.Timedelta(minutes=60) <= pd.Timestamp(now)]
            if len(raw) < 3:
                continue
            df = self.prepare(raw)
            r = df.iloc[-1]
            p = float(r["close"])
            lot = w.lots.get(sym)
            if lot:
                p0 = lot.meta["p0"]
                prices[sym] = p                          # skutečná cena; peněženka si ji převede na zrcadlo
                held_h = (now - datetime.fromisoformat(lot.opened)).total_seconds() / 3600
                why = None
                if float(r["high"]) >= lot.meta["stop_px"]:
                    p = max(float(r["open"]), lot.meta["stop_px"])          # konzervativně: stop zasažen
                    why = "trailing stop" if lot.meta["stop_px"] < p0 else "stop-loss"
                elif held_h >= self.max_hold_h:
                    why = "časový limit"
                elif held_h >= 6 and p > r["ema20"] and r["ema20"] > r["ema50"]:
                    why = "trend se otočil nahoru"
                if not why:            # trailing stop posouvej až po kontrole (nevíme, jestli minimum svíčky bylo před maximem)
                    lot.meta["low"] = min(lot.meta.get("low", p0), float(r["low"]))
                    lot.meta["stop_px"] = min(lot.meta["stop_px"], lot.meta["low"] * (1 + lot.meta["trail"]))
                if why:
                    funding = self.funding_8h * held_h / 8
                    t = w.close(sym, p * (1 + funding), why, now=now)   # funding = za držení shortu se platí
                    notify(f"{'✅' if t['pnl'] > 0 else '🔻'} Stín (simulace) uzavřel short {sym}: "
                           f"{t['pnl']:+.2f} $ ({t['ret']*100:+.1f} %) - {why}")
                continue
            prices[sym] = p
            if paused or w.status != "ALIVE" or mood == "risk_on":
                continue
            sig = self.signal(df)
            if not sig:
                continue
            stop_pct, why = sig
            feats = {"coin": sym, "denní doba": hour_bucket(now), "nálada trhu": mood,
                     "pád za 24 h": num_bucket(-float(df["ret24"].iloc[-1]), [0.04, 0.08], ["2-4 %", "4-8 %", "přes 8 %"])}
            ok, mult, note = self.memory.judge(self.name, feats) if self.memory else (True, 1.0, "")
            skip = "plno pozic" if len(w.lots) >= self.max_positions else ("" if ok else note)
            self._log(sym, p, stop_pct, not skip, skip, why, feats, now)
            if skip:
                continue
            if note:
                why += f" · {note}"
            eq = w.equity()
            usd = min(eq * self.risk_per_trade * mult / stop_pct, eq * self.max_position_frac, w.cash * 0.95)
            if usd < config.MIN_ORDER_USD:
                continue
            qty = usd / p
            w.open(sym, qty, p, stop=p * (1 - stop_pct), reason=why, now=now,
                   meta={"stop_px": p * (1 + stop_pct), "trail": stop_pct, "low": p, "side": "short",
                         "features": feats, "sim": True})
            notify(f"🐻 Stín (simulace) otevřel short {sym} za ${usd:.0f} @ {p:.6g}, stop {p*(1+stop_pct):.6g} - {why}")
        # ceny otevřených pozic, které nepřišly v datech, nech beze změny
        w.mark({s: v for s, v in prices.items() if s in w.lots}, now)
