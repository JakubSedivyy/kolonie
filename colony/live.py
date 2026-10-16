"""Živá kolonie: smyčka, která každých 30 s projde trh, nechá boty jednat a hlídá jejich přežití."""
from __future__ import annotations
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from . import config, survival, report
from .agents.base import Executor, BarAgent
from .agents.strategies import OrbitAgent, TrendAgent, MemeAgent, WaveAgent, WhisperAgent, BearAgent
from .agents.degen import DegenAgent, HunterAgent
from .agents.shadow import ShadowAgent
from .broker import AlpacaBroker
from .indicators import ema
from .market import AlpacaData, AlpacaAssets, DexScreener
from .notify import Telegram
from .trends import TrendRadar
from .grok import Grok
from .agents.oracle import OracleAgent
from . import dashboard
from .wallet import Wallet, utcnow, is_short
from .memory import Memory, hour_bucket
from .kronika import Kronika

log = logging.getLogger("colony")
PRAGUE = ZoneInfo("Europe/Prague")
MOOD_MULT = {"risk_on": 1.0, "neutral": 0.7, "risk_off": 0.4}


class Colony:
    def __init__(self):
        os.makedirs(config.STATE_DIR, exist_ok=True)
        self.state_path = os.path.join(config.STATE_DIR, "colony.json")
        self.tg = Telegram()
        self.events: list = []                       # posledních 150 událostí pro dashboard
        _raw_send = self.tg.send

        def send(msg):
            self.events.append((utcnow().isoformat(timespec="seconds"), msg))
            del self.events[:-150]
            _raw_send(msg)
        self.tg.send = send
        self.grok = Grok()
        self.radar = TrendRadar(grok=self.grok)
        self.data = AlpacaData()
        self.broker = AlpacaBroker()
        self.assets = AlpacaAssets(self.broker.tc)
        self.dex = DexScreener()
        self.exec = Executor(self.broker, self.tg.send)
        self.bar_agents: list[BarAgent] = [OrbitAgent(), TrendAgent(), MemeAgent(), WaveAgent(), WhisperAgent(), BearAgent()]
        self.degen = DegenAgent(self.dex, radar=self.radar)
        self.lovec = HunterAgent(self.dex, radar=self.radar)
        self.stin = ShadowAgent(self.data)
        self.oracle = OracleAgent(self.grok)
        self.lab, self._lab_mtime, self._lab_checked = {}, 0.0, 0.0
        self.memory = Memory(os.path.join(config.STATE_DIR, "memory.json"))
        self.kronika = Kronika(os.path.join(config.STATE_DIR, "kronika.db"))
        self._kronika_hour = ""
        self._skip_told: set = set()
        self.graveyard: list[Wallet] = []          # mrtví boti, kteří už nemají agenta (např. původní Degen)
        for ag in (self.degen, self.lovec, self.stin):
            ag.memory = self.memory
            ag.kronika = self.kronika
        self.mood, self.mood_at = "neutral", None
        self.ctx = {"radar": self.radar}
        self.last_bar: dict[str, str] = {}
        self.blacklist: dict[str, int] = {}        # sdílené: symbol -> počet stop-lossů dnes
        self.blacklist_day = ""
        self.paused = False
        self.started = utcnow().isoformat()
        self.last_summary = ""
        self.eval_sent = False
        self._clock, self._clock_at = None, 0
        self.load()

    # ------------------------------------------------------------------ stav
    @property
    def wallets(self) -> list[Wallet]:
        return [a.wallet for a in self.bar_agents] + [self.degen.wallet, self.lovec.wallet, self.stin.wallet, self.oracle.wallet] + self.graveyard

    def save(self):
        st = {"wallets": {w.name: w.to_dict() for w in self.wallets}, "last_bar": self.last_bar,
              "orbit_traded": self.bar_agents[0].traded_today, "degen_blacklist": sorted(self.degen.blacklist),
              "lovec_blacklist": sorted(self.lovec.blacklist),
              "oracle": {"last_scan": self.oracle.last_scan, "evaluated": self.oracle.evaluated,
                         "estimates": self.oracle.last_estimates},
              "paused": self.paused, "started": self.started, "last_summary": self.last_summary,
              "eval_sent": self.eval_sent, "events": self.events[-150:]}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f)
        os.replace(tmp, self.state_path)

    def load(self):
        if not os.path.exists(self.state_path):
            return
        st = json.load(open(self.state_path))
        names = set()
        for a in self.bar_agents + [self.degen, self.lovec, self.stin, self.oracle]:
            names.add(a.name)
            if a.name in st["wallets"]:
                a.wallet = Wallet.from_dict(st["wallets"][a.name])
        self.graveyard = [Wallet.from_dict(d) for n, d in st["wallets"].items()
                          if n not in names and d.get("status") == "DEAD"]
        self.last_bar = st.get("last_bar", {})
        self.bar_agents[0].traded_today = st.get("orbit_traded", {})
        self.degen.blacklist = set(st.get("degen_blacklist", []))
        self.lovec.blacklist = set(st.get("lovec_blacklist", []))
        o = st.get("oracle", {})
        self.oracle.last_scan, self.oracle.evaluated = o.get("last_scan", ""), o.get("evaluated", {})
        self.oracle.last_estimates = o.get("estimates", [])
        self.paused = st.get("paused", False)
        self.started = st.get("started", self.started)
        self.last_summary = st.get("last_summary", "")
        self.eval_sent = st.get("eval_sent", False)
        self.events = [tuple(e) for e in st.get("events", [])]

    # ------------------------------------------------------------------ trh
    def market_open(self):
        if time.time() - self._clock_at > 60:
            try:
                self._clock = self.broker.clock()
                self._clock_at = time.time()
            except Exception as e:
                log.warning("clock: %s", e)
        return bool(self._clock and self._clock.is_open)

    def update_mood(self, now):
        """Společná nálada trhu pro všechny boty (spolupráce): BTC a S&P 500 v trendu?"""
        if self.mood_at and now - self.mood_at < timedelta(hours=1):
            return
        score, parts = 0, []
        try:
            btc = self.data.bars(["BTC/USD"], 60, now - timedelta(days=12), crypto=True)["BTC/USD"]
            up = btc["close"].iloc[-1] > ema(btc["close"], 50).iloc[-1]
            score += up; parts.append(f"BTC {'↑' if up else '↓'}")
            spy = self.data.bars(["SPY"], 60 * 24, now - timedelta(days=45))["SPY"]
            up = spy["close"].iloc[-1] > spy["close"].tail(20).mean()
            score += up; parts.append(f"S&P {'↑' if up else '↓'}")
        except Exception as e:
            log.warning("mood: %s", e)
            return
        new = {2: "risk_on", 1: "neutral", 0: "risk_off"}[score]
        if new != self.mood:
            self.tg.send(f"🌡️ Nálada trhu: {self.mood} → {new} ({', '.join(parts)})")
        self.mood, self.mood_at = new, now

    def refresh_universe(self, now):
        day = now.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        if self.ctx.get("crypto_day") != day:
            try:
                self.ctx["crypto_majors"] = self.assets.majors()
                self.ctx["crypto_memes"] = self.assets.memes()
                self.ctx["crypto_day"] = day
                log.info("krypto: %s | meme: %s", self.ctx["crypto_majors"], self.ctx["crypto_memes"])
            except Exception as e:
                log.warning("assets: %s", e)
        ny = now.astimezone(ZoneInfo("America/New_York"))
        if self.ctx.get("movers_day") != day and ny.hour == 9 and ny.minute >= 35 and self.market_open():
            movers = self.data.stock_movers()
            if movers:
                self.ctx["stock_movers"] = movers
                self.ctx["movers_day"] = day
                self.tg.send("🔭 Orbit dnes sleduje: " + ", ".join(self.bar_agents[0].universe(self.ctx)))

    # ------------------------------------------------------------------ boti
    def _close_all(self, agent, why, now):
        prices = self.data.latest_prices(list(agent.wallet.lots), crypto=agent.crypto) if agent.wallet.lots else {}
        for s in list(agent.wallet.lots):
            self.exec.exit(agent, s, prices.get(s, agent.wallet.lots[s].entry), why, now)

    # ------------------------------------------------------------------ paměť (učení z vlastních obchodů)
    def bar_features(self, a, symbol, now) -> dict:
        hype = self.radar.score(symbol) if self.radar else 0.0
        return {"coin": symbol, "denní doba": hour_bucket(now), "nálada trhu": self.mood,
                "o coinu se mluví": "ano" if hype >= 3 else "ne"}

    def _k(self, fn, *args, **kw):
        """Kronika nesmí nikdy shodit obchodování."""
        try:
            return fn(*args, **kw)
        except Exception as e:
            log.warning("kronika: %s", e)

    def chronicle_hour(self, now):
        """Jednou za hodinu: snímek radaru, archiv obchodů a událostí, dopočet stínových obchodů."""
        hour = now.strftime("%Y-%m-%dT%H")
        if self._kronika_hour == hour:
            return
        self._kronika_hour = hour
        self._k(self.kronika.radar_snapshot, now, self.mood, self.radar.snapshot(25))
        self._k(self.kronika.trades, self.wallets)
        self._k(self.kronika.events, self.events)
        learned = 0
        for sgn in (self._k(self.kronika.due_signals, now) or []):
            ret, note = self._k(self.shadow_outcome, sgn) or (None, "chyba")
            self._k(self.kronika.resolve, sgn["id"], ret, note)
            if ret is not None:
                self.memory.record(sgn["bot"], sgn["features"], ret, sgn["t"].isoformat(), weight=0.5)
                learned += 1
        if learned:
            self.memory.save()
            log.info("kronika: %d stínových obchodů dopočítáno a zapsáno do paměti", learned)
        try:
            open(os.path.join(config.STATE_DIR, "kronika.txt"), "w").write(self.kronika.report())
        except Exception as e:
            log.warning("kronika report: %s", e)

    def shadow_outcome(self, sgn):
        """Jak by dopadl nekoupený signál."""
        meta, end = sgn["meta"], sgn["t"] + timedelta(minutes=sgn["horizon_min"])
        if meta.get("pair"):                                   # DEX coin: známe jen cenu teď (po uplynutí doby)
            p = self.dex.pair(meta["pair"])
            if not p:
                return None, "pool nenalezen"
            now_px = float(p.get("priceUsd") or 0)
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if liq < meta.get("liq", 0) * 0.5:
                return -0.9, "rug pull"
            ret = now_px / sgn["price"] - 1
            lo = sgn["stop"] / sgn["price"] - 1 if sgn["stop"] else -1
            hi = sgn["target"] / sgn["price"] - 1 if sgn["target"] else 10
            return max(lo, min(hi, ret)), "cena po uplynutí doby"
        minutes, crypto = meta.get("minutes", 60), meta.get("crypto", True)
        bars = self.data.bars([sgn["symbol"]], minutes, sgn["t"], end, crypto=crypto).get(sgn["symbol"])
        if bars is None or bars.empty:
            return None, "chybí data"
        bars = bars[bars.index > pd.Timestamp(sgn["t"])]
        ret, note = Kronika.simulate(sgn["price"], sgn["stop"], sgn["target"], bars, short=meta.get("short", False))
        cost = 2 * (config.COSTS["short" if meta.get("short") else ("crypto" if crypto else "stock")]["fee"] + 0.0005)
        return (None if ret is None else ret - cost), note

    def _memory_skip(self, bot, symbol, note):
        key = (bot, symbol, utcnow().strftime("%Y-%m-%d %H"))
        if key not in self._skip_told:                 # stejnou věc hlas nejvýš jednou za hodinu
            self._skip_told.add(key)
            log.info("%s nekoupil %s - %s", bot, symbol, note)
            self.events.append((utcnow().isoformat(timespec="seconds"), f"🧠 {bot} nekoupil {symbol} - {note}"))
            del self.events[:-150]

    def run_bar_agent(self, a: BarAgent, now):
        w = a.wallet
        if w.status == "DEAD":
            return
        if not a.crypto and not self.market_open():
            return
        boundary = pd.Timestamp(now).floor(f"{a.minutes}min")
        key = boundary.isoformat()
        if self.last_bar.get(a.name) == key or (now - boundary.to_pydatetime()).total_seconds() < 15:
            return
        self.last_bar[a.name] = key

        syms = list(dict.fromkeys(a.universe(self.ctx) + list(w.lots)))
        start = now - timedelta(minutes=a.minutes * (a.lookback_bars + 5))
        if isinstance(a, OrbitAgent):
            start = now - timedelta(hours=20)        # dnešní seance včetně ranního rozpětí
        try:
            bars = self.data.bars(syms, a.minutes, start, crypto=a.crypto)
        except Exception as e:
            log.warning("%s bars: %s", a.name, e)
            return
        mult = MOOD_MULT[self.mood]
        for s, raw in bars.items():
            raw = raw[raw.index + pd.Timedelta(minutes=a.minutes) <= pd.Timestamp(now)]   # jen zavřené svíčky
            if len(raw) < 3:
                continue
            df = a.prepare(raw)
            if df is None or df.empty:
                continue
            i = len(df) - 1
            r, ts = df.iloc[i], df.index[i]
            if (pd.Timestamp(now) - ts) > pd.Timedelta(minutes=a.minutes * 3):
                continue                                # stará data, nejednat
            price = float(r["close"])
            w.mark({s: price}, now)
            lot = w.lots.get(s)
            if lot:
                if a.must_flatten(ts):
                    self.exec.exit(a, s, price, "konec obchodního dne", now)
                    continue
                why = a.short_exit_signal(df, i, lot) if is_short(lot) else a.exit_signal(df, i, lot)
                if why:
                    self.exec.exit(a, s, price, why, now)
                continue
            if self.paused or a.benched or w.status != "ALIVE":
                continue
            if self.mood == "risk_off" and not a.trades_in_risk_off and not config.RISK_OFF_TRADING:
                continue
            if not a.can_enter(ts):
                continue
            e = a.entry(df, i, {**self.ctx, "symbol": s})
            short = False
            if not (e and e.stop < price):
                e = None
                if a.can_short and self.mood != "risk_on":          # sázka na pokles - ne když trh jasně roste
                    e = a.short_entry(df, i, {**self.ctx, "symbol": s})
                    short = bool(e and e.stop > price)
                if not short:
                    continue
            # bot vidí signál - zapíše se do kroniky, ať ho koupí, nebo ne (u nekoupených se později dopočítá výsledek)
            feats = self.bar_features(a, s, now)
            if short:
                feats["směr"] = "short"
            sig = dict(stop=e.stop, target=e.target, horizon_min=int(e.max_hold_min or a.minutes * 48),
                       reason=e.reason, features=feats, meta={"minutes": a.minutes, "crypto": a.crypto, "short": short},
                       now=now)
            skip, usd, note = "", 0.0, ""
            if len(w.lots) >= a.max_positions:
                skip = "plno pozic"
            elif self.blacklist.get(s, 0) >= 2:
                skip = "dnes už 2x stop-loss"
            elif any(s in o.wallet.lots for o in self.bar_agents if o is not a):
                skip = "coin drží kolega"            # na účtu se pozice nesmí míchat
            else:
                ok, mem_mult, note = self.memory.judge(a.name, feats)
                if not ok:
                    skip = note
                    self._memory_skip(a.name, s, note)
                else:
                    risk_stop = 2 * price - e.stop if short else e.stop       # u shortu je riziko nad cenou
                    usd = a.size_usd(price, risk_stop, mult * mem_mult)
                    if not usd:
                        skip = "málo peněz"
            if skip:
                self._k(self.kronika.signal, a.name, s, price, taken=False, skip_reason=skip, **sig)
                continue
            if note:
                e.reason += f" · {note}"
            lot = self.exec.enter(a, s, price, e, usd, now, meta={"features": feats}, short=short)
            self._k(self.kronika.signal, a.name, s, price, taken=bool(lot), skip_reason="" if lot else "příkaz neprošel", **sig)

        if isinstance(a, OrbitAgent) and w.lots and a.must_flatten(pd.Timestamp(now)):
            self._close_all(a, "konec obchodního dne", now)

    def guard_stops(self, now):
        """Každých 30 s kontrola stop-lossů a cílů podle poslední ceny."""
        for a in self.bar_agents:
            w = a.wallet
            if not w.lots or (not a.crypto and not self.market_open()):
                continue
            prices = self.data.latest_prices(list(w.lots), crypto=a.crypto)
            w.mark(prices, now)
            if w.status == "DEAD":                       # mrtvý bot s neprodanou pozicí -> zkusit znovu
                for s, p in prices.items():
                    self.exec.exit(a, s, p, "smrt bota", now)
                continue
            for s, p in prices.items():
                lot = w.lots.get(s)
                if not lot:
                    continue
                x = a.check_exit_short(lot, p, p, p, p, now) if is_short(lot) else a.check_exit(lot, p, p, p, p, now)
                if x:
                    t = self.exec.exit(a, s, p, x[1], now)
                    if t and x[1] == "stop-loss":
                        self.blacklist[s] = self.blacklist.get(s, 0) + 1
                        if self.blacklist[s] == 2:
                            self.tg.send(f"🚫 {s} dnes 2x vyhodil stop-loss - kolonie ho do zítřka nebude obchodovat")

    def check_survival(self, now):
        agents = self.bar_agents + [self.degen, self.lovec, self.stin, self.oracle]
        for a in agents:
            w = a.wallet
            if w.status == "DEAD":
                continue
            for typ, txt in survival.check(w, now, w.equity()):
                if typ == "DEATH":
                    if isinstance(a, BarAgent):
                        self._close_all(a, "smrt bota", now)
                    else:
                        for s in list(w.lots):
                            w.close(s, w.last_prices.get(s, w.lots[s].entry) * 0.95, "smrt bota", now, apply_slippage=False)
                    survival.kill(w, now, txt)
                    s = w.stats()
                    self.tg.send(f"💀 {w.name} UMŘEL: {txt}\nŽil {(now - datetime.fromisoformat(w.born)).days} dní, "
                                 f"{s['trades']} obchodů, úspěšnost {s['win_rate']*100:.0f} %. Odpočívej v pokoji.")
                elif typ in ("INJURED", "HEALED"):
                    self.tg.send(("🤕 " if typ == "INJURED" else "💪 ") + txt)
        total = sum(w.equity() for w in self.wallets)
        start = sum(w.start for w in self.wallets)
        if not self.paused and total < start * config.COLONY_KILL_SWITCH:
            self.paused = True
            self.tg.send(f"🛑 Celá kolonie je pod {config.COLONY_KILL_SWITCH*100:.0f} % startu (${total:.0f}). "
                         f"Nové obchody zastaveny. Pošli /pokracuj pro obnovení.")

    # ------------------------------------------------------------------ příkazy a reporty
    def handle_commands(self, now):
        for c in self.tg.commands():
            if c in ("/status", "/stav"):
                self.tg.send(report.text_summary(self.wallets, self.mood))
            elif c == "/pauza":
                self.paused = True
                self.tg.send("⏸️ Kolonie pozastavena - žádné nové nákupy, stopy dál hlídám.")
            elif c == "/pokracuj":
                self.paused = False
                self.tg.send("▶️ Kolonie zase obchoduje.")
            elif c == "/prodej_vse":
                self.paused = True
                for a in self.bar_agents:
                    self._close_all(a, "příkaz /prodej_vse", now)
                self.tg.send("🧯 Vše prodáno a kolonie pozastavena.")
            elif c == "/hodnoceni":
                days = (now - datetime.fromisoformat(self.started)).total_seconds() / 86400
                self.tg.send(report.eval_text(self.wallets, days))
            elif c in ("/start", "/help", "/pomoc"):
                self.tg.send("Příkazy: /stav, /pauza, /pokracuj, /prodej_vse, /hodnoceni")

    def daily(self, now):
        local = now.astimezone(PRAGUE)
        day = local.strftime("%Y-%m-%d")
        if self.blacklist_day != day:
            self.blacklist, self.blacklist_day = {}, day
        if local.hour == 22 and local.minute >= 15 and self.last_summary != day:
            self.last_summary = day
            self.tg.send(report.text_summary(self.wallets, self.mood, f"Denní souhrn {local:%d.%m.}"))
        days = (now - datetime.fromisoformat(self.started)).total_seconds() / 86400
        if days >= 10 and not self.eval_sent:
            self.eval_sent = True
            self.tg.send(report.eval_text(self.wallets, days))

    # ------------------------------------------------------------------ smyčka
    def load_lab(self):
        """Nastavení z laboratoře (state/lab.json) - zkontroluje jednou za hodinu, jestli není nové."""
        path = os.path.join(config.STATE_DIR, "lab.json")
        if time.time() - self._lab_checked < 3600 or not os.path.exists(path):
            return
        self._lab_checked = time.time()
        m = os.path.getmtime(path)
        if m == self._lab_mtime:
            return
        self._lab_mtime = m
        try:
            self.lab = json.load(open(path))
        except Exception as e:
            log.warning("lab.json: %s", e)
            return
        msgs = []
        for a in self.bar_agents:
            r = self.lab.get("bots", {}).get(a.name)
            if not r:
                continue
            a.apply(r.get("chosen") or {})
            a.lab_warn = bool(r.get("benched"))
            a.benched = a.lab_warn and (config.IS_LIVE or config.LAB_BENCH_ON_PAPER)
            state = "💤 odpočívá" if a.benched else ("⚠️ obchoduje na papíře, laboratoř ho zatím nedoporučuje" if a.lab_warn else "✅ doporučen")
            msgs.append(f"{a.name}: {state} {r.get('chosen')} "
                        f"(test {r.get('test', {}).get('return', 0)*100:+.1f} %)")
        sig = json.dumps({n: (v.get("chosen"), v.get("benched")) for n, v in (self.lab.get("bots") or {}).items()},
                         sort_keys=True, default=str)
        if msgs and sig != getattr(self, "_lab_sig", None):      # laboratoř běží často - hlas jen změny
            self.tg.send("🧪 Laboratoř: nové nastavení botů\n" + "\n".join(msgs))
        self._lab_sig = sig

    def tick(self):
        now = utcnow()
        self.load_lab()
        self.handle_commands(now)
        self.refresh_universe(now)
        self.update_mood(now)
        try:
            if self.radar.maybe_refresh():
                top = [h["symbol"] for h in self.radar.snapshot(8)["hot"]]
                log.info("radar: %s", top)
        except Exception as e:
            log.warning("radar: %s", e)
        for a in self.bar_agents:
            try:
                self.run_bar_agent(a, now)
            except Exception as e:
                log.exception("%s: %s", a.name, e)
        try:
            self.guard_stops(now)
        except Exception as e:
            log.exception("stops: %s", e)
        entries = int(time.time() // 30) % 4 == 0      # nové nákupy degen/lovec každé ~2 min, hlídání pozic každých 30 s
        for sim in (self.degen, self.lovec):
            if not (entries or sim.wallet.lots):
                continue
            try:
                m = "neutral" if self.mood == "risk_off" and config.RISK_OFF_TRADING else self.mood
                sim.tick(now, "risk_off" if self.paused else m, self.tg.send, allow_entries=entries)
            except Exception as e:
                log.exception("%s: %s", sim.name, e)
        try:                                           # Stín si sám hlídá hodinové svíčky
            uni = list(dict.fromkeys((self.ctx.get("crypto_majors") or []) + (self.ctx.get("crypto_memes") or [])))
            self.stin.tick(now, self.mood, self.tg.send, uni, paused=self.paused)
        except Exception as e:
            log.exception("stín: %s", e)
        if int(time.time() // 30) % 20 == 1:           # Věštec každých ~10 minut
            try:
                self.oracle.tick(now, self.tg.send, paused=self.paused)
            except Exception as e:
                log.exception("oracle: %s", e)
        self.check_survival(now)
        try:
            for msg in self.memory.learn_from(self.wallets):
                self.tg.send(msg)
        except Exception as e:
            log.exception("paměť: %s", e)
        if self.kronika:
            self._k(self.chronicle_hour, now)
        self.daily(now)
        self.save()
        dashboard.publish(self.snapshot(now))

    def snapshot(self, now):
        class _Ghost:                                   # mrtvý bot bez agenta - jen na hřbitov v dashboardu
            def __init__(self, w):
                self.name, self.wallet, self.__doc__ = w.name, w, f"{w.name} - zemřel: {w.cause_of_death}"
        agents = self.bar_agents + [self.degen, self.lovec, self.stin, self.oracle] + [_Ghost(w) for w in self.graveyard]
        bots = []
        for a in agents:
            w = a.wallet
            s = w.stats()
            age = (now - datetime.fromisoformat(w.born)).total_seconds() / 86400
            pos = []
            for sym, l in w.lots.items():
                last = w.last_prices.get(sym, l.entry)
                pos.append({"symbol": sym, "qty": l.qty, "entry": l.entry, "last": last, "stop": l.stop,
                            "target": l.target, "opened": l.opened, "reason": l.reason,
                            "pnl": l.qty * last - l.cost, "pnl_pct": (l.qty * last / l.cost - 1) if l.cost else 0,
                            "url": l.meta.get("url", ""), "question": l.meta.get("question", ""),
                            "side": l.meta.get("side", ""), "p": l.meta.get("p"),
                            "chart_entry": l.meta.get("p0", l.entry),
                            "chart_stop": l.meta.get("stop_px", l.stop) if l.stop else None,
                            "chart_target": l.meta.get("target_px") if l.meta.get("side") == "short" else l.target})
            bots.append({**s, "asset_class": w.asset_class, "desc": (a.__doc__ or "").strip().split("\n")[0],
                         "start": w.start, "cash": w.cash, "born": w.born, "died": w.died, "age_days": age,
                         "days_left": max(0, config.PROFIT_DEADLINE_DAYS - age), "curve": w.equity_curve[-600:],
                         "positions": pos, "simulated": w.asset_class in ("degen", "dexmeme", "short", "predict"),
                         "benched": bool(getattr(a, "benched", False)),
                         "lab_warn": bool(getattr(a, "lab_warn", False)),
                         "waiting": (a is self.oracle and not self.grok.enabled),
                         "symbols": self.chart_symbols(a), "minutes": getattr(a, "minutes", 60),
                         "lessons": self.memory.lessons(a.name),
                         "kronika": self._k(self.kronika.bot_summary, a.name) or {},
                         "bot_trades": [dict(t, bot=w.name) for t in w.trades[-40:]],
                         "profit_factor": None if s["profit_factor"] == float("inf") else s["profit_factor"]})
        trades = sorted((dict(t, bot=w.name) for w in self.wallets for t in w.trades), key=lambda t: t["closed"])
        return {"generated": now.isoformat(timespec="seconds"), "mode": "live" if config.IS_LIVE else "paper",
                "mood": self.mood, "paused": self.paused, "started": self.started,
                "days_running": (now - datetime.fromisoformat(self.started)).total_seconds() / 86400,
                "rules": {"start": config.START_CAPITAL, "death": config.DEATH_EQUITY,
                          "deadline_days": config.PROFIT_DEADLINE_DAYS, "daily_loss": config.DAILY_LOSS_LIMIT,
                          "kill_switch": config.COLONY_KILL_SWITCH},
                "bots": bots, "trades": trades[-150:], "events": self.events[-80:],
                "radar": self.radar.snapshot(), "blacklist": self.blacklist,
                "oracle": {"estimates": self.oracle.last_estimates[-10:], "last_scan": self.oracle.last_scan},
                "lab": {k: self.lab.get(k) for k in ("updated", "train", "test")} | {
                    "bots": {n: {kk: v.get(kk) for kk in ("chosen", "benched", "train", "test")}
                             for n, v in (self.lab.get("bots") or {}).items()}},
                "eval": [{"bot": w.name, "ok": ok, "checks": c} for w in self.wallets
                         for ok, c in [report.evaluate(w, 0)]]}

    # ------------------------------------------------------------------ grafy pro dashboard
    TF = {"5": 5, "15": 15, "60": 60, "1440": 1440}

    def chart_symbols(self, a) -> list[str]:
        """Co má smysl u bota ukázat v grafu: otevřené pozice, poslední obchody, pak jeho trh."""
        w = a.wallet
        syms = list(w.lots) + [t["symbol"] for t in reversed(w.trades[-30:])]
        if isinstance(a, BarAgent):
            try:
                syms += a.universe(self.ctx)[:4]
            except Exception:
                pass
        elif a is self.stin:
            syms += (self.ctx.get("crypto_majors") or ["BTC/USD", "ETH/USD"])[:3]
        return list(dict.fromkeys(syms))[:10]

    def _dex_pair(self, a, symbol):
        lot = a.wallet.lots.get(symbol)
        if lot and lot.meta.get("pair"):
            return lot.meta["pair"]
        return next((t.get("pair") for t in reversed(a.wallet.trades) if t["symbol"] == symbol and t.get("pair")), None)

    def candles(self, bot: str, symbol: str, tf: str) -> dict:
        a = next((x for x in self.bar_agents + [self.degen, self.lovec, self.stin, self.oracle] if x.name == bot), None)
        if a is None:
            return {"error": "neznámý bot"}
        if a in (self.degen, self.lovec):                       # DEX: GeckoTerminal OHLCV podle poolu
            pair = self._dex_pair(a, symbol)
            if not pair:
                return {"error": "u tohoto coinu chybí adresa poolu"}
            minutes = self.TF.get(tf, 5)
            unit, agg = ("minute", min(minutes, 15)) if minutes < 60 else ("hour", 1) if minutes == 60 else ("day", 1)
            r = self.radar.s.get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{pair}/ohlcv/{unit}",
                                 params={"aggregate": agg, "limit": 300}, timeout=15,
                                 headers={"Accept": "application/json;version=20230302"})
            r.raise_for_status()
            rows = ((r.json().get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
            bars = [{"t": int(x[0]), "o": x[1], "h": x[2], "l": x[3], "c": x[4], "v": x[5]} for x in reversed(rows)]
            return {"kind": "candles", "bars": bars, "tf": minutes}
        if a is self.oracle:                                    # Polymarket: vývoj ceny sázky
            lot = a.wallet.lots.get(symbol)
            meta = lot.meta if lot else next((t for t in reversed(a.wallet.trades) if t["symbol"] == symbol), {})
            mid, side = meta.get("market"), meta.get("side", "YES")
            if not mid:
                return {"error": "chybí trh"}
            m = self.radar.s.get(f"https://gamma-api.polymarket.com/markets/{mid}", timeout=15).json()
            toks = json.loads(m.get("clobTokenIds") or "[]")
            if len(toks) < 2:
                return {"error": "trh nemá historii"}
            tok = toks[0] if side == "YES" else toks[1]
            h = self.radar.s.get("https://clob.polymarket.com/prices-history",
                                 params={"market": tok, "interval": "max", "fidelity": 60}, timeout=15).json()
            pts = [{"t": int(x["t"]), "c": float(x["p"])} for x in h.get("history", [])]
            return {"kind": "line", "bars": pts, "tf": 60, "label": f"cena {side}"}
        minutes = self.TF.get(tf) or getattr(a, "minutes", 60)
        now = utcnow()
        span = timedelta(minutes=minutes * 300)
        if not a.crypto:
            span *= 3                                            # akcie: noci a víkendy bez svíček
        df = self.data.bars([symbol], minutes, now - span, crypto=a.crypto).get(symbol)
        if df is None or df.empty:
            return {"error": "žádná data"}
        df = df.tail(400)
        bars = [{"t": int(ts.timestamp()), "o": float(r.open), "h": float(r.high), "l": float(r.low),
                 "c": float(r.close), "v": float(r.volume)} for ts, r in df.iterrows()]
        out = {"kind": "candles", "bars": bars, "tf": minutes}
        if hasattr(a, "prepare") and isinstance(a, BarAgent) and minutes == a.minutes:
            try:                                                # čáry, podle kterých bot rozhoduje
                p = a.prepare(df)
                out["lines"] = {c: [[int(ts.timestamp()), float(v)] for ts, v in p[c].dropna().items()]
                                for c in ("ema20", "ema50", "ema200", "vwap", "or_high", "or_low", "hh", "ll", "hh2h", "hh72") if c in p.columns}
            except Exception as e:
                log.debug("lines %s: %s", a.name, e)
        return out

    def run(self):
        dashboard.providers["candles"] = self.candles
        dashboard.serve()
        mode = "SKUTEČNÉ PENÍZE" if config.IS_LIVE else "papírový účet"
        self.tg.send(f"🐣 Kolonie startuje ({mode}). Boti: " + ", ".join(w.name for w in self.wallets)
                     + ". Příkazy: /stav /pauza /pokracuj /prodej_vse /hodnoceni")
        while True:
            t0 = time.time()
            try:
                self.tick()
            except Exception as e:
                log.exception("tick: %s", e)
            if all(w.status == "DEAD" for w in self.wallets):
                self.tg.send("⚰️ Všichni boti jsou mrtví. Kolonie končí.")
                self.save()
                return
            time.sleep(max(1, config.LOOP_SECONDS - (time.time() - t0)))
