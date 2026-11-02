"""OPENING-BULK, R3 round (22.10): the Excel «Без остатка» of a student who joined after the start,
what happens between a preview and its apply, the property «a batch = N single PUTs and the ledger reconciles», VOID_BREAKS_REFUND, the size of an Excel file.
Spec: artifacts/partner_doc_check/opening_bulk_spec.md."""
import random
from datetime import date
import time
import tracemalloc
import uuid
from datetime import datetime, timedelta

import pytest

from src.auth import payment_status as ps
from src.models.auth import Student, StudentGroup, StudentPayment

from tests.pg_world import (  # noqa: F401  (fixtures)
    give_group_price, give_price, make_client, make_group, make_student, make_user, make_member, pg_session, world,
)
from tests.test_opening_bulk_pg import (  # noqa: F401
    BASE, COL, XLSX, apply, audits, build_file, centre, enrolled, export_xlsx, import_xlsx, it, ledger_of, live_openings, lst, preview, row_for,
    settings_of, sheet_of, students, to_bytes, ver,
)
from tests.test_payment_carry_over_pg import engine, run_in_thread  # noqa: F401  (engine = fixture)
from tests.test_pay2_api_pg import (  # noqa: F401
    API, D, TODAY, fixed_today, membership, owner, pytestmark, seed_payment, settings_block, stay,
)


# ---- R3 #6: Excel «Без остатка (да)» on a student who joined AFTER the start ---------------------------------------------------------------------


def test_excel_no_opening_flag_on_a_student_who_joined_after_the_start_changes_nothing_or_clears(world, fixed_today):
    centre(world)
    fixed_today(D(2026, 11, 10))
    early = enrolled(world, D(2026, 9, 1), world.students[0])
    (late_plain, late_with) = students(world, 2, first=D(2026, 11, 3), prefix="Поз")
    out = apply(world, [it(world, late_with, debt_amount=5_000, debt_on="2026-11-03")])                 # an opening on his first day (the floor)
    assert out["changed"] == 1
    wb, ws = sheet_of(export_xlsx(world).content)
    for m in (early, late_plain, late_with):
        ws.cell(row=row_for(ws, m), column=ws.COL["none"]).value = "да"
        for k in ("paid", "debt", "debt_on"):
            ws.cell(row=row_for(ws, m), column=ws.COL[k]).value = None
    pv = import_xlsx(world, to_bytes(wb))
    rows = {r["membership_id"]: r for r in pv["rows"] if r["membership_id"]}
    assert pv["ok"] and pv["file"]["errors"] == 0, [r for r in pv["rows"] if r["status"] == "error"]
    assert rows[str(late_plain.id)]["status"] == "noop"                                                  # nothing to review: no OPENING_RANGE, no row
    assert (rows[str(late_with.id)]["status"], rows[str(late_with.id)]["kind"]) == ("ok", "cleared")      # an opening that exists goes
    assert (rows[str(early.id)]["status"], rows[str(early.id)]["kind"]) == ("ok", "paid_until")           # a student of the start day: unchanged rule
    got = {i["membership_id"]: i for i in pv["batch_items"]}
    assert set(got) == {str(early.id), str(late_with.id)} and got[str(late_with.id)]["clear"] is True
    assert got[str(early.id)]["paid_until"] == "2026-10-31"
    assert apply(world, pv["batch_items"], digest=pv["digest"])["changed"] == 2
    held = {o.membership_id: o for o in live_openings(world)}
    assert set(held) == {early.id} and held[early.id].paid_until == D(2026, 10, 31)
    # a student who joined on 31.10 (the day before the start) is still «reviewed» by the flag
    border = students(world, 1, first=D(2026, 10, 31), prefix="Гр")[0]
    wb, ws = sheet_of(export_xlsx(world).content)
    ws.cell(row=row_for(ws, border), column=ws.COL["none"]).value = "да"
    pv = import_xlsx(world, to_bytes(wb))
    assert pv["ok"] and {i["membership_id"]: i for i in pv["batch_items"]}[str(border.id)]["paid_until"] == "2026-10-31"


# ---- R3 matrix: VOID_BREAKS_REFUND --------------------------------------------------------------------------------------------------------------


