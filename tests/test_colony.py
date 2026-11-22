"""Testy bez internetu: syntetická data, falešná Alpaka a DexScreener.
Spuštění:  python -m pytest -q tests   (nebo python tests/test_colony.py)"""
import os, sys, tempfile, time, types, json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("STATE_DIR", tempfile.mkdtemp())
# testy nikdy nesmí volat placené ani skutečné služby
for _k in ("XAI_API_KEY", "YOUTUBE_API_KEY", "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "COINGECKO_API_KEY"):
    os.environ[_k] = ""

from colony import config, survival
config.TELEGRAM_TOKEN = config.TELEGRAM_CHAT_ID = ""     # testy nikdy nepíšou na skutečný Telegram
from colony.wallet import Wallet
from colony.agents.base import Executor, Entry
from colony.agents.strategies import (OrbitAgent, TrendAgent, MemeAgent, WaveAgent, WhisperAgent, BearAgent, CaptainAgent,
                                     RotationAgent, LeaderAgent, DipAgent, SaverAgent)
from colony.agents.shadow import ShadowAgent, mirror
from colony.agents.degen import DegenAgent, HunterAgent
from colony.broker import SimBroker, Fill
from colony.engine import replay


def bars(n, start, minutes, drift=0.0, vol=0.002, seed=0, p0=100.0, spikes=()):
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, vol, n)
    close = p0 * np.exp(np.cumsum(r))
    open_ = np.r_[p0, close[:-1]]
    high = np.maximum(open_, close) * (1 + rng.uniform(0, vol, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, vol, n))
    volume = rng.uniform(800, 1200, n)
    for i, mult in spikes:
        volume[i] *= mult
    idx = pd.date_range(start, periods=n, freq=f"{minutes}min", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


# ------------------------------------------------------------------ peněženka
def test_wallet_round_trip_costs():
    w = Wallet("t", "crypto")
    w.open("X", 1.0, 100.0, stop=90)
    t = w.close("X", 100.0, "test")
    c = config.COSTS["crypto"]
    assert t["pnl"] < 0                               # na nulovém pohybu se ztrácí poplatky a skluz
    expected_loss = 100 * (1 + c["slip"]) * (1 + c["fee"]) - 100 * (1 - c["slip"]) * (1 - c["fee"])
    assert abs(-t["pnl"] - expected_loss) < 1e-9
    assert abs(w.cash - (1000 - expected_loss)) < 1e-9
    w2 = Wallet.from_dict(w.to_dict())
    assert w2.cash == w.cash and w2.trades == w.trades


def test_survival_rules():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    w = Wallet("t", "stock", born=now.isoformat())
    survival.check(w, now, 1000)
    ev = survival.check(w, now + timedelta(hours=1), 940)
    assert ("INJURED" in [e[0] for e in ev]) and w.status == "INJURED"
    ev = survival.check(w, now + timedelta(days=1), 960)
    assert w.status == "ALIVE" and w.rent_paid == config.RENT_PER_DAY
    ev = survival.check(w, now + timedelta(days=2), 799)
    assert ev[-1][0] == "DEATH"
    w3 = Wallet("t3", "stock", born=now.isoformat())
    ev = survival.check(w3, now + timedelta(days=31), 999)
    assert ev[-1][0] == "DEATH" and "nevydělal" in ev[-1][1]


# ------------------------------------------------------------------ strategie
def orb_day(day, breakout=True, seed=1):
    """Jeden obchodní den 9:30-16:00 NY: klidné rozpětí, pak průraz se silným objemem."""
    start = pd.Timestamp(f"{day} 09:30", tz="America/New_York").tz_convert("UTC")
    df = bars(78, start, 5, vol=0.0008, seed=seed)
    df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].clip(99.0, 100.6)
    if breakout:
        for k in range(5, 40):
            f = 1 + 0.002 * (k - 4)
            df.iloc[k, :4] = [100.4 * f, 100.6 * f, 100.3 * f, 100.55 * f]
        df.iloc[5, 4] = 5000
    return df


def test_orbit_breakout_and_flatten():
    a = OrbitAgent()
    a.wallet.born = "2026-03-01T00:00:00+00:00"
    df = pd.concat([orb_day("2026-03-02"), orb_day("2026-03-03", breakout=False, seed=5)])
    ex = Executor(SimBroker())
    replay(a, {"SPY": df}, ex)
    tr = a.wallet.trades
    assert len(tr) == 1, tr
    t = tr[0]
    opened = pd.Timestamp(t["opened"]).tz_convert("America/New_York")
    closed = pd.Timestamp(t["closed"]).tz_convert("America/New_York")
    assert opened.time() >= pd.Timestamp("09:45").time()
    assert closed.date() == opened.date()             # žádné držení přes noc
    assert t["why"] in ("cíl zisku", "konec obchodního dne", "stop-loss")


def test_trend_and_meme_run_and_pay_fees():
    start = pd.Timestamp("2026-01-01", tz="UTC")
    # trend: pila nahoru a dolů, ať vzniknou křížení
    n = 2000
    x = np.linspace(0, 12 * np.pi, n)
    df = bars(n, start, 15, vol=0.002, seed=3)
    wave = 100 * (1 + 0.04 * np.sin(x)) * np.exp(np.linspace(0, 0.6, n))
    for c in ("open", "high", "low", "close"):
        df[c] = df[c] / 100 * wave
    df["high"] = df[["open", "high", "close"]].max(axis=1)
    df["low"] = df[["open", "low", "close"]].min(axis=1)
    t = TrendAgent(); t.wallet.born = start.isoformat()
    replay(t, {"BTC/USD": df}, Executor(SimBroker()))
    assert len(t.wallet.trades) >= 3
    assert t.wallet.fees_paid > 0
    assert not t.wallet.lots                          # na konci testu vše zavřeno

    m = MemeAgent(); m.wallet.born = start.isoformat()
    md = bars(1500, start, 5, drift=0.0002, vol=0.004, seed=4, spikes=[(400, 8), (900, 10), (1200, 6)])
    replay(m, {"DOGE/USD": md}, Executor(SimBroker()))
    s = m.wallet.stats()
    assert s["equity"] > 0 and m.wallet.status in ("ALIVE", "INJURED", "DEAD")


def test_death_in_crash():
    start = pd.Timestamp("2026-01-01", tz="UTC")
    m = MemeAgent(); m.wallet.born = start.isoformat()
    m.max_position_frac = 1.0; m.risk_per_trade = 0.5; m.stop_pct = 0.6   # schválně hazardér
    orig = m.entry
    def no_time_limit(df, i, ctx):
        e = orig(df, i, ctx)
        if e:
            e.max_hold_min = None; e.target = None
        return e
    m.entry = no_time_limit
    md = bars(700, start, 5, vol=0.001, seed=7, spikes=[(400, 9)])
    pv = md["close"].iloc[399]
    hh = md["high"].iloc[376:400].max()
    top = max(pv, hh) * 1.004
    md.iloc[400, :4] = [pv, top * 1.001, pv * 0.999, top]   # průraz 2h maxima s obřím objemem
    k = np.arange(700 - 401)
    crash = top * (1 - 0.0015 * k)                     # pak plynulý krach -45 %
    md.iloc[401:, 0] = crash; md.iloc[401:, 3] = crash * 0.9985
    md.iloc[401:, 1] = crash * 1.0005; md.iloc[401:, 2] = crash * 0.998
    replay(m, {"DOGE/USD": md}, Executor(SimBroker()))
    assert m.wallet.status == "DEAD", m.wallet.stats()
    assert "zbankrotoval" in m.wallet.cause_of_death
    assert not m.wallet.lots


def test_rejected_sell_keeps_position():
    class Rejecting(SimBroker):
        def sell(self, *a, **k):
            return None
    a = MemeAgent()
    ex = Executor(Rejecting())
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ex.enter(a, "DOGE/USD", 1.0, Entry(stop=0.9), 100, now)
    assert ex.exit(a, "DOGE/USD", 0.95, "stop-loss", now) is None
    assert "DOGE/USD" in a.wallet.lots              # účetnictví neprodá, co broker neprodal


# ------------------------------------------------------------------ degen
class FakeDex:
    def __init__(self):
        self.price, self.liq = 0.001, 150_000
        self.now_ms = int(datetime(2026, 1, 1, 12, tzinfo=timezone.utc).timestamp() * 1000)

    def fresh_tokens(self):
        return ["TokA", "TokB"]

    def _pair(self, tok):
        return {"pairAddress": "P" + tok, "url": "https://dexscreener.com/x",
                "baseToken": {"address": tok, "symbol": tok.upper()}, "quoteToken": {"symbol": "SOL"},
                "priceUsd": str(self.price), "liquidity": {"usd": self.liq}, "fdv": 2_000_000,
                "pairCreatedAt": self.now_ms - 2 * 3600 * 1000,
                "txns": {"m5": {"buys": 60, "sells": 20}}, "priceChange": {"h1": 40, "m5": 3},
                "volume": {"m5": 9000}}

    def pairs_for_tokens(self, addrs):
        return [self._pair(a) for a in addrs]

    def pair(self, pa):
        return self._pair(pa[1:])


def test_degen_buys_then_rug():
    dex = FakeDex()
    d = DegenAgent(dex)
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    msgs = []
    d.tick(now, "neutral", msgs.append)
    assert len(d.wallet.lots) == 2
    dex.liq = 10_000                                  # rug pull
    d.tick(now + timedelta(minutes=5), "neutral", msgs.append)
    assert not d.wallet.lots
    assert all(t["ret"] < -0.8 for t in d.wallet.trades)
    assert "TokA" in d.blacklist


class FakeDexEstablished(FakeDex):
    """Zavedený meme coin: 30 dní starý, hluboký pool, roste v hodině i za 6 h."""
    def __init__(self):
        super().__init__()
        self.price, self.liq = 0.5, 900_000

    def _pair(self, tok):
        p = super()._pair(tok)
        p.update({"fdv": 300_000_000, "pairCreatedAt": self.now_ms - 30 * 86400 * 1000,
                  "txns": {"h1": {"buys": 400, "sells": 250}, "m5": {"buys": 30, "sells": 20}},
                  "priceChange": {"h1": 4, "h6": 9, "h24": 15, "m5": 0.5}, "volume": {"h1": 60_000, "m5": 5000}})
        return p


def test_hunter_only_trending_established():
    class R:
        tokens = {"TokW"}
    dex = FakeDexEstablished()
    h = HunterAgent(dex, radar=R())
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    msgs = []
    h.tick(now, "neutral", msgs.append)
    assert list(h.wallet.lots) == ["TOKW·TokW"], h.wallet.lots       # jen coin z radaru, ne čerstvé tokeny
    assert DegenAgent(FakeDexEstablished()).score(dex._pair("X"), dex.now_ms) is None   # pro Degena moc starý
    assert h.score(FakeDex()._pair("Y"), dex.now_ms) is None                            # pro Lovce moc nový
    dex.price *= 1.3
    h.tick(now + timedelta(hours=1), "neutral", msgs.append)
    assert not h.wallet.lots and h.wallet.trades[0]["why"] == "cíl zisku"
    assert any("Lovec" in m for m in msgs)


