"""Money-1 pack B: shared helpers of the PostgreSQL tests (a group with a real schedule, the account of a membership through the real loader)."""
from datetime import date

from sqlalchemy.orm.attributes import flag_modified

from src.auth import billing_ledger as bl
from src.auth import ledger_service as ls
from src.auth import payment_status as ps


def slot(slot_id: str, weekday: int, valid_from: str, start: str = "10:00", end: str = "11:00") -> dict:
    """A weekly slot of the schedule of a group (``weekday``: 1 = Monday)."""
    return {"id": slot_id, "dayOfWeek": weekday, "startTime": start, "endTime": end, "format": "offline", "validFrom": valid_from}


def set_schedule(w, slots, group=None):
    group = group or w.group
    group.schedule = list(slots)
    flag_modified(group, "schedule")
    w.db.commit()


def account(w, membership_id, today: date | None = None) -> bl.Ledger:
    """The ledger of a membership by the REAL loader (the schedule, the prices, the payments of the database)."""
    w.db.expire_all()
    loader = ls.LedgerLoader(w.db, str(w.org.id), today or ps.business_today())
    loader.load_facts([membership_id])
    return bl.run_ledger(loader.build_input(membership_id))


def charge_of(ledger: bl.Ledger, period_start: date) -> int:
    """The charge of the period (cell) that starts on ``period_start``."""
    (seg,) = [s for s in ledger.segments if s.kind == "period" and s.period_start == period_start]
    return seg.amount