def _refunded_world(world, fixed_today):
    centre(world)
    fixed_today(D(2026, 11, 25))
    m = enrolled(world, D(2026, 9, 1))
    c = owner(world)
    assert apply(world, [it(world, m, paid_until="2026-12-31")])["changed"] == 1                         # paid until 31.12: November is not charged
    seed_payment(world, m, 600_000, D(2026, 11, 3))
    led = ledger_of(world, m)
    assert (led.debt, led.advance, led.refundable) == (0, 600_000, 600_000)
    r = c.post(f"{API}/org/memberships/{m.id}/refunds", json={"amount": 600_000, "method": "cash", "reason": "ушёл"})
    assert r.status_code == 201, r.text
    return m, c


def test_an_opening_holds_no_money_so_removing_it_never_takes_a_refund_away(world, fixed_today):
    """The refund is limited by the MONEY (payments), never by an opening: the ledger flag ``outflow_exceeds_money`` does not read the opening at all.
    Removing the opening that «paid» November leaves the refund as it was; November becomes a debt, the books reconcile."""
    m, c = _refunded_world(world, fixed_today)
    pv = preview(world, [it(world, m, clear=True)])
    assert pv["ok"] and pv["items"][0]["status"] == "ok" and pv["impact"]["debt_delta"] == 600_000
    out = apply(world, [it(world, m, clear=True)], reason="ошибка ввода")
    assert out["summary"]["cleared"] == 1 and not live_openings(world)
    led = ledger_of(world, m)
    assert led.reconciles and "outflow_exceeds_money" not in led.flags and (led.debt, led.advance, led.refundable) == (600_000, 0, 0)


@pytest.mark.parametrize("where", ["plan", "after_the_writes"])
def test_a_removal_that_would_leave_a_refund_without_money_is_refused_and_nothing_is_written(world, fixed_today, monkeypatch, where):
    """The guard VOID_BREAKS_REFUND of the batch (preview / apply, and the check after the writes).  The ledger flag cannot be raised by an opening today, so the
    flag is injected: the day it can (a future rule), the batch stays atomic and says so with the same code as the void of a payment."""
    from dataclasses import replace

    from src.auth import billing_ledger as bl
    from src.auth import logica_service

    m, c = _refunded_world(world, fixed_today)
    (opening,) = live_openings(world)
    audit_before, ring_before = len(audits(world)), settings_of(world).get("openings_batches")
    state = {"inject": where == "plan"}
    real_run, real_apply = bl.run_ledger, logica_service.OpeningService.apply

    def run(inp):
        led = real_run(inp)
        if state["inject"] and inp.opening is None:                      # the books WITHOUT the opening
            return replace(led, flags=tuple(led.flags) + (bl.F_OUTFLOW_EXCEEDS,))
        return led

    def apply_then_inject(self, *args, **kwargs):
        out = real_apply(self, *args, **kwargs)
        state["inject"] = True                                           # from the first write on, the check after the writes sees the flag
        return out

    monkeypatch.setattr(bl, "run_ledger", run)
    monkeypatch.setattr(logica_service.OpeningService, "apply", apply_then_inject)
    items = [it(world, m, clear=True)]
    if where == "plan":
        pv = preview(world, items)
        assert not pv["ok"] and pv["items"][0]["code"] == "VOID_BREAKS_REFUND"
        r = c.post(f"{BASE}/batch", json={"items": items, "reason": "ошибка ввода", "idempotency_key": str(uuid.uuid4()), "digest": pv["digest"]})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "BATCH_INVALID" and r.json()["detail"]["items"][0]["code"] == "VOID_BREAKS_REFUND", r.text
    else:
        state["inject"] = False
        pv = preview(world, items)
        assert pv["ok"]
        r = c.post(f"{BASE}/batch", json={"items": items, "reason": "ошибка ввода", "idempotency_key": str(uuid.uuid4()), "digest": pv["digest"]})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "VOID_BREAKS_REFUND", r.text
    assert [o.id for o in live_openings(world)] == [opening.id] and len(audits(world)) == audit_before and settings_of(world).get("openings_batches") == ring_before
    monkeypatch.setattr(logica_service.OpeningService, "apply", real_apply)
    monkeypatch.setattr(bl, "run_ledger", real_run)
    assert apply(world, items, reason="ошибка ввода")["summary"]["cleared"] == 1                         # the same batch goes through once the flag is gone


# ---- R3 matrix: between the preview and the apply ----------------------------------------------------------------------------------------------


def _archive(w, a, b):
    w.db.get(Student, a.student_id).status = "archived"
    w.db.commit()


