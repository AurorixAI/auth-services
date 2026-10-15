"""N13 P1: admin API for excused absences - permissions, tenancy, decisions, files (PostgreSQL)."""
from datetime import timedelta
from uuid import uuid4

import pytest

from src.models.auth import AttendanceExcuse, AttendanceExcuseFile, GroupAttendance, GroupAttendanceAudit

from tests.pg_world import (  # noqa: F401  (fixtures)
    business_today, excused_lesson, make_client, needs_pg, pg_session, reconcile, save, world,
)
from tests.test_attendance_excuses import JPEG, PDF, PNG, excuse_of, key_of

pytestmark = needs_pg

BASE = "/api/v1/auth/org/attendance/excuses"


def window():
    """Explicit period: the default (current month) would miss yesterday on the 1st."""
    today = business_today()
    return {"date_from": (today - timedelta(days=20)).isoformat(), "date_to": today.isoformat()}


@pytest.fixture
def lesson(world):
    day, lesson_id = excused_lesson(world)
    return day, lesson_id, excuse_of(world)


def audit_actions(w, att_id):
    w.db.expire_all()
    return [a.action for a in w.db.query(GroupAttendanceAudit).filter_by(attendance_id=att_id).order_by(GroupAttendanceAudit.created_at)]


def upload(client, excuse_id, data, name="spravka.png", ctype="image/png"):
    return client.post(f"{BASE}/{excuse_id}/files", files={"file": (name, data, ctype)})


# ============================================================================
# Permissions and tenancy
# ============================================================================


def all_new_requests(excuse_id, file_id):
    return [
        ("get", BASE, None), ("get", f"{BASE}/summary", None),
        ("post", f"{BASE}/{excuse_id}/review", {"json": {"decision": "confirmed"}}),
        ("post", f"{BASE}/review-bulk", {"json": {"ids": [str(excuse_id)], "decision": "confirmed"}}),
        ("post", f"{BASE}/{excuse_id}/reset", None),
        ("post", f"{BASE}/{excuse_id}/files", {"files": {"file": ("a.png", PNG, "image/png")}}),
        ("get", f"{BASE}/files/{file_id}", None), ("delete", f"{BASE}/files/{file_id}", None),
    ]


def test_teacher_gets_403_on_every_new_endpoint(world, lesson):
    client = make_client(world, user=world.teacher)
    for method, url, kw in all_new_requests(lesson[2].id, uuid4()):
        r = getattr(client, method)(url, **(kw or {}))
        assert r.status_code == 403, (method, url, r.status_code)


def test_superadmin_without_organisation_gets_403_not_500(world, lesson):
    client = make_client(world, user=world.admin, org=None, role="superadmin")
    for method, url, kw in all_new_requests(lesson[2].id, uuid4()):
        r = getattr(client, method)(url, **(kw or {}))
        assert r.status_code == 403 and "организ" in r.json()["detail"], (method, url, r.text)


def test_other_organisation_sees_nothing_and_gets_404(world, lesson):
    excuse = lesson[2]
    foreign = make_client(world, user=world.foreign_admin, org="other")
    assert foreign.get(BASE, params={"status": "all"}).json() == {"items": [], "total": 0, "pending_total": 0}
    summary = foreign.get(f"{BASE}/summary").json()
    assert summary["pending_total"] == 0
    owner = make_client(world)
    stored = owner.post(f"{BASE}/{excuse.id}/files", files={"file": ("a.png", PNG, "image/png")}).json()
    for method, url, kw in [
        ("post", f"{BASE}/{excuse.id}/review", {"json": {"decision": "confirmed"}}),
        ("post", f"{BASE}/{excuse.id}/reset", None),
        ("post", f"{BASE}/{excuse.id}/files", {"files": {"file": ("a.png", PNG, "image/png")}}),
        ("get", f"{BASE}/files/{stored['id']}", None),
        ("delete", f"{BASE}/files/{stored['id']}", None),
    ]:
        assert getattr(foreign, method)(url, **(kw or {})).status_code == 404, (method, url)
    bulk = foreign.post(f"{BASE}/review-bulk", json={"ids": [str(excuse.id)], "decision": "confirmed"}).json()
    assert bulk["updated"] == 0 and len(bulk["skipped"]) == 1
    world.db.expire_all()
    assert excuse_of(world).status == "pending"