def test_hunter_waits_week_before_rebuying_same_coin():
    class R:
        tokens = {"TokW"}
    dex = FakeDexEstablished()
    h = HunterAgent(dex, radar=R())
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    h.tick(now, "neutral", lambda m: None)
    dex.price *= 1.3                                   # cíl zisku -> prodá
    h.tick(now + timedelta(hours=1), "neutral", lambda m: None)
    assert not h.wallet.lots and h.wallet.trades[0]["why"] == "cíl zisku"
    h.tick(now + timedelta(days=3), "neutral", lambda m: None)
    assert not h.wallet.lots                           # za 3 dny ještě ne (dřív kupoval znovu už po 6 h)
    h.tick(now + timedelta(days=8), "neutral", lambda m: None)
    assert list(h.wallet.lots) == ["TOKW·TokW"]        # po týdnu zase smí


def daily(n=300, drift=0.0, seed=0, p0=100.0, vol=0.01, end="2026-11-13"):
    idx = pd.date_range(end=pd.Timestamp(end, tz="UTC") + pd.Timedelta(hours=5), periods=n, freq="1D")
    df = bars(n, idx[0], 1440, drift=drift, vol=vol, seed=seed, p0=p0)
    df.index = idx
    return df


def test_kompas_monthly_rotation():
    k = RotationAgent()
    drifts = {"VTI": 0.001, "XLK": 0.002, "VEA": 0.0005, "VWO": -0.001, "GLD": 0.0015,
              "TLT": -0.002, "IEF": -0.0005, "VNQ": -0.001, "DBC": -0.0015, "BIL": 0.0001}
    data = {s: daily(n=400, drift=d, seed=i, vol=0.003) for i, (s, d) in enumerate(drifts.items())}
    data["GLD"] = data["GLD"].iloc[::5].combine_first(data["GLD"].iloc[-100:])   # díry v datech IEX (každý 5. den)
    now = datetime(2026, 11, 16, 15, tzinfo=timezone.utc)
    k.on_bars(data, now)
    assert len(k.scores) == 9 and "GLD" in k.news and "9 z 9 ETF" in k.news, k.news
    assert set(k.targets) == {"XLK", "GLD", "VTI"}, k.targets       # tři nejsilnější, žádné dluhopisy
    assert abs(sum(k.targets.values()) - 0.98) < 1e-9
    k.wallet.cash = 1000
    df = data["XLK"]
    e = k.entry(df, len(df) - 1, {"symbol": "XLK"})
    assert e and e.stop < df["close"].iloc[-1]
    assert 300 < k.size_usd(df["close"].iloc[-1], e.stop) < 340      # třetina peněženky
    assert k.entry(df, len(df) - 1, {"symbol": "TLT"}) is None
    from colony.wallet import Lot
    lot = Lot("TLT", 1, 100, 85, None, None, now.isoformat(), None)
    assert "rotace" in k.exit_signal(df, len(df) - 1, lot)
    # během měsíce se cíle nemění, i když se trh otočí
    k.on_bars({s: daily(n=400, drift=-0.003, seed=i) for i, s in enumerate(drifts)}, now + timedelta(days=5))
    assert set(k.targets) == {"XLK", "GLD", "VTI"}
    # nový měsíc a všechno padá -> všechno do bezpečných krátkých dluhopisů
    k.on_bars({s: daily(n=400, drift=-0.003, seed=i, vol=0.003) for i, s in enumerate(drifts)}, now + timedelta(days=20))
    assert list(k.targets) == ["BIL"] and abs(k.targets["BIL"] - 0.98) < 1e-9, k.targets


def test_lev_holds_leaders_sells_laggards():
    lev = LeaderAgent()
    data = {s: daily(drift=0.0005 + 0.0002 * i, seed=i, vol=0.004) for i, s in enumerate(lev.stocks)}
    data["WMT"] = daily(drift=-0.002, seed=99, vol=0.004)            # padá -> nesmí koupit
    now = datetime(2026, 11, 16, 15, tzinfo=timezone.utc)
    lev.on_bars(data, now)
    top = sorted(lev.rank, key=lev.rank.get)[:5]
    assert "WMT" not in lev.rank and lev.ready
    s = top[0]
    assert lev.entry(data[s], 299, {"symbol": s})
    last = sorted(lev.rank, key=lev.rank.get)[-1]
    assert lev.entry(data[last], 299, {"symbol": last}) is None or lev.rank[last] <= 5
    from colony.wallet import Lot
    assert "trend zlomen" in lev.exit_signal(data["WMT"], 299, Lot("WMT", 1, 100, 80, None, 0.2, now.isoformat(), None))
    assert lev.exit_signal(data[s], 299, Lot(s, 1, 100, 80, None, 0.2, now.isoformat(), None)) is None
    lev.on_bars({}, now)                                              # bez dat nerozhoduje
    assert lev.exit_signal(data["WMT"], 299, Lot("WMT", 1, 100, 80, None, 0.2, now.isoformat(), None)) is None


def test_sokol_buys_dip_in_uptrend():
    sok = DipAgent()
    df = daily(drift=0.002, seed=3, vol=0.004)
    for k in range(3):                                                # tři dny prudkého poklesu
        df.iloc[-3 + k, 3] = df["close"].iloc[-4] * (1 - 0.02 * (k + 1))
    p = sok.prepare(df)
    e = sok.entry(p, len(p) - 1, {"symbol": "IVV"})
    assert e and e.max_hold_min == 14 * 24 * 60 and e.stop < p["close"].iloc[-1]
    falling = sok.prepare(daily(drift=-0.002, seed=3, vol=0.004))
    falling.iloc[-1, falling.columns.get_loc("rsi2")] = 3
    assert sok.entry(falling, len(falling) - 1, {"symbol": "IVV"}) is None    # pod 200denním průměrem nekupuje
    up = sok.prepare(daily(drift=0.002, seed=3, vol=0.004))
    from colony.wallet import Lot
    lot = Lot("IVV", 1, 100, 90, None, None, "2026-11-10T15:00:00+00:00", None)
    if up["close"].iloc[-1] > up["sma5"].iloc[-1]:
        assert "odraz" in sok.exit_signal(up, len(up) - 1, lot)


def test_wallet_add_averages_price():
    w = Wallet("Spořič", "stock")
    w.open("VOO", 1.0, 100.0, stop=1.0, apply_slippage=False)
    w.add("VOO", 1.0, 120.0, apply_slippage=False)
    lot = w.lots["VOO"]
    assert lot.qty == 2 and abs(lot.entry - 110) < 1e-9 and abs(lot.cost - 220) < 1e-9
    assert abs(w.cash - 780) < 1e-9
    t = w.close("VOO", 121.0, "test", apply_slippage=False)
    assert abs(t["pnl"] - 22) < 1e-9


def test_partial_sell_keeps_rest_and_forced_fill():
    class Half(SimBroker):
        def sell(self, symbol, qty, price, crypto=False):
            return Fill(qty / 2, price, "x")             # Alpaca prodala jen polovinu
    a = BearAgent()
    ex = Executor(Half())
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ex.enter(a, "SH", 10.0, Entry(stop=9.0), 200, now)
    q = a.wallet.lots["SH"].qty
    t = ex.exit(a, "SH", 11.0, "test", now)
    lot = a.wallet.lots["SH"]                            # zbytek pozice pořád v peněžence
    assert abs(t["qty"] - q / 2) < 1e-9 and abs(lot.qty - q / 2) < 1e-9
    assert abs(a.wallet.equity({"SH": 11.0}) - (1000 + q * 1.0)) < 1.0
    # broker nakoupil víc, než peněženka čekala (skok ceny u celých akcií) -> zapsat i tak
    class Gap(SimBroker):
        def buy_usd(self, symbol, usd, price, crypto=False):
            return Fill(round(usd / price), price * 1.03, "x")
    b = BearAgent(); b.wallet.cash = 100.0
    lot = Executor(Gap()).enter(b, "PSQ", 10.0, Entry(stop=9.0), 99.0, now)
    assert lot and "PSQ" in b.wallet.lots and b.wallet.cash < 0.5


def test_short_death_books_real_loss():
    from colony.live import Colony
    w = Wallet("Stín", "short")
    w.open("BTC/USD", 1.0, 100.0, stop=0, meta={"side": "short"}, apply_slippage=False)
    w.mark({"BTC/USD": 110.0})                            # cena vyrostla = short prodělává
    assert abs(w.real_price("BTC/USD") - 110.0) < 1e-9
    real = w.real_price("BTC/USD") * 1.05
    t = w.close("BTC/USD", real, "smrt bota", apply_slippage=False)
    assert t["pnl"] < -10, t                              # dřív se tu zapsal zisk +14 $


def test_daily_bots_guards():
    lev = LeaderAgent()
    data = {s: daily(drift=0.001 + 0.0001 * i, seed=i, vol=0.004) for i, s in enumerate(lev.stocks[:3])}
    lev.on_bars(data, datetime(2026, 11, 16, tzinfo=timezone.utc))
    s = next(iter(lev.rank))
    assert not lev.ready and lev.entry(data[s], 299, {"symbol": s}) is None     # 3 firmy z 16 = málo dat
    data = {s: daily(drift=0.001 + 0.0001 * i, seed=i, vol=0.004) for i, s in enumerate(lev.stocks)}
    lev.on_bars(data, datetime(2026, 11, 16, tzinfo=timezone.utc))
    top = min(lev.rank, key=lev.rank.get)
    assert lev.entry(data[top], 299, {"symbol": top})
    lev.wallet.trades.append({"symbol": top, "why": "trailing stop", "closed": "2026-11-10T15:00:00+00:00", "pnl": -5})
    assert lev.entry(data[top], 299, {"symbol": top}) is None          # měsíc po stopu nekupuje zpět
    k = RotationAgent()
    k.restore({"v": 2, "month": "2026-11", "targets": {"GLD": 0.49}, "scores": {}})
    k.wallet.trades.append({"symbol": "GLD", "why": "stop-loss", "closed": "2026-11-12T15:00:00+00:00", "pnl": -5})
    df = daily(drift=0.001)
    assert k.entry(df, 299, {"symbol": "GLD"}) is None                 # do další rotace ne
    k.on_bars({}, datetime(2026, 11, 20, tzinfo=timezone.utc))         # stejný měsíc -> cíle drží i po restartu
    assert k.state()["targets"] == {"GLD": 0.49}
    old = RotationAgent(); old.restore({"month": "2026-11", "targets": {"VTI": 0.33}})
    assert old.month == "" and not old.targets                          # výběr starou metodou se přepočítá


def test_longtest_replays_daily_bots():
    from colony.longtest import run as long_run

    class D:
        def bars(self, syms, minutes, start, end=None, crypto=False):
            assert minutes == 1440
            return {s: daily(n=700, drift=0.0003 * (i % 4), seed=i, vol=0.01) for i, s in enumerate(syms)}
    lines, out = long_run(D(), [RotationAgent(), LeaderAgent(), DipAgent(), SaverAgent()])
    assert lines[0].startswith("DLOUHODOBÝ TEST") and len(lines) == 6, lines
    assert set(out) == {"Kompas", "Lev", "Sokol", "Spořič", "S&P 500"}
    assert out["Kompas"]["trades"] > 0 and all(-1 < v["return"] < 10 for v in out.values())


