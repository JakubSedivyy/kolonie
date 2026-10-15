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
from .agents.strategies import OrbitAgent, TrendAgent, MemeAgent, WaveAgent, WhisperAgent
from .agents.degen import DegenAgent, HunterAgent
from .broker import AlpacaBroker
from .indicators import ema
from .market import AlpacaData, AlpacaAssets, DexScreener
from .notify import Telegram
from .trends import TrendRadar
from .grok import Grok
from .agents.oracle import OracleAgent
from . import dashboard
from .wallet import Wallet, utcnow

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
        self.bar_agents: list[BarAgent] = [OrbitAgent(), TrendAgent(), MemeAgent(), WaveAgent(), WhisperAgent()]
        self.degen = DegenAgent(self.dex, radar=self.radar)
        self.lovec = HunterAgent(self.dex, radar=self.radar)
        self.oracle = OracleAgent(self.grok)
        self.lab, self._lab_mtime, self._lab_checked = {}, 0.0, 0.0
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
        return [a.wallet for a in self.bar_agents] + [self.degen.wallet, self.lovec.wallet, self.oracle.wallet]

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
        for a in self.bar_agents + [self.degen, self.lovec, self.oracle]:
            if a.name in st["wallets"]:
                a.wallet = Wallet.from_dict(st["wallets"][a.name])
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
                why = a.exit_signal(df, i, lot)
                if why:
                    self.exec.exit(a, s, price, why, now)
                continue
            if self.paused or a.benched or w.status != "ALIVE" or len(w.lots) >= a.max_positions:
                continue
            if self.mood == "risk_off" and not a.trades_in_risk_off and not config.RISK_OFF_TRADING:
                continue
            if self.blacklist.get(s, 0) >= 2 or not a.can_enter(ts):
                continue
            if any(s in o.wallet.lots for o in self.bar_agents if o is not a):
                continue                                # coin už drží kolega - na účtu se pozice nesmí míchat
            e = a.entry(df, i, {**self.ctx, "symbol": s})
            if e and e.stop < price:
                usd = a.size_usd(price, e.stop, mult)
                if usd:
                    self.exec.enter(a, s, price, e, usd, now)

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
                x = a.check_exit(lot, p, p, p, p, now)
                if x:
                    t = self.exec.exit(a, s, p, x[1], now)
                    if t and x[1] == "stop-loss":
                        self.blacklist[s] = self.blacklist.get(s, 0) + 1
                        if self.blacklist[s] == 2:
                            self.tg.send(f"🚫 {s} dnes 2x vyhodil stop-loss - kolonie ho do zítřka nebude obchodovat")

    def check_survival(self, now):
        agents = self.bar_agents + [self.degen, self.lovec, self.oracle]
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
        if int(time.time() // 30) % 4 == 0:            # degen a lovec každé ~2 minuty
            for sim in (self.degen, self.lovec):
                try:
                    m = "neutral" if self.mood == "risk_off" and config.RISK_OFF_TRADING else self.mood
                    sim.tick(now, "risk_off" if self.paused else m, self.tg.send)  # pauza = jen správa pozic
                except Exception as e:
                    log.exception("%s: %s", sim.name, e)
        if int(time.time() // 30) % 20 == 1:           # Věštec každých ~10 minut
            try:
                self.oracle.tick(now, self.tg.send, paused=self.paused)
            except Exception as e:
                log.exception("oracle: %s", e)
        self.check_survival(now)
        self.daily(now)
        self.save()
        dashboard.publish(self.snapshot(now))

    def snapshot(self, now):
        agents = self.bar_agents + [self.degen, self.lovec, self.oracle]
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
                            "side": l.meta.get("side", ""), "p": l.meta.get("p")})
            bots.append({**s, "asset_class": w.asset_class, "desc": (a.__doc__ or "").strip().split("\n")[0],
                         "start": w.start, "cash": w.cash, "born": w.born, "died": w.died, "age_days": age,
                         "days_left": max(0, config.PROFIT_DEADLINE_DAYS - age), "curve": w.equity_curve[-600:],
                         "positions": pos, "simulated": w.asset_class in ("degen", "dexmeme", "predict"),
                         "benched": bool(getattr(a, "benched", False)),
                         "lab_warn": bool(getattr(a, "lab_warn", False)),
                         "waiting": (a is self.oracle and not self.grok.enabled),
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

    def run(self):
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