def _new_price(w, a, b):
    give_group_price(w.db, w.group, 750_000, valid_from=D(2026, 11, 1))


def _start_moved(w, a, b):
    assert owner(w).put(f"{API}/org/settings/payments", json={"accounting_start_date": "2026-10-20"}).status_code == 200


def _start_moved_later(w, a, b):
    assert owner(w).put(f"{API}/org/settings/payments", json={"accounting_start_date": "2026-11-15"}).status_code == 200


def _left(w, a, b):
    a.is_active = False
    a.left_at = datetime(2026, 10, 18, 6)
    (period,) = a.periods
    period.left_at = a.left_at
    w.db.commit()


@pytest.mark.parametrize("change,codes", [
    (_archive, {"PREVIEW_STALE"}), (_new_price, {"PREVIEW_STALE"}), (_start_moved, {"PREVIEW_STALE"}), (_start_moved_later, {"PREVIEW_STALE"}),
    (_left, {"PREVIEW_STALE"}),
], ids=["archived", "price_changed", "start_moved_earlier", "start_moved_later", "left_the_group"])
def test_between_the_preview_and_the_apply_the_world_changes_the_apply_refuses_cleanly(world, fixed_today, change, codes):
    centre(world)
    fixed_today(D(2026, 11, 10))                                                                       # November is running: the price matters for the numbers
    a, b = enrolled(world, D(2026, 9, 1), world.students[0]), enrolled(world, D(2026, 9, 1), world.students[1])
    items = [it(world, a, paid_until="2026-11-30"), it(world, b, debt_amount=550_000, debt_on="2026-10-31")]
    pv = preview(world, items)
    assert pv["ok"]
    ring_before, audit_before = settings_of(world).get("openings_batches"), len(audits(world))
    change(world, a, b)
    key = str(uuid.uuid4())
    r = owner(world).post(f"{BASE}/batch", json={"items": items, "reason": "проверка", "idempotency_key": key, "digest": pv["digest"]})
    assert r.status_code == 409 and r.json()["detail"]["code"] in codes, r.text
    if r.json()["detail"]["code"] == "PREVIEW_STALE":
        assert r.json()["detail"]["preview"]["digest"] != pv["digest"]                                 # the fresh preview is in the answer
    assert not live_openings(world) and len(audits(world)) == audit_before
    assert settings_of(world).get("openings_batches") == ring_before                                    # the key is not remembered: the refusal is not an answer
    # the same key with the FRESH preview is a normal request (the refusal did not burn it)
    fresh = r.json()["detail"].get("preview")
    if fresh and fresh["ok"]:
        r2 = owner(world).post(f"{BASE}/batch", json={"items": items, "reason": "проверка", "idempotency_key": key, "digest": fresh["digest"]})
        assert r2.status_code == 200 and r2.json()["replayed"] is False, r2.text
        assert len(live_openings(world)) == 2


def test_an_archived_student_in_a_batch_is_refused_with_his_row_when_the_preview_is_fresh(world, fixed_today):
    """The same case after a NEW preview: the row is an error STUDENT_ARCHIVED and the batch is refused whole (the other rows are not written)."""
    centre(world)
    a, b = enrolled(world, D(2026, 9, 1), world.students[0]), enrolled(world, D(2026, 9, 1), world.students[1])
    items = [it(world, a, paid_until="2026-11-30"), it(world, b, paid_until="2026-11-30")]
    _archive(world, a, b)
    pv = preview(world, items)
    assert not pv["ok"] and {r["membership_id"]: r["code"] for r in pv["items"] if r["status"] == "error"} == {str(a.id): "STUDENT_ARCHIVED"}
    r = owner(world).post(f"{BASE}/batch", json={"items": items, "idempotency_key": str(uuid.uuid4()), "digest": pv["digest"]})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "BATCH_INVALID" and not live_openings(world)


# ---- R3 matrix: a random batch == N single PUTs, and the ledger reconciles ---------------------------------------------------------------------

SEEDS = [3, 11, 29, 57, 101, 2026]


def _books_of(w, m):
    led = ledger_of(w, m)
    return {"debt": led.debt, "advance": led.advance, "refundable": led.refundable, "charged": led.charged, "paid": led.paid, "covered_through": led.covered_through,
            "status": led.status.status, "reconciles": led.reconciles, "flags": tuple(sorted(led.flags))}


