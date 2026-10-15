"""VĚŠTEC - sázky na predikční trhy Polymarket (jen simulace), jako Grok bot z videa.

Jak to dělá:
  1. Vezme nejobchodovanější trhy, které skončí za 12 h až 21 dní (bez sportovních zápasů).
  2. Grok si přečte X a zprávy a ke každému trhu odhadne skutečnou pravděpodobnost.
  3. Když se odhad liší od ceny trhu aspoň o 8 procentních bodů, vsadí.
     Velikost: čtvrtinový Kellyho vzorec (plný Kelly je hazard), max 10 % peněženky.
  4. Drží do vyhodnocení trhu (výhra = $1 za podíl, prohra = $0), nebo vybere zisk při ceně ≥ 0.97.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timedelta, timezone

import requests

from .. import config
from ..grok import extract_json
from ..wallet import Wallet

log = logging.getLogger("colony.oracle")
GAMMA = "https://gamma-api.polymarket.com"
SPORT_PREFIX = ("nhl-", "nba-", "nfl-", "mlb-", "epl-", "ucl-", "atp-", "wta-", "cfb-", "cbb-", "mls-", "lol-",
                "cs2-", "dota", "val-", "bun-", "lal-", "sea-", "fl1-", "ufc-", "f1-")


def _jl(x, default=None):
    if isinstance(x, list):
        return x
    try:
        return json.loads(x)
    except Exception:
        return default


class OracleAgent:
    name = "Věštec"
    asset_class = "predict"
    crypto = False
    min_edge = 0.08
    kelly_fraction = 0.25
    max_bet_frac = 0.10
    max_positions = 8
    scan_every_h = 12
    markets_per_scan = 5
    take_profit_price = 0.97

    def __init__(self, grok, wallet: Wallet | None = None, session=None):
        self.grok = grok
        self.s = session or requests.Session()
        self.wallet = wallet or Wallet(self.name, self.asset_class)
        self.last_scan = ""                      # ISO čas posledního dotazu na Groka
        self.evaluated: dict[str, str] = {}      # market id -> kdy naposledy posouzen
        self.last_estimates: list[dict] = []     # pro dashboard

    # ------------------------------------------------------------------ data
    def _get(self, path, **params):
        r = self.s.get(GAMMA + path, params=params or None, timeout=30)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def parse(m: dict) -> dict | None:
        outs, prices = _jl(m.get("outcomes"), []), _jl(m.get("outcomePrices"), [])
        if len(outs) != 2 or len(prices) != 2:
            return None
        try:
            p_yes = float(prices[0])
            bid = float(m.get("bestBid") or p_yes)
            ask = float(m.get("bestAsk") or p_yes)
        except (TypeError, ValueError):
            return None
        return {"id": str(m.get("id")), "question": m.get("question", ""), "slug": m.get("slug", ""),
                "outcomes": outs, "p_yes": p_yes, "yes_bid": bid, "yes_ask": ask,
                "end": m.get("endDate") or m.get("endDateIso") or "", "closed": bool(m.get("closed")),
                "liquidity": float(m.get("liquidityNum") or m.get("liquidity") or 0),
                "volume24h": float(m.get("volume24hr") or 0), "final": [float(x) for x in prices],
                "accepting": m.get("acceptingOrders", True)}

    def candidates(self, now: datetime) -> list[dict]:
        raw = self._get("/markets", closed="false", active="true", limit=200, order="volume24hr", ascending="false")
        raw = raw if isinstance(raw, list) else raw.get("markets", [])
        held = {l.meta.get("market") for l in self.wallet.lots.values()}
        out = []
        for m in raw:
            p = self.parse(m)
            if not p or p["id"] in held or not p["accepting"]:
                continue
            if p["slug"].startswith(SPORT_PREFIX) or " vs. " in p["question"] or " vs " in p["question"]:
                continue
            try:
                end = datetime.fromisoformat(p["end"].replace("Z", "+00:00"))
            except Exception:
                continue
            if not (now + timedelta(hours=12) <= end <= now + timedelta(days=21)):
                continue
            if p["liquidity"] < 10_000 or p["volume24h"] < 5_000 or not 0.08 <= p["p_yes"] <= 0.92:
                continue
            last = self.evaluated.get(p["id"])
            if last and now - datetime.fromisoformat(last) < timedelta(days=2):
                continue
            out.append(p)
        return out[: self.markets_per_scan]

    def ask_grok(self, markets: list[dict], now: datetime) -> dict:
        lines = "\n".join(f'- id {m["id"]}: "{m["question"]}" (ends {m["end"][:10]}, market YES price {m["p_yes"]:.2f})'
                          for m in markets)
        prompt = f"""You are a calibrated forecaster. Today is {now:%Y-%m-%d}. Search X and use your knowledge of current news.
