"""Kronika kolonie - trvalý archiv všeho, co boti viděli a udělali (SQLite, state/kronika.db).

Co se zapisuje:
  signals  - každý signál k nákupu: koupený i nekoupený (proč ne: paměť, plno pozic, kolega drží coin,
             pauza…). U nekoupených se po čase dopočítá, jak by obchod dopadl ("stínový obchod"),
             a výsledek jde do paměti s poloviční váhou - boti se tak učí i z toho, co jen viděli.
  trades   - každý uzavřený obchod se vším (rysy situace, důvod nákupu a prodeje, výsledek).
  radar    - každou hodinu snímek toho, o čem se mluví (top coiny, zdroje, nálada, Fear & Greed).
  events   - všechny události kolonie (nákupy, prodeje, zranění, lekce…).
Nic se nemaže. Paměť (memory.py) rozhoduje podle posledních obchodů, kronika drží všechno navždy.
"""
from __future__ import annotations
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

log = logging.getLogger("colony.kronika")

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY, t TEXT, bot TEXT, symbol TEXT, price REAL,
    stop REAL, target REAL, horizon_min INTEGER, taken INTEGER, skip_reason TEXT, reason TEXT,
    features TEXT, meta TEXT, resolved INTEGER DEFAULT 0, outcome REAL, outcome_note TEXT);
CREATE INDEX IF NOT EXISTS sig_open ON signals(resolved, taken);
CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, bot TEXT, symbol TEXT, opened TEXT, closed TEXT,
    entry REAL, exit REAL, ret REAL, pnl REAL, why TEXT, reason TEXT, features TEXT, UNIQUE(bot, symbol, opened, closed));