def test_review2_fixes():
    from colony.kronika import Kronika
    from colony import report
    idx = pd.date_range("2026-01-01", periods=3, freq="1h", tz="UTC")
    up = pd.DataFrame({"open": [100, 100, 100], "high": [101, 111, 101], "low": [99, 99, 99], "close": [100, 100, 100]}, index=idx)
    r, note = Kronika.simulate(100.0, 110.0, 90.0, up, short=True)
    assert note == "stop" and abs(r - (-0.10)) < 1e-9, r                 # short se stopem +10 % = -10 % (dřív -9,1 %)
    gap = pd.DataFrame({"open": [100, 80], "high": [101, 81], "low": [99, 79], "close": [100, 80]}, index=idx[:2])
    r, _ = Kronika.simulate(100.0, 90.0, None, gap)
    assert abs(r - (-0.20)) < 1e-9                                        # gap pod stop = plnění za open
    w = Wallet("Stín", "short")
    w.trades = [{"symbol": "X", "pnl": 5.0, "ret": 0.05}] * 30
    ok, checks = report.evaluate(w, 20)
    assert not ok and any("simulace" in c[0] for c in checks)            # simulace nikdy není kandidát na reálné peníze
    dex = FakeDex()
    d = DegenAgent(dex)
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    d.tick(now, "neutral", lambda m: None)
    assert d.wallet.lots
    dex._pair = lambda tok: {"pairAddress": "P" + tok, "priceUsd": None}  # neúplná odpověď DexScreeneru
    d.tick(now + timedelta(minutes=5), "neutral", lambda m: None, allow_entries=False)
    assert d.wallet.lots and not d.wallet.trades                          # žádný falešný -100 %


def test_dashboard_html_ids_exist():
    import re
    html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "colony", "web", "dashboard.html")).read()
    used = set(re.findall(r'\$\("([A-Za-z]+)"\)', html))
    have = set(re.findall(r'id="([A-Za-z]+)"', html))
    assert used and not (used - have), used - have                      # každý prvek, na který JS sahá, v HTML existuje


def test_long_term_bots_get_90_days():
    now = datetime(2026, 12, 20, tzinfo=timezone.utc)
    for name, dies in (("Spořič", False), ("Lev", False), ("Trendy", True)):
        w = Wallet(name, "stock"); w.born = (now - timedelta(days=40)).isoformat(); w.day = now.strftime("%Y-%m-%d")
        ev = survival.check(w, now, 995.0)
        assert any(t == "DEATH" for t, _ in ev) == dies, (name, ev)
    w = Wallet("Kompas", "stock"); w.born = (now - timedelta(days=91)).isoformat(); w.day = now.strftime("%Y-%m-%d")
    assert any("za 90 dní" in txt for _, txt in survival.check(w, now, 995.0))


def test_whisper_needs_two_sources():
    class R:
        def __init__(self, sources):
            self.hot = {"PEPE": {"score": 6.0, "sources": sources}}
        def score(self, s): return self.hot.get(s.split("/")[0], {}).get("score", 0.0)
    df = bars(200, pd.Timestamp("2026-01-01", tz="UTC"), 15, drift=0.002, vol=0.003, seed=4)
    a = WhisperAgent()
    p = a.prepare(df)
    i = len(p) - 1
    p.iloc[i, p.columns.get_loc("ret1h")] = 0.02
    p.iloc[i, p.columns.get_loc("rsi")] = 60
    one = R(["4chan /biz/", "4chan /biz/: vlákno"])
    two = R(["4chan /biz/", "X přes Groka", "StockTwits"])
    assert a.entry(p, i, {"symbol": "PEPE/USD", "radar": one}) is None
    e = a.entry(p, i, {"symbol": "PEPE/USD", "radar": two})
    assert e and "3 zdrojů" in e.reason and e.stop < p["close"].iloc[i]
    assert a.entry(p, i, {"symbol": "DOGE/USD", "radar": two}) is None   # o DOGE se nemluví


def test_bear_buys_inverse_etf_when_market_falls():
    a = BearAgent()
    assert "SQQQ" in a.universe({})
    start = pd.Timestamp("2026-01-05 14:30", tz="UTC")             # 9:30 New York
    df = bars(200, start, 15, drift=0.001, vol=0.003, seed=5)       # inverzní ETF roste = trh padá
    p = a.prepare(df)
    hits = [i for i in range(60, len(p)) if a.can_enter(p.index[i]) and a.entry(p, i, {"symbol": "SQQQ"})]
    assert hits, "medvěd by měl při rostoucím inverzním ETF nakoupit"
    e = a.entry(p, hits[0], {"symbol": "SQQQ"})
    assert e.stop < p["close"].iloc[hits[0]] and "padá" in e.reason
    down = a.prepare(bars(200, start, 15, drift=-0.001, vol=0.003, seed=5))
    assert not any(a.entry(down, i, {"symbol": "SQQQ"}) for i in range(60, 200))   # trh roste -> nic


def test_shadow_short_profits_when_coin_falls():
    assert abs(mirror(100, 90) - 110) < 1e-9
    start = pd.Timestamp("2026-01-01", tz="UTC")
    n = 24 * 8
    rng = np.random.default_rng(3)
    close = 100 * np.exp(np.cumsum(np.r_[np.zeros(80), rng.normal(-0.003, 0.008, n - 80)]))
    idx = pd.date_range(start, periods=n, freq="60min", tz="UTC")
    full = pd.DataFrame({"open": close * 1.001, "high": close * 1.004, "low": close * 0.996,
                         "close": close, "volume": 1000.0}, index=idx)

    class D:
        upto = 100
        def bars(self, syms, minutes, start, crypto=False):
            return {"XYZ/USD": full.iloc[:self.upto]}
    d = D()
    sh = ShadowAgent(d)
    msgs = []
    opened = None
    for k in range(100, n):
        d.upto = k
        now = (idx[k - 1] + pd.Timedelta(minutes=61)).to_pydatetime()
        sh.tick(now, "neutral", msgs.append, ["XYZ/USD"])
        if sh.wallet.lots and opened is None:
            opened = k
    assert opened, "stín měl při padajícím coinu otevřít short"
    assert sh.wallet.trades, "short se měl časem uzavřít"
    assert sh.wallet.trades[0]["pnl"] > 0, sh.wallet.trades[0]
    assert any("short" in m for m in msgs)
    sh2 = ShadowAgent(d); d.upto = 120
    sh2.tick((idx[119] + pd.Timedelta(minutes=61)).to_pydatetime(), "risk_on", msgs.append, ["XYZ/USD"])
    assert not sh2.wallet.lots                                       # v býčím trhu neshortuje


def test_shadow_trailing_does_not_use_same_bar_twice():
    """Svíčka s hlubokým minimem i maximem: stop se smí posunout až po kontrole, ne se stejnou svíčkou."""
    start = pd.Timestamp("2026-01-01", tz="UTC")
    idx = pd.date_range(start, periods=70, freq="60min", tz="UTC")
    close = np.full(70, 100.0)
    df = pd.DataFrame({"open": close, "high": close * 1.001, "low": close * 0.999, "close": close, "volume": 1.0}, index=idx)
    df.iloc[-1] = [100.0, 102.0, 94.0, 100.0, 1.0]          # minimum -6 %, maximum +2 %
    class D:
        def bars(self, syms, minutes, start, crypto=False): return {"X/USD": df}
    sh = ShadowAgent(D())
    opened = (idx[-2]).to_pydatetime()
    sh.wallet.open("X/USD", 1.0, 100.0, stop=96.0, now=opened,
                   meta={"p0": 100.0, "stop_px": 104.0, "trail": 0.04, "low": 100.0, "side": "short"})
    sh.tick((idx[-1] + pd.Timedelta(minutes=61)).to_pydatetime(), "neutral", lambda m: None, [])
    assert "X/USD" in sh.wallet.lots, "maximum 102 nezasáhlo původní stop 104 - short musí zůstat otevřený"
    assert sh.wallet.lots["X/USD"].meta["stop_px"] < 98.0          # až teď se stop utáhl podle minima 94


def test_orbit_breakout_later_in_day():
    """Průraz v 10:30 s objemem nad běžným objemem (ne nad obřím ranním) musí projít."""
    o = OrbitAgent().apply({"or_bars": 3, "vol_mult": 1.2, "last_entry": "14:00"})
    idx = pd.date_range("2026-01-05 14:30", periods=30, freq="5min", tz="UTC")
    vol = np.r_[np.full(3, 50_000.0), np.full(27, 8_000.0)]
    close = np.r_[100.5, 101.0, 100.6, np.full(26, 100.8), 101.6]
    df = pd.DataFrame({"open": close - 0.1, "high": np.r_[101.2, 101.4, 101.0, np.full(26, 101.0), 101.7],
                       "low": np.r_[99.8, 100.4, 100.2, np.full(26, 100.6), 100.9], "close": close, "volume": vol}, index=idx)
    df.iloc[-1, df.columns.get_loc("volume")] = 15_000.0      # 1,9x běžného, ale jen 0,3x ranního
    p = o.prepare(df)
    e = o.entry(p, len(p) - 1, {"symbol": "NVDA"})
    assert e and "běžného" in e.reason


def test_memory_learns_to_avoid_losing_situation():
    from colony.memory import Memory
    m = Memory(os.path.join(tempfile.mkdtemp(), "mem.json"))
    w = Wallet("Degen II", "degen")
    bad = {"likvidita": "do 150k $", "denní doba": "noc (0-6 UTC)"}
    good = {"likvidita": "nad 2M $", "denní doba": "ráno (6-12 UTC)"}
    for i in range(8):
        w.trades.append({"symbol": f"B{i}", "ret": -0.30, "pnl": -9, "closed": "x", "features": bad})
    for i in range(6):
        w.trades.append({"symbol": f"G{i}", "ret": 0.20, "pnl": 6, "closed": "x", "features": good})
    news = m.learn_from([w])
    assert any("poučil" in n and "do 150k" in n for n in news), news
    ok, mult, note = m.judge("Degen II", bad)
    assert not ok and "prodělal jsem 8x" in note
    ok, mult, note = m.judge("Degen II", good)
    assert ok and mult > 1.2                                  # kde se daří, vsadí víc
    ok, mult, _ = m.judge("Degen II", {"likvidita": "neznámá"})
    assert ok and mult == 1.0                                 # neznámá situace = normální sázka
    assert m.learn_from([w]) == []                            # stejné obchody se nepočítají dvakrát
    m2 = Memory(m.path)                                       # paměť přežije restart
    assert not m2.judge("Degen II", bad)[0]
    les = m2.lessons("Degen II")
    assert les[0]["verdict"] == "vyhýbá se" and any(l["verdict"] == "daří se" for l in les)