def test_unknown_excuse_is_404(world):
    client = make_client(world)
    assert client.post(f"{BASE}/{uuid4()}/review", json={"decision": "confirmed"}).status_code == 404
    assert client.post(f"{BASE}/{uuid4()}/reset").status_code == 404
    assert client.get(f"{BASE}/files/{uuid4()}").status_code == 404
    assert client.delete(f"{BASE}/files/{uuid4()}").status_code == 404


# ============================================================================
# List / summary
# ============================================================================


def test_list_shape_filters_and_counters(world, lesson):
    day, lesson_id, excuse = lesson
    client = make_client(world, user=world.owner)
    data = client.get(BASE, params=window()).json()
    assert data["total"] == 1 and data["pending_total"] == 1
    item = data["items"][0]
    assert item["id"] == str(excuse.id) and item["lesson_date"] == day.isoformat()
    assert item["lesson_key"] == key_of(day, lesson_id)
    assert item["group"] == {"id": str(world.group.id), "name": world.group.name}
    assert item["teacher"]["id"] == str(world.teacher.id) and item["teacher"]["name"]
    student = world.students[0]
    assert item["student"] == {"id": str(student.id), "full_name": f"{student.last_name} {student.first_name}",
                               "display_id": student.display_id, "phone": None}
    assert item["reason"] == "Болел, написал в Telegram" and item["status"] == "pending"
    assert item["reason_by"]["id"] == str(world.admin.id)
    assert item["reviewed_by"] is None and item["reviewed_at"] is None and item["files"] == []
    assert client.get(f"{BASE}/summary").json()["pending_total"] == 1

    assert client.get(BASE, params={**window(), "status": "confirmed"}).json()["total"] == 0
    assert client.get(BASE, params={**window(), "status": "all"}).json()["total"] == 1
    assert client.get(BASE, params={**window(), "group_id": str(world.group.id)}).json()["total"] == 1
    assert client.get(BASE, params={**window(), "group_id": str(uuid4())}).json()["total"] == 0
    assert client.get(BASE, params={**window(), "teacher_id": str(world.teacher.id)}).json()["total"] == 1
    assert client.get(BASE, params={**window(), "teacher_id": str(world.teacher2.id)}).json()["total"] == 0
    for term in ("каримова", "АЛИЯ", "n13-001"):
        assert client.get(BASE, params={**window(), "search": term}).json()["total"] == 1, term
    assert client.get(BASE, params={**window(), "search": "юсупов"}).json()["total"] == 0
    assert client.get(BASE, params={**window(), "search": "%"}).json()["total"] == 0  # LIKE wildcards are literal
    # The period filters on the lesson date.
    far = {"date_from": (day + timedelta(days=1)).isoformat(), "date_to": (day + timedelta(days=5)).isoformat()}
    assert client.get(BASE, params=far).json()["total"] == 0
    around = {"date_from": (day - timedelta(days=1)).isoformat(), "date_to": (day + timedelta(days=1)).isoformat()}
    assert client.get(BASE, params=around).json()["total"] == 1


