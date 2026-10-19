"""Paměť kolonie - boti se učí z vlastních obchodů.

U každého obchodu si bot při nákupu zapíše, v jaké byl situaci ("rysy": hodina, nálada trhu,
likvidita, jestli byl coin v trendu…). Když obchod skončí, zapíše se výsledek. Před dalším nákupem
se bot podívá do paměti:
  - situace, ve které opakovaně prodělával (aspoň MIN_N obchodů, průměr výrazně v mínusu) -> nekoupí,
  - situace, ve které se mu daří -> vsadí víc (max 1,5x), kde se mu nedaří -> méně (min 0,5x).
Průměry jsou "smrštěné" k nule (jako by měl navíc PRIOR obchodů s nulovým výsledkem), aby se bot
nenechal ošálit dvěma náhodnými obchody. Paměť drží posledních MAX_TRADES obchodů, takže starší
lekce postupně vyprchají, když se trh změní.
"""
from __future__ import annotations
import json
import logging
import os
from datetime import datetime

log = logging.getLogger("colony.memory")

MIN_N = 6            # kolik obchodů v dané situaci musí být, než z toho bot vyvodí zákaz
PRIOR = 5            # smrštění průměru k nule
AVOID_BELOW = -0.02  # smrštěný průměr horší než -2 % na obchod -> situaci se vyhnout
MAX_TRADES = 1500    # skutečné i stínové obchody; rozhoduje se podle nich, starší vyprchají
COVER_MAX = 0.9      # rys, který měl skoro každý obchod (např. "v trendu = ano" u Degena), nic nerozlišuje:
                     # zákaz podle něj by znamenal "už nikdy nic nekupuj" -> podle takového rysu nezakazujeme
MIN_REST = 2         # zákaz jen když máme s čím srovnat: aspoň tolik obchodů BEZ toho rysu, a ty dopadly líp
                     # (Lovec 19. 10.: "stáří přes týden" průměr -5,9 %, ale jediný mladší coin -11,9 % ->
                     # rys nebyl příčinou ztrát, jen ho měl skoro každý obchod)


def hour_bucket(dt: datetime) -> str:
    h = dt.hour
    return "noc (0-6 UTC)" if h < 6 else "ráno (6-12 UTC)" if h < 12 else "odpoledne (12-18 UTC)" if h < 18 else "večer (18-24 UTC)"


def num_bucket(x: float, edges: list[float], labels: list[str]) -> str:
    for e, lab in zip(edges, labels):
        if x < e:
            return lab
    return labels[-1]