def test_memory_ignores_feature_every_trade_had():
    from colony.memory import Memory
    m = Memory(os.path.join(tempfile.mkdtemp(), "mem.json"))
    for i in range(6):                                        # jako Degen II: všechny obchody "v trendu = ano"
        m.record("Degen II", {"v trendu na internetu": "ano", "likvidita": "do 150k $"}, -0.25)
    ok, mult, _ = m.judge("Degen II", {"v trendu na internetu": "ano", "likvidita": "150-500k $"})
    assert ok and mult < 1.0                                  # nezakáže všechno, jen vsadí míň
    assert m.avoid_list("Degen II") == []
    for i in range(6):                                        # když rys začne rozlišovat, zákaz zase platí
        m.record("Degen II", {"v trendu na internetu": "ne", "likvidita": "150-500k $"}, 0.05)
    assert not m.judge("Degen II", {"v trendu na internetu": "ano"})[0]


def test_memory_forbids_only_feature_worse_than_rest():
    from colony.memory import Memory
    m = Memory(os.path.join(tempfile.mkdtemp(), "mem.json"))
    m.record("Lovec", {"stáří coinu": "1-7 dní"}, -0.12)      # jako Lovec 19. 10.: jediný mladší coin dopadl hůř
    for r in (-0.12, 0.25, -0.12, -0.13, -0.12, -0.12):
        m.record("Lovec", {"stáří coinu": "přes týden"}, r)
    assert m.avoid_list("Lovec") == []                        # rys nerozlišuje -> žádný zákaz
    assert m.judge("Lovec", {"stáří coinu": "přes týden"})[0]
    m.record("Lovec", {"stáří coinu": "1-7 dní"}, 0.10)       # zbytek je teď lepší (2 obchody, průměr -1 %)
    assert not m.judge("Lovec", {"stáří coinu": "přes týden"})[0]


def test_memory_judges_longs_and_shorts_separately():
    from colony.memory import Memory
    m = Memory(os.path.join(tempfile.mkdtemp(), "mem.json"))
    for i in range(8):                                        # jako Vlna: odpolední longy prodělávaly
        m.record("Vlna", {"denní doba": "odpoledne (12-18 UTC)"}, -0.07)
    for i in range(3):
        m.record("Vlna", {"denní doba": "noc (0-6 UTC)"}, 0.01)
    for i in range(8):                                        # odpolední shorty byly v pořádku
        m.record("Vlna", {"denní doba": "odpoledne (12-18 UTC)", "směr": "short"}, 0.01)
    for i in range(3):
        m.record("Vlna", {"denní doba": "noc (0-6 UTC)", "směr": "short"}, -0.01)
    assert not m.judge("Vlna", {"denní doba": "odpoledne (12-18 UTC)"})[0]               # long dál zakázaný
    assert m.judge("Vlna", {"denní doba": "odpoledne (12-18 UTC)", "směr": "short"})[0]  # short smí
    assert [k for k, *_ in m.avoid_list("Vlna")] == ["denní doba = odpoledne (12-18 UTC)"]
    for i in range(8):                                        # když prodělávají i shorty, zákaz se jich týká taky
        m.record("Vlna", {"denní doba": "večer (18-24 UTC)", "směr": "short"}, -0.08)
    ok, _, note = m.judge("Vlna", {"denní doba": "večer (18-24 UTC)", "směr": "short"})
    assert not ok and "u shortů" in note
    assert m.lessons("Vlna")


def test_degen_skips_what_memory_forbids():
    from colony.memory import Memory
    dex = FakeDex()
    d = DegenAgent(dex)
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    feats = d.features(dex._pair("TokA"), now, False)
    assert feats["likvidita"] == "150-500k $" and feats["v trendu na internetu"] == "ne"
    m = Memory(os.path.join(tempfile.mkdtemp(), "mem.json"))
    for i in range(7):
        m.record(d.name, {"likvidita": "150-500k $"}, -0.25)
        m.record(d.name, {"likvidita": "do 150k $"}, 0.0)
    d.memory = m
    d.tick(now, "neutral", lambda x: None)
    assert not d.wallet.lots                                  # paměť nákup zakázala
    d.tick(now, "neutral", lambda x: None, allow_entries=False)
    d2 = DegenAgent(dex); d2.tick(now, "neutral", lambda x: None)
    assert d2.wallet.lots and all("features" in l.meta for l in d2.wallet.lots.values())


def test_kronika_records_and_learns_from_missed_signals():
    from colony.kronika import Kronika
    from colony.memory import Memory
    k = Kronika(os.path.join(tempfile.mkdtemp(), "k.db"))
    t0 = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    k.signal("Vlna", "SOL/USD", 100.0, taken=False, skip_reason="plno pozic", stop=95, target=110, horizon_min=60,
             features={"coin": "SOL/USD"}, meta={"minutes": 15, "crypto": True}, now=t0)
    k.signal("Vlna", "SOL/USD", 101.0, taken=False, skip_reason="plno pozic", horizon_min=60, now=t0 + timedelta(minutes=5))
    k.signal("Vlna", "ETH/USD", 50.0, taken=True, horizon_min=60, now=t0)
    assert k.bot_summary("Vlna")["signals"] == 2                 # stejný nekoupený signál se nezapíše dvakrát
    assert k.due_signals(t0 + timedelta(minutes=30)) == []       # ještě neuplynula doba
    due = k.due_signals(t0 + timedelta(minutes=61))
    assert len(due) == 1 and due[0]["features"] == {"coin": "SOL/USD"}
    idx = pd.date_range(t0 + timedelta(minutes=15), periods=4, freq="15min", tz="UTC")
    up = pd.DataFrame({"open": [100, 103, 106, 109.0], "high": [103, 106, 111, 112.0], "low": [99, 102, 105, 108.0],
                       "close": [103, 106, 109, 111.0]}, index=idx)
    ret, note = Kronika.simulate(100, 95, 110, up)
    assert note == "cíl" and abs(ret - 0.10) < 1e-9
    down = up.copy(); down["low"] = [99, 94, 90, 90.0]; down["high"] = [101, 111, 100, 95.0]
    assert Kronika.simulate(100, 95, 110, down)[1] == "stop"     # stop i cíl ve stejné svíčce -> počítá stop
    assert Kronika.simulate(100, 105, None, up, short=True)[1] == "stop"
    k.resolve(due[0]["id"], 0.10, "cíl")
    s = k.bot_summary("Vlna")
    assert s["missed_resolved"] == 1 and s["missed_would_win"] == 1
    w = Wallet("Vlna", "crypto"); w.trades.append({"symbol": "ETH/USD", "opened": "a", "closed": "b", "entry": 1, "exit": 2,
                                                   "ret": 0.5, "pnl": 1, "why": "cíl", "reason": "x", "features": {}})
    k.trades([w]); k.trades([w])
    k.radar_snapshot(t0, "neutral", {"hot": [{"symbol": "PEPE", "score": 5, "sources": ["4chan"]}], "fear_greed": {"value": 30}})
    rep = k.report(days=36500)
    assert "1 obchodů" in rep and "PEPE (1h)" in rep and "vyhrálo by 1" in rep
    m = Memory(os.path.join(tempfile.mkdtemp(), "m.json"))
    for i in range(12):
        m.record("Vlna", {"coin": "SOL/USD"}, -0.2, weight=0.5)          # stínové obchody mají poloviční váhu
        m.record("Vlna", {"coin": "ETH/USD"}, 0.0, weight=0.5)
    assert m.stats("Vlna")["coin = SOL/USD"][0] == 6.0
    assert not m.judge("Vlna", {"coin": "SOL/USD"})[0]


def test_wallet_short_accounting():
    w = Wallet("Test", "stock")                                # akcie: bez poplatku, skluz 0,05 %
    w.open("XYZ", 10, 100.0, stop=0, now=datetime(2026, 1, 1, tzinfo=timezone.utc), meta={"side": "short"},
           apply_slippage=False)
    assert w.lots["XYZ"].meta["p0"] == 100.0 and abs(w.cash - 0.0) < 1e-6 + config.START_CAPITAL - 1000
    w.mark({"XYZ": 90.0})
    assert abs(w.equity() - (config.START_CAPITAL + 100)) < 1e-6          # cena -10 % -> short +$100
    w.mark({"XYZ": 120.0})
    assert abs(w.equity() - (config.START_CAPITAL - 200)) < 1e-6          # cena +20 % -> short -$200
    t = w.close("XYZ", 90.0, "cíl", apply_slippage=False)
    assert abs(t["pnl"] - 100) < 1e-6 and t["side"] == "short" and t["p0"] == 100.0