def _all_keys(node):
    """Every dict key anywhere inside ``node`` (dicts and lists, recursively)."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield str(key)
            yield from _all_keys(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _all_keys(value)


def test_list_carries_the_student_phone_and_no_money_fields(world, lesson):
    world.students[0].student_phone = "+998901234567"
    world.db.commit()
    item = make_client(world, user=world.owner).get(BASE, params=window()).json()["items"][0]
    assert item["student"]["phone"] == "+998901234567"
    # The student block is an allow-list: contact data yes, anything financial no.
    assert set(item["student"]) == {"id", "full_name", "display_id", "phone"}
    keys = [key.lower() for key in _all_keys(item)]      # the field NAMES: values (random ids) may contain "fee"
    assert keys and not any(
        word in key for key in keys for word in ("balance", "fee", "monthly", "amount", "payment", "price", "debt")
    )
    world.students[0].student_phone = ""
    world.db.commit()
    again = make_client(world, user=world.owner).get(BASE, params=window()).json()["items"][0]
    assert again["student"]["phone"] is None  # empty is "no phone", not an empty link


def test_list_validation_errors_are_422(world):
    client = make_client(world)
    for params in (
        {"status": "bogus"}, {"limit": 0}, {"limit": 201}, {"offset": -1}, {"group_id": "nope"},
        {"date_from": "2026-10-01"}, {"date_from": "2026-13-01", "date_to": "2026-13-02"},
        {"date_from": "2026-10-05", "date_to": "2026-10-01"},
        {"date_from": "2026-01-01", "date_to": "2026-04-05"},  # 95 days
    ):
        assert client.get(BASE, params=params).status_code == 422, params
    ok = client.get(BASE, params={"date_from": "2026-01-01", "date_to": "2026-04-03"})  # exactly 93 days
    assert ok.status_code == 200


def test_pagination_and_pending_total(world):
    for offset in (1, 2, 3):
        excused_lesson(world, offset_days=offset)
    client = make_client(world)
    page1 = client.get(BASE, params={"limit": 2, "offset": 0, "date_from": (business_today() - timedelta(days=10)).isoformat(),
                                     "date_to": business_today().isoformat()}).json()
    page2 = client.get(BASE, params={"limit": 2, "offset": 2, "date_from": (business_today() - timedelta(days=10)).isoformat(),
                                     "date_to": business_today().isoformat()}).json()
    assert (len(page1["items"]), len(page2["items"]), page1["total"], page1["pending_total"]) == (2, 1, 3, 3)
    ids = [i["id"] for i in page1["items"] + page2["items"]]
    assert len(set(ids)) == 3
    dates = [i["lesson_date"] for i in page1["items"] + page2["items"]]
    assert dates == sorted(dates, reverse=True)  # newest lesson first


# ============================================================================
# Decisions
# ============================================================================


def test_review_confirm_reject_reset_and_audit(world, lesson):
    day, lesson_id, excuse = lesson
    client = make_client(world, user=world.owner)
    r = client.post(f"{BASE}/{excuse.id}/review", json={"decision": "confirmed", "note": "  Справка принесена  "})
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["status"] == "confirmed" and item["review_note"] == "Справка принесена"
    assert item["reviewed_by"]["id"] == str(world.owner.id) and item["reviewed_at"]
    # A decision can be changed; "reset" returns the excuse to the queue.
    assert client.post(f"{BASE}/{excuse.id}/review", json={"decision": "rejected"}).json()["status"] == "rejected"
    assert client.get(BASE, params={**window(), "status": "rejected"}).json()["total"] == 1
    reset = client.post(f"{BASE}/{excuse.id}/reset").json()
    assert (reset["status"], reset["reviewed_by"], reset["reviewed_at"], reset["review_note"]) == ("pending", None, None, None)
    assert client.get(f"{BASE}/summary").json()["pending_total"] == 1
    # Reset of a pending excuse is a harmless no-op without a journal row.
    before = audit_actions(world, excuse.attendance_id)
    client.post(f"{BASE}/{excuse.id}/reset")
    after = audit_actions(world, excuse.attendance_id)
    assert after == before and after.count("excuse_reviewed") == 2 and after.count("excuse_reset") == 1
    # The lesson and its marks are untouched.
    world.db.expire_all()
    att = world.db.get(GroupAttendance, excuse.attendance_id)
    assert att.records[str(world.students[0].id)] == "excused"
    rows = world.db.query(GroupAttendanceAudit).filter_by(attendance_id=att.id).filter(
        GroupAttendanceAudit.action.in_(["excuse_reviewed", "excuse_reset"])).order_by(GroupAttendanceAudit.created_at).all()
    assert all(r.after_status == att.status and r.after_records == att.records for r in rows)
    sid = str(world.students[0].id)
    assert [r.details for r in rows] == [
        {"excuse_id": str(excuse.id), "student_id": sid, "decision_before": "pending", "decision_after": "confirmed", "note": "Справка принесена"},
        {"excuse_id": str(excuse.id), "student_id": sid, "decision_before": "confirmed", "decision_after": "rejected", "note": None},
        {"excuse_id": str(excuse.id), "student_id": sid, "decision_before": "rejected", "decision_after": "pending", "note": None},
    ]


@pytest.mark.parametrize("body", [{"decision": "maybe"}, {}, {"decision": "confirmed", "note": "x" * 501}])
def test_review_validation_is_422(world, lesson, body):
    assert make_client(world).post(f"{BASE}/{lesson[2].id}/review", json=body).status_code == 422


def test_review_conflicts_when_student_is_no_longer_excused(world, lesson):
    day, lesson_id, excuse = lesson
    att = world.db.get(GroupAttendance, excuse.attendance_id)
    records = dict(att.records)
    records[str(world.students[0].id)] = "present"
    att.records = records  # an inconsistent legacy row: the excuse outlived the mark
    world.db.commit()
    r = make_client(world).post(f"{BASE}/{excuse.id}/review", json={"decision": "confirmed"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "NOT_EXCUSED" and "уважительн" in r.json()["detail"]["message"]
    world.db.expire_all()
    assert excuse_of(world).status == "pending"


def test_bulk_review_updates_and_reports_skipped(world):
    ids = []
    for offset in (1, 2, 3):
        excused_lesson(world, offset_days=offset)
    world.db.expire_all()
    ids = [e.id for e in world.db.query(AttendanceExcuse).filter_by(org_id=world.org.id).all()]
    client = make_client(world)
    client.post(f"{BASE}/{ids[0]}/review", json={"decision": "rejected"})
    foreign = uuid4()
    r = client.post(f"{BASE}/review-bulk", json={"ids": [str(i) for i in ids] + [str(foreign), str(ids[1])],
                                                 "decision": "confirmed", "note": "ok"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updated"] == 2
    skipped = {s["id"]: s["reason"] for s in body["skipped"]}
    assert set(skipped) == {str(ids[0]), str(foreign)}  # already decided / unknown; duplicates collapse
    assert all(skipped.values())
    world.db.expire_all()
    statuses = sorted(e.status for e in world.db.query(AttendanceExcuse).filter_by(org_id=world.org.id))
    assert statuses == ["confirmed", "confirmed", "rejected"]
    for bad in ({"ids": [], "decision": "confirmed"}, {"ids": [str(uuid4())] * 201, "decision": "confirmed"},
                {"ids": ["x"], "decision": "confirmed"}, {"ids": [str(ids[0])], "decision": "nope"}):
        assert client.post(f"{BASE}/review-bulk", json=bad).status_code == 422, bad


# ============================================================================
# Files
# ============================================================================


def test_file_type_is_checked_by_signature_not_by_name(world, lesson):
    client = make_client(world)
    excuse = lesson[2]
    # A PNG pretending to be a PDF is stored as what it really is.
    r = upload(client, excuse.id, PNG, name="scan.pdf", ctype="application/pdf")
    assert r.status_code == 200, r.text
    assert r.json()["content_type"] == "image/png" and r.json()["size_bytes"] == len(PNG)
    assert r.json()["filename"].endswith(".png")
    # Text with an image name and type is refused.
    for data, name, ctype in ((b"<script>alert(1)</script>", "x.jpg", "image/jpeg"), (b"GIF89a....", "x.gif", "image/gif"),
                              (b"MZ\x90\x00", "x.pdf", "application/pdf")):
        assert upload(client, excuse.id, data, name, ctype).status_code == 415, name
    assert upload(client, excuse.id, b"", "empty.png").status_code == 422
    assert upload(client, excuse.id, JPEG, "a.jpg", "image/jpeg").json()["content_type"] == "image/jpeg"
    assert upload(client, excuse.id, PDF, "a.pdf", "application/pdf").json()["content_type"] == "application/pdf"


def test_file_size_limit_and_three_file_cap(world, lesson):
    client = make_client(world)
    excuse = lesson[2]
    assert upload(client, excuse.id, PNG + b"\x00" * (2 * 1024 * 1024 - len(PNG) + 1)).status_code == 413
    assert upload(client, excuse.id, PNG + b"\x00" * (2 * 1024 * 1024 - len(PNG))).status_code == 200  # exactly 2 MB
    assert upload(client, excuse.id, JPEG, "b.jpg").status_code == 200
    assert upload(client, excuse.id, PDF, "c.pdf").status_code == 200
    assert upload(client, excuse.id, PNG, "d.png").status_code == 409
    items = client.get(BASE).json()["items"][0]
    assert len(items["files"]) == 3
    assert all(set(f) == {"id", "filename", "content_type", "size_bytes"} for f in items["files"])
    assert "data" not in str(items)


def test_download_headers_and_delete_with_audit(world, lesson):
    client = make_client(world)
    excuse = lesson[2]
    file_id = upload(client, excuse.id, PDF, name="Справка №1 (врач).pdf", ctype="application/pdf").json()["id"]
    r = client.get(f"{BASE}/files/{file_id}")
    assert r.status_code == 200 and r.content == PDF
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["cache-control"] == "private, no-store"
    assert r.headers["content-disposition"].startswith("inline; filename=")
    assert "\n" not in r.headers["content-disposition"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"] == "sandbox"
    assert client.delete(f"{BASE}/files/{file_id}").json() == {"ok": True}
    assert client.get(f"{BASE}/files/{file_id}").status_code == 404
    assert client.delete(f"{BASE}/files/{file_id}").status_code == 404
    actions = audit_actions(world, excuse.attendance_id)
    assert actions.count("excuse_file_added") == 1 and actions.count("excuse_file_removed") == 1
    world.db.expire_all()
    added, removed = [world.db.query(GroupAttendanceAudit).filter_by(attendance_id=excuse.attendance_id, action=a).one()
                      for a in ("excuse_file_added", "excuse_file_removed")]
    expected = {"excuse_id": str(excuse.id), "student_id": str(world.students[0].id), "file_id": file_id,
                "filename": added.details["filename"], "size_bytes": len(PDF)}
    assert added.details == removed.details == expected and expected["filename"].endswith(".pdf")
    # Journal flag follows the files.
    journal = client.get(f"/api/v1/auth/org/groups/{world.group.id}/attendance",
                         params={"date_from": lesson[0].isoformat(), "date_to": lesson[0].isoformat()}).json()
    assert journal["excuses"][key_of(lesson[0], lesson[1])][str(world.students[0].id)]["has_files"] is False
    upload(client, excuse.id, PNG)
    journal = client.get(f"/api/v1/auth/org/groups/{world.group.id}/attendance",
                         params={"date_from": lesson[0].isoformat(), "date_to": lesson[0].isoformat()}).json()
    assert journal["excuses"][key_of(lesson[0], lesson[1])][str(world.students[0].id)]["has_files"] is True


def test_teacher_view_of_journal_has_no_review_note_over_http(world, lesson):
    day, lesson_id, excuse = lesson
    make_client(world).post(f"{BASE}/{excuse.id}/review", json={"decision": "rejected", "note": "Нет справки"})
    teacher = make_client(world, user=world.teacher)
    got = teacher.get(f"/api/v1/auth/org/groups/{world.group.id}/attendance",
                      params={"date_from": day.isoformat(), "date_to": day.isoformat()}).json()
    entry = got["excuses"][key_of(day, lesson_id)][str(world.students[0].id)]
    assert entry["status"] == "rejected" and "review_note" not in entry


def test_default_period_is_the_current_business_month(world):
    from src.auth.attendance_excuses import AttendanceExcuseService

    today = business_today()
    start, end = AttendanceExcuseService.resolve_period(None, None)
    assert start == today.replace(day=1).isoformat()
    assert end[:7] == start[:7] and end >= today.isoformat() and (end[8:] in ("28", "29", "30", "31"))
    # An excuse on a lesson dated today is inside the default window.
    from src.auth.schemas import StartAttendanceRequest
    from tests.pg_world import LIVE_SLOT
    world.svc.start_group_attendance(str(world.org.id), str(world.group.id),
                                     StartAttendanceRequest(lessonDate=today, lessonId=LIVE_SLOT),
                                     started_by=str(world.teacher.id), is_org_admin=False)
    save(world, today, LIVE_SLOT, [(world.students[0], "excused")], actor=world.teacher, is_admin=False,
         excuseReasons={world.students[0].id: "Болел"})
    assert make_client(world).get(BASE).json()["total"] == 1


def test_oversized_upload_is_refused_by_content_length_before_the_body_is_read(world, lesson):
    client = make_client(world)
    big = PNG + b"\x00" * (3 * 1024 * 1024)
    r = upload(client, lesson[2].id, big)
    assert r.status_code == 413 and r.json()["detail"] == "Файл слишком большой (макс. 2 МБ)"
    world.db.expire_all()
    assert world.db.query(AttendanceExcuseFile).filter_by(org_id=world.org.id).count() == 0
    # A body with no usable length is refused as well.
    r = client.post(f"{BASE}/{lesson[2].id}/files", content=b"x", headers={"content-type": "multipart/form-data; boundary=x", "content-length": ""})
    assert r.status_code in (411, 400, 422)
    # Not multipart / no "file" field.
    assert client.post(f"{BASE}/{lesson[2].id}/files", data={"other": "1"}).status_code in (415, 422, 400)
