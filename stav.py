"""Vypíše živý stav kolonie ze state/colony.json (papírové obchody od spuštění).
Použití na serveru:  .venv/bin/python stav.py"""
import json
import os
import sys

from colony import config
from colony.wallet import Wallet


def main():
    path = os.path.join(config.STATE_DIR, "colony.json")
    if not os.path.exists(path):
        print("Kolonie ještě neuložila žádný stav.")
        return
    st = json.load(open(path))
    print(f"ŽIVÝ STAV KOLONIE (papír) | běží od {str(st.get('started'))[:16]} UTC"
          f"{' | POZASTAVENO' if st.get('paused') else ''}")
    total = start = 0.0
    for name, d in st.get("wallets", {}).items():
        w = Wallet.from_dict(d)
        s = w.stats()
        total += s["equity"]
        start += w.start
        pf = s["profit_factor"]
        pf = "∞" if pf == float("inf") else f"{pf:.2f}"
        print(f"\n{name} [{s['status']}] ${s['equity']:.2f} ({s['return']*100:+.2f} %) | "
              f"obchodů {s['trades']}, úspěšnost {s['win_rate']*100:.0f} %, PF {pf}, "
              f"max propad {s['max_drawdown']*100:.1f} %, poplatky ${s['fees']}")
        for sym, lot in w.lots.items():
            p = w.last_prices.get(sym) or lot.entry
            print(f"   otevřeno {sym}: nákup {lot.entry:.6g}, teď {p:.6g} ({(p/lot.entry-1)*100:+.1f} %) - {lot.reason[:70]}")
        for t in w.trades[-5:]:
            print(f"   {t['closed'][:16]} {t['symbol']}: {t['pnl']:+.2f} $ ({t['ret']*100:+.1f} %) {t['why']}")
    if start:
        print(f"\nCELKEM ${total:.2f} z ${start:.0f} ({(total/start-1)*100:+.2f} %)")
    ev = st.get("events", [])[-15:]
    if ev:
        print("\nPOSLEDNÍ UDÁLOSTI:")
        for e in ev:
            print("  ", " ".join(str(x) for x in e)[:160])
    est = (st.get("oracle") or {}).get("estimates") or []
    if est:
        print(f"\nVĚŠTEC: {len(est)} odhadů z posledního skenu")


if __name__ == "__main__":
    sys.exit(main())