def test_executor_short_stock_and_crypto():
    class B(SimBroker):
        simulated = False
        calls = []
        def short_sell(self, symbol, usd, price):
            self.calls.append(("short", symbol)); return Fill(int(usd // price), price, "x")
        def cover(self, symbol, qty, price):
            self.calls.append(("cover", symbol)); return Fill(qty, price, "x")
    b, now = B(), datetime(2026, 1, 5, 15, tzinfo=timezone.utc)
    ex = Executor(b)
    o = OrbitAgent()
    lot = ex.enter(o, "NVDA", 100.0, Entry(stop=102.0, target=96.0, reason="test"), 300, now, short=True)
    assert lot and ("short", "NVDA") in b.calls and lot.meta["stop_px"] == 102.0 and not lot.meta["sim"]
    assert o.check_exit_short(lot, 101, 99, 100, 100, now) is None
    assert o.check_exit_short(lot, 99, 95.5, 96, 98, now)[1] == "cíl zisku"
    t = ex.exit(o, "NVDA", 96.0, "cíl zisku", now)
    assert ("cover", "NVDA") in b.calls and t["pnl"] > 0
    v = WaveAgent()                                           # krypto short = jen simulace, broker se nevolá
    n = len(b.calls)
    lot = ex.enter(v, "SOL/USD", 100.0, Entry(stop=108.0, trail_pct=0.08, reason="x"), 200, now, short=True)
    assert lot.meta["sim"] and len(b.calls) == n
    assert v.check_exit_short(lot, 101, 90, 91, 100, now) is None          # minimum 90 -> trailing se utáhne
    assert abs(lot.meta["stop_px"] - 90 * 1.08) < 1e-9
    assert v.check_exit_short(lot, 98, 95, 97, 96, now)[1] == "trailing stop"


def test_short_entries_mirror_long_logic():
    start = pd.Timestamp("2026-01-01", tz="UTC")
    rng = np.random.default_rng(3)
    close = 100 * np.exp(np.cumsum(np.r_[np.zeros(100), rng.normal(-0.002, 0.012, 140)]))
    df = pd.DataFrame({"open": close * 1.002, "high": close * 1.006, "low": close * 0.994, "close": close,
                       "volume": 1000.0}, index=pd.date_range(start, periods=240, freq="60min", tz="UTC"))
    v = WaveAgent(); p = v.prepare(df)
    hits = [i for i in range(80, 240) if v.short_entry(p, i, {})]
    assert hits, "Vlna má v sestupném trhu shortovat"
    e = v.short_entry(p, hits[0], {})
    assert e.stop > p["close"].iloc[hits[0]] and "sestupná" in e.reason
    up = df.copy(); up[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].values[::-1]
    pu = v.prepare(up)
    assert not any(v.short_entry(pu, i, {}) for i in range(80, 140))       # v rostoucím trhu ne
    # Orbit: propad pod ranní minimum
    o = OrbitAgent().apply({"or_bars": 3, "vol_mult": 1.2, "last_entry": "14:00"})
    idx = pd.date_range("2026-01-05 14:30", periods=30, freq="5min", tz="UTC")
    c = np.r_[100.5, 100.0, 100.4, np.full(26, 100.2), 99.4]
    d = pd.DataFrame({"open": c + 0.1, "high": np.r_[101.0, 100.6, 100.8, np.full(26, 100.4), 100.3],
                      "low": np.r_[99.8, 99.6, 100.0, np.full(26, 100.0), 99.3], "close": c,
                      "volume": np.r_[np.full(3, 50_000.0), np.full(26, 8_000.0), 15_000.0]}, index=idx)
    po = o.prepare(d)
    e = o.short_entry(po, len(po) - 1, {"symbol": "TSLA"})
    assert e and e.stop > 99.4 and e.target < 99.4 and "propad" in e.reason


def test_dex_bot_never_rebuys_coin_after_stop_loss():
    """Degen II koupil NUBI 4x po sobě a pokaždé prodělal 25 % - po stop-lossu už ten coin nesmí znovu koupit."""
    dex = FakeDex()
    d = DegenAgent(dex)
    now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    d.tick(now, "neutral", lambda m: None)
    assert "TOKA·TokA" in d.wallet.lots
    dex.price *= 0.7                                          # -30 % -> stop-loss
    d.tick(now + timedelta(minutes=5), "neutral", lambda m: None, allow_entries=False)
    assert "TokA" in d.blacklist and not d.wallet.lots
    dex.price /= 0.7
    d.tick(now + timedelta(days=2), "neutral", lambda m: None)
    assert not d.wallet.lots                                  # ani po dvou dnech


def test_video_helpers():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy"))
    import videa
    assert videa.video_id("https://youtu.be/eynxyoKgpng?is=abc") == "eynxyoKgpng"
    assert videa.video_id("https://www.youtube.com/watch?v=O5eC5lY7ZXY&t=3") == "O5eC5lY7ZXY"
    vtt = "WEBVTT\nKind: captions\n\n00:00:01.000 --> 00:00:02.000\nkup <c>když</c>\n\n00:00:02.000 --> 00:00:03.000\nkup když\nRSI pod 30"
    assert videa.vtt_to_text(vtt) == "kup když RSI pod 30"


# ------------------------------------------------------------------ živá kolonie s falešnou Alpakou
def test_live_colony_ticks():
    import colony.live as live

    now = pd.Timestamp(datetime.now(timezone.utc)).floor("15min").to_pydatetime() + timedelta(seconds=40)
    live.utcnow = lambda: now                         # pevný čas = test nezávisí na hodinách

    class FakeData:
        def bars(self, syms, minutes, start_, end=None, crypto=False):
            out = {}
            for k, s in enumerate(syms):
                n = 450 if minutes < 60 else 300
                st = pd.Timestamp(now).floor(f"{minutes}min") - pd.Timedelta(minutes=minutes * n)
                spikes = [(n - 1, 10)] if s == "DOGE/USD" else []
                df = bars(n, st, minutes, drift=0.0003, vol=0.002, seed=k, spikes=spikes)
                if s == "DOGE/USD":
                    top = df["high"].iloc[-25:-1].max() * 1.004
                    df.iloc[-1, :4] = [df["close"].iloc[-2], top * 1.001, df["close"].iloc[-2] * 0.999, top]
                out[s] = df
            return out

        def latest_prices(self, syms, crypto=False):
            return {s: 100.0 for s in syms}

        def stock_movers(self, top=20):
            return ["NVDA", "TSLA"]

    class FakeTC:
        def get_clock(self):
            return types.SimpleNamespace(is_open=False)

    class FakeBroker(SimBroker):
        simulated = True
        def __init__(self):
            self.tc = FakeTC()
        def clock(self):
            return self.tc.get_clock()

    class FakeAssets:
        def __init__(self, tc): pass
        def majors(self): return ["BTC/USD", "ETH/USD"]
        def memes(self): return ["DOGE/USD", "SHIB/USD"]

    live.AlpacaData, live.AlpacaBroker, live.AlpacaAssets = FakeData, FakeBroker, FakeAssets
    live.DexScreener = FakeDex
    sent = []
    c = live.Colony()
    c.retired = {}                                    # tady testujeme obchodování Memce; vyřazení má vlastní test
    c.radar.maybe_refresh = lambda force=False: False
    c.oracle.tick = lambda *a, **k: None
    assert not c.grok.enabled
    c.tg.send = sent.append
    c.exec.notify = sent.append
    c.tick()
    assert set(c.bench.get("start", {})) == {"SPY", "BTC/USD"} and set(c.bench["now"]) == {"SPY", "BTC/USD"}, c.bench
    c.mood, c.mood_at = "risk_on", now + timedelta(days=1)   # vynutit dobrou náladu
    held = lambda: {s: l.entry for a in c.bar_agents for s, l in a.wallet.lots.items()}
    c.data.latest_prices = lambda syms, crypto=False: {s: held().get(s, 100.0) for s in syms}
    c.last_bar = {}
    c.tick()
    assert any("Memec KOUPIL DOGE/USD" in m for m in sent), sent
    # stop-loss přes guard_stops: cena spadne
    lot = c.bar_agents[2].wallet.lots["DOGE/USD"]
    c.data.latest_prices = lambda syms, crypto=False: {s: lot.stop * 0.99 for s in syms}
    c.guard_stops(now)
    assert "DOGE/USD" not in c.bar_agents[2].wallet.lots
    assert any("Memec PRODAL DOGE/USD" in m for m in sent), sent
    # short bez živé ceny (TRUMP 11. 11.): stop musí chytit aspoň hodinová svíčka
    vl = c.bar_agents[3]
    assert vl.name == "Vlna"
    c.exec.enter(vl, "ETH/USD", 50.0, Entry(stop=55.0, reason="test"), 100.0, now, short=True)
    assert "ETH/USD" in vl.wallet.lots
    c.data.latest_prices = lambda syms, crypto=False: {}       # Alpaca cenu nevrátí
    c.guard_stops(now)
    assert "ETH/USD" in vl.wallet.lots                         # guard_stops nic neví
    c.last_bar = {}
    c.tick()                                                   # svíčka ~100 > stop 55
    assert "ETH/USD" not in vl.wallet.lots, vl.wallet.lots
    assert any("Vlna UZAVŘEL SHORT ETH/USD" in m and "stop-loss" in m for m in sent), sent[-5:]
    assert os.path.exists(c.state_path)
    # snapshot pro dashboard: čisté JSON hodnoty (žádné numpy "False" jako text)
    import json
    from colony.dashboard import _jsonable
    snap = json.loads(json.dumps(c.snapshot(now), default=_jsonable))
    assert len(snap["bots"]) == 15
    assert all("kronika" in b and "lessons" in b for b in snap["bots"])
    assert os.path.exists(os.path.join(config.STATE_DIR, "kronika.txt"))     # hodinový přehled kroniky vznikl
    assert all(isinstance(ok, bool) for e in snap["eval"] for _, ok in e["checks"])
    c.save()
    st = json.load(open(c.state_path))                # původní mrtvý Degen ze starší verze -> hřbitov
    old = Wallet("Degen", "degen"); old.status, old.cause_of_death, old.cash = "DEAD", "pod $800", 796.78
    st["wallets"]["Degen"] = old.to_dict()
    json.dump(st, open(c.state_path, "w"))
    c.degen.cooldown["TokCool"] = now - timedelta(minutes=10)      # čerstvě prodaný coin
    c.degen.cooldown["TokOld"] = now - timedelta(days=2)           # pauza dávno vypršela
    c.lovec.cooldown["TokL"] = now - timedelta(hours=1)
    c.save()
    st2 = json.load(open(c.state_path)); st2["wallets"]["Degen"] = old.to_dict()
    json.dump(st2, open(c.state_path, "w"))
    c2 = live.Colony()                                # obnovení ze souboru
    assert "TokCool" in c2.degen.cooldown and "TokOld" not in c2.degen.cooldown, c2.degen.cooldown
    assert "TokL" in c2.lovec.cooldown                # pauza po prodeji přežije restart
    assert [w.name for w in c2.wallets] == ["Orbit", "Trendy", "Memec", "Vlna", "Šeptač", "Medvěd", "Kapitán", "Kompas", "Lev", "Sokol", "Spořič",
                                          "Degen II", "Lovec", "Stín", "Věštec", "Degen"]
    assert abs(sum(w.equity() for w in c2.wallets) - sum(w.equity() for w in c.wallets) - 796.78) < 1e-6
    snap2 = c2.snapshot(datetime.now(timezone.utc))
    assert not [b for b in snap2["bots"] if b["name"] == "Degen"]        # mrtví boti v dashboardu nejsou
    assert all(b["group"] in ("active", "calm") and "turnover" in b for b in snap2["bots"])
    assert "bench" in snap2 and "longtest" in snap2
    print("\n".join(sent[:15]))

    # vyřazení botů na přání Jakuba (16. 11.): Memec prodá pozici a odejde na hřbitov, ostatní žijí dál
    out = []
    c2.tg.send = c2.exec.notify = out.append
    mem = c2.bar_agents[2]
    assert mem.name == "Memec" and c2.retired.keys() >= {"Memec", "Lovec"}
    c2.exec.enter(mem, "DOGE/USD", 100.0, Entry(stop=90.0, reason="test"), 100.0, now)
    c2.retire_bots(now)
    assert mem.wallet.status == "DEAD" and not mem.wallet.lots, mem.wallet.lots
    assert mem.wallet.trades[-1]["why"] == "vyřazení z kolonie"
    assert c2.lovec.wallet.status == "DEAD" and "na přání Jakuba" in c2.lovec.wallet.cause_of_death
    alive = [a for a in c2.bar_agents if a.name not in config.RETIRED]
    assert alive and all(a.wallet.status == "ALIVE" for a in alive) and c2.stin.wallet.status == "ALIVE"
    # 22. 11.: Orbit, Trendy, Vlna a Medvěd neprošli pětiletým testem -> na hřbitov, laboratoř je neladí
    import lab
    assert all(a.wallet.status == "DEAD" for a in c2.bar_agents if a.name in ("Orbit", "Trendy", "Vlna", "Medvěd"))
    assert not lab.active_variants()
    # 21. 11.: Šeptač, Kapitán a Věštec (0 obchodů) taky na hřbitov
    assert c2.retired.keys() >= {"Šeptač", "Kapitán", "Věštec"}
    assert c2.oracle.wallet.status == "DEAD" and "na přání Jakuba" in c2.oracle.wallet.cause_of_death
    assert all(a.wallet.status == "DEAD" for a in c2.bar_agents if a.name in ("Šeptač", "Kapitán"))
    n_gone = len(c2.retired)
    assert sum("odchází na hřbitov" in m for m in out) == n_gone
    c2.retire_bots(now)
    assert sum("odchází na hřbitov" in m for m in out) == n_gone      # podruhé už nic
    c2.last_bar = {}
    c2.run_bar_agent(mem, now)
    assert not mem.wallet.lots                                    # mrtvý bot nenakupuje

    # dlouhodobí boti na denních svíčkách běží jen při otevřené burze
    c2._clock, c2._clock_at = types.SimpleNamespace(is_open=True), time.time() + 1e9
    sav = [a for a in c2.bar_agents if a.name == "Spořič"][0]
    c2.mood = "risk_off"                                          # nálada spoření nezmenší
    c2.run_bar_agent(sav, now)
    assert set(sav.wallet.lots) == {"VOO", "IBIT"}, sav.wallet.lots
    assert all(abs(l.cost - 50) < 0.5 for l in sav.wallet.lots.values())
    c2.last_bar = {}
    c2.run_bar_agent(sav, now)
    assert all(abs(l.cost - 50) < 0.5 for l in sav.wallet.lots.values())   # stejný týden nepřikupuje
    sav.wallet.lots["VOO"].meta["topup_key"] = "2000-01"          # jako by minulý nákup byl dávno
    c2.last_bar = {}
    c2.run_bar_agent(sav, now)
    assert abs(sav.wallet.lots["VOO"].cost - 100) < 1, sav.wallet.lots["VOO"].cost
    assert sav.wallet.lots["VOO"].meta["topup_key"] == SaverAgent.week(now)
    assert any("Spořič PŘIKOUPIL VOO" in m for m in out)
    # Kompas si výběr měsíce pamatuje přes restart
    kom = [a for a in c2.bar_agents if a.name == "Kompas"][0]
    kom.restore({"v": 2, "month": "2026-11", "targets": {"XLK": 0.33}, "scores": {"XLK": 0.1}})
    c2.save()
    c3 = live.Colony()
    assert c3.bench == c2.bench and c3.bench.get("start")      # srovnání s trhem přežije restart
    import subprocess
    out_stav = subprocess.run([sys.executable, "stav.py"], capture_output=True, text=True,
                              cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))).stdout
    assert "SROVNÁNÍ S TRHEM" in out_stav and "Kompas" in out_stav, out_stav[-500:]
    assert [a for a in c3.bar_agents if a.name == "Kompas"][0].targets == {"XLK": 0.33}
    # výpadek dat: denní bot to zkusí znovu (dřív propadl celý den)
    c3._clock, c3._clock_at = types.SimpleNamespace(is_open=True), time.time() + 1e9
    def boom(*a, **k):
        raise RuntimeError("Alpaca nedostupná")
    real_bars, c3.data.bars = c3.data.bars, boom
    c3.last_bar = {}
    sok = [a for a in c3.bar_agents if a.name == "Sokol"][0]
    c3.run_bar_agent(sok, now)
    assert "Sokol" not in c3.last_bar


