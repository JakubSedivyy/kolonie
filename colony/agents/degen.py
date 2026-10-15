"""DEGEN - simulace obchodování úplně nových meme coinů na Solaně (DexScreener).
Nikdy neposílá skutečné příkazy. Účtuje tvrdé náklady: 1 % poplatek + skluz 3 % a víc,
a když z poolu zmizí likvidita (rug pull), počítá s téměř úplnou ztrátou."""
from __future__ import annotations
import logging
from datetime import datetime, timezone

from .. import config
from ..wallet import Wallet

log = logging.getLogger("colony.degen")


class DegenAgent:
    name = "Degen"
    asset_class = "degen"
    crypto = True
    position_usd = 50.0           # 5 % peněženky na jeden coin
    max_positions = 5
    min_liquidity = 30_000
    max_fdv = 20_000_000
    min_age_min, max_age_min = 30, 24 * 60
    take_profit, stop_loss = 0.40, 0.25
    max_hold_min = 6 * 60
    rug_liquidity_drop = 0.5      # likvidita klesla o víc než 50 % -> rug
    icon = "🎰"
    trend_bonus = 2.0
    cooldown_min = 60             # po prodeji coin chvíli nekupovat znovu
    use_fresh = True              # brát i úplně nové tokeny z DexScreeneru

    def __init__(self, dex, wallet: Wallet | None = None, radar=None):
        self.dex = dex
        self.radar = radar
        self.wallet = wallet or Wallet(self.name, self.asset_class)
        self.blacklist: set[str] = set()
        self.cooldown: dict[str, datetime] = {}

    # ------------------------------------------------------------------ výběr
    def score(self, p: dict, now_ms: int) -> float | None:
        try:
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            fdv = float(p.get("fdv") or p.get("marketCap") or 0)
            age = (now_ms - int(p.get("pairCreatedAt") or now_ms)) / 60000
            tx = (p.get("txns") or {}).get("m5") or {}
            buys, sells = int(tx.get("buys", 0)), int(tx.get("sells", 0))
            ch = p.get("priceChange") or {}
            h1, m5 = float(ch.get("h1") or 0), float(ch.get("m5") or 0)
            vol_m5 = float((p.get("volume") or {}).get("m5") or 0)
        except (TypeError, ValueError):
            return None
        if (p.get("quoteToken") or {}).get("symbol") not in ("SOL", "USDC", "WSOL", "USDT"):
            return None
        if liq < self.min_liquidity or not 0 < fdv <= self.max_fdv:
            return None
        if not self.min_age_min <= age <= self.max_age_min:
            return None
        if buys < 20 or buys < 1.5 * max(sells, 1):    # musí převažovat kupci
            return None
        if not (0 < h1 < 300) or m5 <= 0:              # roste, ale ještě není kolmo nahoře
            return None
        if vol_m5 < 0.05 * liq:                        # musí se reálně obchodovat
            return None
        return buys / max(sells, 1) + h1 / 100

    def slippage(self, liq: float) -> float:
        return max(config.COSTS[self.asset_class]["slip"], min(0.10, 2 * self.position_usd / max(liq, 1)))

    # ------------------------------------------------------------------ hlavní tah
    def tick(self, now: datetime, mood: str, notify) -> None:
        w = self.wallet
        if w.status == "DEAD":
            return
        prices = {}
        # 1) správa otevřených pozic
        for sym, lot in list(w.lots.items()):
            p = self.dex.pair(lot.meta["pair"])
            if not p:
                continue
            price = float(p.get("priceUsd") or 0)
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            prices[sym] = price
            why = None
            if liq < lot.meta["liq"] * (1 - self.rug_liquidity_drop):
                price, why = price * 0.1, "RUG PULL - likvidita zmizela"
                self.blacklist.add(lot.meta["token"])
            elif price <= lot.entry * (1 - self.stop_loss):
                why = "stop-loss"
            elif price >= lot.entry * (1 + self.take_profit):
                why = "cíl zisku"
            elif (now - datetime.fromisoformat(lot.opened)).total_seconds() / 60 >= self.max_hold_min:
                why = "časový limit"
            if why:
                self.cooldown[lot.meta["token"]] = now
                exit_price = price * (1 - self.slippage(liq))
                t = w.close(sym, exit_price, why, now=now, apply_slippage=False)
                icon = "✅" if t["pnl"] > 0 else "💀" if "RUG" in why else "🔻"
                notify(f"{icon} {self.name} (simulace) prodal {sym}: {t['pnl']:+.2f} $ ({t['ret']*100:+.0f} %) - {why}")
        w.mark(prices, now)

        # 2) nové nákupy
        if w.status != "ALIVE" or mood == "risk_off" or len(w.lots) >= self.max_positions:
            return
        now_ms = int(now.timestamp() * 1000)
        held = {l.meta["token"] for l in w.lots.values()}
        cands = []
        trending = set(self.radar.tokens) if self.radar else set()
        fresh = self.dex.fresh_tokens() if self.use_fresh else []
        addrs = list(dict.fromkeys(fresh + sorted(trending)))[:90]
        seen_tok = set()
        for p in self.dex.pairs_for_tokens(addrs):
            tok = (p.get("baseToken") or {}).get("address")
            if not tok or tok in held or tok in self.blacklist or tok in seen_tok:
                continue
            last = self.cooldown.get(tok)
            if last and (now - last).total_seconds() < self.cooldown_min * 60:
                continue
            seen_tok.add(tok)                             # token může mít víc poolů - vezmi první (nejlikvidnější)
            s = self.score(p, now_ms)
            if s is not None:
                if tok in trending:
                    s += self.trend_bonus                 # internet o něm mluví
                cands.append((s, p))
        cands.sort(key=lambda x: -x[0])
        for s, p in cands[: self.max_positions - len(w.lots)]:
            usd = min(self.position_usd, w.cash * 0.9 / (1 + config.COSTS[self.asset_class]["fee"]))
            if usd < config.MIN_ORDER_USD:
                break
            liq = float(p["liquidity"]["usd"])
            price = float(p["priceUsd"]) * (1 + self.slippage(liq))
            qty = usd / price           # poplatek 1 % strhne peněženka navíc
            sym = f'{p["baseToken"]["symbol"]}·{p["baseToken"]["address"][:4]}'
            w.open(sym, qty, price, stop=price * (1 - self.stop_loss), reason=f"skóre {s:.1f}",
                   now=now, apply_slippage=False,
                   meta={"pair": p["pairAddress"], "token": p["baseToken"]["address"], "liq": liq,
                         "url": p.get("url", "")})
            held.add(p["baseToken"]["address"])
            notify(f"{self.icon} {self.name} (simulace) koupil {sym} za ${usd:.0f}, likvidita ${liq:,.0f} - {p.get('url','')}")


