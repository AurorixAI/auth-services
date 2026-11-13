"""Money-1 / G-14 (N16): «подтвердить пакетом» shows the total of the package before the decision - the same calculation as the single decision, summed (PostgreSQL + a unit test of the sum)."""
from src.auth import money_effects as me
from src.models.auth import AttendanceExcuse

from tests.pg_world import excused_lesson, make_client, needs_pg, payments_block, pg_session, world  # noqa: F401  (fixtures)

API = "/api/v1/auth"
NB = " "


def sheet_line(changed, *, teacher="t1", month="2026-10", before=1_000_000, balance=200_000):
    return {"month": month, "state": "open", "sheetId": "s-" + teacher, "teacherId": teacher, "teacherName": "Иванов А.", "savedTotal": before, "paid": 800_000,
            "freshBefore": before, "freshAfter": before + changed, "freshTotal": before + changed, "changedBy": changed, "driftAfter": changed, "balanceAfter": balance + changed}


def test_g14_merge_adds_the_lessons_of_a_student_and_the_change_of_a_sheet_never_recalculates_it():
    """Two lessons of one student (+1, +1), a third of another (+1); the sheet of one teacher moves by −62 500 twice: −125 000, the balance after = before − 125 000."""
    block = lambda sid, delta, changed: {"students": [{"studentId": sid, "name": "Сидоров " + sid, "delta": delta}], "payroll": [sheet_line(changed)], "text": "", "dryRun": True}
    merged = me.merge_blocks([block("a", 1, -62_500), block("a", 1, -62_500), block("b", 1, -62_500)])
    assert [(s["studentId"], s["delta"]) for s in merged["students"]] == [("a", 2), ("b", 1)]
    assert (merged["lessonsBack"], merged["lessonsSpent"], merged["dryRun"]) == (3, 0, True)
    (line,) = merged["payroll"]
    assert (line["changedBy"], line["freshAfter"], line["driftAfter"], line["balanceAfter"]) == (-187_500, 812_500, -187_500, 12_500)
    assert merged["text"].startswith("В пакеты вернётся 3 урока") and "изменится на" in merged["text"]


def test_g14_merge_of_nothing_is_an_empty_block():
    assert me.merge_blocks([]) == {"students": [], "lessonsBack": 0, "lessonsSpent": 0, "payroll": [], "text": "", "dryRun": True}


def two_package_excuses(w):
    payments_block(w.db, w.org, period_mode="lessons", lessons_per_payment=12, billing_history=[{"from": "min", "period_mode": "lessons", "lessons_per_payment": 12}])
    excused_lesson(w, offset_days=1, complete=True)
    excused_lesson(w, offset_days=2, complete=True)
    w.db.expire_all()
    return [str(e.id) for e in w.db.query(AttendanceExcuse).filter_by(org_id=w.org.id).all()]


def statuses(w):
    w.db.expire_all()
    return sorted(e.status for e in w.db.query(AttendanceExcuse).filter_by(org_id=w.org.id).all())


def test_g14_the_preview_of_a_package_counts_its_lessons_and_writes_nothing(world):
    """Was: no preview - 200 excuses decided blind.  Now: two lessons of one student's pack go back, the same as two single previews say; every excuse stays pending."""
    ids = two_package_excuses(world)
    owner = make_client(world, user=world.owner)
    dry = owner.post(f"{API}/org/attendance/excuses/review-bulk", params={"dry_run": True}, json={"ids": ids, "decision": "confirmed"})
    assert dry.status_code == 200, dry.text
    body = dry.json()
    assert (body["updated"], body["skipped"], body["dry_run"]) == (2, [], True)
    effects = body["money_effects"]
    assert (effects["lessonsBack"], effects["lessonsSpent"], [s["delta"] for s in effects["students"]]) == (2, 0, [2])
    assert effects["text"].startswith("В пакеты вернётся 2 урока")
    assert statuses(world) == ["pending", "pending"]
    singles = [owner.post(f"{API}/org/attendance/excuses/{i}/review", params={"dry_run": True}, json={"decision": "confirmed"}).json()["money_effects"] for i in ids]
    assert sum(s["delta"] for e in singles for s in e["students"]) == effects["lessonsBack"]             # the same calculation as the single decision
    done = owner.post(f"{API}/org/attendance/excuses/review-bulk", json={"ids": ids, "decision": "confirmed"})
    assert done.status_code == 200 and (done.json()["updated"], done.json()["dry_run"], done.json()["money_effects"]) == (2, False, None)
    assert statuses(world) == ["confirmed", "confirmed"]


def test_g14_a_rejection_spends_nothing_and_a_decided_item_is_skipped_in_the_preview_as_in_the_decision(world):
    ids = two_package_excuses(world)
    owner = make_client(world, user=world.owner)
    assert owner.post(f"{API}/org/attendance/excuses/{ids[0]}/review", json={"decision": "confirmed"}).status_code == 200
    dry = owner.post(f"{API}/org/attendance/excuses/review-bulk", params={"dry_run": True}, json={"ids": ids, "decision": "rejected"}).json()
    assert dry["updated"] == 1 and len(dry["skipped"]) == 1 and dry["skipped"][0]["id"] == ids[0]
    assert dry["money_effects"]["lessonsSpent"] == 0 and dry["money_effects"]["lessonsBack"] == 0
