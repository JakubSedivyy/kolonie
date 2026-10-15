"""Trend radar: každých 10 minut projede internet a zjistí, které coiny a témata jsou "in".

Zdroje (všechny zdarma):
  - CoinGecko Trending        - co lidé nejvíc hledají (top 15 coinů)
  - GeckoTerminal             - trendující pooly na Solaně
  - DexScreener Metas         - trendující témata (AI, psi, kočky…) a jejich coiny
  - DexScreener Boosts (top)  - coiny, do kterých se nejvíc platí propagace
  - Reddit (volitelné)        - zmínky na r/memecoins, r/CryptoCurrency, r/SatoshiStreetBets
  - Grok / X (volitelné)      - každých 8 h projde X a vrátí nejžhavější coiny, akcie a světové události
  - Zprávy                    - GDELT (celý svět) + CoinDesk, Cointelegraph, Decrypt
  - YouTube (volitelné)       - nejsledovanější krypto videa za posledních 12 h
  - Fear & Greed index        - nálada krypto trhu 0-100
  - 4chan /biz/               - tam se meme coiny často objeví jako první
  - StockTwits trending       - co řeší traders akcií a krypta
  - Telegram kanály           - Whale Alert (velké přesuny), Binance oznámení (nové listingy)
  - Hacker News, Google News  - technologické a obecné zprávy
  - Bluesky (volitelné)       - vyhledávání $TICKERŮ (zdarma s app heslem)

Výstup:
  hot[SYMBOL] = {"score": číslo, "sources": [...]}   -> Memec, Vlna
  tokens = set(adres na Solaně)                       -> Degen
"""
from __future__ import annotations
import logging
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone

import xml.etree.ElementTree as ET

import requests

log = logging.getLogger("colony.trends")
UA = {"User-Agent": "colony-trend-radar/1.0", "Accept": "application/json"}
CASHTAG = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,9})\b")
COIN_NAMES = {"bitcoin": "BTC", "ethereum": "ETH", "ether": "ETH", "solana": "SOL", "dogecoin": "DOGE",
              "shiba": "SHIB", "pepe": "PEPE", "xrp": "XRP", "ripple": "XRP", "cardano": "ADA", "chainlink": "LINK",
              "avalanche": "AVAX", "litecoin": "LTC", "bonk": "BONK", "dogwifhat": "WIF", "floki": "FLOKI",
              "sui": "SUI", "trump coin": "TRUMP", "$trump": "TRUMP", "polkadot": "DOT", "uniswap": "UNI"}
NEWS_FEEDS = {"CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
              "Cointelegraph": "https://cointelegraph.com/rss", "Decrypt": "https://decrypt.co/feed"}
GROK_PROMPT = """Search X (Twitter) for what traders and the crypto/stock community are talking about RIGHT NOW (last few hours).
Return ONLY valid JSON, no other text:
{"hot":[{"symbol":"PEPE","kind":"meme|crypto|stock","buzz":0-10,"sentiment":-1.0..1.0,"why":"max 12 words"}],
 "world":[{"headline":"max 15 words","impact":"bullish|bearish|neutral","assets":["BTC"]}],
 "mood":"risk_on|neutral|risk_off"}
Also use web search to check what is going viral on TikTok, YouTube, Reddit and in the news about crypto, memecoins and stocks.
Max 15 hot items, max 8 world items. Prefer tickers that are tradable on major exchanges. Be factual, no hype."""
TELEGRAM_CHANNELS = {"whale_alert_io": "Whale Alert", "binance_announcements": "Binance oznámení",
                     "cointelegraph": "Cointelegraph TG", "WatcherGuru": "Watcher Guru"}
NOT_COINS = {"USD", "USDT", "USDC", "EUR", "CEO", "ATH", "ETF", "SEC", "IMO", "LOL", "THE", "AND", "FOR", "NFT", "APY"}


