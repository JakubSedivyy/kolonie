#!/usr/bin/env bash
# Spouští ho časovač colony-sync každých 15 minut.
# 1) Když je na GitHubu nová verze (větev main), stáhne ji, spustí testy a restartuje kolonii.
#    Když testy neprojdou, vrátí se k předchozí verzi - rozbitý kód se nikdy nespustí.
# 2) Jednou za hodinu nahraje živý stav botů a výsledek laboratoře do větve "reports",
#    odkud si je Claude čte. Klíče (.env) ani hesla se nikam neposílají.
set -uo pipefail
cd "$(dirname "$0")/.."
DIR="$(pwd)"
REPO="${KOLONIE_REPO:-$(grep '^KOLONIE_REPO=' .env 2>/dev/null | cut -d= -f2-)}"
REPO="${REPO:-git@github.com:JakubSedivyy/kolonie.git}"
export GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
LOG=state/sync.log
mkdir -p state
log() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; tail -n 300 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"; }

# ---------------------------------------------------------------- 1) nová verze
if [ ! -d .git ]; then
  git init -q && git remote add origin "$REPO"
fi
git remote set-url origin "$REPO"
if git fetch -q origin main 2>>"$LOG"; then
  NEW=$(git rev-parse origin/main)
  OLD=$(git rev-parse -q --verify HEAD 2>/dev/null || echo "")
  # i když je verze stejná, mohl soubory přepsat ruční aktualizátor (zip) -> vrať je do stavu z GitHubu
  if [ "$NEW" = "$OLD" ] && [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    git reset -q --hard "$NEW" && systemctl restart colony && log "soubory vráceny na verzi ${NEW:0:7}"
  fi
  if [ "$NEW" != "$OLD" ]; then
    git reset -q --hard "$NEW"
    .venv/bin/pip install -q -r requirements.txt >> "$LOG" 2>&1
    if .venv/bin/python tests/test_colony.py > state/tests.log 2>&1; then
      # když se změnila instalace (služby, časovače), přeinstaluj; jinak jen restart
      if [ -n "$OLD" ] && git diff --quiet "$OLD" "$NEW" -- deploy/install.sh; then
        systemctl restart colony
      else
        DEBIAN_FRONTEND=noninteractive bash deploy/install.sh 2>&1 | grep -v -i "heslo\|password" >> "$LOG" || systemctl restart colony
      fi
      log "nasazena verze ${NEW:0:7}: $(git log -1 --format=%s)"
    else
      log "TESTY SELHALY pro ${NEW:0:7} - zůstávám na ${OLD:0:7}: $(grep -E 'Error|assert' state/tests.log | tail -n 3)"
      [ -n "$OLD" ] && git reset -q --hard "$OLD"
    fi
  fi
else
  log "fetch selhal (repozitář nebo deploy key ještě nejsou nastavené?)"
  exit 0
fi

# ---------------------------------------------------------------- 2) hlášení stavu
STAMP=state/.last_report
NEWVIDEO=$(python3 - <<'PYV' 2>/dev/null
import os, re
ids = set(re.findall(r"(?:youtu\.be/|v=|shorts/)([A-Za-z0-9_-]{11})", open("learning/videos.txt").read())) if os.path.exists("learning/videos.txt") else set()
have = {f[:11] for f in os.listdir("/root/kolonie-reports/reports/videa")} if os.path.isdir("/root/kolonie-reports/reports/videa") else set()
print("1" if ids - have else "")
PYV
)
if [ -z "$NEWVIDEO" ] && [ -f "$STAMP" ] && [ $(( $(date +%s) - $(stat -c %Y "$STAMP") )) -lt 3300 ]; then
  exit 0
fi
R=/root/kolonie-reports
if [ ! -d "$R/.git" ]; then
  rm -rf "$R"
  git clone -q --branch reports "$REPO" "$R" 2>/dev/null || {
    git init -q "$R" && git -C "$R" remote add origin "$REPO" && git -C "$R" checkout -q --orphan reports; }
fi
git -C "$R" pull -q --rebase origin reports 2>/dev/null || true
mkdir -p "$R/reports"
.venv/bin/python stav.py > "$R/reports/stav.txt" 2>&1
cp -f state/lab.txt "$R/reports/lab.txt" 2>/dev/null || true
cp -f state/lab.json "$R/reports/lab.json" 2>/dev/null || true
.venv/bin/python - > "$R/reports/obchody.json" <<'PY' 2>/dev/null || true
import json
st = json.load(open("state/colony.json"))
out = {"started": st.get("started"), "events": st.get("events", [])[-150:], "bots": {}}
for n, w in st.get("wallets", {}).items():
    out["bots"][n] = {k: w.get(k) for k in ("status", "cash", "start", "born", "cause_of_death", "fees_paid",
                                              "max_drawdown", "equity_curve", "trades", "lots")}
print(json.dumps(out, ensure_ascii=False, default=str))
PY
journalctl -u colony --since "-1h" --no-pager 2>/dev/null | grep -iE "error|traceback|warning" | tail -n 80 > "$R/reports/chyby.txt" || true
timeout 900 .venv/bin/python deploy/videa.py "$R/reports/videa" >> "$LOG" 2>&1 || log "stahování videí selhalo"
grep -v -i "heslo\|password" "$LOG" | tail -n 60 > "$R/reports/sync.log"
git -C "$R" add -A reports
if ! git -C "$R" diff --cached --quiet; then
  git -C "$R" -c user.name="Kolonie" -c user.email="kolonie@server" commit -q -m "stav $(date -u +%F' '%H:%M) UTC"
  git -C "$R" push -q origin reports 2>>"$LOG" && touch "$STAMP" || log "push reportu selhal"
else
  touch "$STAMP"
fi
