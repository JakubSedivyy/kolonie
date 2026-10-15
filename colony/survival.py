"""Pravidla přežití: smrt, zranění (denní limit ztráty), nájem."""
from __future__ import annotations
from datetime import datetime

from . import config


def check(wallet, now: datetime, equity: float) -> list[tuple[str, str]]:
    """Vrátí seznam událostí [(typ, text)]. Typy: NEW_DAY, INJURED, HEALED, DEATH."""
    ev = []
    if wallet.status == "DEAD":
        return ev
    today = now.strftime("%Y-%m-%d")

    # nový den: zaplatit nájem, vyléčit zranění, resetovat denní limit
    if wallet.day != today:
        if wallet.day:
            wallet.cash -= config.RENT_PER_DAY
            wallet.rent_paid += config.RENT_PER_DAY
            equity -= config.RENT_PER_DAY
        if wallet.status == "INJURED":
            wallet.status = "ALIVE"
            ev.append(("HEALED", f"{wallet.name} se vyléčil a zase obchoduje"))
        wallet.day = today
        wallet.day_start_equity = equity
        ev.append(("NEW_DAY", today))

    # smrt: pád pod hranici
    if equity <= config.DEATH_EQUITY:
        ev.append(("DEATH", f"zbankrotoval (${equity:.2f} < ${config.DEATH_EQUITY:.0f})"))
        return ev

    # smrt: po termínu nevydělal
    born = datetime.fromisoformat(wallet.born)
    age_days = (now - born).total_seconds() / 86400
    if age_days >= config.PROFIT_DEADLINE_DAYS and equity <= wallet.start:
        ev.append(("DEATH", f"za {config.PROFIT_DEADLINE_DAYS} dní nevydělal (${equity:.2f})"))
        return ev

    # zranění: denní ztráta
    if wallet.status == "ALIVE" and wallet.day_start_equity > 0:
        dd = 1 - equity / wallet.day_start_equity
        if dd >= config.DAILY_LOSS_LIMIT:
            wallet.status = "INJURED"
            ev.append(("INJURED", f"{wallet.name} dnes ztratil {dd*100:.1f} % - do zítřka nesmí obchodovat"))
    return ev


def kill(wallet, now: datetime, cause: str):
    wallet.status = "DEAD"
    wallet.cause_of_death = cause
    wallet.died = now.isoformat()
