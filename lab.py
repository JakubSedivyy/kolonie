"""LABORATOŘ - hledá nejlepší nastavení botů poctivě (walk-forward).

  1. Stáhne 90 dní historie z Alpaky.
  2. Každou variantu bota pustí na prvních 60 dnech ("trénink") a vybere nejlepší.
  3. Vítěze ověří na posledních 30 dnech, které při výběru neviděl ("test").
  4. Když vydělává i v testu, kolonie ho nasadí. Když ne, bot ODPOČÍVÁ (nové obchody nedělá),
     aby zbytečně neprodělával.

Spouští se samo jednou týdně (systemd timer) nebo ručně:  python lab.py
Výsledek: state/lab.json (kolonie si ho načte sama) a state/lab.txt (čitelné shrnutí).
"""
import argparse
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from colony import config
from colony.agents.base import Executor
from colony.agents.strategies import OrbitAgent, TrendAgent, MemeAgent, WaveAgent, BearAgent
from colony.broker import SimBroker
from colony.engine import replay

logging.basicConfig(level=logging.WARNING)

VARIANTS = {
    "Orbit": (OrbitAgent, [
        {"or_bars": 3, "vol_mult": 1.5, "last_entry": "11:30"},
        {"or_bars": 3, "vol_mult": 1.2, "last_entry": "14:00"},
        {"or_bars": 6, "vol_mult": 1.2, "last_entry": "14:00"},
        {"or_bars": 6, "vol_mult": 1.5, "last_entry": "12:30"},
    ]),
    "Trendy": (TrendAgent, [
        {"minutes": 60, "mode": "donchian", "donchian_n": 48, "top_n": 3},
        {"minutes": 60, "mode": "donchian", "donchian_n": 24, "top_n": 3},
        {"minutes": 60, "mode": "donchian", "donchian_n": 48, "top_n": 6},
        {"minutes": 60, "mode": "cross", "top_n": 3},
        {"minutes": 15, "mode": "donchian", "donchian_n": 96, "top_n": 3},
    ]),
    "Memec": (MemeAgent, [
        {"vol_mult": 3.0, "stop_pct": 0.04, "target_pct": 0.10},
        {"vol_mult": 4.0, "stop_pct": 0.05, "target_pct": 0.15},
        {"vol_mult": 2.5, "stop_pct": 0.03, "target_pct": 0.08},
        {"vol_mult": 4.0, "stop_pct": 0.06, "target_pct": 0.20},
    ]),
    "Vlna": (WaveAgent, [
        {"atr_mult": 3.0},
        {"atr_mult": 2.5},
        {"atr_mult": 3.5},
    ]),
    "Medvěd": (BearAgent, [
        {"atr_mult": 1.5},
        {"atr_mult": 2.0},
        {"atr_mult": 3.0},
    ]),
}
MIN_TRADES = 8


def score(w):
    s = w.stats()
    return {"return": round(s["return"], 4), "trades": s["trades"], "win_rate": round(s["win_rate"], 3),
            "pf": None if s["profit_factor"] == float("inf") else round(s["profit_factor"], 2),
            "max_dd": round(s["max_drawdown"], 4)}


def run(cls, params, data, start, end, symbols=None):
    a = cls().apply(params)
    a.wallet.born = start.isoformat()
    sl = {s: df[df.index < end] for s, df in data.items() if symbols is None or s in symbols}
    replay(a, sl, Executor(SimBroker()), survival=False)
    return a.wallet


def universe_for(agent_cls, params, majors, memes):
    a = agent_cls().apply(params)
    ctx = {"crypto_majors": majors, "crypto_memes": memes}
    if isinstance(a, OrbitAgent):
        return OrbitAgent.core + OrbitAgent.default_watch
    return a.universe(ctx)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--test-days", type=int, default=30)
    ap.add_argument("--only", default="")
    a = ap.parse_args()

    from colony.market import AlpacaData, AlpacaAssets
    from alpaca.trading.client import TradingClient
    data_api = AlpacaData()
    try:
        assets = AlpacaAssets(TradingClient(config.ALPACA_KEY, config.ALPACA_SECRET, paper=True))
        majors, memes = assets.majors(), assets.memes()
    except Exception:
        majors = TrendAgent.preferred
        memes = ["DOGE/USD", "SHIB/USD", "PEPE/USD"]

    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    start = end - timedelta(days=a.days)
    split = end - timedelta(days=a.test_days)
    out = {"updated": end.isoformat(timespec="minutes"), "train": [start.isoformat()[:10], split.isoformat()[:10]],
           "test": [split.isoformat()[:10], end.isoformat()[:10]], "bots": {}}
    cache = {}
    lines = [f"LABORATOŘ {end:%Y-%m-%d}: trénink {out['train'][0]}..{out['train'][1]}, test {out['test'][0]}..{out['test'][1]}"]

    for name, (cls, variants) in VARIANTS.items():
        if a.only and name != a.only:
            continue
        rows = []
        for params in variants:
            proto = cls().apply(params)
            syms = universe_for(cls, params, majors, memes)
            key = (proto.minutes, proto.crypto, tuple(sorted(syms)))
            if key not in cache:
                warm = timedelta(minutes=proto.minutes * (proto.lookback_bars + 10))
                print(f"{name}: stahuji {len(syms)} symbolů ({proto.minutes} min)…", flush=True)
                cache[key] = data_api.bars(syms, proto.minutes, start - warm, end, crypto=proto.crypto)
            data = cache[key]
            tr = score(run(cls, params, data, start, split))
            rows.append({"params": params, "train": tr})
            print(f"  {params} -> trénink {tr['return']*100:+.1f} % ({tr['trades']} obch.)", flush=True)
        eligible = [r for r in rows if r["train"]["trades"] >= MIN_TRADES] or rows
        best = max(eligible, key=lambda r: r["train"]["return"])
        proto = cls().apply(best["params"])
        key = (proto.minutes, proto.crypto, tuple(sorted(universe_for(cls, best["params"], majors, memes))))
        te = score(run(cls, best["params"], cache[key], split, end))
        good = best["train"]["return"] > 0 and te["return"] > 0
        out["bots"][name] = {"chosen": best["params"], "train": best["train"], "test": te,
                             "benched": not good, "variants": rows}
        verdict = "NASAZEN ✅" if good else "ODPOČÍVÁ 💤 (v testu neprošel)"
        lines.append(f"{name}: {verdict} | nejlepší {best['params']} | trénink {best['train']['return']*100:+.1f} % "
                     f"| test {te['return']*100:+.1f} % ({te['trades']} obch., PF {te['pf']})")

    os.makedirs(config.STATE_DIR, exist_ok=True)
    path = os.path.join(config.STATE_DIR, "lab.json")
    old = {}
    if a.only and os.path.exists(path):
        old = json.load(open(path)).get("bots", {})
    out["bots"] = {**old, **out["bots"]}
    json.dump(out, open(path, "w"), indent=1)
    open(os.path.join(config.STATE_DIR, "lab.txt"), "w").write("\n".join(lines) + "\n")
    print("\n" + "\n".join(lines))


if __name__ == "__main__":
    if config.is_stopped():
        raise SystemExit("Kolonie je vypnutá (soubor VYPNUTO) - laboratoř neběží.")
    main()
