"""PAY-TAILS Б5.6: the GOLDEN SNAPSHOT of the money.  Package Б changes words and shows, never money: the accounts, the statements, the summary and the «Поступления» of the worlds below are
frozen in ``tests/fixtures/paytails_golden/money.json`` (written by the code BEFORE package Б, commit 0 of its server part) and must stay equal to the last digit while package Б is built.

What is compared is a WHITELIST of money fields (a new word or a new field of package Б is not in it).  The worlds go through the routes of the product: a limited personal period (ЛП), the common month (ОМ),
a package; payments, a forgiveness, a refund, a pause, leavings, a transfer; every world is read on three days.  The windows of a leaving and of an archive of a student are NOT in the snapshot:
their numbers change on purpose (Б5.2, the rule «a day is not paid twice» is seen by the probe).

To write the file again (only the lead decides): ``PAYTAILS_GOLDEN_WRITE=1``."""
import json
import os
from datetime import date
from pathlib import Path

from src.auth import money_reconcile as mr
from src.models.auth import Group, Student, StudentGroup

from tests.money1b_helpers import account, add, fresh_student, set_schedule, slot, transfer
from tests.pg_world import ensure_billing, give_group_price, make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner, settings_block, stay  # noqa: F401  (fixtures)
from tests.test_pay_tails_money_pg import TTF, forgive, leave, lp, om, refund
from tests.test_payments_service_pg import pay

pytestmark = needs_pg
D = date
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "paytails_golden" / "money.json"      # the files are money_<world>.json next to it

LEDGER = ("charged", "paid", "transferred_in", "written_off", "written_off_granted", "refunded", "transferred_out", "debt", "past_debt", "current_due", "advance", "refundable", "partial_credit",
          "covered_through", "recalculated", "reconciles")
STATUS_ROW = ("unpaid", "advance", "refundable", "debt_amount", "due_amount", "due_until", "paid_until", "status", "overdue_days", "is_active", "lessons_left", "partial_credit")
SEGMENT = ("kind", "period_start", "period_end", "amount", "money", "forgiven", "recalculated", "unpaid", "covered_days", "begun", "ended")
SUMMARY = ("debt_total", "debtors", "due_total", "due_students", "advance_total", "students_with_advance", "memberships_with_debt", "tracked_total", "buckets", "bucket_rows", "paused", "free",
           "expiring", "ends_today", "none", "active")
STATEMENT = ("charged", "paid", "transferred_in", "written_off", "refunded", "transferred_out", "unpaid", "advance", "debt_amount", "due_amount", "reconciles", "recalculated")
ROW = ("date", "type", "debit", "credit", "outflow")
OLD_TABS = ("debtors", "to_pay", "expiring", "active", "paused")


def pick(source, keys):
    return {k: (source.get(k).isoformat() if isinstance(source.get(k), date) else source.get(k)) for k in keys if k in source}


def get(w, path, **params):
    w.db.expire_all()
    r = owner(w).get(f"{API}{path}", params=params)
    assert r.status_code == 200, (path, r.text)
    return r.json()


def snapshot(w, day: date) -> dict:
    """Everything the money of the centre says on ``day``, keyed by names (the ids of a run are not stable)."""
    w.db.expire_all()
    out = {"accounts": {}, "statements": {}, "status": {}}
    rows = w.db.query(StudentGroup, Student, Group).join(Student, Student.id == StudentGroup.student_id).join(Group, Group.id == StudentGroup.group_id).filter(Group.org_id == w.org.id).all()
    for m, s, g in sorted(rows, key=lambda t: (t[1].first_name, t[2].name)):
        key = f"{s.first_name} | {g.name}"
        led = account(w, m.id, day)
        view = pick({k: getattr(led, k) for k in LEDGER}, LEDGER)
        view["segments"] = [pick({k: getattr(seg, k) for k in SEGMENT}, SEGMENT) for seg in led.segments]
        view["paid_until"] = led.status.paid_until.isoformat() if led.status.paid_until else None
        out["accounts"][key] = view
        st = get(w, f"/org/memberships/{m.id}/statement")
        out["statements"][key] = {"summary": pick(st["summary"], STATEMENT), "rows": [pick(r, ROW) for r in st["rows"]]}
    for bucket in ("debtors", "to_pay", "expiring", "active", "paused"):
        page = get(w, "/org/payments/status", bucket=bucket, limit=200)
        out["status"][bucket] = {"total": page["total"], "students": page["students"], "amount_total": page["amount_total"],
                                 "items": sorted([{**pick(i, STATUS_ROW), "who": f"{i['student_name']} | {i['group_name']}"} for i in page["items"]], key=lambda i: i["who"])}
    out["summary"] = pick(get(w, "/org/payments/summary"), SUMMARY)
    for key in ("buckets", "bucket_rows"):         # Б1.1 added the count ``advance`` to the tabs: the five that were there are the ones frozen
        out["summary"][key] = {k: v for k, v in out["summary"][key].items() if k in OLD_TABS}
    adv = get(w, "/org/payments/balances", kind="advance")
    out["balances_advance"] = {"total": adv["total"], "advance_total": adv.get("advance_total"), "items": sorted(f"{i['student_name']} | {i['group_name']} | {i['advance']}" for i in adv["items"])}
    income = get(w, "/org/payments/income", **{"from": "2026-10-01", "to": "2026-12-31"})
    out["income"] = {"position": income["position"], "current": income["current"], "checks": income["checks"]}
    return out


