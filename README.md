# Kolonie obchodních botů 🐣

Deset botů, každý s vlastní peněženkou **$1 000**. Obchodují sami a nonstop. Když bot spadne
pod **$800** nebo za **30 dní** nic nevydělá, **umře**. Na všechno se díváš v **dashboardu**
v prohlížeči nebo na mobilu a zprávy ti chodí do **Telegramu**.

| Bot | Co obchoduje | Jak | Drží |
|---|---|---|---|
| **Orbit** | US akcie, které si každé ráno sám vybere (nejaktivnější a nejvíc rostoucí) | průraz ranního rozpětí s objemem nad VWAP | minuty až hodiny, večer vše prodá |
| **Trendy** | BTC, ETH, SOL a další velké krypto | otočení trendu EMA 20/50 nad EMA 200 | hodiny až 3 dny |
| **Memec** | meme coiny na Alpace (DOGE, SHIB, PEPE…) | výbuch objemu + nové maximum; coiny v trendu na internetu mají přednost | max. 4 hodiny |
| **Vlna** | krypto i meme coiny | swing: coin v trendu na internetu nebo průraz 3denního maxima, rostoucí trend | 3–7 dní |
| **Šeptač** | meme coiny na Alpace | kupuje, jen když o coinu mluví aspoň 2 různé zdroje (X, 4chan, StockTwits, Telegram…) a cena to potvrzuje | max. 24 hodin |
| **Lovec** | zavedené meme coiny na Solaně (3+ dny staré, likvidita od $250k) | **jen simulace**: coiny z radaru, které rostou a kupci převažují; cíl +25 %, stop −10 % | max. 2 dny |
| **Medvěd** | inverzní ETF (SQQQ, SPXS, SOXS…), které rostou, když trh padá | vstup jen když pád potvrzuje trend; stop 2x ATR, trailing | max. 3 dny |
| **Degen II** | úplně nové meme coiny na Solaně | **jen simulace**, vzkříšený po smrti prvního Degena: až 2 h staré coiny, likvidita od $100k, 3% sázky, hlídání každých 30 s | max. 4 hodiny |
| **Stín** | krypto - sázky na pokles (short) | **jen simulace** (Alpaca short krypta neumí): sestupný trend, coin padá 2-15 % za 24 h; nikdy v býčím trhu; poplatky a funding jako na burze | max. 3 dny |
| **Věštec** | predikční trhy Polymarket | **jen simulace**: Grok přečte X a zprávy, odhadne pravděpodobnost; sází při hraně ≥ 8 b., čtvrtinový Kelly | do vyhodnocení trhu |

## Trend radar: co je teď „in“ na internetu
Radar každých 10 minut projde tyhle zdroje (všechny zdarma):
- **CoinGecko Trending:** co lidé nejvíc hledají
- **GeckoTerminal:** trendující pooly na Solaně
- **DexScreener Metas:** témata, která zrovna letí (AI agenti, psi, kočky, politika…)
- **DexScreener Boosts:** coiny, do kterých se nejvíc platí propagace
- **Reddit:** zmínky $COINŮ na r/memecoins, r/CryptoCurrency a dalších (volitelné, potřebuje klíč)

Z toho vznikne **skóre hype**. Podle něj:
- **Memec** u coinu v trendu nakoupí už při menším výbuchu objemu,
- **Vlna** na coin v trendu naskočí,
- **Degen** dostane seznam adres, které má prověřit.

## Oči kolonie: X, sociální sítě a zprávy ze světa
- **X/Twitter přes Grok (xAI):** každých 8 h Grok projde X a vrátí, o čem se teď mluví (coiny, akcie, světové události, nálada). Denní strop útraty `XAI_DAILY_BUDGET` (výchozí $1). Bez klíče se přeskočí.
- **Zprávy:** GDELT (zprávy z celého světa), CoinDesk, Cointelegraph, Decrypt
- **YouTube** (volitelně, klíč zdarma): nejsledovanější krypto videa za 12 h
- **Fear & Greed index:** nálada krypto trhu 0–100 = „počasí“ ve vesničce

## Paměť: boti se učí z vlastních chyb
Každý bot si u nákupu zapíše, v jaké byl situaci (denní doba, nálada trhu, coin, jestli se o něm mluví,
u DEX coinů likvidita, stáří, růst a převaha kupců). Po prodeji si zapíše výsledek.
- Situaci, ve které prodělal aspoň 6x a v průměru výrazně, už nekoupí (a napíše to do Telegramu 🧠).
- Kde se mu daří, sází víc (až 1,5x), kde ne, méně (až 0,5x).
- Paměť drží posledních 400 obchodů, staré lekce postupně vyprchají. Co se naučil, vidíš v detailu bota.
Tři úrovně zlepšování: paměť (po každém obchodu), laboratoř (každých 6 h ladí parametry),
Claude (každé ráno čte výsledky a opravuje kód).

## Laboratoř: poctivé ladění botů
Každých 6 hodin laboratoř stáhne 90 dní dat. Varianty botů trénuje na prvních 60 dnech a vítěze ověří na posledních 30 dnech, které při výběru neviděl. Když vydělává i tam, bude nasazen. Když ne, na papíře obchoduje dál **na zkoušku** (chceme vidět živé výsledky), ale u reálných peněz by **odpočíval**. Na papíře se dá odpočinek vynutit: `LAB_BENCH_ON_PAPER=yes`. Výsledek: `state/lab.txt`.

