"""Přehrání historie pro backtest: stejná logika jako live (signál na zavření svíčky,
plnění za open další svíčky, stopy podle high/low, pravidla přežití)."""
from __future__ import annotations
from dataclasses import replace

import pandas as pd

from . import survival as survival_mod
from .agents.base import BarAgent, Executor


def replay(agent: BarAgent, data: dict, executor: Executor, mood_mult: float = 1.0, notify=None, survival_on=True,
           survival=None):
    if survival is not None:          # alias
        survival_on = survival
    notify = notify or (lambda m: None)
    w = agent.wallet
    prepared = {}
    for s, df in data.items():
        p = agent.prepare(df)
        if p is not None and len(p):
            prepared[s] = p
    timeline = sorted((ts, s, i) for s, df in prepared.items() for i, ts in enumerate(df.index))
    pending = {}
    last = {}
    born = pd.Timestamp(w.born)
    for ts, s, i in timeline:
        df = prepared[s]
        r = df.iloc[i]
        now = ts.to_pydatetime()
        last[s] = (r["close"], now)
        if ts < born:                    # zahřívací historie jen pro indikátory, ještě se neobchoduje
            continue

        eq = w.mark({s: float(r["close"])}, now)
        for typ, txt in (survival_mod.check(w, now, eq) if survival_on else []):
            if typ == "DEATH":
                for sym in list(w.lots):
                    executor.exit(agent, sym, last.get(sym, (w.lots[sym].entry,))[0], "smrt bota", now)
                survival_mod.kill(w, now, txt)
                notify(f"💀 {agent.name} umřel: {txt}")
            elif typ in ("INJURED", "HEALED"):
                notify(txt)
        if w.status == "DEAD":
            break

        # 1) vyplnit signál z minulé svíčky za dnešní open
        if s in pending:
            e, sig_px = pending.pop(s)
            if s not in w.lots and w.status == "ALIVE" and len(w.lots) < agent.max_positions:
                k = r["open"] / sig_px
                e2 = replace(e, stop=e.stop * k, target=e.target * k if e.target else None)
                if e2.stop < r["open"]:
                    usd = agent.size_usd(r["open"], e2.stop, mood_mult)
                    if usd:
                        executor.enter(agent, s, float(r["open"]), e2, usd, now)

        # 2) správa otevřené pozice
        lot = w.lots.get(s)
        if lot:
            if agent.must_flatten(ts):
                executor.exit(agent, s, float(r["close"]), "konec obchodního dne", now)
                continue
            x = agent.check_exit(lot, r["high"], r["low"], r["close"], r["open"], now)
            if x:
                executor.exit(agent, s, float(x[0]), x[1], now)
            else:
                why = agent.exit_signal(df, i, lot)
                if why:
                    executor.exit(agent, s, float(r["close"]), why, now)
            continue

        # 3) nový signál
        if w.status == "ALIVE" and agent.can_enter(ts) and len(w.lots) < agent.max_positions:
            e = agent.entry(df, i, {"symbol": s})
            if e and e.stop < r["close"]:
                pending[s] = (e, float(r["close"]))

    # konec testu: zavřít, co zbylo
    for sym in list(w.lots):
        px, t = last[sym]
        executor.exit(agent, sym, float(px), "konec testu", t)
    return w