class FakeResp:
    def __init__(self, j): self.j = j
    def raise_for_status(self): pass
    def json(self): return self.j


class FakeSession:
    headers = {}
    def get(self, url, **kw):
        if "coingecko" in url:
            return FakeResp({"coins": [{"item": {"symbol": "PEPE", "score": 0}}, {"item": {"symbol": "DOGE", "score": 3}}]})
        if "geckoterminal" in url:
            return FakeResp({"data": [{"attributes": {"name": "PEPE / SOL"},
                                       "relationships": {"base_token": {"data": {"id": "solana_AbC123"}}}}]})
        if "metas/trending" in url:
            return FakeResp([{"name": "AI agenti", "slug": "ai", "marketCapChange": {"h24": 12.5}, "tokenCount": 9}])
        if "metas/meta" in url:
            return FakeResp({"pairs": [{"chainId": "solana", "baseToken": {"symbol": "AIDOG", "address": "Zz9"}}]})
        if "token-boosts/top" in url:
            return FakeResp([{"chainId": "solana", "tokenAddress": "Bst1"}, {"chainId": "ethereum", "tokenAddress": "0x1"}])
        raise RuntimeError("offline")
    def post(self, *a, **k):
        raise RuntimeError("offline")


class FakeSocialSession(FakeSession):
    """Odpovědi všech bezplatných sociálních a zpravodajských zdrojů."""
    def get(self, url, **kw):
        import time as _t
        rss = b"<rss><channel><item><title>Solana and PEPE rally as memecoin season returns</title><link>https://x.y</link></item></channel></rss>"
        if "4cdn.org" in url:
            now = int(_t.time())
            return FakeResp([{"threads": [{"sub": "$WIF to the moon", "com": "buying $WIF and pepe", "replies": 250, "last_modified": now},
                                          {"sub": "old", "com": "$OLD", "replies": 5, "last_modified": now - 99999}]}])
        if "stocktwits" in url:
            return FakeResp({"symbols": [{"symbol": "NVDA"}, {"symbol": "DOGE.X"}]})
        if "t.me/s/" in url:
            r = FakeResp(None)
            r.text = f'<div class="tgme_widget_message_text js-message_text" dir="auto">🚨 Binance will list <b>$BONK</b> perpetual ({url[-12:]})</div>'
            return r
        if "algolia" in url:
            return FakeResp({"hits": [{"title": "Bitcoin ETF flows hit record", "url": "https://hn"}]})
        if "news.google.com" in url or "coindesk" in url or "cointelegraph.com/rss" in url or "decrypt.co" in url:
            r = FakeResp(None); r.content = rss.replace(b"returns", url.split("/")[2].encode()); return r
        if "alternative.me" in url:
            return FakeResp({"data": [{"value": "72", "value_classification": "Greed"}]})
        if "gdeltproject" in url:
            return FakeResp({"articles": [{"title": "Fed signals rate cut, bitcoin jumps", "domain": "reuters.com", "url": "https://r"}]})
        return super().get(url, **kw)


def test_social_sources():
    from colony.trends import TrendRadar
    r = TrendRadar(session=FakeSocialSession())
    r.maybe_refresh(force=True)
    for sym in ("WIF", "NVDA", "DOGE", "BONK", "PEPE", "SOL", "BTC"):
        assert r.score(sym) > 0, (sym, r.errors)
    assert "OLD" not in r.hot                                   # staré vlákno se nepočítá
    assert r.fear_greed["value"] == 72
    srcs = {w["source"] for w in r.world}
    assert {"Binance oznámení", "Hacker News", "Google News", "reuters.com"} <= srcs, srcs
    snap = r.snapshot()
    assert snap["sources"].get("4chan /biz/") and snap["sources"].get("StockTwits trending")
    assert not any(k.startswith(("4chan", "stocktwits", "telegram", "hackernews", "google_news", "news", "gdelt", "fear")) for k in r.errors), r.errors


def test_trend_radar():
    from colony.trends import TrendRadar
    r = TrendRadar(session=FakeSession())
    r.maybe_refresh(force=True)
    assert r.score("PEPE/USD") > r.score("DOGE/USD") > 0          # PEPE: CoinGecko #1 + GeckoTerminal
    assert set(r.hot["PEPE"]["sources"]) == {"CoinGecko trending", "GeckoTerminal trending"}
    assert {"AbC123", "Zz9", "Bst1"} <= r.tokens and "0x1" not in r.tokens
    assert r.snapshot()["metas"][0]["name"] == "AI agenti"


def test_radar_pauses_blocked_source():
    """Zdroj, který vrací 429/403, se hodinu (resp. 6 h) nezkouší; ostatní zdroje běží dál."""
    import requests
    from colony.trends import TrendRadar

    class Blocked(FakeSession):
        calls = 0
        def get(self, url, **kw):
            if "stocktwits" in url:
                Blocked.calls += 1
                resp = requests.Response(); resp.status_code = 403
                raise requests.HTTPError("403 Forbidden", response=resp)
            return super().get(url, **kw)

    r = TrendRadar(session=Blocked())
    r.maybe_refresh(force=True)
    assert Blocked.calls == 1 and "stocktwits" in r._paused
    r.maybe_refresh(force=True)
    assert Blocked.calls == 1, "pozastavený zdroj se nemá zkoušet znovu"
    assert r.errors["stocktwits"].startswith("pauza do") and r.score("PEPE/USD") > 0
    r._paused["stocktwits"] = 0                       # pauza vypršela -> zkusí znovu
    r.maybe_refresh(force=True)
    assert Blocked.calls == 2
    assert "gdelt" not in r._paused                   # obyčejná chyba (offline) pauzu nespouští


def test_meme_prefers_hyped_coin():
    class R:
        def score(self, s): return 5.0 if s.startswith("PEPE") else 0.0
    m = MemeAgent()
    assert m.universe({"crypto_memes": ["DOGE/USD", "PEPE/USD"], "radar": R()})[0] == "PEPE/USD"
    df = bars(400, pd.Timestamp("2026-01-01", tz="UTC"), 5, seed=2, spikes=[(399, 2.5)])
    top = df["high"].iloc[375:399].max() * 1.003
    df.iloc[399, :4] = [df["close"].iloc[398], top * 1.001, df["close"].iloc[398], top]
    p = m.prepare(df)
    assert m.entry(p, 399, {"symbol": "DOGE/USD", "radar": R()}) is None       # 2.5x objem nestačí
    e = m.entry(p, 399, {"symbol": "PEPE/USD", "radar": R()})                  # coin v trendu stačí 2x
    assert e and "trendu" in e.reason


def test_wave_swing():
    start = pd.Timestamp("2026-01-01", tz="UTC")
    df = bars(24 * 30, start, 60, drift=0.0015, vol=0.008, seed=11)
    v = WaveAgent(); v.wallet.born = start.isoformat()
    replay(v, {"SOL/USD": df}, Executor(SimBroker()))
    tr = v.wallet.trades
    assert tr, "vlna by v rostoucím trhu měla obchodovat"
    held = [(pd.Timestamp(t["closed"]) - pd.Timestamp(t["opened"])).total_seconds() / 86400 for t in tr]
    assert max(held) <= 7.05


def test_dashboard_requires_password():
    import base64, urllib.request, urllib.error
    from colony import dashboard
    os.environ["DASHBOARD_PASSWORD"] = "heslo-test"
    srv = dashboard.serve(port=18765)
    dashboard.publish({"bots": [], "x": np.bool_(True)})
    def get(pw):
        req = urllib.request.Request("http://127.0.0.1:18765/api/state")
        if pw:
            req.add_header("Authorization", "Basic " + base64.b64encode(f"a:{pw}".encode()).decode())
        try:
            r = urllib.request.urlopen(req); return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""
    assert get(None)[0] == 401 and get("spatne")[0] == 401
    code, body = get("heslo-test")
    assert code == 200 and b'"x":true' in body
    srv.shutdown()