For each prediction market below, estimate the TRUE probability that it resolves YES. Do not anchor on the market price;
only deviate from it when you have concrete recent evidence. Be honest about uncertainty.
{lines}
Return ONLY valid JSON: {{"markets":[{{"id":"...","p_yes":0.00,"confidence":"low|medium|high","why":"max 20 words"}}]}}"""
        text = self.grok.ask(prompt, x_search=True, hours=72, max_turns=3, reserve=0.35)
        data = extract_json(text) or {}
        return {str(x.get("id")): x for x in data.get("markets", []) if isinstance(x, dict)}

    # ------------------------------------------------------------------ hlavní tah
    def tick(self, now: datetime, notify, paused=False):
        w = self.wallet
        if w.status == "DEAD":
            return
        self.manage(now, notify)
        if paused or w.status != "ALIVE" or not self.grok or not self.grok.enabled:
            return
        if len(w.lots) >= self.max_positions:
            return
        if self.last_scan and now - datetime.fromisoformat(self.last_scan) < timedelta(hours=self.scan_every_h):
            return
        try:
            markets = self.candidates(now)
        except Exception as e:
            log.warning("polymarket: %s", e)
            return
        if not markets:
            self.last_scan = now.isoformat()
            return
        est = self.ask_grok(markets, now)
        if not est:                                # bez rozpočtu nebo chyba - zkusí příště
            return
        self.last_scan = now.isoformat()
        self.last_estimates = []
        for m in markets:
            self.evaluated[m["id"]] = now.isoformat()
            e = est.get(m["id"])
            if not e:
                continue
            try:
                p = min(0.99, max(0.01, float(e.get("p_yes"))))
            except (TypeError, ValueError):
                continue
            conf = str(e.get("confidence", "low")).lower()
            yes_ask, no_ask = m["yes_ask"], 1 - m["yes_bid"]
            edge_yes, edge_no = p - yes_ask, (1 - p) - no_ask
            side, edge, ask, p_side = ("YES", edge_yes, yes_ask, p) if edge_yes >= edge_no else ("NO", edge_no, no_ask, 1 - p)
            self.last_estimates.append({"question": m["question"], "market": m["p_yes"], "grok": p, "confidence": conf,
                                        "side": side, "edge": edge, "why": str(e.get("why", ""))[:140]})
            if edge < self.min_edge or conf == "low" or not 0.02 < ask < 0.98:
                continue
            b = 1 / ask - 1                                   # výplata na 1 $ při výhře
            kelly = (p_side * b - (1 - p_side)) / b
            usd = min(kelly * self.kelly_fraction * w.equity(), self.max_bet_frac * w.equity(),
                      w.cash * 0.95 / (1 + w.costs["fee"]))
            if usd < config.MIN_ORDER_USD:
                continue
            sym = f"PM{m['id']}·{side}"
            w.open(sym, usd / ask, ask, stop=0.0, reason=f"Grok {p:.0%} vs trh {m['p_yes']:.0%} ({conf})", now=now,
                   apply_slippage=False, meta={"market": m["id"], "side": side, "question": m["question"],
                                               "p": p, "end": m["end"], "url": f"https://polymarket.com/market/{m['slug']}"})
            notify(f"🔮 Věštec (simulace) vsadil ${usd:.0f} na {side}: „{m['question'][:80]}“ - Grok {p:.0%}, trh {m['p_yes']:.0%}")

    def manage(self, now, notify):
        w = self.wallet
        prices = {}
        for sym, lot in list(w.lots.items()):
            try:
                m = self.parse(self._get(f"/markets/{lot.meta['market']}"))
            except Exception as e:
                log.warning("polymarket market: %s", e)
                continue
            if not m:
                continue
            yes = lot.meta["side"] == "YES"
            if m["closed"]:
                payout = m["final"][0] if yes else m["final"][1]
                t = w.close(sym, payout, "trh vyhodnocen", now=now, apply_slippage=False)
                icon = "🏆" if t["pnl"] > 0 else "❌"
                notify(f"{icon} Věštec: „{lot.meta['question'][:70]}“ vyhodnoceno - {t['pnl']:+.2f} $")
                continue
            bid = m["yes_bid"] if yes else 1 - m["yes_ask"]
            prices[sym] = bid
            if bid >= self.take_profit_price:
                t = w.close(sym, bid, "vybrán zisk (cena ≥ 0.97)", now=now, apply_slippage=False)
                notify(f"💰 Věštec vybral zisk {t['pnl']:+.2f} $ na „{lot.meta['question'][:70]}“")
        w.mark(prices, now)