## Jak si boti pomáhají
- **Nálada trhu:** jednou za hodinu se podívají, jestli rostou BTC a S&P 500. Podle toho všichni zvětší nebo zmenší sázky, při strachu na trhu meme boti nenakupují.
- **Sdílený černý seznam:** co dvakrát za den vyhodí stop-loss, do zítřka nikdo neobchoduje.
- **Zámek:** jeden coin drží vždy jen jeden bot, aby se jim pozice na účtu nemíchaly.
- **Hlídač kolonie:** když celá kolonie klesne pod 85 % startu, všechny nové nákupy se zastaví.

Papír nelže: ke každému obchodu se započítávají poplatky a skluz ceny.

---

## Instalace krok za krokem

### 1. Alpaca klíče (5 min)
1. https://alpaca.markets → přihlásit → vlevo nahoře přepni na **Paper Trading**.
2. Doporučuju založit **nový paper účet** jen pro kolonii, ať se nemíchá s botem na politiky.
3. Vpravo **API Keys → Generate New Keys** a zkopíruj si Key i Secret. Secret se ukáže jen jednou.

### 2. Telegram bot (5 min)
1. V Telegramu najdi **@BotFather** → pošli `/newbot` → zvol jméno → dostaneš **token**.
2. Napiš svému novému botovi „ahoj“.
3. Chat ID zjistíš na serveru v kroku 4 příkazem `.venv/bin/python telegram_setup.py`.

### 3. Server (10 min)
1. Účet na **Hetzner Cloud**: https://www.hetzner.com/cloud
2. **Add Server** → lokalita Německo → **Ubuntu 24.04** → nejlevnější typ (zhruba 4–6 € měsíčně) → Create.
3. Do e-mailu ti přijde **IP adresa** a **heslo** pro uživatele `root`.

### 4. Nahrát a nainstalovat (10 min)
Na svém počítači otevři terminál (Windows: **PowerShell**, Mac: **Terminál**) ve složce s `kolonie.zip`:
```
scp kolonie.zip root@TVOJE_IP:~
ssh root@TVOJE_IP
```
(Na otázku „Are you sure…“ napiš `yes`, pak heslo z e-mailu.) Na serveru:
```
apt-get install -y unzip && unzip kolonie.zip && cd colony
bash deploy/install.sh          # 1. běh: vytvoří soubor .env
nano .env                       # vlož Alpaca klíče a TELEGRAM_TOKEN; uložit Ctrl+O, Enter, konec Ctrl+X
.venv/bin/python telegram_setup.py   # vypíše TELEGRAM_CHAT_ID -> vlož ho do .env (nano .env)
```

### 5. Backtest: přežili by? (5 min)
```
.venv/bin/python backtest.py --days 60
```
Výsledky mi pošli. Když bot v testu umře, upravíme ho dřív, než ho pustíš.

### 6. Spuštění
```
bash deploy/install.sh
```
Na konci uvidíš **adresu dashboardu** (`https://…sslip.io`) a **heslo**. Otevři ji v mobilu a ulož si ji
na plochu. V Telegramu ti přijde 🐣.

---

## Dashboard
- **Vesnička:** domeček každého bota (kouř z komína = obchoduje, Zzz = odpočívá, náhrobek = umřel), bublina s tím, co právě dělá, počasí podle nálady trhu a běžící zprávy ze světa
- **Křišťálová koule Věštce:** co si Grok myslí o trzích na Polymarketu oproti ceně trhu
- **Boti:** peníze, výnos a **pruh života** (jak daleko je bot od smrti), dny do 30denního termínu, úspěšnost, profit factor a max. propad
- **Peníze v čase:** křivky všech botů s čarou smrti na $800
- **Otevřené pozice:** nákup, aktuální cena, zisk, stop, cíl a proč bot koupil
- **Trend radar:** co je teď v trendu a odkud to radar ví
- **Co se děje:** nákupy, prodeje, zranění, smrti, změny nálady
- **Obchody:** historie s filtrem podle bota
- **Hodnocení:** kdo splňuje podmínky pro reálné peníze

Dashboard data jen ukazuje. Ovládání je přes Telegram.

## Aktualizace (samy, přes GitHub)
Server se každých 15 minut podívá do soukromého repozitáře na GitHubu. Když tam Claude nahraje novou verzi,
server ji stáhne, spustí testy a teprve když projdou, kolonii restartuje (jinak zůstane na staré verzi).
Jednou za hodinu server nahraje do větve `reports` živý stav botů, výsledek laboratoře a chyby z logu -
odtud si je Claude čte. Klíče z `.env` se nikam neposílají. Log synchronizace: `state/sync.log`.

## Telegram příkazy
`/stav` · `/pauza` · `/pokracuj` · `/prodej_vse` (nouzová brzda) · `/hodnoceni`

## Po 10 dnech: reálné peníze?
Bot musí:
- žít,
- mít zisk po poplatcích,
- mít aspoň 20 obchodů,
- mít profit factor ≥ 1,3,
- mít propad nejvýš 10 %.

10 dní je krátká doba, ani ✅ nezaručuje budoucí zisk. Přepnutí:
1. Ověřený **live účet** na Alpace, jen peníze, které můžeš ztratit.
2. V `.env`: `TRADING_MODE=live`, `I_UNDERSTAND_REAL_MONEY=yes`, místo paper klíčů **live klíče**.
3. Boty, kteří neprošli, předtím vypneme. Napiš mi.

Degen zůstává vždy simulace. Zisky z obchodování se v Česku daní. Tohle není finanční poradenství.

## Užitečné příkazy na serveru
| | |
|---|---|
| živý stav botů | `.venv/bin/python stav.py` (nebo dvojklik na `stav-kolonie.command` na Macu) |
| živý log | `journalctl -u colony -f` |
| restart po změně .env | `sudo systemctl restart colony` |
| zastavit | `sudo systemctl stop colony` |
| testy | `.venv/bin/python tests/test_colony.py` |
