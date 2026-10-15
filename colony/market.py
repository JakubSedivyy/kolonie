"""Tržní data: Alpaca (akcie + krypto, zdarma) a DexScreener (nové meme coiny)."""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

from . import config

log = logging.getLogger("colony.market")

# Známé meme coiny - bot z nich vezme ty, které Alpaka právě nabízí
MEME_COINS = {"DOGE", "SHIB", "PEPE", "TRUMP", "BONK", "WIF", "FLOKI", "POPCAT", "FARTCOIN",
              "PENGU", "MOODENG", "PNUT", "BRETT", "MEW", "TURBO", "NEIRO", "SPX", "GOAT"}
MAJOR_COINS = {"BTC", "ETH", "SOL", "XRP", "LTC", "LINK", "AVAX", "DOT", "UNI", "AAVE",
               "BCH", "ADA", "XTZ", "SUSHI", "CRV", "GRT", "MKR", "YFI", "BAT", "SUI", "HBAR"}


def _tf(minutes: int):
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    if minutes % 1440 == 0:
        return TimeFrame(minutes // 1440, TimeFrameUnit.Day)
    if minutes % 60 == 0:
        return TimeFrame(minutes // 60, TimeFrameUnit.Hour)
    return TimeFrame(minutes, TimeFrameUnit.Minute)


def _split(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    out = {}
    if df is None or df.empty:
        return out
    for sym, g in df.groupby(level=0):
        g = g.droplevel(0).sort_index()
        out[sym] = g[["open", "high", "low", "close", "volume"]].astype(float)
    return out


class AlpacaData:
    def __init__(self):
        from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
        self.stocks = StockHistoricalDataClient(config.ALPACA_KEY, config.ALPACA_SECRET)
        self.crypto = CryptoHistoricalDataClient(config.ALPACA_KEY or None, config.ALPACA_SECRET or None)

    # ------------------------------------------------------------------ svíčky
    def bars(self, symbols, minutes, start: datetime, end: datetime | None = None, crypto=False):
        if not symbols:
            return {}
        from alpaca.data.requests import StockBarsRequest, CryptoBarsRequest
        from alpaca.data.enums import DataFeed
        if crypto:
            req = CryptoBarsRequest(symbol_or_symbols=list(symbols), timeframe=_tf(minutes), start=start, end=end)
            return _split(self.crypto.get_crypto_bars(req).df)
        req = StockBarsRequest(symbol_or_symbols=list(symbols), timeframe=_tf(minutes), start=start, end=end,
                               feed=DataFeed.IEX)   # IEX = zdarma
        return _split(self.stocks.get_stock_bars(req).df)

    def latest_prices(self, symbols, crypto=False) -> dict[str, float]:
        if not symbols:
            return {}
        try:
            if crypto:
                from alpaca.data.requests import CryptoLatestTradeRequest
                r = self.crypto.get_crypto_latest_trade(CryptoLatestTradeRequest(symbol_or_symbols=list(symbols)))
            else:
                from alpaca.data.requests import StockLatestTradeRequest
                from alpaca.data.enums import DataFeed
                r = self.stocks.get_stock_latest_trade(
                    StockLatestTradeRequest(symbol_or_symbols=list(symbols), feed=DataFeed.IEX))
            return {s: float(t.price) for s, t in r.items()}
        except Exception as e:
            log.warning("latest prices failed: %s", e)
            return {}

    # ------------------------------------------------------------------ skener trhu
    def stock_movers(self, top=20) -> list[str]:
        """Nejobchodovanější akcie a největší růsty dne (Alpaca screener)."""
        syms = []
        try:
            from alpaca.data.historical.screener import ScreenerClient
            from alpaca.data.requests import MostActivesRequest, MarketMoversRequest
            sc = ScreenerClient(config.ALPACA_KEY, config.ALPACA_SECRET)
            act = sc.get_most_actives(MostActivesRequest(top=top))
            syms += [a.symbol for a in act.most_actives]
            mov = sc.get_market_movers(MarketMoversRequest(top=top))
            syms += [g.symbol for g in mov.gainers if 5 <= float(g.price) <= 1500]
        except Exception as e:
            log.warning("screener failed: %s", e)
        seen, out = set(), []
        for s in syms:
            if s not in seen and s.isalpha():
                seen.add(s); out.append(s)
        return out


class AlpacaAssets:
    def __init__(self, trading_client):
        self.tc = trading_client
        self._crypto = None

    def crypto_symbols(self) -> list[str]:
        if self._crypto is None:
            from alpaca.trading.requests import GetAssetsRequest
            from alpaca.trading.enums import AssetClass, AssetStatus
            assets = self.tc.get_all_assets(GetAssetsRequest(asset_class=AssetClass.CRYPTO, status=AssetStatus.ACTIVE))
            self._crypto = [a.symbol for a in assets if a.tradable and a.symbol.endswith("/USD")]
        return self._crypto

    def memes(self):
        return [s for s in self.crypto_symbols() if s.split("/")[0] in MEME_COINS]

    def majors(self):
        return [s for s in self.crypto_symbols() if s.split("/")[0] in MAJOR_COINS]


# ---------------------------------------------------------------------- DexScreener
class DexScreener:
    """Veřejné API bez klíče. Limit cca 60 dotazů/min na profily, 300/min na páry."""
    BASE = "https://api.dexscreener.com"

    def __init__(self, chain="solana"):
        self.chain = chain
        self.s = requests.Session()

    def _get(self, path):
        r = self.s.get(self.BASE + path, timeout=20)
        r.raise_for_status()
        return r.json()

    def fresh_tokens(self) -> list[str]:
        """Adresy tokenů, které se právě objevily (nové profily + boosty)."""
        addrs = []
        for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
            try:
                for t in self._get(path) or []:
                    if t.get("chainId") == self.chain and t.get("tokenAddress"):
                        addrs.append(t["tokenAddress"])
            except Exception as e:
                log.warning("dexscreener %s: %s", path, e)
        return list(dict.fromkeys(addrs))

    def pairs_for_tokens(self, addrs: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(addrs), 30):
            chunk = ",".join(addrs[i:i + 30])
            try:
                out += self._get(f"/tokens/v1/{self.chain}/{chunk}") or []
            except Exception as e:
                log.warning("dexscreener tokens: %s", e)
        return out

    def pair(self, pair_address: str) -> dict | None:
        try:
            d = self._get(f"/latest/dex/pairs/{self.chain}/{pair_address}")
            ps = d.get("pairs") or ([d["pair"]] if d.get("pair") else [])
            return ps[0] if ps else None
        except Exception as e:
            log.warning("dexscreener pair: %s", e)
            return None
