"""Peněženka jednoho bota: hotovost, pozice, poplatky, historie a zdraví."""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from . import config


def utcnow():
    return datetime.now(timezone.utc)


@dataclass
class Lot:
    symbol: str
    qty: float
    entry: float                 # průměrná nákupní cena (po skluzu)
    stop: float
    target: float | None
    trail_pct: float | None      # trailing stop v % od nejvyšší ceny
    opened: str                  # ISO čas
    max_hold_min: int | None
    high: float = 0.0            # nejvyšší cena od nákupu
    cost: float = 0.0            # kolik dolarů stál nákup vč. poplatku
    reason: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class Wallet:
    name: str
    asset_class: str             # stock / crypto / meme / degen
    cash: float = config.START_CAPITAL
    start: float = config.START_CAPITAL
    born: str = field(default_factory=lambda: utcnow().isoformat())
    status: str = "ALIVE"        # ALIVE / INJURED / DEAD
    cause_of_death: str = ""
    died: str = ""
    lots: dict = field(default_factory=dict)      # symbol -> Lot
    trades: list = field(default_factory=list)    # uzavřené obchody
    fees_paid: float = 0.0
    rent_paid: float = 0.0
    day: str = ""                # aktuální den (UTC datum) pro denní limit
    day_start_equity: float = config.START_CAPITAL
    peak_equity: float = config.START_CAPITAL
    max_drawdown: float = 0.0
    last_prices: dict = field(default_factory=dict)
    equity_curve: list = field(default_factory=list)  # [(iso, equity)] max ~1 bod/hod

    # ---------------------------------------------------------------- účetnictví
    @property
    def costs(self):
        return config.COSTS[self.asset_class]

    def equity(self, prices: dict | None = None) -> float:
        prices = prices or {}
        val = self.cash
        for s, lot in self.lots.items():
            p = prices.get(s) or self.last_prices.get(s) or lot.entry
            val += lot.qty * p
        return val

    def mark(self, prices: dict, now: datetime | None = None):
        """Zapíše poslední ceny, aktualizuje drawdown a křivku."""
        self.last_prices.update({k: v for k, v in prices.items() if v})
        eq = self.equity()
        self.peak_equity = max(self.peak_equity, eq)
        if self.peak_equity > 0:
            self.max_drawdown = max(self.max_drawdown, 1 - eq / self.peak_equity)
        now = now or utcnow()
        stamp = now.strftime("%Y-%m-%dT%H")
        if not self.equity_curve or self.equity_curve[-1][0][:13] != stamp:
            self.equity_curve.append((now.isoformat(timespec="minutes"), round(eq, 2)))
        else:
            self.equity_curve[-1] = (self.equity_curve[-1][0], round(eq, 2))
        return eq

    def open(self, symbol, qty, fill_price, *, stop, target=None, trail_pct=None,
             max_hold_min=None, reason="", now=None, meta=None, apply_slippage=True):
        """Zaúčtuje nákup. fill_price = skutečná cena plnění (live) nebo tržní cena (simulace)."""
        price = fill_price * (1 + self.costs["slip"]) if apply_slippage else fill_price
        gross = qty * price
        fee = gross * self.costs["fee"]
        if gross + fee > self.cash + 1e-6:
            raise ValueError(f"{self.name}: málo hotovosti na {symbol}")
        self.cash -= gross + fee
        self.fees_paid += fee
        self.lots[symbol] = Lot(symbol, qty, price, stop, target, trail_pct,
                                (now or utcnow()).isoformat(), max_hold_min,
                                high=price, cost=gross + fee, reason=reason, meta=meta or {})
        return self.lots[symbol]

    def close(self, symbol, fill_price, why, now=None, apply_slippage=True, qty=None):
        lot = self.lots.pop(symbol)
        q = lot.qty if qty is None else min(qty, lot.qty)
        price = fill_price * (1 - self.costs["slip"]) if apply_slippage else fill_price
        gross = q * price
        fee = gross * self.costs["fee"]
        self.cash += gross - fee
        self.fees_paid += fee
        cost = lot.cost * (q / lot.qty)
        pnl = gross - fee - cost
        t = {"symbol": symbol, "opened": lot.opened, "closed": (now or utcnow()).isoformat(),
             "entry": lot.entry, "exit": price, "qty": q, "pnl": round(pnl, 4),
             "ret": pnl / cost if cost else 0, "why": why, "reason": lot.reason}
        for k in ("pair", "market", "side", "p0", "features"):          # pro grafy v dashboardu (DEX pool, Polymarket trh, short)
            if k in (lot.meta or {}):
                t[k] = lot.meta[k]
        self.trades.append(t)
        return t

    # ---------------------------------------------------------------- serializace
    def to_dict(self):
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d):
        d = dict(d)
        d["lots"] = {k: Lot(**v) for k, v in d.get("lots", {}).items()}
        d["equity_curve"] = [tuple(x) for x in d.get("equity_curve", [])]
        return cls(**d)

    # ---------------------------------------------------------------- statistiky
    def stats(self):
        tr = self.trades
        wins = [t for t in tr if t["pnl"] > 0]
        losses = [t for t in tr if t["pnl"] <= 0]
        gp = sum(t["pnl"] for t in wins)
        gl = -sum(t["pnl"] for t in losses)
        eq = self.equity()
        return {
            "name": self.name, "status": self.status, "equity": round(eq, 2),
            "return": eq / self.start - 1, "trades": len(tr),
            "win_rate": len(wins) / len(tr) if tr else 0,
            "profit_factor": (gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0),
            "avg_win": gp / len(wins) if wins else 0,
            "avg_loss": -gl / len(losses) if losses else 0,
            "fees": round(self.fees_paid, 2), "rent": round(self.rent_paid, 2),
            "max_drawdown": self.max_drawdown, "open_positions": len(self.lots),
            "cause_of_death": self.cause_of_death,
        }
