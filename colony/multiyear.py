"""VÍCELETÝ TEST - Orbit, Trendy, Vlna a Medvěd na ~5 letech historie, po čtvrtletích.

Proč: laboratoř (90 dní) vidí jen poslední náladu trhu. Nastavení, které vyšlo v listopadovém
propadu, může v růstu prodělávat. Tady každá varianta bota projde každé čtvrtletí zvlášť
(pád 2022, růst 2023-24, propad 2026...) a vybere se ta, která vydělává ve většině období.

Poctivost (žádné "učení se historie nazpaměť"):
  * Walk-forward: pro každé čtvrtletí se varianta vybere JEN podle čtvrtletí PŘED ním a výsledek
    se změří na tom čtvrtletí. Součet těchto výsledků = jak by si bot vedl, kdyby se takhle
    ladil celou dobu. Podle něj se rozhoduje, jestli bot prošel.
  * Bot prošel, když v tomhle poctivém přehrání vydělal a byl v plusu aspoň v polovině čtvrtletí.

Server má 1 GB paměti: data se stahují po jednom čtvrtletí a po přehrání se zahodí.
Práce se dělí na kousky (bot × čtvrtletí), postup se ukládá do state/multiyear.json
a laboratoř pokračuje při dalším běhu (každých 6 h), dokud není hotovo. Pak se celé
přepočítá jednou za týden.
"""
from __future__ import annotations

import json
import os
import statistics
import time
from datetime import datetime, timedelta, timezone

YEARS = 5
QUARTER_DAYS = 91
MIN_HISTORY = 4            # walk-forward začne vybírat až po 4 čtvrtletích (1 rok)
REFRESH_DAYS = 7           # hotový test se přepočítá jednou týdně
MIN_TRADES_QUARTER = 3     # čtvrtletí s méně obchody se pro výběr varianty bere jako slabé


def periods(end: datetime, years: float | None = None) -> list[tuple[datetime, datetime]]:
    """Čtvrtletí od nejstaršího po nejnovější, poslední končí v `end`."""
    n = max(1, int(round((YEARS if years is None else years) * 365 / QUARTER_DAYS)))
    out = []
    for k in range(n, 0, -1):
        s = end - timedelta(days=QUARTER_DAYS * k)
        out.append((s, s + timedelta(days=QUARTER_DAYS)))
    return out


def _rank_key(res: list[dict]):
    """Čím víc čtvrtletí v plusu a čím vyšší medián, tím líp (jeden zázračný kvartál nerozhoduje).
    Čtvrtletí bez obchodu je neutrální (bot správně seděl stranou) - nepočítá se ani pro, ani proti."""
    rets = [r["return"] for r in res if r is not None and r["trades"] > 0]
    if not rets:
        return (-1.0, -1.0)
    return (sum(x > 0 for x in rets) / len(rets), statistics.median(rets))


def pick_robust(rows: list[list[dict]]) -> int:
    """rows[q][v] = {"return", "trades"} pro čtvrtletí q a variantu v. Vrátí index nejodolnější varianty."""
    nv = len(rows[0]) if rows else 0
    return max(range(nv), key=lambda v: _rank_key([q[v] for q in rows]))


def walk_forward(rows: list[list[dict]], min_history: int = MIN_HISTORY) -> list[dict]:
    """Poctivé přehrání: v čtvrtletí q se použije varianta vybraná jen z čtvrtletí 0..q-1."""
    out = []
    for q in range(min_history, len(rows)):
        v = pick_robust(rows[:q])
        r = rows[q][v]
        if r is not None:
            out.append({"q": q, "variant": v, "return": r["return"], "trades": r["trades"]})
    return out


def verdict(oos: list[dict]) -> tuple[bool, str, float]:
    """Prošel = poctivé přehrání vydělalo a v plusu byl aspoň v polovině čtvrtletí."""
    if len(oos) < 2:
        return False, "málo dat", 0.0
    total = 1.0
    for r in oos:
        total *= 1 + r["return"]
    total -= 1
    traded = [r for r in oos if r["trades"] > 0]          # čtvrtletí bez obchodu jsou neutrální
    if not traded:
        return False, "NEPROŠEL ❌ (žádný obchod)", 0.0
    share = sum(r["return"] > 0 for r in traded) / len(traded)
    ok = total > 0 and share >= 0.5
    return ok, ("PROŠEL ✅" if ok else "NEPROŠEL ❌"), total