class FakeGrokSession:
    """xAI Responses API: vrátí JSON s odhady a údaje o spotřebě."""
    def __init__(self, reply):
        self.reply, self.calls, self.headers = reply, [], {}
    def post(self, url, json=None, **kw):
        self.calls.append(json)
        body = {"output": [{"type": "custom_tool_call", "name": "x_keyword_search"},
                           {"type": "message", "content": [{"type": "output_text", "text": self.reply}]}],
                "usage": {"input_tokens": 20000, "output_tokens": 500,
                          "server_side_tool_usage_details": {"x_posts_fetched": 40, "x_users_fetched": 0}}}
        r = FakeResp(body); r.status_code = 200; r.text = "ok"
        return r


def test_grok_budget_and_parsing():
    from colony.grok import Grok
    os.environ["XAI_API_KEY"] = "test"; os.environ["XAI_DAILY_BUDGET"] = "1.0"
    g = Grok(session=FakeGrokSession('Here you go: ```json\n{"hot":[{"symbol":"PEPE","buzz":8,"sentiment":0.7}]}\n```'))
    g.spent = 0; g.day = ""
    t = g.ask("x")
    assert "PEPE" in t
    cost = 20000 * 2e-6 + 500 * 6e-6 + 40 * 0.005
    assert abs(g.spent - cost) < 1e-9
    assert g.s.calls[0]["tools"][0]["type"] == "x_search"
    while g.remaining() >= 0.30:
        g.ask("x")
    n = len(g.s.calls)
    assert g.ask("x") is None and len(g.s.calls) == n         # strop drží - další dotaz se neodešle
    assert g.spent <= 1.0 + cost
    os.environ.pop("XAI_API_KEY")


def test_grok_bad_key_pauses():
    from colony.grok import Grok
    class BadKey:
        def __init__(self): self.n = 0
        def post(self, url, json=None, **kw):
            self.n += 1
            r = FakeResp({}); r.status_code = 400
            r.text = '{"code":"invalid-argument","error":"Incorrect API key provided."}'
            return r
    os.environ["XAI_API_KEY"] = "spatny"
    g = Grok(session=BadKey()); g.spent = 0; g.day = ""
    assert g.ask("x") is None and g.s.n == 1
    assert g.ask("x") is None and g.s.n == 1                  # během pauzy se dotaz neodešle
    g.paused_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    g.ask("x"); assert g.s.n == 2                             # po pauze to zkusí znovu
    os.environ.pop("XAI_API_KEY")


class FakeGamma:
    headers = {}
    def __init__(self):
        self.closed = False
    def get(self, url, params=None, **kw):
        end = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat().replace("+00:00", "Z")
        def m(i, q, p, slug="x"):
            prices = ["1", "0"] if self.closed else [str(p), str(round(1 - p, 3))]
            return {"id": i, "question": q, "slug": slug, "outcomes": '["Yes", "No"]', "outcomePrices": json.dumps(prices),
                    "bestBid": p - 0.01, "bestAsk": p + 0.01, "endDate": end, "closed": self.closed,
                    "liquidity": "50000", "volume24hr": 90000, "acceptingOrders": True}
        if url.endswith("/markets"):
            return FakeResp([m(1, "Will the Fed cut rates in October?", 0.40), m(2, "Blues vs. Senators", 0.5, "nhl-x"),
                             m(3, "Will BTC hit $200k by Friday?", 0.10)])
        return FakeResp(m(int(url.rsplit("/", 1)[1]), "Will the Fed cut rates in October?", 0.40))


def test_oracle_bets_and_resolves():
    from colony.agents.oracle import OracleAgent
    class G:
        enabled = True
        def ask(self, *a, **k):
            return '{"markets":[{"id":"1","p_yes":0.62,"confidence":"medium","why":"inflation cooled"},' \
                   '{"id":"3","p_yes":0.11,"confidence":"high","why":"too far"}]}'
    gamma = FakeGamma()
    o = OracleAgent(G(), session=gamma)
    msgs = []
    now = datetime.now(timezone.utc)
    o.tick(now, msgs.append)
    assert len(o.wallet.lots) == 1, msgs                       # jen trh 1 má hranu >= 8 b.; sport přeskočen
    lot = next(iter(o.wallet.lots.values()))
    assert lot.meta["side"] == "YES" and abs(lot.entry - 0.41) < 1e-9
    stake = lot.cost
    assert 10 <= stake <= 0.10 * 1000 * 1.02                   # čtvrtinový Kelly, max 10 %
    gamma.closed = True                                        # trh vyhodnocen YES
    o.tick(now + timedelta(minutes=10), msgs.append)
    assert not o.wallet.lots and o.wallet.trades[0]["pnl"] > 0
    assert any("Věštec" in m for m in msgs)


def test_lab_params_apply():
    t = TrendAgent().apply({"mode": "cross", "minutes": 15, "top_n": 2})
    assert t.mode == "cross" and t.minutes == 15
    assert t.universe({"crypto_majors": ["ETH/USD", "BTC/USD", "SOL/USD"]}) == ["BTC/USD", "ETH/USD"]
    o = OrbitAgent().apply({"or_bars": 6, "last_entry": "14:00"})
    ts = pd.Timestamp("2026-03-02 13:30", tz="America/New_York")
    assert o.can_enter(ts) and not o.can_enter(pd.Timestamp("2026-03-02 09:55", tz="America/New_York"))


def test_stop_file():
    import tempfile
    from colony import config
    d = tempfile.mkdtemp()
    f = os.path.join(d, "VYPNUTO")
    assert not config.is_stopped(f)
    open(f, "w").write("x")
    assert config.is_stopped(f)


def _bars4h(closes):
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="4h", tz="UTC")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c * 1.005, "low": c * 0.995, "close": c, "volume": 1.0})


def test_captain_slow_trend():
    rng = np.random.default_rng(1)
    n = 6 * 150
    a = CaptainAgent()
    # pomalý růst s šumem -> koupí, stop 6-25 %, velikost podle kolísavosti
    up = _bars4h(100 * np.exp(np.cumsum(0.002 + rng.normal(0, 0.01, n))))
    df = a.prepare(up)
    e = a.entry(df, len(df) - 1, {})
    assert e and 0.06 <= (1 - e.stop / df["close"].iloc[-1]) <= 0.25 and e.trail_pct
    assert a.exit_signal(df, len(df) - 1, None) is None
    # krátká historie -> nic nedělá (96denní průměr ještě není)
    assert a.entry(a.prepare(up.iloc[:300]), 299, {}) is None
    # dlouhý pokles -> nekoupí, držená pozice se prodá
    down = _bars4h(100 * np.exp(np.cumsum(-0.002 + rng.normal(0, 0.01, n))))
    dd = a.prepare(down)
    assert a.entry(dd, len(dd) - 1, {}) is None
    assert a.exit_signal(dd, len(dd) - 1, None)
    # klidnější coin dostane větší sázku než divoký
    a.wallet = Wallet("Kapitán", "crypto")
    assert a.size_usd(100, 100 * (1 - a.stop_pct(0.02))) > a.size_usd(100, 100 * (1 - a.stop_pct(0.05)))


def test_fee_filter():
    t = TrendAgent()                          # krypto: poplatky+skluz 0,6 % tam i zpět
    assert not t.fees_ok(100, 98.5)           # stop 1,5 % -> poplatky by sežraly zisk
    assert t.fees_ok(100, 97)                 # stop 3 % -> ok
    assert t.fees_ok(100, 103)                # short (stop nad cenou) taky
    assert OrbitAgent().fees_ok(100, 99.8)    # akcie bez poplatku -> nefiltruje
    assert MemeAgent().fees_ok(100, 96)       # Memec se stopem 4 % pořád obchoduje


def test_memory_import_once():
    from colony.memory import Memory
    d = tempfile.mkdtemp()
    old = os.path.join(d, "stara.json")
    json.dump({"bots": {"Lovec": [{"f": {"a": "1"}, "r": -0.1, "t": "x", "w": 1.0}]}, "seen": {"Lovec": 17}}, open(old, "w"))
    m = Memory(os.path.join(d, "memory.json"))
    m.record("Lovec", {"a": "2"}, 0.05)
    assert m.import_once(old) == 1
    assert [r["f"]["a"] for r in m.data["bots"]["Lovec"]] == ["1", "2"]     # staré před novými
    assert m.data["seen"] == {}                                              # počítadla se nepřebírají
    assert Memory(os.path.join(d, "memory.json")).import_once(old) == 0     # podruhé už ne
    assert m.import_once(os.path.join(d, "neni.json")) == 0


def test_memec_long_only_when_lab_recommends():
    import numpy as np
    idx = pd.date_range("2026-01-01", periods=400, freq="5min", tz="UTC")
    base = 1.0 + 0.01 * np.sin(np.arange(400))   # drobné kolísání, ať RSI není extrém
    df = pd.DataFrame({"open": base, "high": base * 1.001, "low": base * 0.999, "close": base,
                       "volume": np.full(400, 100.0)}, index=idx)
    up = df.copy()                                 # výbuch objemu + nové 2h maximum -> nákup
    up.iloc[-1] = [1.0, 1.03, 1.0, 1.025, 2000.0]
    dn = df.copy()                                 # výplach -> short
    dn.iloc[-1] = [1.0, 1.0, 0.97, 0.975, 2000.0]
    m = MemeAgent()
    pu, pd_ = m.prepare(up), m.prepare(dn)
    assert m.entry(pu, len(pu) - 1, {}) is not None
    m.lab_warn = True                              # laboratoř longy nedoporučuje
    assert m.entry(pu, len(pu) - 1, {}) is None
    assert m.short_entry(pd_, len(pd_) - 1, {}) is not None   # shorty dál
    assert TrendAgent().lab_warn is False          # ostatní boti beze změny


def test_memec_ignores_dead_market_volume():
    import numpy as np
    idx = pd.date_range("2026-01-01", periods=400, freq="5min", tz="UTC")
    base = 1.0 + 0.01 * np.sin(np.arange(400))
    df = pd.DataFrame({"open": base, "high": base * 1.001, "low": base * 0.999, "close": base,
                       "volume": np.full(400, 100.0)}, index=idx)
    dn = df.copy()
    dn.iloc[-1] = [1.0, 1.0, 0.97, 0.975, 2000.0]          # 20x průměru -> short
    m = MemeAgent()
    assert m.short_entry(m.prepare(dn), 399, {}) is not None
    dead = dn.copy()
    dead["volume"] = 0.01                                   # skoro nikdo neobchoduje
    dead.iloc[-1, 4] = 2000.0                               # jeden obchod = 200 000x "průměru"
    assert m.short_entry(m.prepare(dead), 399, {}) is None
    up = dead.copy(); up.iloc[-1] = [1.0, 1.03, 1.0, 1.025, 2000.0]
    assert m.entry(m.prepare(up), 399, {}) is None          # ani nákup