# ---------------------------------------------------------------------------------------------------------------------------- the worlds


def personal_world(w, fixed_today):
    """ЛП 420 000, 01.11 - 30.11 / 01.12 - 31.12: six students; a paid, forgiven, left (С2); b paid two periods ahead; c nothing; d 100 000 and left with a debt; e paid and left (advance, refunded in part); f forgiven whole."""
    g, a = lp(w, fixed_today)
    names = {n: fresh_student(w, n) for n in ("Бета", "Вега", "Дельта", "Эпсилон", "Фи")}
    for s in names.values():
        add(w, s, group=g, mode="add")
    pay(w, a, g, amount=200_000, paid_on=D(2026, 11, 1))
    pay(w, names["Бета"], g, amount=420_000, paid_on=D(2026, 11, 1))
    pay(w, names["Дельта"], g, amount=100_000, paid_on=D(2026, 11, 1))
    pay(w, names["Эпсилон"], g, amount=420_000, paid_on=D(2026, 11, 1))
    forgive(w, names["Фи"], g, 420_000)
    fixed_today(D(2026, 11, 2))
    pay(w, names["Бета"], g, amount=420_000, paid_on=D(2026, 11, 2))
    fixed_today(D(2026, 11, 10))
    leave(w, g, names["Дельта"])
    leave(w, g, names["Эпсилон"])                                  # charged 420 000 x 10 / 30 = 140 000: advance 280 000
    refund(w, names["Эпсилон"], g, 100_000, expect=201)
    fixed_today(D(2026, 11, 21))
    forgive(w, a, g, 220_000)
    leave(w, g, a)                                                   # 294 000
    return g


def common_world(w, fixed_today):
    """ОМ 600 000, Tue / Thu / Fri: g paid whole; h 300 000 + forgiven 300 000, left 11.11; i 200 000 paid; j a pause 17.11 - 30.11; k moved to the second group."""
    g, a = om(w, fixed_today)                                        # clock 03.11
    g2 = make_group(w.db, w.org, w.teacher, "Второй ОМ")
    give_group_price(w.db, g2, 500_000)
    set_schedule(w, TTF, g2)
    names = {n: fresh_student(w, n) for n in ("Хи", "Йота", "Каппа", "Лямбда")}
    for s in names.values():
        add(w, s, group=g, mode="add")
    pay(w, a, g, amount=600_000, paid_on=D(2026, 11, 3))
    pay(w, names["Хи"], g, amount=300_000, paid_on=D(2026, 11, 3))
    forgive(w, names["Хи"], g, 300_000)
    pay(w, names["Йота"], g, amount=200_000, paid_on=D(2026, 11, 3))
    pay(w, names["Лямбда"], g, amount=600_000, paid_on=D(2026, 11, 3))
    r = owner(w).post(f"{API}/org/memberships/{membership(w, names['Каппа'], g).id}/pauses", json={"first_day": "2026-11-17", "last_day": "2026-11-30", "reason": "Болезнь"})
    assert r.status_code == 201, r.text
    fixed_today(D(2026, 11, 11))
    leave(w, g, names["Хи"])                                         # 4 lessons x 50 000 = 200 000, advance 100 000 (money)
    transfer(w, names["Лямбда"], g2)
    return g


def package_world(w, fixed_today):
    """Packages of 8 lessons for 400 000 (50 000 a lesson), Tuesday: p1 held 3, paid 150 000; p2 held 8, paid 400 000; p3 held 2 and left."""
    fixed_today(D(2026, 10, 27))
    settings_block(w, accounting_start_date="2026-09-01", period_mode="lessons", lessons_per_payment=8, billing_history=[{"from": "min", "period_mode": "lessons", "lessons_per_payment": 8}])
    give_group_price(w.db, w.group, 400_000)
    set_schedule(w, [slot("s2", 2, "2026-01-01", "18:00", "19:00")])
    s1, s2, s3 = w.students
    tuesdays = [D(2026, 9, 29), D(2026, 10, 6), D(2026, 10, 13), D(2026, 10, 20), D(2026, 10, 27)]
    for s in (s1, s2, s3):
        stay(w, membership(w, s, w.group), D(2026, 9, 29))
    from src.models.auth import GroupAttendance

    for n, day in enumerate(tuesdays):               # one lesson row per day: who was there (s1: three lessons, s2: all, s3: two)
        present = [s for s, upto in ((s1, 3), (s2, 5), (s3, 2)) if n < upto]
        w.db.add(GroupAttendance(org_id=w.org.id, group_id=w.group.id, lesson_date=day.isoformat(), lesson_id="s2", status="completed", records={str(s.id): "present" for s in present},
                                 roster_snapshot=[{"student_id": str(s.id)} for s in present]))
    w.db.commit()
    pay(w, s1, w.group, amount=150_000, paid_on=D(2026, 10, 14))
    pay(w, s2, w.group, amount=400_000, paid_on=D(2026, 9, 30))
    fixed_today(D(2026, 10, 27))
    leave(w, w.group, s3)
    return w.group


