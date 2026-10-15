#!/usr/bin/env bash
# Instalace kolonie na Ubuntu server (Hetzner, DigitalOcean…). Spusť ve složce s kolonií:
#   bash deploy/install.sh
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$(pwd)"

echo "== 1/5 Systémové balíčky"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip curl caddy

echo "== 2/5 Python knihovny"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "== 3/5 Nastavení"
if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "Vytvořen soubor .env - doplň do něj klíče:  nano .env"
  echo "Pak spusť tento skript znovu:              bash deploy/install.sh"
  exit 0
fi
if ! grep -q '^ALPACA_API_KEY=.\+' .env; then
  echo "V .env chybí ALPACA_API_KEY. Doplň ho (nano .env) a spusť skript znovu."
  exit 1
fi
# heslo k dashboardu - když chybí, vygeneruje se
if ! grep -q '^DASHBOARD_PASSWORD=.\+' .env; then
  PW=$(python3 -c "import secrets; print(secrets.token_urlsafe(12))")
  sed -i '/^DASHBOARD_PASSWORD=/d' .env
  echo "DASHBOARD_PASSWORD=$PW" >> .env
fi
.venv/bin/python tests/test_colony.py > /dev/null && echo "Testy OK"

echo "== 4/5 Služba, která běží nonstop a sama se restartuje"
sudo tee /etc/systemd/system/colony.service > /dev/null <<SERVICE
[Unit]
Description=Kolonie obchodních botů
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python run.py
Restart=always
RestartSec=20

[Install]
WantedBy=multi-user.target
SERVICE
# laboratoř: každých 6 hodin přeladí boty na nových datech
sudo tee /etc/systemd/system/colony-lab.service > /dev/null <<LABSVC
[Unit]
Description=Laboratoř kolonie (walk-forward ladění botů)

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python lab.py
Nice=10
LABSVC
sudo tee /etc/systemd/system/colony-lab.timer > /dev/null <<LABTMR
[Unit]
Description=Laboratoř kolonie každých 6 hodin

[Timer]
OnCalendar=*-*-* 00,06,12,18:20:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
LABTMR
# automatické aktualizace z GitHubu + hlášení stavu (každých 15 min)
sudo tee /etc/systemd/system/colony-sync.service > /dev/null <<SYNCSVC
[Unit]
Description=Kolonie: stáhni novou verzi z GitHubu a pošli tam stav

[Service]
Type=oneshot
WorkingDirectory=$DIR
ExecStart=/bin/bash $DIR/deploy/sync.sh
SYNCSVC
sudo tee /etc/systemd/system/colony-sync.timer > /dev/null <<SYNCTMR
[Unit]
Description=Kolonie: synchronizace s GitHubem každých 15 minut

[Timer]
OnBootSec=3min
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
SYNCTMR
# klíč, kterým se server přihlašuje ke GitHub repozitáři (veřejnou část přidá Claude jako deploy key)
sudo apt-get install -y -qq git > /dev/null
if [ ! -f /root/.ssh/kolonie_github ]; then
  mkdir -p /root/.ssh && chmod 700 /root/.ssh
  ssh-keygen -q -t ed25519 -N "" -C "kolonie-server" -f /root/.ssh/kolonie_github
fi
grep -q "kolonie_github" /root/.ssh/config 2>/dev/null || printf 'Host github.com\n  IdentityFile /root/.ssh/kolonie_github\n  IdentitiesOnly yes\n' >> /root/.ssh/config
ssh-keyscan -q github.com >> /root/.ssh/known_hosts 2>/dev/null || true
sort -u -o /root/.ssh/known_hosts /root/.ssh/known_hosts
sudo systemctl daemon-reload
sudo systemctl enable --now colony
sudo systemctl enable --now colony-lab.timer
sudo systemctl enable --now colony-sync.timer
sudo systemctl restart colony

echo "== 5/5 Dashboard s HTTPS (Caddy + adresa zdarma přes sslip.io)"
IP=$(curl -4 -s --max-time 10 https://api.ipify.org || hostname -I | awk '{print $1}')
HOST="$(echo "$IP" | tr '.' '-').sslip.io"
sudo tee /etc/caddy/Caddyfile > /dev/null <<CADDY
$HOST {
    reverse_proxy 127.0.0.1:8080
}
CADDY
sudo systemctl restart caddy
PW=$(grep '^DASHBOARD_PASSWORD=' .env | cut -d= -f2-)

echo
echo "Hotovo. Kolonie běží."
echo
echo "  DASHBOARD:  https://$HOST"
echo "  jméno: cokoliv   heslo: $PW"
echo "  (první načtení může trvat minutu, než se vystaví HTTPS certifikát)"
echo
echo "  Živý log:        journalctl -u colony -f"
echo "  Zastavit:        sudo systemctl stop colony"
echo "  Znovu spustit:   sudo systemctl restart colony"