class Memory:
    def __init__(self, path: str):
        self.path = path
        self.data: dict = {"bots": {}, "seen": {}}
        try:
            self.data = json.load(open(path))
        except Exception:
            pass
        self.data.setdefault("bots", {})
        self.data.setdefault("seen", {})

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        json.dump(self.data, open(tmp, "w"), ensure_ascii=False)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ zápis
    def record(self, bot: str, features: dict, ret: float, when: str = "", weight: float = 1.0):
        """weight 1 = skutečný obchod, 0.5 = stínový (signál, který bot viděl, ale nekoupil)."""
        if not features:
            return
        rows = self.data["bots"].setdefault(bot, [])
        rows.append({"f": {k: str(v) for k, v in features.items()}, "r": float(ret), "t": when, "w": weight})
        del rows[:-MAX_TRADES]

    def learn_from(self, wallets) -> list[str]:
        """Zapíše nové uzavřené obchody všech peněženek. Vrátí krátké zprávy o nových lekcích."""
        news = []
        for w in wallets:
            seen = self.data["seen"].get(w.name, 0)
            if seen > len(w.trades):                 # nová peněženka (vzkříšený bot) -> začni znova
                seen = 0
            before = {k for k, *_ in self.avoid_list(w.name)}
            for t in w.trades[seen:]:
                self.record(w.name, t.get("features") or {}, t.get("ret", 0.0), t.get("closed", ""))
            self.data["seen"][w.name] = len(w.trades)
            for key, n, avg in self.avoid_list(w.name):
                if key not in before:
                    news.append(f"🧠 {w.name} se poučil: když {key}, prodělal {n:g}x (průměr {avg*100:+.1f} %) - příště to nekoupí")
        if news or any(len(w.trades) for w in wallets):
            self.save()
        return news

    # ------------------------------------------------------------------ čtení
    def stats(self, bot: str) -> dict:
        out: dict = {}
        for row in self.data["bots"].get(bot, []):
            wt = row.get("w", 1.0)
            for k, v in row["f"].items():
                s = out.setdefault(f"{k} = {v}", [0.0, 0.0])
                s[0] += wt
                s[1] += wt * row["r"]
        # n = vážený počet (stínový obchod = půlka), smrštěný průměr, hrubý průměr
        return {k: (round(n, 1), tot / (n + PRIOR), tot / n) for k, (n, tot) in out.items() if n > 0}

    def total_weight(self, bot: str) -> float:
        return sum(row.get("w", 1.0) for row in self.data["bots"].get(bot, []))

    def _totals(self, bot: str) -> tuple[float, float]:
        rows = self.data["bots"].get(bot, [])
        return sum(r.get("w", 1.0) for r in rows), sum(r.get("w", 1.0) * r["r"] for r in rows)

    def _forbids(self, n: float, shr: float, total: float, raw: float = 0.0, total_sum: float = 0.0) -> bool:
        if not (n >= MIN_N and shr < AVOID_BELOW and n < COVER_MAX * total):
            return False
        rest_n = total - n                                   # obchody bez tohoto rysu
        if rest_n < MIN_REST:
            return False
        rest_avg = (total_sum - raw * n) / rest_n
        return raw < rest_avg                                # rys musí dopadat hůř než zbytek

    def avoid_list(self, bot: str):
        total, tsum = self._totals(bot)
        return sorted(((k, n, raw) for k, (n, shr, raw) in self.stats(bot).items()
                       if self._forbids(n, shr, total, raw, tsum)), key=lambda x: x[2])

    def judge(self, bot: str, features: dict) -> tuple[bool, float, str]:
        """(smí koupit?, násobek sázky, důvod)."""
        if not features:
            return True, 1.0, ""
        st = self.stats(bot)
        total, tsum = self._totals(bot)
        keys = [f"{k} = {v}" for k, v in features.items()]
        for k in keys:
            n, shr, raw = st.get(k, (0, 0.0, 0.0))
            if self._forbids(n, shr, total, raw, tsum):
                return False, 0.0, f"paměť: když {k}, prodělal jsem {n:g}x (průměr {raw*100:+.1f} %)"
        known = [st[k][1] for k in keys if k in st and st[k][0] >= 3]
        if not known:
            return True, 1.0, ""
        edge = sum(known) / len(known)
        mult = max(0.5, min(1.5, 1 + 10 * edge))           # +5 % průměr -> 1,5x, -5 % -> 0,5x
        return True, mult, (f"paměť: podobné obchody {edge*100:+.1f} % -> sázka {mult:.1f}x" if abs(mult - 1) > 0.05 else "")

    def lessons(self, bot: str, top: int = 6) -> list[dict]:
        """Pro dashboard: co se bot zatím naučil (nejhorší a nejlepší situace)."""
        total, tsum = self._totals(bot)
        st = [(k, n, shr, raw) for k, (n, shr, raw) in self.stats(bot).items() if n >= 3]
        worst = sorted(st, key=lambda x: x[2])[:top // 2]
        best = sorted((x for x in st if x[2] > 0), key=lambda x: -x[2])[:top // 2]
        return [{"when": k, "n": n, "avg": raw, "score": shr,
                 "verdict": "vyhýbá se" if self._forbids(n, shr, total, raw, tsum) else "daří se" if shr > 0 else "sleduje"}
                for k, n, shr, raw in worst + [b for b in best if b not in worst]]
