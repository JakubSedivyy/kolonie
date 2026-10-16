"""Tři boti obchodující přes Alpaku. Každý má vlastní trh, aby si nelezli do pozic."""
from __future__ import annotations
from datetime import datetime, timedelta, time as dtime

import numpy as np
import pandas as pd

from .base import BarAgent, Entry
from ..indicators import ema, atr, rsi, session_vwap

NY = "America/New_York"


class OrbitAgent(BarAgent):
    """ORBIT - akcie, Opening Range Breakout.
    Prvních 15 minut po otevření burzy změří rozpětí. Když cena rozpětí prorazí nahoru
    se silným objemem a nad VWAP, koupí. Stop pod rozpětím, cíl 2x riziko.
    Před koncem obchodování všechno prodá (žádné držení přes noc)."""
    name = "Orbit"
    asset_class = "stock"
    minutes = 5
    lookback_bars = 120
    risk_per_trade = 0.01
    max_position_frac = 0.30
    max_positions = 3
    or_bars = 3                  # 3 x 5 min = 15 min
    vol_mult = 1.5
    max_stop_pct = 0.025
    rr = 2.0
    last_entry = dtime(11, 30)   # do kdy smí vstoupit
    core = ["SPY", "QQQ"]
    default_watch = ["NVDA", "TSLA", "AMD", "AAPL", "META", "AMZN", "PLTR", "COIN", "MSTR", "SMCI"]

    def __init__(self, wallet=None):
        super().__init__(wallet)
        self.traded_today: dict[str, str] = {}

    def universe(self, ctx):
        movers = ctx.get("stock_movers") or self.default_watch
        return list(dict.fromkeys(self.core + movers[:10]))

    def prepare(self, df):
        df = df.copy()
        ny = df.index.tz_convert(NY)
        df["t"] = ny.time
        df["day"] = ny.date
        df = df[(df["t"] >= dtime(9, 30)) & (df["t"] < dtime(16, 0))]
        if df.empty:
            return df
        df["bar_no"] = df.groupby("day").cumcount()
        first = df[df["bar_no"] < self.or_bars].groupby("day")
        df["or_high"] = df["day"].map(first["high"].max())
        df["or_low"] = df["day"].map(first["low"].min())
        df["or_vol"] = df["day"].map(first["volume"].mean())
        df["vwap"] = session_vwap(df, df["day"])
        # běžný objem posledních hodin (ranní rozpětí má vždy obří objem, srovnání s ním skoro nikdy neprošlo)
        df["vol_ref"] = df["volume"].rolling(12, min_periods=4).mean().shift(1)
        return df

    def can_enter(self, ts):
        t = ts.tz_convert(NY).time()
        first = (datetime.combine(datetime(2000, 1, 1), dtime(9, 30)) + timedelta(minutes=5 * self.or_bars)).time()
        return first <= t <= self.last_entry

    def must_flatten(self, ts):
        return ts.tz_convert(NY).time() >= dtime(15, 50)

    def entry(self, df, i, ctx):
        r = df.iloc[i]
        if r["bar_no"] < self.or_bars or pd.isna(r["or_high"]):
            return None
        key = f'{r["day"]}'
        sym = ctx.get("symbol")
        if self.traded_today.get(sym) == key:
            return None
        prev_close = df["close"].iloc[i - 1] if i > 0 else r["close"]
        broke = r["close"] > r["or_high"] and prev_close <= r["or_high"]
        vol_ref = r["vol_ref"] if not pd.isna(r["vol_ref"]) else r["or_vol"]
        if not (broke and r["close"] > r["vwap"] and r["volume"] > self.vol_mult * vol_ref):
            return None
        stop = r["or_low"]
        risk = (r["close"] - stop) / r["close"]
        if risk > self.max_stop_pct:            # moc široké rozpětí -> stop v půlce
            stop = (r["or_high"] + r["or_low"]) / 2
            risk = (r["close"] - stop) / r["close"]
            if risk > self.max_stop_pct or risk <= 0:
                return None
        self.traded_today[sym] = key
        return Entry(stop=stop, target=r["close"] * (1 + self.rr * risk),
                     reason=f"průraz ranního rozpětí {r['or_high']:.2f}, objem {r['volume']/vol_ref:.1f}x běžného")

    can_short = True

    def short_entry(self, df, i, ctx):
        """Zrcadlo průrazu: cena propadne pod ranní minimum se silným objemem a pod VWAP -> short."""
        r = df.iloc[i]
        if r["bar_no"] < self.or_bars or pd.isna(r["or_low"]):
            return None
        key, sym = f'{r["day"]}', ctx.get("symbol")
        if self.traded_today.get(sym) == key:
            return None
        prev_close = df["close"].iloc[i - 1] if i > 0 else r["close"]
        vol_ref = r["vol_ref"] if not pd.isna(r["vol_ref"]) else r["or_vol"]
        broke = r["close"] < r["or_low"] and prev_close >= r["or_low"]
        if not (broke and r["close"] < r["vwap"] and r["volume"] > self.vol_mult * vol_ref):
            return None
        stop = r["or_high"]
        risk = (stop - r["close"]) / r["close"]
        if risk > self.max_stop_pct:
            stop = (r["or_high"] + r["or_low"]) / 2
            risk = (stop - r["close"]) / r["close"]
            if risk > self.max_stop_pct or risk <= 0:
                return None
        self.traded_today[sym] = key
        return Entry(stop=stop, target=r["close"] * (1 - self.rr * risk),
                     reason=f"propad pod ranní minimum {r['or_low']:.2f}, objem {r['volume']/vol_ref:.1f}x běžného")