def _random_value(rnd, floor):
    kind = rnd.choice(["paid", "debt", "both", "paid", "debt"])
    value = {}
    if kind in ("paid", "both"):
        value["paid_until"] = (floor + timedelta(days=rnd.randint(0, 90))).isoformat()
    if kind in ("debt", "both"):
        value["debt_amount"] = rnd.randint(10, 2_000) * 1000
        value["debt_on"] = (floor - timedelta(days=rnd.randint(0, 60))).isoformat()
    return value


@pytest.mark.parametrize("seed", SEEDS)
def test_a_random_batch_equals_the_same_single_puts_and_the_ledger_reconciles(world, fixed_today, seed):
    rnd = random.Random(seed)
    centre(world)
    fixed_today(rnd.choice([D(2026, 10, 21), D(2026, 11, 10), D(2026, 12, 5)]))
    start = D(2026, 11, 1)
    pairs = []                                                                         # (batch member, twin for the single PUTs, floor)
    for i in range(6):
        first = rnd.choice([D(2026, 8, 20), D(2026, 9, 1), D(2026, 10, 15), D(2026, 11, 3)])
        x = students(world, 1, first=first, prefix=f"Б{i}")[0]
        y = students(world, 1, first=first, prefix=f"О{i}")[0]
        pairs.append((x, y, max(first, start - timedelta(days=1))))
    for x, y, _ in pairs:                                                              # money on some of them (a reason is then needed - both ways)
        if rnd.random() < 0.4:
            day = rnd.choice([D(2026, 10, 5), D(2026, 11, 2)])
            if day <= ps.business_today():
                amount = rnd.choice([300_000, 600_000, 900_000])
                seed_payment(world, x, amount, day)
                seed_payment(world, y, amount, day)
    for round_no in (1, 2):
        batch, singles = [], []
        for x, y, floor in pairs:
            has = round_no == 2 and bool(next((o for o in live_openings(world) if o.membership_id == x.id), None))
            choice = rnd.choice(["set", "set", "skip", "same", "clear"] if has else ["set", "set", "skip"])
            if choice == "skip":
                continue
            if choice == "clear":
                batch.append(it(world, x, clear=True))
                singles.append((y, {}))
            elif choice == "same":
                o = next(o for o in live_openings(world) if o.membership_id == x.id)
                value = {k: v for k, v in (("paid_until", o.paid_until), ("debt_amount", o.debt_amount), ("debt_on", o.debt_on)) if v is not None}
                value = {k: (v.isoformat() if isinstance(v, date) else int(v)) for k, v in value.items()}
                batch.append(it(world, x, **value))
                singles.append((y, value))
            else:
                value = _random_value(rnd, floor)
                batch.append(it(world, x, **value))
                singles.append((y, value))
        if not batch:
            continue
        before_a = {x.id: _books_of(world, x) for x, _, _ in pairs}
        before_b = {y.id: _books_of(world, y) for _, y, _ in pairs}
        pv = preview(world, batch, reason="случайная проверка")
        out = apply(world, batch, reason="случайная проверка", digest=pv["digest"])
        for y, value in singles:
            r = owner(world).put(f"{API}/org/memberships/{y.id}/opening-balance", json={**value, "reason": "случайная проверка"})
            assert r.status_code == 200, (seed, round_no, value, r.text[:300])
        after_a = {x.id: _books_of(world, x) for x, _, _ in pairs}
        after_b = {y.id: _books_of(world, y) for _, y, _ in pairs}
        for x, y, _ in pairs:
            a, b = after_a[x.id], after_b[y.id]
            assert a == b, (seed, round_no, a, b)                                      # I5: the batch changed the books exactly like the single PUTs did
            assert a["reconciles"] and "outflow_exceeds_money" not in a["flags"]
        delta_a = sum(after_a[k]["debt"] - before_a[k]["debt"] for k in after_a)
        delta_b = sum(after_b[k]["debt"] - before_b[k]["debt"] for k in after_b)
        assert delta_a == delta_b == out["impact"]["debt_delta"] == pv["impact"]["debt_delta"], (seed, round_no)
        assert out["summary"] == pv["summary"] and out["changed"] == pv["summary"]["changed"]
    # the openings themselves are the same rows (values), and the audit tells the same story per membership
    held = {o.membership_id: (o.paid_until, o.debt_amount, o.debt_on) for o in live_openings(world)}
    assert held, seed
    for x, y, _ in pairs:
        assert held.get(x.id) == held.get(y.id), (seed, held.get(x.id), held.get(y.id))
    snaps = {}
    for a in audits(world):
        snaps.setdefault(a.snapshot["membership_id"], []).append((a.action, a.snapshot["debt_before"], a.snapshot["debt_after"], a.snapshot["paid_until"]))
    for x, y, _ in pairs:
        assert sorted(snaps.get(str(x.id), []), key=str) == sorted(snaps.get(str(y.id), []), key=str), (seed, x.id)


