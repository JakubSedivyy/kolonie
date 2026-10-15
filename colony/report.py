"""Přehledy: krátký text do Telegramu, HTML stránka a hodnocení po 10 dnech."""
from __future__ import annotations
import html
import json
from datetime import datetime, timezone

STATUS_CZ = {"ALIVE": "žije", "INJURED": "zraněný", "DEAD": "mrtvý"}
ICON = {"ALIVE": "🟢", "INJURED": "🟠", "DEAD": "💀"}


def pct(x):
    return f"{x*100:+.1f} %"


def text_summary(wallets, mood=None, title="Stav kolonie"):
    lines = [f"📊 {title}" + (f" · nálada trhu: {mood}" if mood else "")]
    tot_eq = tot_start = 0
    for w in wallets:
        s = w.stats()
        tot_eq += s["equity"]; tot_start += w.start
        pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
        lines.append(f"{ICON[w.status]} {w.name}: ${s['equity']:.2f} ({pct(s['return'])}) · "
                     f"{s['trades']} obch. · úspěšnost {s['win_rate']*100:.0f} % · PF {pf}"
                     + (f" · ✝ {w.cause_of_death}" if w.status == "DEAD" else "")
                     + (f" · otevřeno {s['open_positions']}" if s["open_positions"] else ""))
    lines.append(f"Celkem: ${tot_eq:.2f} z ${tot_start:.0f} ({pct(tot_eq/tot_start-1)})")
    return "\n".join(lines)


def evaluate(w, days):
    """Verdikt pro přechod na reálné peníze. Záměrně přísný."""
    s = w.stats()
    checks = [   # bool(): numpy vrací np.bool_, který by se do JSON dostal jako text "False"
        ("žije", bool(w.status != "DEAD")),
        ("vydělal po poplatcích", bool(s["return"] > 0)),
        ("aspoň 20 obchodů (jinak to může být náhoda)", bool(s["trades"] >= 20)),
        ("profit factor ≥ 1.3", bool(s["profit_factor"] >= 1.3)),
        ("max. propad ≤ 10 %", bool(s["max_drawdown"] <= 0.10)),
    ]
    if w.asset_class == "degen":
        checks.append(("je to simulace - na reálné peníze nejde", False))
    ok = all(c[1] for c in checks)
    return ok, checks


def eval_text(wallets, days):
    out = [f"🧪 Hodnocení po {days:.0f} dnech - kdo je kandidát na reálné peníze?"]
    for w in wallets:
        ok, checks = evaluate(w, days)
        out.append(f"\n{'✅' if ok else '❌'} {w.name}")
        out += [f"   {'✓' if c else '✗'} {name}" for name, c in checks]
    out.append("\nPoznámka: 10 dní je statisticky krátká doba. Ani ✅ nezaručuje zisk do budoucna.")
    return "\n".join(out)


def write_html(wallets, path, mood=None):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = []
    for w in wallets:
        s = w.stats()
        pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
        rows.append(f"<tr><td>{ICON[w.status]} {html.escape(w.name)}</td><td>{STATUS_CZ[w.status]}</td>"
                    f"<td>${s['equity']:.2f}</td><td>{pct(s['return'])}</td><td>{s['trades']}</td>"
                    f"<td>{s['win_rate']*100:.0f} %</td><td>{pf}</td><td>{s['max_drawdown']*100:.1f} %</td>"
                    f"<td>${s['fees']:.2f}</td><td>{html.escape(s['cause_of_death'])}</td></tr>")
    trades = sorted((dict(t, bot=w.name) for w in wallets for t in w.trades), key=lambda t: t["closed"])[-40:]
    trows = "".join(
        f"<tr><td>{t['closed'][:16]}</td><td>{html.escape(t['bot'])}</td><td>{html.escape(t['symbol'])}</td>"
        f"<td style='color:{'#1f8a52' if t['pnl']>0 else '#c2412d'}'>{t['pnl']:+.2f} $</td>"
        f"<td>{html.escape(t['why'])}</td></tr>" for t in reversed(trades))
    curves = {w.name: w.equity_curve[-500:] for w in wallets}
    doc = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kolonie botů</title><style>body{{font:14px system-ui;margin:16px;background:#f5f7f9;color:#17202b}}
table{{border-collapse:collapse;width:100%;background:#fff;margin:8px 0 20px}}td,th{{padding:6px 10px;border-bottom:1px solid #dde3ea;text-align:left}}
.wrap{{overflow-x:auto}}</style>
<h1>Kolonie botů</h1><p>Aktualizováno {now}{' · nálada trhu: ' + html.escape(mood) if mood else ''}</p>
<div class=wrap><table><tr><th>Bot</th><th>Stav</th><th>Peníze</th><th>Výnos</th><th>Obchody</th><th>Úspěšnost</th>
<th>Profit factor</th><th>Max. propad</th><th>Poplatky</th><th>Příčina smrti</th></tr>{''.join(rows)}</table></div>
<h2>Posledních 40 obchodů</h2><div class=wrap><table><tr><th>Čas</th><th>Bot</th><th>Symbol</th><th>Zisk</th><th>Důvod</th></tr>{trows}</table></div>
<script>const curves={json.dumps(curves)};</script>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