class TrendAgent(BarAgent):
    """TRENDY - velké kryptoměny 24/7, sleduje trend.
    Režim "cross": koupí, když EMA 20 překříží EMA 50 nad EMA 200.
    Režim "donchian": koupí průraz maxima posledních N svíček nad EMA 200 (méně obchodů, delší trendy).
    Stop 2.5x ATR + trailing. Laboratoř vybírá režim, délku svíček a počet coinů podle výsledků."""
    name = "Trendy"
    asset_class = "crypto"
    crypto = True
    minutes = 60
    lookback_bars = 400
    risk_per_trade = 0.01
    max_position_frac = 0.25
    max_positions = 3
    atr_mult = 2.5
    mode = "donchian"
    donchian_n = 48
    top_n = 3
    preferred = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "LINK/USD", "AVAX/USD", "LTC/USD", "DOT/USD"]

    def universe(self, ctx):
        avail = ctx.get("crypto_majors") or self.preferred
        return [s for s in self.preferred if s in avail][: self.top_n] or avail[: self.top_n]

    def prepare(self, df):
        df = df.copy()
        df["ema20"], df["ema50"], df["ema200"] = ema(df["close"], 20), ema(df["close"], 50), ema(df["close"], 200)
        df["atr"] = atr(df, 14)
        df["hh"] = df["high"].rolling(self.donchian_n).max().shift(1)
        df["ll"] = df["low"].rolling(max(2, self.donchian_n // 2)).min().shift(1)
        return df

    def entry(self, df, i, ctx):
        if i < 210:
            return None
        r, p = df.iloc[i], df.iloc[i - 1]
        if r["close"] <= r["ema200"]:
            return None
        if self.mode == "cross":
            sig = p["ema20"] <= p["ema50"] and r["ema20"] > r["ema50"]
            why = "EMA20 překřížila EMA50"
        else:
            sig = r["close"] > r["hh"] and p["close"] <= p["hh"]
            why = f"průraz {self.donchian_n}-svíčkového maxima"
        if not sig:
            return None
        atr_pct = r["atr"] / r["close"]
        if not (0.002 <= atr_pct <= 0.04):
            return None
        stop_pct = self.atr_mult * atr_pct
        return Entry(stop=r["close"] * (1 - stop_pct), trail_pct=stop_pct, max_hold_min=7 * 24 * 60,
                     reason=f"{why}, ATR {atr_pct*100:.2f} %")

    def exit_signal(self, df, i, lot):
        r = df.iloc[i]
        if self.mode == "cross" and r["ema20"] < r["ema50"]:
            return "trend se otočil"
        if self.mode == "donchian" and r["close"] < r["ll"]:
            return "pád pod minimum kanálu"
        return None

    can_short = True

    def short_entry(self, df, i, ctx):
        """Zrcadlo: sestupný trend pod EMA 200 a propad pod minimum kanálu (nebo EMA20 pod EMA50)."""
        if i < 210:
            return None
        r, p = df.iloc[i], df.iloc[i - 1]
        if r["close"] >= r["ema200"]:
            return None
        lo = df["low"].rolling(self.donchian_n).min().shift(1)
        if self.mode == "cross":
            sig, why = p["ema20"] >= p["ema50"] and r["ema20"] < r["ema50"], "EMA20 klesla pod EMA50"
        else:
            sig, why = r["close"] < lo.iloc[i] and p["close"] >= lo.iloc[i - 1], f"propad pod {self.donchian_n}-svíčkové minimum"
        if not sig:
            return None
        atr_pct = r["atr"] / r["close"]
        if not (0.002 <= atr_pct <= 0.04):
            return None
        stop_pct = self.atr_mult * atr_pct
        return Entry(stop=r["close"] * (1 + stop_pct), trail_pct=stop_pct, max_hold_min=7 * 24 * 60,
                     reason=f"{why} pod EMA 200, ATR {atr_pct*100:.2f} %")

    def short_exit_signal(self, df, i, lot):
        r = df.iloc[i]
        hh = df["high"].rolling(max(2, self.donchian_n // 2)).max().shift(1).iloc[i]
        if self.mode == "cross" and r["ema20"] > r["ema50"]:
            return "trend se otočil nahoru"
        if self.mode == "donchian" and r["close"] > hh:
            return "průraz nad maximum kanálu"
        return None


class MemeAgent(BarAgent):
    """MEMEC - meme coiny na Alpace (DOGE, SHIB, PEPE…), 5min svíčky.
    Hledá náhlý výbuch objemu (3x víc než průměr za 24 h) spolu s průrazem 2hodinového maxima.
    Rychle dovnitř, rychle ven: stop -4 %, trailing 4 %, cíl +10 %, max 4 hodiny."""
    name = "Memec"
    asset_class = "meme"
    crypto = True
    minutes = 5
    lookback_bars = 320
    risk_per_trade = 0.01
    max_position_frac = 0.25
    max_positions = 3
    trades_in_risk_off = False
    vol_mult = 3.0
    stop_pct = 0.04
    target_pct = 0.10

    hot_vol_mult = 2.0           # coin, o kterém se mluví na internetu, stačí menší výbuch objemu

    def universe(self, ctx):
        memes = ctx.get("crypto_memes") or ["DOGE/USD", "SHIB/USD", "PEPE/USD"]
        radar = ctx.get("radar")
        if radar:                                         # nejdřív ty, co jsou zrovna v trendu
            memes = sorted(memes, key=lambda s: -radar.score(s))
        return memes

    def prepare(self, df):
        df = df.copy()
        df["vol_avg"] = df["volume"].rolling(288, min_periods=100).mean().shift(1)
        df["hh2h"] = df["high"].rolling(24).max().shift(1)
        df["rsi"] = rsi(df["close"], 14)
        return df

    def entry(self, df, i, ctx):
        r = df.iloc[i]
        if pd.isna(r["vol_avg"]) or r["vol_avg"] <= 0 or pd.isna(r["hh2h"]):
            return None
        hype = ctx["radar"].score(ctx.get("symbol", "")) if ctx.get("radar") else 0.0
        mult = self.hot_vol_mult if hype >= 3 else self.vol_mult
        if r["volume"] > mult * r["vol_avg"] and r["close"] > r["hh2h"] \
                and r["close"] > r["open"] and r["rsi"] < 85:
            why = f"objem {r['volume']/r['vol_avg']:.1f}x průměru, nové 2h maximum"
            if hype >= 3:
                why += f", v trendu na internetu (hype {hype:.1f})"
            return Entry(stop=r["close"] * (1 - self.stop_pct), target=r["close"] * (1 + self.target_pct),
                         trail_pct=self.stop_pct, max_hold_min=240, reason=why)
        return None

    can_short = True

    def short_entry(self, df, i, ctx):
        """Zrcadlo: výplach - obří objem, nové 2h minimum, červená svíčka -> short (simulace)."""
        r = df.iloc[i]
        if pd.isna(r["vol_avg"]) or r["vol_avg"] <= 0:
            return None
        ll2h = df["low"].rolling(24).min().shift(1).iloc[i]
        if pd.isna(ll2h):
            return None
        if r["volume"] > self.vol_mult * r["vol_avg"] and r["close"] < ll2h and r["close"] < r["open"] and r["rsi"] > 15:
            return Entry(stop=r["close"] * (1 + self.stop_pct), target=r["close"] * (1 - self.target_pct),
                         trail_pct=self.stop_pct, max_hold_min=240,
                         reason=f"výplach: objem {r['volume']/r['vol_avg']:.1f}x průměru, nové 2h minimum")
        return None


class WaveAgent(BarAgent):
    """VLNA - swing trading na 3-7 dní, krypto i meme coiny, 1h svíčky.
    Naskočí na vlnu, když je coin v trendu na internetu (nebo prorazí 3denní maximum)
    a cena je v rostoucím trendu, ale ještě není přehřátá. Stop 3x ATR (6-15 %),
    trailing stop, nejdéle 7 dní, prodá i při otočení trendu."""
    name = "Vlna"
    asset_class = "crypto"
    crypto = True
    minutes = 60
    lookback_bars = 300
    risk_per_trade = 0.015
    max_position_frac = 0.30
    max_positions = 3
    trades_in_risk_off = False
    min_hold_hours = 24          # otočení trendu řeší až po prvním dni, ať ji nevyhodí šum
    atr_mult = 3.0

    def universe(self, ctx):
        syms = list(dict.fromkeys((ctx.get("crypto_majors") or []) + (ctx.get("crypto_memes") or [])))
        if not syms:
            syms = ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD"]
        radar = ctx.get("radar")
        return sorted(syms, key=lambda s: -radar.score(s)) if radar else syms

    def prepare(self, df):
        df = df.copy()
        df["ema20"], df["ema50"] = ema(df["close"], 20), ema(df["close"], 50)
        df["atr"] = atr(df, 14)
        df["ret24"] = df["close"] / df["close"].shift(24) - 1
        df["hh72"] = df["high"].rolling(72).max().shift(1)
        df["rsi"] = rsi(df["close"], 14)
        return df

    def entry(self, df, i, ctx):
        if i < 80:
            return None
        r = df.iloc[i]
        hype = ctx["radar"].score(ctx.get("symbol", "")) if ctx.get("radar") else 0.0
        uptrend = r["close"] > r["ema50"] and r["ema20"] > r["ema50"]
        not_hot = 0.02 <= r["ret24"] <= 0.40 and r["rsi"] < 78      # roste, ale není to kolmice
        breakout = r["close"] > r["hh72"]
        if not (uptrend and not_hot and (hype >= 3 or breakout)):
            return None
        stop_pct = min(0.15, max(0.06, self.atr_mult * r["atr"] / r["close"]))
        why = f"hype {hype:.1f}" if hype >= 3 else "průraz 3denního maxima"
        return Entry(stop=r["close"] * (1 - stop_pct), trail_pct=stop_pct, max_hold_min=7 * 24 * 60,
                     reason=f"vlna: {why}, +{r['ret24']*100:.0f} % za 24 h")

    def exit_signal(self, df, i, lot):
        from datetime import datetime
        held_h = (df.index[i].to_pydatetime() - datetime.fromisoformat(lot.opened)).total_seconds() / 3600
        r = df.iloc[i]
        if held_h >= self.min_hold_hours and r["ema20"] < r["ema50"] and r["close"] < r["ema50"]:
            return "vlna opadla (trend se otočil)"
        return None

    can_short = True

    def short_entry(self, df, i, ctx):
        """Zrcadlo: sestupná vlna - pod EMA 50, EMA20 pod EMA50, propad pod 3denní minimum, ještě ne vyprodané."""
        if i < 80:
            return None
        r = df.iloc[i]
        ll72 = df["low"].rolling(72).min().shift(1).iloc[i]
        downtrend = r["close"] < r["ema50"] and r["ema20"] < r["ema50"]
        not_sold_out = -0.40 <= r["ret24"] <= -0.02 and r["rsi"] > 22
        if not (downtrend and not_sold_out and r["close"] < ll72):
            return None
        stop_pct = min(0.15, max(0.06, self.atr_mult * r["atr"] / r["close"]))
        return Entry(stop=r["close"] * (1 + stop_pct), trail_pct=stop_pct, max_hold_min=7 * 24 * 60,
                     reason=f"sestupná vlna: propad pod 3denní minimum, {r['ret24']*100:.0f} % za 24 h")

    def short_exit_signal(self, df, i, lot):
        from datetime import datetime
        held_h = (df.index[i].to_pydatetime() - datetime.fromisoformat(lot.opened)).total_seconds() / 3600
        r = df.iloc[i]
        if held_h >= self.min_hold_hours and r["ema20"] > r["ema50"] and r["close"] > r["ema50"]:
            return "sestupná vlna skončila (trend se otočil nahoru)"
        return None


class WhisperAgent(BarAgent):
    """ŠEPTAČ - meme coiny na Alpace, které právě rozjíždí internet. 15min svíčky.
    Kupuje jen když o coinu mluví aspoň 2 různé zdroje (X přes Groka, 4chan, StockTwits, Telegram,
    CoinGecko…) a cena to potvrzuje (nad EMA 20 i 50, roste poslední hodinu, ještě není přehřátá).
    Stop 2x ATR (3-10 %), trailing, nejdéle 24 h, prodá když cena spadne pod EMA 50."""
    name = "Šeptač"
    asset_class = "meme"
    crypto = True
    minutes = 15
    lookback_bars = 200
    risk_per_trade = 0.012
    max_position_frac = 0.30
    max_positions = 3
    trades_in_risk_off = False
    min_hype = 4.0
    min_sources = 2
    atr_mult = 2.0

    def universe(self, ctx):
        memes = ctx.get("crypto_memes") or ["DOGE/USD", "SHIB/USD", "PEPE/USD"]
        radar = ctx.get("radar")
        return sorted(memes, key=lambda s: -radar.score(s)) if radar else memes

    def prepare(self, df):
        df = df.copy()
        df["ema20"], df["ema50"] = ema(df["close"], 20), ema(df["close"], 50)
        df["atr"] = atr(df, 14)
        df["rsi"] = rsi(df["close"], 14)
        df["ret1h"] = df["close"] / df["close"].shift(4) - 1
        return df

    @staticmethod
    def _sources(radar, symbol):
        try:
            base = symbol.split("/")[0].upper()
            src = (radar.hot.get(base) or {}).get("sources", [])
            return len({s.split(":")[0].split(" (")[0].split(" ")[0] for s in src})
        except Exception:
            return 0

    def entry(self, df, i, ctx):
        radar = ctx.get("radar")
        if not radar or i < 60:
            return None
        s = ctx.get("symbol", "")
        hype = radar.score(s)
        n = self._sources(radar, s)
        if hype < self.min_hype or n < self.min_sources:
            return None
        r = df.iloc[i]
        if not (r["close"] > r["ema20"] > r["ema50"] and 0.005 <= r["ret1h"] <= 0.12 and r["rsi"] < 75):
            return None
        stop_pct = min(0.10, max(0.03, self.atr_mult * r["atr"] / r["close"]))
        return Entry(stop=r["close"] * (1 - stop_pct), trail_pct=stop_pct, max_hold_min=24 * 60,
                     reason=f"šeptá se o něm: hype {hype:.1f} z {n} zdrojů, +{r['ret1h']*100:.1f} % za hodinu")

    def exit_signal(self, df, i, lot):
        r = df.iloc[i]
        if r["close"] < r["ema50"]:
            return "hype vyprchal (cena pod EMA 50)"
        return None


class BearAgent(BarAgent):
    """MEDVĚD - vydělává, když trh padá. Kupuje inverzní ETF (rostou, když index klesá):
    SQQQ (Nasdaq ×3 dolů), SPXS (S&P ×3), SOXS (čipy ×3), SDOW (Dow ×3), TZA (malé firmy ×3), SH, PSQ.
    Vstup jen když pád potvrzuje trend (cena ETF nad EMA 20 i 50, roste poslední hodinu).
    Stop 2x ATR, trailing, nejdéle 3 dny (pákové ETF dlouhým držením ztrácejí)."""
    name = "Medvěd"
    asset_class = "stock"
    crypto = False
    minutes = 15
    lookback_bars = 160
    risk_per_trade = 0.01
    max_position_frac = 0.35
    max_positions = 2
    trades_in_risk_off = True
    inverse = ["SQQQ", "SPXS", "SOXS", "SDOW", "TZA", "SH", "PSQ"]
    atr_mult = 2.0
    first_entry, last_entry = dtime(10, 0), dtime(15, 30)

    def universe(self, ctx):
        return list(self.inverse)

    def prepare(self, df):
        df = df.copy()
        df["ema20"], df["ema50"] = ema(df["close"], 20), ema(df["close"], 50)
        df["atr"] = atr(df, 14)
        df["rsi"] = rsi(df["close"], 14)
        df["ret1h"] = df["close"] / df["close"].shift(4) - 1
        return df

    def can_enter(self, ts):
        t = ts.tz_convert("America/New_York").time() if ts.tzinfo else ts.time()
        return self.first_entry <= t <= self.last_entry

    def entry(self, df, i, ctx):
        if i < 55:
            return None
        r = df.iloc[i]
        if not (r["close"] > r["ema20"] > r["ema50"] and 0.003 <= r["ret1h"] <= 0.06 and r["rsi"] < 78):
            return None
        stop_pct = min(0.08, max(0.015, self.atr_mult * r["atr"] / r["close"]))
        return Entry(stop=r["close"] * (1 - stop_pct), trail_pct=stop_pct, max_hold_min=3 * 24 * 60,
                     reason=f"trh padá: {ctx.get('symbol', '')} +{r['ret1h']*100:.1f} % za hodinu, nad EMA 20/50")

    def exit_signal(self, df, i, lot):
        r = df.iloc[i]
        if r["close"] < r["ema50"]:
            return "pád se zastavil (ETF pod EMA 50)"
        return None