CREATE TABLE IF NOT EXISTS radar (t TEXT PRIMARY KEY, mood TEXT, fear_greed INTEGER, hot TEXT, world TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, t TEXT, msg TEXT, UNIQUE(t, msg));
"""


def utcnow():
    return datetime.now(timezone.utc)


class Kronika:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._seen_signal: dict = {}

    def _exec(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def _query(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    # ------------------------------------------------------------------ zápis
    def signal(self, bot, symbol, price, *, taken, skip_reason="", stop=None, target=None, horizon_min=240,
               reason="", features=None, meta=None, now=None):
        """Zapíše signál. Stejný nekoupený signál (bot+coin) zapíše nejvýš jednou za horizont."""
        now = now or utcnow()
        if not taken:
            last = self._seen_signal.get((bot, symbol))
            if last and (now - last).total_seconds() < horizon_min * 60:
                return
            self._seen_signal[(bot, symbol)] = now
        self._exec("INSERT INTO signals (t, bot, symbol, price, stop, target, horizon_min, taken, skip_reason, reason,"
                   " features, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (now.isoformat(timespec="seconds"), bot, symbol, float(price), stop, target, int(horizon_min),
                    1 if taken else 0, skip_reason, reason, json.dumps(features or {}, ensure_ascii=False),
                    json.dumps(meta or {}, ensure_ascii=False)))

    def trades(self, wallets):
        for w in wallets:
            for t in w.trades[-50:]:
                self._exec("INSERT OR IGNORE INTO trades (bot, symbol, opened, closed, entry, exit, ret, pnl, why, reason,"
                           " features) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (w.name, t["symbol"], t.get("opened"), t.get("closed"), t.get("entry"), t.get("exit"),
                            t.get("ret"), t.get("pnl"), t.get("why"), t.get("reason"),
                            json.dumps(t.get("features") or {}, ensure_ascii=False)))

    def radar_snapshot(self, now, mood, snap: dict):
        hot = [{"s": h.get("symbol"), "score": h.get("score"), "src": h.get("sources", [])[:4]}
               for h in (snap.get("hot") or [])[:25]]
        world = [w.get("title") for w in (snap.get("world") or [])[:15]]
        self._exec("INSERT OR IGNORE INTO radar VALUES (?,?,?,?,?)",
                   (now.strftime("%Y-%m-%dT%H:00"), mood, (snap.get("fear_greed") or {}).get("value"),
                    json.dumps(hot, ensure_ascii=False), json.dumps(world, ensure_ascii=False)))

    def events(self, events):
        for t, msg in events[-30:]:
            self._exec("INSERT OR IGNORE INTO events (t, msg) VALUES (?,?)", (t, msg))

    # ------------------------------------------------------------------ stínové obchody
    def due_signals(self, now, limit=40):
        rows = self._query("SELECT id, t, bot, symbol, price, stop, target, horizon_min, features, meta FROM signals "
                           "WHERE resolved=0 AND taken=0 ORDER BY id LIMIT 400")
        out = []
        for r in rows:
            t = datetime.fromisoformat(r[1])
            if now >= t + timedelta(minutes=r[7]):
                out.append({"id": r[0], "t": t, "bot": r[2], "symbol": r[3], "price": r[4], "stop": r[5],
                            "target": r[6], "horizon_min": r[7], "features": json.loads(r[8] or "{}"),
                            "meta": json.loads(r[9] or "{}")})
            if len(out) >= limit:
                break
        return out

    def resolve(self, sid, outcome, note=""):
        self._exec("UPDATE signals SET resolved=1, outcome=?, outcome_note=? WHERE id=?",
                   (None if outcome is None else float(outcome), note, sid))
        self._exec("UPDATE signals SET resolved=1 WHERE taken=1 AND resolved=0 AND t < ?",
                   ((utcnow() - timedelta(days=30)).isoformat(),))

    @staticmethod
    def simulate(price, stop, target, bars, short=False):
        """Jak by dopadl obchod podle svíček (konzervativně: stop dřív než cíl ve stejné svíčce)."""
        if bars is None or len(bars) == 0:
            return None, "chybí data"
        for _, b in bars.iterrows():
            if not short:
                if stop and b["low"] <= stop:
                    return stop / price - 1, "stop"
                if target and b["high"] >= target:
                    return target / price - 1, "cíl"
            else:
                if stop and b["high"] >= stop:
                    return price / stop - 1, "stop"
        last = float(bars["close"].iloc[-1])
        return (price / last - 1 if short else last / price - 1), "konec doby"

    # ------------------------------------------------------------------ přehledy
    def bot_summary(self, bot) -> dict:
        r = self._query("SELECT COUNT(*), SUM(taken), SUM(CASE WHEN taken=0 AND resolved=1 AND outcome IS NOT NULL THEN 1 ELSE 0 END),"
                        " SUM(CASE WHEN taken=0 AND outcome > 0 THEN 1 ELSE 0 END), AVG(CASE WHEN taken=0 THEN outcome END)"
                        " FROM signals WHERE bot=?", (bot,))[0]
        n_tr = self._query("SELECT COUNT(*) FROM trades WHERE bot=?", (bot,))[0][0]
        return {"signals": r[0] or 0, "taken": r[1] or 0, "missed_resolved": r[2] or 0,
                "missed_would_win": r[3] or 0, "missed_avg": r[4], "trades": n_tr}

    def report(self, days=7) -> str:
        since = (utcnow() - timedelta(days=days)).isoformat()
        lines = [f"KRONIKA - posledních {days} dní (archiv od začátku: {self.path})"]
        tot = self._query("SELECT (SELECT COUNT(*) FROM signals), (SELECT COUNT(*) FROM trades), "
                          "(SELECT COUNT(*) FROM radar), (SELECT COUNT(*) FROM events)")[0]
        lines.append(f"Celkem zapsáno: {tot[0]} signálů, {tot[1]} obchodů, {tot[2]} hodinových snímků radaru, {tot[3]} událostí")
        lines.append("\nNEKOUPENÉ SIGNÁLY A JAK BY DOPADLY (stínové obchody):")
        for bot, reason, n, wins, avg in self._query(
                "SELECT bot, skip_reason, COUNT(*), SUM(outcome > 0), AVG(outcome) FROM signals "
                "WHERE taken=0 AND resolved=1 AND outcome IS NOT NULL AND t >= ? GROUP BY bot, skip_reason "
                "ORDER BY bot, COUNT(*) DESC", (since,)):
            lines.append(f"  {bot:9} | {reason[:45]:45} | {n:3}x, vyhrálo by {wins or 0}, průměr {avg*100:+.1f} %")
        lines.append("\nUZAVŘENÉ OBCHODY:")
        for bot, n, wins, avg, pnl in self._query(
                "SELECT bot, COUNT(*), SUM(ret > 0), AVG(ret), SUM(pnl) FROM trades WHERE closed >= ? GROUP BY bot", (since,)):
            lines.append(f"  {bot:9} | {n:3} obchodů, {wins or 0} výher, průměr {avg*100:+.1f} %, celkem {pnl:+.2f} $")
        lines.append("\nNEJČASTĚJŠÍ COINY NA RADARU:")
        cnt: dict = {}
        for (hot,) in self._query("SELECT hot FROM radar WHERE t >= ?", (since,)):
            for h in json.loads(hot or "[]")[:10]:
                cnt[h["s"]] = cnt.get(h["s"], 0) + 1
        lines.append("  " + ", ".join(f"{s} ({n}h)" for s, n in sorted(cnt.items(), key=lambda x: -x[1])[:20]))
        return "\n".join(lines)
