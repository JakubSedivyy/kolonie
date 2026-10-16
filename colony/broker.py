"""Brokeři: Alpaca (paper/live) a simulace. Vrací skutečné plnění (množství, cena)."""
from __future__ import annotations
import logging
import math
import time

from . import config

log = logging.getLogger("colony.broker")


class Fill:
    def __init__(self, qty, price, order_id=""):
        self.qty, self.price, self.order_id = qty, price, order_id

    def __repr__(self):
        return f"Fill({self.qty:.6g} @ {self.price:.6g})"


class SimBroker:
    """Plní okamžitě za zadanou cenu. Skluz a poplatky počítá peněženka."""
    simulated = True

    def buy_usd(self, symbol, usd, price, crypto=False):
        return Fill(usd / price, price, "sim")

    def sell(self, symbol, qty, price, crypto=False):
        return Fill(qty, price, "sim")

    def short_sell(self, symbol, usd, price):
        return Fill(usd / price, price, "sim")

    def cover(self, symbol, qty, price):
        return Fill(qty, price, "sim")


class AlpacaBroker:
    simulated = False   # skutečné plnění z Alpaky (paper nebo live)

    def __init__(self):
        from alpaca.trading.client import TradingClient
        paper = not config.IS_LIVE
        self.tc = TradingClient(config.ALPACA_KEY, config.ALPACA_SECRET, paper=paper)
        self.paper = paper
        self._fractionable = {}

    def clock(self):
        return self.tc.get_clock()

    def _wait_fill(self, order_id, timeout=30):
        from alpaca.trading.enums import OrderStatus
        t0 = time.time()
        while time.time() - t0 < timeout:
            o = self.tc.get_order_by_id(order_id)
            if o.status == OrderStatus.FILLED:
                return Fill(float(o.filled_qty), float(o.filled_avg_price), str(o.id))
            if o.status in (OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
                log.warning("order %s %s", order_id, o.status)
                return None
            time.sleep(1)
        # částečně vyplněno nebo pořád čeká -> zrušit zbytek, vzít co je
        try:
            self.tc.cancel_order_by_id(order_id)
        except Exception:
            pass
        o = self.tc.get_order_by_id(order_id)
        q = float(o.filled_qty or 0)
        return Fill(q, float(o.filled_avg_price), str(o.id)) if q > 0 else None

    def _is_fractionable(self, symbol):
        if symbol not in self._fractionable:
            try:
                self._fractionable[symbol] = bool(self.tc.get_asset(symbol).fractionable)
            except Exception:
                self._fractionable[symbol] = False
        return self._fractionable[symbol]

    def buy_usd(self, symbol, usd, price, crypto=False):
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        usd = math.floor(usd * 100) / 100
        if crypto:
            req = MarketOrderRequest(symbol=symbol, notional=usd, side=OrderSide.BUY, time_in_force=TimeInForce.GTC)
        elif self._is_fractionable(symbol):
            req = MarketOrderRequest(symbol=symbol, notional=usd, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
        else:
            qty = math.floor(usd / price)
            if qty < 1:
                return None
            req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
        o = self.tc.submit_order(req)
        return self._wait_fill(o.id)

    def position_qty(self, symbol):
        key = symbol.replace("/", "")
        for p in self.tc.get_all_positions():
            if p.symbol.replace("/", "") == key:
                return float(p.qty_available if getattr(p, "qty_available", None) is not None else p.qty)
        return 0.0

    def sell(self, symbol, qty, price, crypto=False):
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        have = self.position_qty(symbol)
        qty = min(qty, have)       # u krypta Alpaca strhává poplatek v coinu -> drží o kousek míň
        if qty <= 0:
            log.warning("sell %s: na účtu nic není", symbol)
            return Fill(0, price, "none")
        tif = TimeInForce.GTC if crypto else TimeInForce.DAY
        o = self.tc.submit_order(MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=tif))
        return self._wait_fill(o.id)

    # ------------------------------------------------------------------ short (jen akcie; krypto short Alpaca neumí)
    def short_sell(self, symbol, usd, price):
        """Prodej nakrátko: celé akcie, jen u akcií, které jde půjčit (easy to borrow)."""
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        try:
            a = self.tc.get_asset(symbol)
            if not (a.shortable and a.easy_to_borrow):
                log.info("short %s: akcii nejde půjčit", symbol)
                return None
        except Exception as e:
            log.warning("short %s asset: %s", symbol, e)
            return None
        qty = math.floor(usd / price)
        if qty < 1:
            return None
        o = self.tc.submit_order(MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
        return self._wait_fill(o.id)

    def cover(self, symbol, qty, price):
        """Zpětný nákup shortu (na účtu je záporná pozice)."""
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        have = -self.position_qty(symbol)            # short = záporné množství
        qty = min(qty, have)
        if qty <= 0:
            log.warning("cover %s: na účtu žádný short", symbol)
            return Fill(0, price, "none")
        o = self.tc.submit_order(MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
        return self._wait_fill(o.id)
