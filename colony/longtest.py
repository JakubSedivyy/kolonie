"""DLOUHODOBÝ TEST - jak by si dlouhodobí boti (Kompas, Lev, Sokol, Spořič) vedli za posledních několik let.

Stejná logika jako živá kolonie: bot se rozhoduje ráno podle včerejší zavřené denní svíčky,
nakupuje a prodává za ranní cenu (open), stopy hlídá podle denního minima. Srovnává se s obyčejným
držením S&P 500 (SPY) - když bot nevydělá víc než index nebo nemá menší propady, nemá smysl.
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .agents.base import Executor
from .broker import SimBroker

log = logging.getLogger("colony.longtest")


def simulate(agent, data: dict, start: pd.Timestamp, end: pd.Timestamp | None = None):
    """Přehraje denní svíčky. data: symbol -> DataFrame (open/high/low/close/volume), index = dny (UTC)."""
    ex = Executor(SimBroker())
    w = agent.wallet
    w.born = start.isoformat()
    full = {s: df.sort_index() for s, df in data.items() if df is not None and len(df)}
    prepared = {s: agent.prepare(df) for s, df in full.items()}
    days = sorted({ts for df in full.values() for ts in df.index if ts >= start and (end is None or ts <= end)})
    pos = {s: {ts: k for k, ts in enumerate(df.index)} for s, df in full.items()}
    curve = []
    for d in days:
        now = d.to_pydatetime()
        today = {s: pos[s][d] for s in full if d in pos[s]}          # symboly, které dnes mají svíčku
        closed = {s: full[s].iloc[:k] for s, k in today.items() if k > 0}
        if hasattr(agent, "on_bars"):
            agent.on_bars(closed, now)
        # 1) ráno: signály z včerejší svíčky (nejdřív držené pozice -> prodeje uvolní hotovost)
        for s in sorted(today, key=lambda x: x not in w.lots):
            k = today[s]
            if k < 3:
                continue
            df, i = prepared[s], k - 1
            op = float(full[s]["open"].iloc[k])
            lot = w.lots.get(s)
            if lot:
                why = agent.exit_signal(df, i, lot)
                if why:
                    ex.exit(agent, s, op, why, now)
                elif hasattr(agent, "topup"):
                    add = agent.topup(df, i, lot, now)
                    if add and ex.add(agent, s, op, add[0], now, add[2]):
                        lot.meta["topup_key"] = add[1]
                continue
            if len(w.lots) >= agent.max_positions or not agent.can_enter(df.index[i]):
                continue
            e = agent.entry(df, i, {"symbol": s})
            if not e:
                continue
            shift = op / float(df["close"].iloc[i])                   # stop posunout k ranní ceně
            e.stop *= shift
            if e.target:
                e.target *= shift
            if e.stop >= op:
                continue
            usd = agent.size_usd(op, e.stop)
            if usd:
                ex.enter(agent, s, op, e, usd, now)
        # 2) během dne: stopy podle denního minima/maxima
        for s in list(w.lots):
            if s not in today:
                continue
            r = full[s].iloc[today[s]]
            lot = w.lots[s]
            x = agent.check_exit(lot, float(r["high"]), float(r["low"]), float(r["close"]), float(r["open"]), now)
            if x:
                ex.exit(agent, s, float(x[0]), x[1], now)
        w.mark({s: float(full[s]["close"].iloc[k]) for s, k in today.items()}, now)
        curve.append((d, w.equity()))
    return w, pd.Series([v for _, v in curve], index=[d for d, _ in curve], dtype=float)


def metrics(curve: pd.Series) -> dict:
    if len(curve) < 2:
        return {"return": 0.0, "cagr": 0.0, "max_dd": 0.0, "sharpe": 0.0, "years": {}}
    ret = curve.iloc[-1] / curve.iloc[0] - 1
    yrs = max((curve.index[-1] - curve.index[0]).days / 365.25, 1e-9)
    daily = curve.pct_change().dropna()
    dd = float((1 - curve / curve.cummax()).max())
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0.0
    by_year = {}
    for y, c in curve.groupby(curve.index.year):
        prev = curve[curve.index.year < y]
        base = prev.iloc[-1] if len(prev) else c.iloc[0]
        by_year[int(y)] = float(c.iloc[-1] / base - 1)
    return {"return": float(ret), "cagr": float((1 + ret) ** (1 / yrs) - 1), "max_dd": dd,
            "sharpe": round(sharpe, 2), "years": by_year}


def buy_hold(df: pd.DataFrame, start: pd.Timestamp) -> pd.Series:
    c = df["close"][df.index >= start]
    return c / c.iloc[0] * 1000.0


def report(results: dict, bench: dict, start, end) -> list[str]:
    def fmt(name, m, extra=""):
        yrs = " ".join(f"{y}:{v*100:+.0f}%" for y, v in m["years"].items())
        return (f"{name:8s} roč. {m['cagr']*100:+5.1f} % | celkem {m['return']*100:+6.1f} % | max propad "
                f"{m['max_dd']*100:4.1f} % | Sharpe {m['sharpe']:.2f}{extra} | {yrs}")
    lines = [f"DLOUHODOBÝ TEST {start:%Y-%m-%d}..{end:%Y-%m-%d} (denní svíčky, nákup za ranní cenu, bez přežití)"]
    for name, (m, st) in results.items():
        lines.append(fmt(name, m, f" | {st['trades']} obch., úspěšnost {st['win_rate']*100:.0f} %"))
    for name, m in bench.items():
        lines.append(fmt(name, m, " | srovnání: jen držet"))
    return lines


def run(data_api, agents, years: int = 6) -> tuple[list[str], dict]:
    """Stáhne denní historii (očištěnou o dividendy a splity) a přehraje všechny dlouhodobé boty."""
    end = datetime.now(timezone.utc)
    first = end - pd.Timedelta(days=int(365.25 * years))
    syms = sorted({s for a in agents if not a.crypto for s in a.universe({})} | {"SPY"})
    data = data_api.bars(syms, 1440, first, end)
    csyms = sorted({s for a in agents if a.crypto for s in a.universe({})})
    if csyms:                                         # krypto (Želva) + bitcoin jako srovnání
        try:
            data.update(data_api.bars(sorted(set(csyms) | {"BTC/USD"}), 1440, first, end, crypto=True))
        except Exception as e:
            log.warning("dlouhodobý test - krypto data: %s", e)
    have = [df.index[0] for s, df in data.items() if len(df) and "/" not in s]   # začátek podle akcií
    if not have:
        return ["DLOUHODOBÝ TEST: žádná data"], {}
    base = sorted(have)[len(have) * 3 // 4]          # od kdy má data většina symbolů
    start = pd.Timestamp(base) + pd.Timedelta(days=400)   # rok+ na zahřátí průměrů (200 dní, 12měsíční momentum)
    results, out = {}, {}
    for a in agents:
        w, curve = simulate(a, {s: data[s] for s in a.universe({}) if s in data}, start)
        m = metrics(curve)
        results[a.name] = (m, w.stats())
        out[a.name] = {**m, "trades": w.stats()["trades"], "win_rate": w.stats()["win_rate"]}
    bench = {}
    if "SPY" in data:
        bench["S&P 500"] = metrics(buy_hold(data["SPY"], start))
        out["S&P 500"] = bench["S&P 500"]
    if "BTC/USD" in data and len(data["BTC/USD"]) and data["BTC/USD"].index[0] <= start:
        bench["Bitcoin"] = metrics(buy_hold(data["BTC/USD"], start))
        out["Bitcoin"] = bench["Bitcoin"]
    last = max(df.index[-1] for df in data.values() if len(df))
    return report(results, bench, start, last), out