def _save(st, path):
    """Zápis přes dočasný soubor - když server proces zabije uprostřed, postup se neztratí."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    os.replace(tmp, path)


def _load(path):
    try:
        return json.load(open(path))
    except Exception:
        return None


def _new_cycle(now: datetime, variants: dict) -> dict:
    ps = periods(now)
    return {"started": now.isoformat(timespec="minutes"), "finished": None,
            "periods": [[s.isoformat(timespec="minutes"), e.isoformat(timespec="minutes")] for s, e in ps],
            "variants": {n: v for n, (_, v) in variants.items()},
            "results": {n: {} for n in variants}}


def needs_new_cycle(st: dict | None, now: datetime, variants: dict) -> bool:
    if not st:
        return True
    if {n: v for n, (_, v) in variants.items()} != st.get("variants"):   # změnily se varianty -> znovu
        return True
    fin = st.get("finished")
    return bool(fin) and now - datetime.fromisoformat(fin) > timedelta(days=REFRESH_DAYS)


def step(variants: dict, fetch, run_one, state_dir: str, budget_s: float = 2 * 3600,
         now: datetime | None = None, clock=time.monotonic) -> dict:
    """Udělá tolik kousků (bot × čtvrtletí), kolik stihne za budget_s, a uloží postup.
    fetch(cls, params, start, end, cache) -> data (už s historií na zahřátí); cache sdílí data mezi variantami
    run_one(cls, params, data, start, end) -> {"return", "trades"}"""
    now = now or datetime.now(timezone.utc)
    path = os.path.join(state_dir, "multiyear.json")
    st = _load(path)
    if needs_new_cycle(st, now, variants):
        st = _new_cycle(now, variants)
    t0 = clock()
    ps = [(datetime.fromisoformat(s), datetime.fromisoformat(e)) for s, e in st["periods"]]
    for name, (cls, vs) in variants.items():
        res = st["results"].setdefault(name, {})
        for qi, (s, e) in enumerate(ps):
            if str(qi) in res:
                continue
            if clock() - t0 > budget_s:
                _save(st, path)
                return st
            row, cache = [], {}
            for params in vs:
                try:
                    data = fetch(cls, params, s, e, cache)
                    row.append(run_one(cls, params, data, s, e))
                except Exception as ex:                  # výpadek dat jednoho čtvrtletí test nezastaví
                    row.append(None)
                    st.setdefault("errors", []).append(f"{name} {s:%Y-%m}: {ex}"[:200])
            cache.clear()                                # paměť: data čtvrtletí hned zahodit
            res[str(qi)] = row
            _save(st, path)
    if not st.get("finished"):
        st["finished"] = now.isoformat(timespec="minutes")
        st["summary"] = summarize(st)
        _save(st, path)
    return st


def summarize(st: dict) -> dict:
    out = {}
    for name, res in st["results"].items():
        vs = st["variants"].get(name) or []
        rows = [res[str(q)] for q in range(len(st["periods"])) if str(q) in res]
        rows = [row for row in rows if any(r is not None for r in row)]
        if not rows or not vs:
            continue
        oos = walk_forward(rows)
        ok, txt, total = verdict(oos)
        best = pick_robust(rows)
        rets = [row[best]["return"] for row in rows if row[best] is not None and row[best]["trades"] > 0]
        out[name] = {"passed": ok, "verdict": txt, "oos_return": round(total, 4),
                     "oos_quarters": sum(r["trades"] > 0 for r in oos), "oos_positive": sum(r["return"] > 0 for r in oos),
                     "chosen": vs[best], "chosen_positive": sum(r > 0 for r in rets), "chosen_quarters": len(rets),
                     "chosen_median": round(statistics.median(rets), 4) if rets else 0.0,
                     "chosen_trades": sum(row[best]["trades"] for row in rows if row[best] is not None),
                     "quarters": [None if row[best] is None else round(row[best]["return"], 4) for row in rows]}
    return out


def apply_to_lab(bots: dict, st: dict | None) -> dict:
    """Výsledek víceletého testu přednostně určí nastavení botů v lab.json.
    Prošel -> dostane nejodolnější nastavení (90denní laboratoř dál rozhoduje o varování).
    Neprošel -> nastavení z 90denní laboratoře, ale varování (na papíře obchoduje dál)."""
    summary = (st or {}).get("summary") or {}
    if not (st or {}).get("finished"):
        return bots
    for name, b in bots.items():
        s = summary.get(name)
        if not s:
            continue
        b["multiyear"] = {k: s[k] for k in ("passed", "oos_return", "oos_positive", "oos_quarters", "chosen")}
        if s["passed"]:
            b["chosen"] = s["chosen"]
        else:
            b["benched"] = True
    return bots


def report_lines(st: dict | None) -> list[str]:
    if not st:
        return ["VÍCELETÝ TEST: zatím nezačal"]
    total = len(st["periods"]) * len(st["variants"])
    done = sum(len(r) for r in st["results"].values())
    first, last = st["periods"][0][0][:10], st["periods"][-1][1][:10]
    if not st.get("finished"):
        return [f"VÍCELETÝ TEST {first}..{last}: probíhá, hotovo {done}/{total} kousků (bot × čtvrtletí)"]
    lines = [f"VÍCELETÝ TEST {first}..{last} (po čtvrtletích; rozhoduje poctivé přehrání - nastavení vybrané "
             f"jen z minulosti) | dokončen {st['finished'][:10]}"]
    for name, s in (st.get("summary") or {}).items():
        q = " ".join("·" if r is None else f"{r*100:+.0f}" for r in s["quarters"])
        lines.append(f"{name:7} {s['verdict']} | poctivě {s['oos_return']*100:+.1f} % "
                     f"(v plusu {s['oos_positive']}/{s['oos_quarters']} čtvrtletí s obchody) | nejodolnější {s['chosen']} "
                     f"v plusu {s['chosen_positive']}/{s['chosen_quarters']}, {s['chosen_trades']} obch.")
        lines.append(f"        čtvrtletí %: {q}")
    if st.get("errors"):
        lines.append(f"  (výpadky dat: {len(st['errors'])}, např. {st['errors'][0]})")
    return lines
