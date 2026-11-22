# Kolonie obchodních botů – pokyny pro Clauda

Majitel: Jakub (píše česky, neformálně). Odpovídej česky, srozumitelně, bez žargonu.

## Jak to běží
- Server Hetzner 2.29.158.122 spouští kolonii (`run.py`, služba `colony`) 24/7 na **papírovém** účtu Alpaca.
- Server každých 15 min stáhne větev `main` (`deploy/sync.sh`). Nasadí ji jen když projdou testy
  `python tests/test_colony.py`, jinak zůstane na staré verzi.
- Jednou za hodinu server nahraje do větve `reports` složku `reports/`:
  `stav.txt` (živé výsledky botů), `obchody.json` (obchody, křivky, události), `lab.txt`/`lab.json`
  (laboratoř), `chyby.txt` (chyby z logu), `sync.log` (nasazování).
  Čti: `git fetch origin reports && git show origin/reports:reports/stav.txt`.
- Laboratoř `lab.py` běží na serveru každých 6 h (walk-forward 60/30 dní).
  Jednou denně přidá do `lab.txt` i DLOUHODOBÝ TEST (`colony/longtest.py`): Kompas, Lev, Sokol, Spořič na ~6 letech
  denní historie vs. držení S&P 500. Dlouhodobé boty hodnoť hlavně podle něj (živě obchodují málo).
- VÍCELETÝ TEST (`colony/multiyear.py`, od 21. 11. 2026 na přání Jakuba): Orbit, Trendy, Vlna, Medvěd na ~5 letech
  po čtvrtletích, poctivý walk-forward (nastavení jen z minulosti). Běží po kouscích uvnitř laboratoře (max 2 h/běh,
  postup v `state/multiyear.json`), přepočet 1× týdně. Prošel -> lab.json dostane nejodolnější nastavení; neprošel -> varování.
  Výsledek na konci `lab.txt`. Od 22. 11. v něm je i Stín (`lab.MULTIYEAR_ONLY`, přehrání `colony/shadowtest.py`).
- V dlouhodobém testu jsou od 22. 11. i dva kandidáti, kteří ŽIVĚ NEOBCHODUJÍ: Lev-50 (Lev na 50 největších firmách
  ke konci 2021 - bez zkreslení dnešními hvězdami) a Želva (týdenní trend BTC/ETH). Do kolonie jen když projdou (pravidlo 7).
- `stav.txt` končí řádkem SROVNÁNÍ S TRHEM (kolonie vs. jen držet S&P 500 a bitcoin od startu).
- Vyřazení boti: `config.RETIRED` (Memec, Lovec od 16. 11. 2026; Šeptač, Kapitán, Věštec od 21. 11. 2026; Orbit, Trendy, Vlna, Medvěd
  od 22. 11. 2026 po pětiletém testu - vše na přání Jakuba). Laboratoř vyřazené boty neladí (`lab.active_variants()`).

## Pravidla (nikdy neporušit)
1. **Nikdy nepřepínej na reálné peníze** (`TRADING_MODE=live`, `I_UNDERSTAND_REAL_MONEY`), ani to nenavrhuj
   bez výslovné žádosti Jakuba v chatu.
2. Nikdy necommituj klíče, hesla ani `.env`. Server je má jen lokálně.
3. Před každým pushem do `main` spusť `python tests/test_colony.py` – všechny testy musí projít.
   Nová logika = nový test.
4. Změny dělej malé a vysvětlitelné. Do commit zprávy napiš česky co a proč.
5. Nepřeceňuj výsledky: pár dní papíru je statisticky málo. Při hodnocení uváděj počet obchodů.
6. Pravidla přežití (smrt pod $800, 30 dní bez zisku; dlouhodobí Kompas/Lev/Sokol/Spořič 90 dní - Jakub schválil
   16. 11. 2026, `config.PROFIT_DEADLINE_BY_BOT`) neměň bez souhlasu Jakuba. Mrtvé boty nevzkřišuj
   bez jeho souhlasu (16. 10. sám požádal o vzkříšení Degena -> Degen II, původní je na hřbitově).

7. **Žádný nový bot bez víceletého testu** (Jakub souhlasil 22. 11. 2026): nový bot smí obchodovat (i na papíře)
   až když v dlouhodobém nebo víceletém testu na ~5 letech vydělá a nemá horší propad než držení S&P 500
   (u krypta než držení bitcoinu). Krátké výsledky z papíru nestačí.

## Denní kontrola (naplánovaná úloha)
1. Přečti `reports/stav.txt`, `chyby.txt`, `sync.log`, `lab.txt`, `kronika.txt` (stínové obchody: jak by dopadly
   nekoupené signály - ukazuje, které filtry pomáhají a které škodí) a `pamet.json` (co se boti naučili -
   `colony/memory.py`; když paměť něco zakazuje opakovaně, zvaž úpravu strategie přímo v kódu).
2. Oprav chyby z logu (výpadky zdrojů, výjimky).
3. U botů s dostatkem obchodů (≥ 10) hledej, proč prodělávají; uprav parametry/logiku, ověř backtestem
   (`python backtest.py --days 60` potřebuje Alpaca klíče – v cloudu nejsou, takže spoléhej na testy
   a na laboratoř na serveru).
4. Pushni do `main`, napiš Jakubovi krátké shrnutí (co boti dělali, co se změnilo).
