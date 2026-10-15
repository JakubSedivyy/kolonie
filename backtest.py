"""Backtest kolonie na skutečných historických datech z Alpaky.

    python backtest.py               # posledních 60 dní
    python backtest.py --days 120

Používá stejný kód botů jako živá kolonie. Plnění za open další svíčky, poplatky a skluz
podle config.COSTS, pravidla přežití platí i tady. Degen (nové meme coiny) se backtestovat
nedá - historie z DexScreeneru není veřejně k dispozici. Trend radar (hype z internetu)
v backtestu chybí - Memec a Vlna se tu rozhodují jen podle ceny a objemu.
"""
import argparse
import logging
from datetime import datetime, timedelta, timezone

from colony import config, report
from colony.agents.base import Executor
from colony.agents.strategies import OrbitAgent, TrendAgent, MemeAgent, WaveAgent
from colony.broker import SimBroker
from colony.engine import replay
from colony.market import AlpacaData, MEME_COINS, MAJOR_COINS

logging.basicConfig(level=logging.WARNING)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    start = end - timedelta(days=a.days)
    data = AlpacaData()
    ex = Executor(SimBroker(), notify=(lambda m: None) if a.quiet else print)

    try:
        from alpaca.trading.client import TradingClient
        from colony.market import AlpacaAssets
        assets = AlpacaAssets(TradingClient(config.ALPACA_KEY, config.ALPACA_SECRET, paper=True))
        majors, memes = assets.majors(), assets.memes()
    except Exception as e:
        print("Nelze načíst seznam krypta z Alpaky, používám výchozí:", e)
        majors = [f"{c}/USD" for c in ("BTC", "ETH", "SOL", "LINK", "AVAX", "LTC")]
        memes = [f"{c}/USD" for c in ("DOGE", "SHIB", "PEPE")]

    bots = []
    for agent, syms, crypto in [
        (OrbitAgent(), OrbitAgent.core + OrbitAgent.default_watch, False),
        (TrendAgent(), majors, True),
        (MemeAgent(), memes, True),
        (WaveAgent(), list(dict.fromkeys(majors + memes)), True),
    ]:
        # boti se "narodí" na začátku testu
        agent.wallet.born = start.isoformat()
        print(f"\n=== {agent.name}: stahuji {len(syms)} symbolů, {agent.minutes}min svíčky, {a.days} dní…")
        warmup = timedelta(minutes=agent.minutes * agent.lookback_bars)
        bars = data.bars(syms, agent.minutes, start - warmup, end, crypto=crypto)
        print(f"    staženo {sum(len(v) for v in bars.values()):,} svíček")
        replay(agent, bars, ex, notify=print)
        bots.append(agent.wallet)

    print("\n" + report.text_summary(bots, title=f"Backtest {a.days} dní"))
    print("\n" + report.eval_text(bots, a.days))
    report.write_html(bots, "backtest_report.html")
    print("\nPodrobnosti: backtest_report.html")
    print("Pozor: Orbit testuje pevný seznam známých akcií (historické 'movers' nejsou k dispozici),\n"
          "v živém provozu si akcie vybírá každé ráno sám.")


if __name__ == "__main__":
    main()
