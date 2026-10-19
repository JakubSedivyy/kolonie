"""Testy bez internetu: syntetická data, falešná Alpaka a DexScreener.
Spuštění:  python -m pytest -q tests   (nebo python tests/test_colony.py)"""
import os, sys, tempfile, types, json
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
from colony.agents.strategies import OrbitAgent, TrendAgent, MemeAgent, WaveAgent, WhisperAgent, BearAgent
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
    c.radar.maybe_refresh = lambda force=False: False
    c.oracle.tick = lambda *a, **k: None
    assert not c.grok.enabled
    c.tg.send = sent.append
    c.exec.notify = sent.append
    c.tick()
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
    assert os.path.exists(c.state_path)
    # snapshot pro dashboard: čisté JSON hodnoty (žádné numpy "False" jako text)
    import json
    from colony.dashboard import _jsonable
    snap = json.loads(json.dumps(c.snapshot(now), default=_jsonable))
    assert len(snap["bots"]) == 10
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
    assert [w.name for w in c2.wallets] == ["Orbit", "Trendy", "Memec", "Vlna", "Šeptač", "Medvěd", "Degen II", "Lovec", "Stín", "Věštec", "Degen"]
    assert abs(sum(w.equity() for w in c2.wallets) - sum(w.equity() for w in c.wallets) - 796.78) < 1e-6
    snap2 = c2.snapshot(datetime.now(timezone.utc))
    ghost = [b for b in snap2["bots"] if b["name"] == "Degen"][0]
    assert ghost["status"] == "DEAD" and "lessons" in ghost
    print("\n".join(sent[:15]))


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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("OK", name)