def extra_world(w, fixed_today):
    """The holes of the first snapshot (R3 Б6.2): ОМ 600 000 (Tue / Thu / Fri) on 03.11 and a second group of the Fridays 400 000.  Students: a (ОМ) paid 600 000, the centre cancels Tue 24.11 (recalculation on);
    Остаток has an opening «оплачено до 10.11», Долг-остаток an opening debt 100 000 on 31.10; Архивный paid 300 000 and is archived 11.11; Прогнозов is enrolled on 10.11 into the Friday group: the first lesson is
    Friday 13.11, three days later (the forecast)."""
    g, a = om(w, fixed_today)                                        # clock 03.11
    g3 = make_group(w.db, w.org, w.teacher, "Пятничная")
    give_group_price(w.db, g3, 400_000)
    set_schedule(w, [slot("s5", 5, "2026-01-01", "14:00", "15:00")], g3)
    ensure_billing(w, g3, price=None)
    names = {n: fresh_student(w, n) for n in ("Остаток", "Долг-остаток", "Архивный")}
    for s in names.values():
        add(w, s, group=g, mode="add")
    assert owner(w).put(f"{API}/org/settings/payments", json={"centre_cancel_recalc": True}).status_code == 200
    pay(w, a, g, amount=600_000, paid_on=D(2026, 11, 3))
    pay(w, names["Архивный"], g, amount=300_000, paid_on=D(2026, 11, 3))
    r = owner(w).put(f"{API}/org/memberships/{membership(w, names['Остаток'], g).id}/opening-balance", json={"paid_until": "2026-11-10"})
    assert r.status_code == 200, r.text
    r = owner(w).put(f"{API}/org/memberships/{membership(w, names['Долг-остаток'], g).id}/opening-balance", json={"debt_amount": 100_000, "debt_on": "2026-10-31"})
    assert r.status_code == 200, r.text
    r = owner(w).post(f"{API}/org/groups/{g.id}/attendance", json={"lessonDate": "2026-11-24", "lessonId": "s2", "status": "cancelled", "records": {}, "cancellationReason": "Преподаватель заболел", "cancellationKind": "centre"})
    assert r.status_code == 200, r.text
    fixed_today(D(2026, 11, 10))
    add(w, fresh_student(w, "Прогнозов"), group=g3, mode="add")                  # Tuesday 10.11: the first lesson of the Fridays is 13.11, three days later
    fixed_today(D(2026, 11, 11))
    r = owner(w).post(f"{API}/org/students/{names['Архивный'].id}/archive", json={})
    assert r.status_code == 200, r.text
    return g


def check_golden(name: str, w, days, fixed_today):
    shots = {}
    for day in days:
        fixed_today(day)
        shots[f"{name} {day}"] = snapshot(w, day)
        w.db.expire_all()
        probes = [f for f in mr.reconcile_org(w.db, w.org.id, today=day) if f.invariant == "I-6"]            # Б10: the probes of the night find nothing on the accounts of the golden worlds
        assert probes == [], (name, day, [f.details["text"] for f in probes][:3])
    got = json.loads(json.dumps(shots, ensure_ascii=False, sort_keys=True, default=str))
    path = GOLDEN.with_name(f"money_{name}.json")
    if os.environ.get("PAYTAILS_GOLDEN_WRITE") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(got, ensure_ascii=False, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    want = json.loads(path.read_text(encoding="utf-8"))
    assert got.keys() == want.keys()
    for key in want:
        assert got[key] == want[key], key


def test_the_money_of_the_personal_period_is_the_golden_snapshot(world, fixed_today):
    personal_world(world, fixed_today)
    check_golden("personal", world, (D(2026, 11, 21), D(2026, 11, 25), D(2026, 12, 5)), fixed_today)


def test_the_money_of_the_common_month_is_the_golden_snapshot(world, fixed_today):
    common_world(world, fixed_today)
    check_golden("common", world, (D(2026, 11, 11), D(2026, 11, 25), D(2026, 12, 5)), fixed_today)


def test_the_money_of_a_package_is_the_golden_snapshot(world, fixed_today):
    package_world(world, fixed_today)
    check_golden("package", world, (D(2026, 10, 27), D(2026, 11, 10)), fixed_today)


def test_the_money_of_the_holes_of_the_first_snapshot_is_the_golden_snapshot(world, fixed_today):
    extra_world(world, fixed_today)
    check_golden("extra", world, (D(2026, 11, 11), D(2026, 11, 25), D(2026, 12, 5)), fixed_today)