def test_wave_max_same_side():
    from colony.wallet import Lot
    v = WaveAgent()
    def lot(sym, short):
        return Lot(sym, 1.0, 100.0, 90.0, None, None, "2026-11-05T00:00:00+00:00", None,
                   meta={"side": "short", "p0": 100.0} if short else {})
    v.wallet.lots = {"AVAX/USD": lot("AVAX/USD", False)}
    assert not v.side_full(False)                  # 1 long -> další long smí
    v.wallet.lots["ADA/USD"] = lot("ADA/USD", False)
    assert v.side_full(False)                      # 2 longy -> třetí ne
    assert not v.side_full(True)                   # short (opačný směr) smí
    v.wallet.lots["SOL/USD"] = lot("SOL/USD", True)
    assert not v.side_full(True)
    t = TrendAgent(); t.wallet.lots = dict(v.wallet.lots)
    assert not t.side_full(False)                  # ostatní boti bez limitu


def test_stav_shows_grok_status():
    import io, contextlib, stav
    st = {"started": "2026-11-02T00:00", "wallets": {}, "events": [],
          "grok": {"enabled": False, "last_error": "", "paused_until": None}}
    path = os.path.join(config.STATE_DIR, "colony.json")
    old = open(path).read() if os.path.exists(path) else None
    json.dump(st, open(path, "w"))
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            stav.main()
        assert "chybí XAI_API_KEY" in buf.getvalue()
    finally:
        if old is None:
            os.remove(path)
        else:
            open(path, "w").write(old)


def test_stav_grok_line_after_oracle_retired():
    """22. 11.: Věštec je na hřbitově, hlášení už nemá tvrdit, že Věštec čeká na odhady.
    Grok ale dál slouží radaru trendů, takže chyba klíče se má ukázat pod jménem radaru."""
    import io, contextlib, stav
    st = {"started": "2026-11-02T00:00", "wallets": {}, "events": [],
          "oracle": {"estimates": []},
          "grok": {"enabled": True, "last_error": "HTTP 400", "paused_until": "2026-11-22T06:41"}}
    path = os.path.join(config.STATE_DIR, "colony.json")
    old = open(path).read() if os.path.exists(path) else None
    json.dump(st, open(path, "w"))
    try:
        assert "Věštec" in config.RETIRED
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            stav.main()
        out = buf.getvalue()
        assert "VĚŠTEC" not in out and "GROK (radar trendů): Grok odmítl klíč" in out, out
    finally:
        if old is None:
            os.remove(path)
        else:
            open(path, "w").write(old)


def test_lab_verdict_says_where_it_failed():
    from lab import lab_verdict
    assert lab_verdict(0.05, 0.02) == (True, "NASAZEN ✅")
    ok, txt = lab_verdict(-0.015, 0.089)             # Medvěd 13. 11.
    assert not ok and "v tréninku neprošel" in txt
    ok, txt = lab_verdict(0.2, -0.07)                # Vlna 13. 11.
    assert not ok and "v testu neprošel" in txt
    ok, txt = lab_verdict(-0.09, -0.09)              # Memec 13. 11.
    assert not ok and "i v testu" in txt


def test_wave_no_long_on_bounce_in_downtrend():
    """18. 11.: všech 25 longových signálů Vlny v klesajícím listopadu prodělalo. Krátký odraz
    (průraz 3denního maxima) pod EMA 200 už není vlna - long jen když je coin i v delším trendu."""
    rng = np.random.default_rng(0)
    n1, n2 = 24 * 12, 72                                     # 12 dní pád, pak 3 dny odraz
    t = np.r_[-0.0012 * np.ones(n1), 0.0012 * np.ones(n2)]
    c = 100 * np.exp(np.cumsum(t + rng.normal(0, 0.006, n1 + n2)))
    df = pd.DataFrame({"open": np.r_[c[0], c[:-1]], "close": c},
                      index=pd.date_range("2026-01-01", periods=n1 + n2, freq="60min", tz="UTC"))
    df["high"], df["low"], df["volume"] = np.maximum(df.open, df.close) * 1.002, np.minimum(df.open, df.close) * 0.998, 1000.0
    v = WaveAgent(); p = v.prepare(df)
    v.long_trend_filter = False
    hits = [i for i in range(n1, n1 + n2) if v.entry(p, i, {})]
    assert hits, "bez filtru by Vlna odraz koupila"
    v.long_trend_filter = True
    assert all(v.entry(p, i, {}) is None for i in hits if p["close"].iloc[i] < p["ema200"].iloc[i])
    assert any(p["close"].iloc[i] < p["ema200"].iloc[i] for i in hits)


def test_multiyear_periods_and_honest_walk_forward():
    """21. 11.: Jakub chce boty prověřit na letech historie. Výběr nastavení musí vidět jen minulost,
    jeden zázračný kvartál nesmí přebít stabilní variantu a čtvrtletí bez obchodu je neutrální."""
    from colony import multiyear as my
    end = datetime(2026, 11, 21, tzinfo=timezone.utc)
    ps = my.periods(end, 5)
    assert len(ps) == 20 and ps[-1][1] == end and all(a[1] == b[0] for a, b in zip(ps, ps[1:]))
    R = lambda r, t=5: {"return": r, "trades": t}
    # varianta 0: jeden kvartál +80 %, jinak ztráty; varianta 1: stabilně malý zisk
    rows = [[R(0.8 if q == 0 else -0.03), R(0.02)] for q in range(8)]
    assert my.pick_robust(rows) == 1
    # režim se mění: do q=6 vydělává varianta 0, pak varianta 1 -> walk-forward se to dozví až zpětně
    rows = [[R(0.05), R(-0.05)] for _ in range(6)] + [[R(-0.05), R(0.05)] for _ in range(2)]
    oos = my.walk_forward(rows, min_history=4)
    assert [o["q"] for o in oos] == [4, 5, 6, 7] and oos[2]["variant"] == 0 and oos[2]["return"] == -0.05
    ok, txt, tot = my.verdict(oos)
    assert ok is False and "NEPROŠEL" in txt                    # 2 plusy ze 4, ale celkem mínus
    ok, txt, tot = my.verdict([{"return": 0.03, "trades": 4}, {"return": 0.0, "trades": 0},
                               {"return": -0.01, "trades": 2}])
    assert ok and abs(tot - (1.03 * 0.99 - 1)) < 1e-9       # kvartál bez obchodu nevadí
    assert not my.verdict([{"return": 0.0, "trades": 0}] * 3)[0]


def test_multiyear_step_resumes_in_chunks_and_feeds_lab():
    """Server má 1 GB: test běží po kouscích (bot × čtvrtletí), postup se ukládá a příště pokračuje.
    Výsledek pak určí nastavení v lab.json (prošel -> nejodolnější nastavení, neprošel -> varování)."""
    from colony import multiyear as my
    d = tempfile.mkdtemp()
    variants = {"A": (object, [{"x": 1}, {"x": 2}]), "B": (object, [{"x": 1}])}
    calls = []

    def fetch(cls, params, s, e, cache):
        cache.setdefault("k", 0); cache["k"] += 1
        if s.year == 2021 and params["x"] == 1:          # první kvartál začíná v prosinci 2021
            raise ValueError("výpadek")
        return {"s": s}

    def run_one(cls, params, data, s, e):
        calls.append(1)
        return {"return": 0.02 if params["x"] == 2 else -0.01, "trades": 5}

    t = [0.0]
    clock = lambda: t[0]
    def run_counting(*a):
        t[0] += 1.0
        return run_one(*a)
    now = datetime(2026, 11, 21, tzinfo=timezone.utc)
    st = my.step(variants, fetch, run_counting, d, budget_s=5, now=now, clock=clock)
    assert not st.get("finished") and 0 < sum(len(r) for r in st["results"].values()) < 40
    assert "probíhá" in my.report_lines(st)[0]
    bots = {"A": {"chosen": {"x": 1}, "benched": False}}
    assert my.apply_to_lab(bots, st)["A"]["chosen"] == {"x": 1}  # nehotový test nic nemění
    for _ in range(30):
        st = my.step(variants, fetch, run_counting, d, budget_s=5, now=now, clock=clock)
        if st.get("finished"):
            break
    assert st["finished"] and sum(len(r) for r in st["results"].values()) == 40
    assert st["errors"] and st["results"]["A"]["0"][0] is None   # výpadek dat = chybějící kvartál, ne nula
    sm = st["summary"]
    assert sm["A"]["passed"] and sm["A"]["chosen"] == {"x": 2} and not sm["B"]["passed"]
    bots = {"A": {"chosen": {"x": 1}, "benched": False}, "B": {"chosen": {"x": 1}, "benched": False}}
    my.apply_to_lab(bots, st)
    assert bots["A"]["chosen"] == {"x": 2} and not bots["A"]["benched"] and bots["A"]["multiyear"]["passed"]
    assert bots["B"]["benched"]
    txt = "\n".join(my.report_lines(st))
    assert "A" in txt and "PROŠEL" in txt and "NEPROŠEL" in txt
    # hotový test se přepočítá až po týdnu nebo při změně variant
    n = len(calls)
    my.step(variants, fetch, run_counting, d, now=now + timedelta(days=3), clock=clock)
    assert len(calls) == n
    assert my.needs_new_cycle(st, now + timedelta(days=8), variants)
    assert my.needs_new_cycle(st, now, {"A": (object, [{"x": 3}])})


def test_lab_multi_year_runs_real_bot_on_fake_history():
    """Propojení laboratoře s víceletým testem na skutečném kódu Vlny (data jsou vymyšlená)."""
    import lab
    from colony import multiyear as my

    class FakeData:
        def bars(self, syms, minutes, start, end, crypto=False):
            idx = pd.date_range(start, end, freq=f"{minutes}min", tz="UTC")[:-1]
            out = {}
            for k, sym in enumerate(syms[:3]):
                rng = np.random.default_rng(k + idx[0].dayofyear)
                c = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, len(idx))))
                df = pd.DataFrame({"open": np.r_[c[0], c[:-1]], "close": c}, index=idx)
                df["high"], df["low"] = df[["open", "close"]].max(axis=1) * 1.003, df[["open", "close"]].min(axis=1) * 0.997
                df["volume"] = rng.uniform(500, 1500, len(idx))
                out[sym] = df
            return out

    old_v, old_dir, old_years = lab.VARIANTS, config.STATE_DIR, my.YEARS
    try:
        lab.VARIANTS = {"Zkouška": (WaveAgent, [{"atr_mult": 3.0}, {"atr_mult": 2.5}])}   # kód Vlny, nevyřazené jméno
        config.STATE_DIR = tempfile.mkdtemp()
        my.YEARS = 0.5                                             # 2 čtvrtletí, ať test netrvá dlouho
        st = lab.multi_year(FakeData(), ["BTC/USD", "ETH/USD", "SOL/USD"], [], budget_s=600)
        assert st and st.get("finished") and not st.get("errors"), st.get("errors")
        rows = st["results"]["Zkouška"]
        assert len(rows) == 2 and all(r is not None and "return" in r for row in rows.values() for r in row)
        assert "Zkouška" in st["summary"] and my.report_lines(st)[1].startswith("Zkouška")
    finally:
        lab.VARIANTS, config.STATE_DIR, my.YEARS = old_v, old_dir, old_years


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("OK", name)
