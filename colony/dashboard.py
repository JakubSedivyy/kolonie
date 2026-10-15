"""Webový dashboard kolonie. Běží ve stejném procesu jako boti, jen čte (nic neovládá).

Adresa: http://127.0.0.1:8080 na serveru; ven ho bezpečně (HTTPS) vystaví Caddy - viz deploy/install.sh.
Přihlášení: jméno libovolné, heslo = DASHBOARD_PASSWORD z .env (když chybí, vygeneruje se
a uloží do state/dashboard_password.txt).
"""
from __future__ import annotations
import base64
import hmac
import json
import logging
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config

log = logging.getLogger("colony.dashboard")
HTML_PATH = os.path.join(os.path.dirname(__file__), "web", "dashboard.html")
_state = {"json": b'{"bots":[],"loading":true}'}
_lock = threading.Lock()


def _jsonable(o):
    if hasattr(o, "item"):              # numpy čísla a bool -> obyčejné Python hodnoty
        return o.item()
    return str(o)


providers: dict = {}           # "candles": fn(bot, symbol, tf) -> dict  (zaregistruje živá kolonie)
_cache: dict = {}


def candles(query: str) -> bytes:
    from urllib.parse import parse_qs
    import time as _t
    q = {k: v[0] for k, v in parse_qs(query).items()}
    key = (q.get("bot"), q.get("symbol"), q.get("tf"))
    hit = _cache.get(key)
    if hit and _t.time() - hit[0] < 45:
        return hit[1]
    fn = providers.get("candles")
    try:
        data = fn(q.get("bot", ""), q.get("symbol", ""), q.get("tf", "")) if fn else {"error": "graf není k dispozici"}
    except Exception as e:                       # graf nesmí shodit dashboard
        log.warning("candles %s: %s", key, e)
        data = {"error": str(e)[:200]}
    body = json.dumps(data, default=_jsonable, separators=(",", ":")).encode()
    _cache[key] = (_t.time(), body)
    if len(_cache) > 200:
        _cache.clear()
    return body


def publish(snapshot: dict):
    data = json.dumps(snapshot, default=_jsonable, separators=(",", ":")).encode()
    with _lock:
        _state["json"] = data


def password() -> str:
    pw = os.getenv("DASHBOARD_PASSWORD", "")
    if pw:
        return pw
    path = os.path.join(config.STATE_DIR, "dashboard_password.txt")
    if os.path.exists(path):
        return open(path).read().strip()
    os.makedirs(config.STATE_DIR, exist_ok=True)
    pw = secrets.token_urlsafe(12)
    with open(path, "w") as f:
        f.write(pw)
    os.chmod(path, 0o600)
    return pw


class Handler(BaseHTTPRequestHandler):
    pw = ""

    def log_message(self, *a):          # bez spamu v logu
        pass

    def _authed(self):
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            _, _, given = base64.b64decode(h[6:]).decode().partition(":")
        except Exception:
            return False
        return hmac.compare_digest(given.encode(), self.pw.encode())

    def do_GET(self):
        if not self._authed():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Kolonie", charset="UTF-8"')
            self.end_headers()
            return
        if self.path.startswith("/api/candles"):
            body = candles(self.path.partition("?")[2])
            ctype = "application/json"
        elif self.path.startswith("/api/state"):
            with _lock:
                body = _state["json"]
            ctype = "application/json"
        elif self.path.split("?")[0] == "/lightweight-charts.js":     # knihovna grafů (TradingView, Apache 2.0)
            body = open(os.path.join(os.path.dirname(HTML_PATH), "lightweight-charts.js"), "rb").read()
            ctype = "application/javascript"
        elif self.path in ("/", "/index.html"):
            body = open(HTML_PATH, "rb").read()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(host=None, port=None):
    host = host or os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(port or os.getenv("DASHBOARD_PORT", "8080"))
    Handler.pw = password()
    srv = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("dashboard běží na http://%s:%s", host, port)
    return srv