# ---- R3 matrix: Excel with 5 000 rows - time and memory ----------------------------------------------------------------------------------------

EXPORT_SECONDS, IMPORT_SECONDS, APPLY_SECONDS, PEAK_MB = 30, 45, 180, 300       # CI-runner bounds; the measured values are several times lower (see the print)


def _big_centre(w, n):
    """``n`` students in ONE group, all enrolled on 01.09 (bulk inserts: one flush; the periods come from the ``before_flush`` hook)."""
    from tests.pg_world import make_group

    centre(w)
    ids = []
    for i in range(n):
        s = Student(org_id=w.org.id, first_name=f"Имя{i:04d}", last_name=f"Фамилия{i:04d}", class_level=9, group_id=w.group.id, display_id=f"BIG-{i:05d}")
        w.db.add(s)
        ids.append(s)
    w.db.flush()
    for s in ids:
        w.db.add(StudentGroup(student_id=s.id, group_id=w.group.id, is_active=True, joined_at=datetime(2026, 9, 1, 6)))
    w.db.commit()
    return n


def _timed(call):
    """``(seconds, peak_MB, result)``: the time without the tracer (it slows the code down), the peak of Python memory with it (a second run)."""
    started = time.perf_counter()
    result = call()
    seconds = time.perf_counter() - started
    tracemalloc.start()
    try:
        call()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return seconds, peak / 1e6, result


def test_excel_export_and_import_of_5000_rows_stay_within_a_time_and_memory_bound(world, fixed_today):
    n = _big_centre(world, 5000 - len(world.students)) + len(world.students)                      # exactly 5 000 memberships = the biggest allowed file
    c = owner(world)
    export_s, export_mb, r = _timed(lambda: c.get(f"{BASE}/export"))
    assert r.status_code == 200 and len(r.content) < 5 * 1024 * 1024, "the export of the biggest allowed table is itself importable (< 5 MB)"
    wb, ws = sheet_of(r.content)
    assert ws.max_row == ws.FIRST + n                                                              # n rows and the blue «Итого»
    # a re-uploaded export is «no changes»; 1 999 rows get a value (below the limit of 2 000 changes)
    for row in range(ws.FIRST, ws.FIRST + 1999):
        ws.cell(row=row, column=ws.COL["paid"]).value = datetime(2026, 11, 30)
    data = to_bytes(wb)
    assert len(data) < 5 * 1024 * 1024
    import_s, import_mb, r = _timed(lambda: c.post(f"{BASE}/import/preview", files={"file": ("ostatki.xlsx", data, XLSX)}))
    assert r.status_code == 200, r.text[:300]
    pv = r.json()
    assert pv["file"]["rows_read"] == n and pv["file"]["changed"] == 1999 and pv["file"]["errors"] == 0 and len(pv["batch_items"]) == 1999
    # the apply of those 1 999 rows (the biggest Excel batch), once, traced
    tracemalloc.start()
    started = time.perf_counter()
    r = c.post(f"{BASE}/batch", json={"items": pv["batch_items"], "idempotency_key": str(uuid.uuid4()), "digest": pv["digest"]})
    apply_s, apply_mb = time.perf_counter() - started, tracemalloc.get_traced_memory()[1] / 1e6
    tracemalloc.stop()
    assert r.status_code == 200 and r.json()["changed"] == 1999, r.text[:300]
    print(f"EXCEL5000 apply of 1999 rows (traced) {apply_s:.1f}s {apply_mb:.0f}MB")
    assert apply_s < APPLY_SECONDS and apply_mb < PEAK_MB, (apply_s, apply_mb)
    print(f"EXCEL5000 export {export_s:.1f}s {export_mb:.0f}MB; import {import_s:.1f}s {import_mb:.0f}MB")
    assert export_s < EXPORT_SECONDS and import_s < IMPORT_SECONDS, (export_s, import_s)
    assert export_mb < PEAK_MB and import_mb < PEAK_MB, (export_mb, import_mb)
