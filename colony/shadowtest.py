"""PŘEHRÁNÍ STÍNA NA HISTORII - stejný kód jako živě (ShadowAgent.tick), jen místo živých dat
dostává hodinové svíčky z minulosti. Hodina po hodině: Stín "vidí" jen svíčky, které už skončily.

Nálada trhu se počítá stejně jako v kolonii (BTC nad hodinovým EMA 50 + S&P 500 nad 20denním
průměrem; obojí = risk_on a Stín pak neshortuje). Paměť (memory) a kronika se nepoužívají.
"""
from __future__ import annotations

from datetime import timedelta

import pandas as pd

from .indicators import ema


class HistData:
    """Napodobí AlpacaData.bars: vrátí svíčky z minulosti do zadané chvíle."""

    def __init__(self, frames: dict):
        self.frames = {s: df.sort_index() for s, df in frames.items() if df is not None and len(df)}
        self.now = None

    def bars(self, symbols, minutes, start, end=None, crypto=False):
        end = end or self.now
        out = {}
        for s in symbols:
            df = self.frames.get(s)
            if df is None:
                continue
            a, b = df.index.searchsorted(pd.Timestamp(start)), df.index.searchsorted(pd.Timestamp(end))
            if b > a:
                out[s] = df.iloc[a:b]
        return out


def mood_series(btc: pd.DataFrame | None, spy: pd.DataFrame | None):
    """Vrátí funkci now -> nálada ("risk_on"/"neutral"/"risk_off") jako v kolonii, bez nahlédnutí do budoucna."""
    btc_up = (btc["close"] > ema(btc["close"], 50)) if btc is not None and len(btc) else None
    spy_up = (spy["close"] > spy["close"].rolling(20).mean()) if spy is not None and len(spy) else None

    def at(now):
        score = 0
        for ser, step in ((btc_up, timedelta(hours=1)), (spy_up, timedelta(days=1))):
            if ser is None:
                continue
            k = ser.index.searchsorted(pd.Timestamp(now) - step, side="right") - 1   # jen hotové svíčky
            score += bool(ser.iloc[k]) if k >= 0 else 0
        return {2: "risk_on", 1: "neutral", 0: "risk_off"}[score]
    return at


def simulate(agent, frames: dict, spy: pd.DataFrame | None, start, end, universe=None):
    """Přehraje Stína (nebo kohokoli s tick(now, mood, notify, universe)) od start do end po hodinách."""
    # rychlost: indikátory spočítat jednou na celé historii (jsou kauzální - hodnota v čase t závisí jen
    # na svíčkách do t) a tick dostane už hotové řádky místo přepočtu 14denního okna každou hodinu
    hd = HistData({s: agent.prepare(df) for s, df in frames.items() if df is not None and len(df)})
    agent.prepare = lambda df: df
    agent.data, agent.memory, agent.kronika = hd, None, None
    w = agent.wallet
    w.born = pd.Timestamp(start).isoformat()
    mood = mood_series(frames.get("BTC/USD"), spy)
    syms = list(universe or frames)
    t = pd.Timestamp(start).floor("60min") + pd.Timedelta(minutes=1)
    stop = pd.Timestamp(end)
    while t < stop:
        now = t.to_pydatetime()
        hd.now = t
        agent.tick(now, mood(now), lambda m: None, syms)
        t += pd.Timedelta(hours=1)
    # na konci otevřené pozice ocenit poslední cenou (jako lab u ostatních botů)
    last = {s: float(df["close"].iloc[-1]) for s, df in hd.bars(list(w.lots), 60, stop - timedelta(days=3)).items()}
    w.mark(last, stop.to_pydatetime())
    return w