class HunterAgent(DegenAgent):
    """LOVEC - simulace: zavedené meme coiny na Solaně, o kterých se mluví (GeckoTerminal, DexScreener,
    X, 4chan…). Žádné čerstvé tokeny: stáří 3-365 dní, likvidita od $250k, kupci převažují, cena roste
    v hodině i za 6 h. Cíl +25 %, stop -10 %, max 2 dny. Menší skluz než Degen, protože pooly jsou hlubší."""
    name = "Lovec"
    asset_class = "dexmeme"
    icon = "🏹"
    use_fresh = False
    trend_bonus = 0.0             # kandidáti jsou jen z radaru, bonus nedává smysl
    position_usd = 100.0          # 10 % peněženky
    max_positions = 4
    min_liquidity = 250_000
    max_fdv = 2_000_000_000
    min_age_min, max_age_min = 3 * 24 * 60, 365 * 24 * 60
    take_profit, stop_loss = 0.25, 0.10
    max_hold_min = 48 * 60
    rug_liquidity_drop = 0.4
    cooldown_min = 6 * 60

    def score(self, p: dict, now_ms: int) -> float | None:
        try:
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            fdv = float(p.get("fdv") or p.get("marketCap") or 0)
            age = (now_ms - int(p.get("pairCreatedAt") or now_ms)) / 60000
            tx = (p.get("txns") or {}).get("h1") or {}
            buys, sells = int(tx.get("buys", 0)), int(tx.get("sells", 0))
            ch = p.get("priceChange") or {}
            h1, h6, h24 = float(ch.get("h1") or 0), float(ch.get("h6") or 0), float(ch.get("h24") or 0)
            vol_h1 = float((p.get("volume") or {}).get("h1") or 0)
        except (TypeError, ValueError):
            return None
        if (p.get("quoteToken") or {}).get("symbol") not in ("SOL", "USDC", "WSOL", "USDT"):
            return None
        if liq < self.min_liquidity or not 0 < fdv <= self.max_fdv:
            return None
        if not self.min_age_min <= age <= self.max_age_min:
            return None
        if buys < 50 or buys < 1.2 * max(sells, 1):
            return None
        if not (1 < h1 < 25) or h6 <= 0 or h24 > 150:   # roste, ale není po svislé raketě
            return None
        if vol_h1 < 0.03 * liq:
            return None
        return buys / max(sells, 1) + h1 / 10 + min(h6, 50) / 50