class TrendRadar:
    REFRESH_SEC = 600
    REDDIT_SUBS = ("memecoins", "CryptoCurrency", "SatoshiStreetBets", "CryptoMoonShots")

    GROK_EVERY_SEC = 8 * 3600

    def __init__(self, session=None, grok=None):
        self.s = session or requests.Session()
        self.grok = grok
        self.world: list[dict] = []          # zprávy ze světa pro dashboard
        self.fear_greed: dict = {}
        self.grok_mood = ""
        self._grok_last = 0.0
        self._grok_hot: list[dict] = []
        self.youtube_key = os.getenv("YOUTUBE_API_KEY", "")
        self.bsky_handle = os.getenv("BLUESKY_HANDLE", "")
        self.bsky_pw = os.getenv("BLUESKY_APP_PASSWORD", "")
        self._bsky_jwt = ""
        self.source_stats: dict[str, int] = {}   # kolik položek dal který zdroj (pro dashboard)
        self.s.headers.update(UA)
        self.cg_key = os.getenv("COINGECKO_API_KEY", "")
        self.reddit_id = os.getenv("REDDIT_CLIENT_ID", "")
        self.reddit_secret = os.getenv("REDDIT_CLIENT_SECRET", "")
        self._reddit_token, self._reddit_exp = None, 0
        self.hot: dict[str, dict] = {}
        self.tokens: set[str] = set()
        self.metas: list[dict] = []
        self.updated: str = ""
        self.errors: dict[str, str] = {}
        self._last = 0.0

    # ------------------------------------------------------------------ veřejné API
    def score(self, symbol: str) -> float:
        base = symbol.split("/")[0].upper()
        return self.hot.get(base, {}).get("score", 0.0)

    def maybe_refresh(self, force=False):
        if not force and time.time() - self._last < self.REFRESH_SEC:
            return False
        self._last = time.time()
        hot: dict[str, dict] = {}
        tokens: set[str] = set()
        self.errors = {}

        stats: dict[str, int] = {}

        def add(sym, pts, src):
            key = src.split(":")[0].split(" (")[0]
            stats[key] = stats.get(key, 0) + 1
            sym = (sym or "").upper().strip()
            if not sym or sym in NOT_COINS or len(sym) > 12:
                return
            h = hot.setdefault(sym, {"score": 0.0, "sources": []})
            h["score"] += pts
            if src not in h["sources"]:
                h["sources"].append(src)

        world: list[dict] = []
        self._world_acc = world
        for name, fn in (("coingecko", self._coingecko), ("geckoterminal", self._geckoterminal),
                         ("dex_metas", self._dex_metas), ("dex_boosts", self._dex_boosts),
                         ("reddit", self._reddit), ("fear_greed", self._fear_greed),
                         ("news", self._news), ("gdelt", self._gdelt), ("youtube", self._youtube),
                         ("4chan_biz", self._biz), ("stocktwits", self._stocktwits), ("telegram", self._telegram),
                         ("hackernews", self._hn), ("google_news", self._google_news), ("bluesky", self._bluesky),
                         ("grok", self._grok)):
            try:
                fn(add, tokens)
            except Exception as e:                       # jeden výpadek nesmí shodit radar
                self.errors[name] = str(e)[:200]
                log.warning("radar %s: %s", name, e)

        self.hot = dict(sorted(hot.items(), key=lambda kv: -kv[1]["score"]))
        self.tokens = tokens
        self.source_stats = stats
        if world:
            seen, uniq = set(), []
            for w in sorted(world, key=lambda w: w.get("time", ""), reverse=True):
                k = w["title"].lower()[:60]
                if k not in seen:
                    seen.add(k); uniq.append(w)
            self.world = uniq[:40]
        self.updated = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return True

    def snapshot(self, top=25):
        return {"updated": self.updated, "errors": self.errors, "tokens": len(self.tokens),
                "sources": self.source_stats,
                "world": self.world[:25], "fear_greed": self.fear_greed, "grok_mood": self.grok_mood,
                "grok": self.grok.status() if self.grok else {"enabled": False},
                "hot": [{"symbol": k, "score": round(v["score"], 1), "sources": v["sources"]}
                        for k, v in list(self.hot.items())[:top]],
                "metas": self.metas[:8]}

    # ------------------------------------------------------------------ zdroje
    def _get(self, url, **kw):
        r = self.s.get(url, timeout=20, **kw)
        r.raise_for_status()
        return r.json()

    def _coingecko(self, add, tokens):
        hdr = {"x-cg-demo-api-key": self.cg_key} if self.cg_key else {}
        d = self._get("https://api.coingecko.com/api/v3/search/trending", headers=hdr)
        for c in d.get("coins", []):
            it = c.get("item", {})
            rank = int(it.get("score", 10))
            add(it.get("symbol"), 4 - min(rank, 15) * 0.2, "CoinGecko trending")

    def _geckoterminal(self, add, tokens):
        d = self._get("https://api.geckoterminal.com/api/v2/networks/solana/trending_pools",
                      headers={"Accept": "application/json;version=20230302"})
        for p in d.get("data", [])[:20]:
            a = p.get("attributes", {})
            name = a.get("name", "")
            sym = name.split("/")[0].strip()
            add(sym, 2.0, "GeckoTerminal trending")
            base = (((p.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
            if base.startswith("solana_"):
                tokens.add(base[len("solana_"):])

    def _dex_metas(self, add, tokens):
        metas = self._get("https://api.dexscreener.com/metas/trending/v1") or []
        self.metas = [{"name": m.get("name"), "slug": m.get("slug"),
                       "h24": (m.get("marketCapChange") or {}).get("h24"),
                       "marketCap": m.get("marketCap"), "tokens": m.get("tokenCount")} for m in metas[:10]]
        for m in metas[:3]:                               # coiny 3 nejžhavějších témat
            slug = m.get("slug")
            if not slug:
                continue
            d = self._get(f"https://api.dexscreener.com/metas/meta/v1/{slug}")
            for p in (d.get("pairs") or [])[:15]:
                bt = p.get("baseToken") or {}
                add(bt.get("symbol"), 1.5, f"téma: {m.get('name')}")
                if p.get("chainId") == "solana" and bt.get("address"):
                    tokens.add(bt["address"])

    def _dex_boosts(self, add, tokens):
        for t in (self._get("https://api.dexscreener.com/token-boosts/top/v1") or [])[:30]:
            if t.get("chainId") == "solana" and t.get("tokenAddress"):
                tokens.add(t["tokenAddress"])

    def _reddit(self, add, tokens):
        if not (self.reddit_id and self.reddit_secret):
            return
        if time.time() > self._reddit_exp:
            r = self.s.post("https://www.reddit.com/api/v1/access_token", timeout=20,
                            auth=(self.reddit_id, self.reddit_secret), data={"grant_type": "client_credentials"})
            r.raise_for_status()
            j = r.json()
            self._reddit_token, self._reddit_exp = j["access_token"], time.time() + j.get("expires_in", 3600) - 60
        cnt = Counter()
        for sub in self.REDDIT_SUBS:
            d = self._get(f"https://oauth.reddit.com/r/{sub}/new?limit=100",
                          headers={"Authorization": f"bearer {self._reddit_token}"})
            for ch in d.get("data", {}).get("children", []):
                post = ch.get("data", {})
                if time.time() - post.get("created_utc", 0) > 6 * 3600:   # jen posledních 6 hodin
                    continue
                text = f"{post.get('title','')} {post.get('selftext','')[:2000]}"
                for sym in set(CASHTAG.findall(text)):
                    cnt[sym.upper()] += 1 + min(post.get("score", 0), 200) / 100
        for sym, n in cnt.most_common(30):
            if n >= 2:
                add(sym, min(3.0, n / 3), f"Reddit ({n:.0f}x)")

    # ------------------------------------------------------------------ svět a sociální sítě
    def _mentions(self, text, add, src, pts=0.5):
        low = text.lower()
        found = {COIN_NAMES[n] for n in COIN_NAMES if n in low}
        found |= {c.upper() for c in CASHTAG.findall(text)}
        for sym in found:
            add(sym, pts, src)
        return sorted(found)

    def _fear_greed(self, add, tokens):
        d = self._get("https://api.alternative.me/fng/?limit=1")
        x = (d.get("data") or [{}])[0]
        self.fear_greed = {"value": int(x.get("value", 0) or 0), "label": x.get("value_classification", "")}

    def _news(self, add, tokens):
        now = datetime.now(timezone.utc).isoformat(timespec="minutes")
        for name, url in NEWS_FEEDS.items():
            try:
                r = self.s.get(url, timeout=20)
                r.raise_for_status()
                root = ET.fromstring(r.content)
            except Exception as e:
                self.errors[f"news:{name}"] = str(e)[:120]
                continue
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                if not title:
                    continue
                coins = self._mentions(title, add, f"zprávy: {name}")
                self._world_acc.append({"title": title, "source": name, "url": it.findtext("link") or "",
                                        "time": now, "assets": coins})
                if len([w for w in self._world_acc if w["source"] == name]) >= 12:
                    break

    def _gdelt(self, add, tokens):
        q = '(bitcoin OR crypto OR "stock market" OR "federal reserve" OR tariffs OR election OR war)'
        d = self._get("https://api.gdeltproject.org/api/v2/doc/doc", params={
            "query": q + " sourcelang:english", "mode": "artlist", "format": "json", "maxrecords": 25,
            "timespan": "3h", "sort": "datedesc"})
        for a in d.get("articles", [])[:25]:
            title = a.get("title", "").strip()
            if title:
                coins = self._mentions(title, add, "svět (GDELT)", 0.3)
                self._world_acc.append({"title": title, "source": a.get("domain", "GDELT"), "url": a.get("url", ""),
                                        "time": datetime.now(timezone.utc).isoformat(timespec="minutes"), "assets": coins})

    def _youtube(self, add, tokens):
        if not self.youtube_key:
            return
        after = datetime.fromtimestamp(time.time() - 12 * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        d = self._get("https://www.googleapis.com/youtube/v3/search", params={
            "part": "snippet", "q": "crypto|memecoin|altcoin", "type": "video", "order": "viewCount",
            "publishedAfter": after, "maxResults": 25, "key": self.youtube_key})
        for it in d.get("items", []):
            self._mentions((it.get("snippet") or {}).get("title", ""), add, "YouTube", 0.6)

    def _grok(self, add, tokens):
        if not self.grok or not self.grok.enabled:
            return
        if time.time() - self._grok_last >= self.GROK_EVERY_SEC:
            text = self.grok.ask(GROK_PROMPT, x_search=True, web_search=True, hours=8, max_turns=3, reserve=0.40)
            self._grok_last = time.time()
            data = extract_json_safe(text)
            if data:
                self._grok_hot = data.get("hot", [])[:15]
                self.grok_mood = data.get("mood", "") if data.get("mood") in ("risk_on", "neutral", "risk_off") else ""
                now = datetime.now(timezone.utc).isoformat(timespec="minutes")
                self._grok_world = [{"title": w.get("headline", ""), "source": "X (Grok)", "url": "",
                                     "time": now, "assets": w.get("assets", []), "impact": w.get("impact", "")}
                                    for w in data.get("world", [])[:8] if w.get("headline")]
        # výsledek Groka platí 8 h - přičítá se při každém obnovení radaru
        for h in self._grok_hot:
            try:
                buzz, sent = float(h.get("buzz", 0)), float(h.get("sentiment", 0))
            except (TypeError, ValueError):
                continue
            pts = buzz * 0.6 * (1.0 if sent > 0.2 else 0.3 if sent > -0.2 else -0.5)
            add(h.get("symbol"), pts, f"X/Grok: {str(h.get('why',''))[:60]}")
        self._world_acc.extend(getattr(self, "_grok_world", []))

    # ------------------------------------------------------------------ další sociální sítě (zdarma)
    def _biz(self, add, tokens):
        """4chan /biz/ - veřejné JSON API. Počítá zmínky coinů ve vláknech za posledních 6 h."""
        pages = self._get("https://a.4cdn.org/biz/catalog.json")
        cnt = Counter()
        cutoff = time.time() - 6 * 3600
        for page in pages:
            for th in page.get("threads", []):
                if th.get("last_modified", th.get("time", 0)) < cutoff:
                    continue
                text = re.sub(r"<[^>]+>", " ", f"{th.get('sub','')} {th.get('com','')}")
                weight = 1 + min(th.get("replies", 0), 300) / 100
                low = text.lower()
                found = {COIN_NAMES[n] for n in COIN_NAMES if n in low} | {c.upper() for c in CASHTAG.findall(text)}
                for sym in found:
                    cnt[sym] += weight
        for sym, n in cnt.most_common(20):
            if n >= 2:
                add(sym, min(2.5, n / 3), f"4chan /biz/ ({n:.0f})")

    def _stocktwits(self, add, tokens):
        d = self._get("https://api.stocktwits.com/api/2/trending/symbols.json")
        for i, sym in enumerate(d.get("symbols", [])[:20]):
            tick = str(sym.get("symbol", "")).replace(".X", "")
            add(tick, 2.0 - i * 0.07, "StockTwits trending")

    def _telegram(self, add, tokens):
        """Veřejné náhledy Telegram kanálů (t.me/s/kanál) - bez účtu."""
        now = datetime.now(timezone.utc).isoformat(timespec="minutes")
        for chan, label in TELEGRAM_CHANNELS.items():
            try:
                r = self.s.get(f"https://t.me/s/{chan}", timeout=20)
                r.raise_for_status()
            except Exception as e:
                self.errors[f"telegram:{chan}"] = str(e)[:120]
                continue
            msgs = re.findall(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', r.text, re.S)
            for raw in msgs[-8:]:
                text = re.sub(r"<[^>]+>", " ", raw)
                text = re.sub(r"\s+", " ", text).strip()
                if not text:
                    continue
                pts = 1.5 if "binance" in chan and "list" in text.lower() else 0.6
                coins = self._mentions(text, add, f"Telegram: {label}", pts)
                self._world_acc.append({"title": text[:160], "source": label, "url": f"https://t.me/s/{chan}",
                                        "time": now, "assets": coins})

    def _hn(self, add, tokens):
        d = self._get("https://hn.algolia.com/api/v1/search_by_date",
                      params={"query": "crypto OR bitcoin OR stocks OR AI", "tags": "story", "hitsPerPage": 20})
        now = datetime.now(timezone.utc).isoformat(timespec="minutes")
        for h in d.get("hits", [])[:20]:
            title = h.get("title") or ""
            if title:
                coins = self._mentions(title, add, "Hacker News", 0.3)
                self._world_acc.append({"title": title, "source": "Hacker News", "url": h.get("url") or "",
                                        "time": now, "assets": coins})

    def _google_news(self, add, tokens):
        r = self.s.get("https://news.google.com/rss/search", timeout=20, params={
            "q": "crypto OR bitcoin OR memecoin OR \"stock market\" when:6h", "hl": "en-US", "gl": "US", "ceid": "US:en"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        now = datetime.now(timezone.utc).isoformat(timespec="minutes")
        for it in list(root.iter("item"))[:20]:
            title = (it.findtext("title") or "").strip()
            if title:
                coins = self._mentions(title, add, "Google News", 0.4)
                self._world_acc.append({"title": title, "source": "Google News", "url": it.findtext("link") or "",
                                        "time": now, "assets": coins})

    def _bluesky(self, add, tokens):
        if not (self.bsky_handle and self.bsky_pw):
            return
        if not self._bsky_jwt:
            r = self.s.post("https://bsky.social/xrpc/com.atproto.server.createSession", timeout=20,
                            json={"identifier": self.bsky_handle, "password": self.bsky_pw})
            r.raise_for_status()
            self._bsky_jwt = r.json()["accessJwt"]
        cnt = Counter()
        for q in ("memecoin", "crypto pump", "$"):
            try:
                d = self._get("https://bsky.social/xrpc/app.bsky.feed.searchPosts",
                              params={"q": q, "limit": 50, "sort": "latest"},
                              headers={"Authorization": f"Bearer {self._bsky_jwt}"})
            except Exception:
                self._bsky_jwt = ""          # token vypršel - příště se přihlásí znovu
                raise
            for p in d.get("posts", []):
                text = ((p.get("record") or {}).get("text")) or ""
                for sym in set(CASHTAG.findall(text)):
                    cnt[sym.upper()] += 1
        for sym, n in cnt.most_common(15):
            if n >= 2:
                add(sym, min(2.0, n / 3), f"Bluesky ({n})")


def extract_json_safe(text):
    from .grok import extract_json
    return extract_json(text)
