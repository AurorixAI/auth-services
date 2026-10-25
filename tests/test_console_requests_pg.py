"""Console: the requests for access - list/filter/search/pages, card, status, notes, approve (one centre, one owner, one letter), decline."""
import re
import threading
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from tests.console_helpers import (  # noqa: F401  (first: it loads the application)
    API, PASSWORD, actions, audit_rows, body_of, cw, letters_to, needs_pg,
)
from src.auth import demo_requests as dr  # noqa: E402
from src.auth.platform_common import utcnow  # noqa: E402
from src.core.database import get_db  # noqa: E402
from src.api import main_v2  # noqa: E402
from src.models.auth import Organization, OrganizationMember, User  # noqa: E402
from src.models.platform import DemoRequest, PasswordLink  # noqa: E402

pytestmark = needs_pg
URL = f"{API}/demo-requests"


def lead(cw, **over):
    data = {
        "contact_name": "Иванов Иван Иванович", "centre_name": f"Центр {uuid4().hex[:6]}", "city": "Ташкент", "phone": "+998901234567",
        "email": f"lead-{uuid4().hex[:8]}@example.test", "students_count": 50, "comment": "коммент",
    }
    source = over.pop("source", {"utm_source": "telegram"})
    created = over.pop("created_at", None)
    status = over.pop("status", "new")
    data.update(over)
    row = dr.create(cw.db, data, source, "203.0.113.1")
    row.status = status
    if created:
        row.created_at = created
    cw.db.commit()
    return row


# ── permissions ──────────────────────────────────────────────────────────────────────────────


def test_without_the_right_the_requests_are_403(cw):
    row = lead(cw)
    nothing = cw.signed_in(cw.make_staff(["orgs.view"]))
    for method, path, body in (("get", "", None), ("get", f"/{row.id}", None), ("patch", f"/{row.id}/status", {"status": "contacted"}),
                               ("post", f"/{row.id}/notes", {"text": "x"}), ("post", f"/{row.id}/approve", {}), ("post", f"/{row.id}/decline", {})):
        r = getattr(nothing, method)(URL + path, **({"json": body} if body is not None else {}))
        assert r.status_code == 403 and r.json()["code"] == "forbidden", (method, path)
    viewer = cw.signed_in(cw.make_staff(["requests.view"]))
    assert viewer.get(URL).status_code == 200 and viewer.get(f"{URL}/{row.id}").status_code == 200
    for method, path, body in (("patch", f"/{row.id}/status", {"status": "contacted"}), ("post", f"/{row.id}/notes", {"text": "x"}),
                               ("post", f"/{row.id}/approve", {}), ("post", f"/{row.id}/decline", {})):
        assert getattr(viewer, method)(URL + path, json=body).status_code == 403, (method, path)
    cw.db.expire_all()
    assert cw.db.get(DemoRequest, row.id).status == "new" and cw.db.query(Organization).filter_by(name=row.centre_name).count() == 0


# ── list ─────────────────────────────────────────────────────────────────────────────────────


def test_list_filters_search_sort_and_pages(cw):
    now = utcnow()
    a = lead(cw, centre_name="Альфа школа", city="Ташкент", email="a@example.test", contact_name="Сидоров Пётр")
    b = lead(cw, centre_name="Бета центр", city="Самарканд", status="contacted", source={"utm_source": "google"}, created_at=now - timedelta(days=3))
    c = lead(cw, centre_name="Гамма курсы", city="Самарканд", status="spam", created_at=now - timedelta(days=10))
    d = lead(cw, centre_name="Дельта", city="Бухара", email="a@example.test", phone="+998935550000")   # a repeat of a@example.test
    assert d.is_repeat is True
    get = lambda **q: cw.client.get(URL, params=q).json()  # noqa: E731

    everything = get()
    assert everything["total"] == 4 and everything["page"] == 1 and everything["page_size"] == 25 and everything["pages"] == 1
    assert everything["counts"] == {"new": 2, "contacted": 1, "demo_done": 0, "approved": 0, "declined": 0, "spam": 1, "all": 4}
    assert [i["centre_name"] for i in everything["items"]] == ["Дельта", "Альфа школа", "Бета центр", "Гамма курсы"]   # newest first
    assert {"id", "contact_name", "phone", "email", "students_count", "status", "is_repeat", "source", "created_at", "handled_by", "org_id"} <= set(everything["items"][0])
    assert [i["centre_name"] for i in get(status="contacted,spam")["items"]] == ["Бета центр", "Гамма курсы"]
    assert get(city="самарканд")["total"] == 2
    assert [i["centre_name"] for i in get(q="сидоров")["items"]] == ["Альфа школа"]
    assert get(q="935550000")["total"] == 1 and get(q="a@example")["total"] == 2
    assert get(q="%")["total"] == 0                                                        # a LIKE wildcard is text, not a pattern
    assert [i["centre_name"] for i in get(source="google")["items"]] == ["Бета центр"]
    assert [i["centre_name"] for i in get(repeat="true")["items"]] == ["Дельта"]
    since = (now - timedelta(days=5)).date().isoformat()
    assert get(date_from=since)["total"] == 3 and get(date_to=(now - timedelta(days=8)).date().isoformat())["total"] == 1
    assert [i["centre_name"] for i in get(sort="centre_name", order="asc")["items"]] == ["Альфа школа", "Бета центр", "Гамма курсы", "Дельта"]
    page2 = get(page_size=3, page=2)
    assert page2["total"] == 4 and page2["pages"] == 2 and len(page2["items"]) == 1 and page2["page"] == 2


def test_list_rejects_a_bad_query_in_russian(cw):
    for q in ({"status": "dead"}, {"sort": "id"}, {"page": 0}, {"page_size": 500}, {"date_from": "yesterday"}, {"repeat": "maybe"}):
        r = cw.client.get(URL, params=q)
        assert r.status_code == 422 and r.json()["code"] == "validation_error" and r.json()["fields"], q


# ── card, status, notes ──────────────────────────────────────────────────────────────────────


def test_card_status_notes_and_history(cw):
    first = lead(cw, email="card@example.test")
    second = lead(cw, email="card@example.test")
    sales = cw.make_staff(["requests.view", "requests.manage"])
    c = cw.signed_in(sales)
    assert c.get(f"{URL}/{uuid4()}").status_code == 404 and c.get(f"{URL}/not-a-uuid").status_code == 404

    r = c.patch(f"{URL}/{second.id}/status", json={"status": "contacted"})
    assert r.status_code == 200 and r.json()["status"] == "contacted" and r.json()["handled_by"]["id"] == str(sales.user.id)
    assert c.patch(f"{URL}/{second.id}/status", json={"status": "contacted"}).status_code == 200      # same status: nothing happens
    n = c.post(f"{URL}/{second.id}/notes", json={"text": "Созвонились, ждут демо"})
    assert n.status_code == 201 and n.json()["author"]["id"] == str(sales.user.id) and n.json()["text"] == "Созвонились, ждут демо"
    assert c.post(f"{URL}/{second.id}/notes", json={"text": "   "}).status_code == 422
    assert c.post(f"{URL}/{second.id}/notes", json={"text": "x" * 2001}).status_code == 422

    card = c.get(f"{URL}/{second.id}").json()
    assert card["status"] == "contacted" and card["is_repeat"] is True and card["comment"] == "коммент"
    assert [x["text"] for x in card["notes"]] == ["Созвонились, ждут демо"]
    assert [x["id"] for x in card["same_email"]] == [str(first.id)]
    assert [h["action"] for h in reversed(card["history"])] == ["demo_request.received", "demo_request.status_changed", "demo_request.note_added"]
    changed = next(h for h in card["history"] if h["action"] == "demo_request.status_changed")
    assert changed["details"] == {"from": "new", "to": "contacted"} and changed["actor"]["email"] == sales.email and changed["label"]

    for bad in ({"status": "approved"}, {"status": "declined"}, {"status": "bogus"}, {}):
        assert c.patch(f"{URL}/{second.id}/status", json=bad).status_code == 422, bad
    assert len(audit_rows(cw.db, "demo_request.status_changed", target_id=str(second.id))) == 1      # exactly one row for the one change


# ── approve ──────────────────────────────────────────────────────────────────────────────────


def approve_setup(cw, **kw):
    row = lead(cw, contact_name="Иванов Иван Иванович", **kw)
    c = cw.signed_in(cw.make_staff(["requests.view", "requests.manage"]))
    return row, c


def test_approve_creates_the_centre_the_owner_the_link_and_the_letter(cw):
    row, c = approve_setup(cw, centre_name="Учебный центр Альфа")
    r = c.post(f"{URL}/{row.id}/approve", json={})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["org"]["name"] == "Учебный центр Альфа" and j["owner"]["email"] == row.email and j["invite"]["email_sent"] is True
    assert j["request"]["status"] == "approved" and j["request"]["org_id"] == j["org"]["id"]

    org = cw.db.query(Organization).filter_by(name="Учебный центр Альфа").one()
    owner = cw.db.query(User).filter_by(email=row.email).one()
    assert (org.status, org.blocked_at, org.city, org.owner_id) == ("active", None, "Ташкент", owner.id)
    assert (owner.role, owner.default_org_id, owner.is_active, owner.must_change_password) == ("owner", org.id, True, False)
    assert (owner.first_name, owner.last_name, owner.patronymic) == ("Иван", "Иванов", "Иванович")
    member = cw.db.query(OrganizationMember).filter_by(org_id=org.id, user_id=owner.id).one()
    assert member.org_role == "owner" and member.is_active
    links = cw.db.query(PasswordLink).filter_by(user_id=owner.id).all()
    assert len(links) == 1 and links[0].purpose == "centre_invite" and links[0].used_at is None
    assert 71.9 * 3600 < (links[0].expires_at - links[0].created_at).total_seconds() < 72.1 * 3600
    assert len(links[0].token_hash) == 64                                                   # a SHA-256, never the token

    # nobody knows the placeholder password
    assert cw.new_client("app.algolab.me").post("/api/v1/auth/login", json={"email": row.email, "password": "anything-at-all"}).status_code == 401

    mails = letters_to(cw.outbox, row.email)
    assert len(mails) == 1 and mails[0]["Subject"] == "Добро пожаловать в ALGO — задайте пароль" and mails[0]["Reply-To"] == "hello@example.test"
    assert mails[0]["From"] == "ALGO <noreply@algolab.me>"
    text, html = body_of(mails[0]), body_of(mails[0], "html")
    token = re.search(r"https://app\.test/set-password/([\w-]+)", text).group(1)
    assert PasswordLink.token_hash.key and cw.db.query(PasswordLink).filter_by(token_hash=dr.hashlib.sha256(token.encode()).hexdigest()).count() == 1
    assert "Здравствуйте, Иван Иванович!" in text and "72 часа" in text and "Задать пароль и войти" in html and token in html

    rows = audit_rows(cw.db, "demo_request.approved", target_id=str(row.id))
    assert len(rows) == 1 and rows[0].details["org_id"] == str(org.id) and rows[0].details["email_sent"] is True
    assert token not in str(rows[0].details)


def test_a_double_click_makes_one_centre(cw):
    row, c = approve_setup(cw, centre_name="Двойной клик")
    first = c.post(f"{URL}/{row.id}/approve", json={})
    second = c.post(f"{URL}/{row.id}/approve", json={})
    assert (first.status_code, second.status_code) == (200, 409) and second.json()["code"] == "already_processed"
    assert cw.db.query(Organization).filter_by(name="Двойной клик").count() == 1
    assert cw.db.query(User).filter_by(email=row.email).count() == 1 and len(letters_to(cw.outbox, row.email)) == 1
    assert len(audit_rows(cw.db, "demo_request.approved", target_id=str(row.id))) == 1


def test_two_clicks_at_the_same_moment_make_one_centre(cw):
    """Two requests in flight at once, each with its own database session: the row lock lets only one through."""
    row, _ = approve_setup(cw, centre_name="Одновременно")
    cookie = cw.make_staff(["requests.manage"])
    token_holder = {}
    clients = [cw.signed_in(cookie), cw.signed_in(cookie)]
    factory = sessionmaker(cw.engine, autoflush=False)

    def per_request_session():
        s = factory()
        try:
            yield s
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    main_v2.app.dependency_overrides[get_db] = per_request_session
    results = []

    def click(client):
        results.append(client.post(f"{URL}/{row.id}/approve", json={}).status_code)

    threads = [threading.Thread(target=click, args=(cl,)) for cl in clients]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [200, 409], results
    cw.db.expire_all()
    assert cw.db.query(Organization).filter_by(name="Одновременно").count() == 1


def test_approve_twice_or_after_decline_is_409(cw):
    row, c = approve_setup(cw)
    assert c.post(f"{URL}/{row.id}/decline", json={"reason": "нет"}).status_code == 200
    r = c.post(f"{URL}/{row.id}/approve", json={})
    assert r.status_code == 409 and r.json()["code"] == "already_processed"
    done, c2 = approve_setup(cw)
    assert c2.post(f"{URL}/{done.id}/approve", json={}).status_code == 200
    assert c2.post(f"{URL}/{done.id}/decline", json={}).status_code == 409
    assert c2.patch(f"{URL}/{done.id}/status", json={"status": "new"}).json()["code"] == "final_status"


def test_an_owner_email_that_exists_is_409_and_nothing_is_created(cw):
    existing = cw.make_centre()
    row, c = approve_setup(cw, email=existing.owner.email.upper(), centre_name="Не создаётся")
    r = c.post(f"{URL}/{row.id}/approve", json={})
    assert r.status_code == 409 and r.json()["code"] == "email_in_use" and "другую почту" in r.json()["detail"]
    cw.db.expire_all()
    assert cw.db.get(DemoRequest, row.id).status == "new" and cw.db.query(Organization).filter_by(name="Не создаётся").count() == 0
    assert letters_to(cw.outbox, existing.owner.email) == [] and audit_rows(cw.db, "demo_request.approved") == []
    # a console account's address is taken too
    staff_mail = cw.owner.email
    row2, c2 = approve_setup(cw, email=staff_mail)
    assert c2.post(f"{URL}/{row2.id}/approve", json={}).json()["code"] == "email_in_use"
    # ... and the team can name another address for the owner
    r = c2.post(f"{URL}/{row2.id}/approve", json={"owner_email": f"other-{uuid4().hex[:6]}@example.test", "org_name": "Новое имя", "owner_first_name": "Пётр", "owner_last_name": "Петров"})
    assert r.status_code == 200 and r.json()["org"]["name"] == "Новое имя" and r.json()["owner"]["name"] == "Пётр Петров"
    assert "Здравствуйте, Пётр!" in body_of(letters_to(cw.outbox, r.json()["owner"]["email"])[0])


def test_the_centre_exists_even_if_the_letter_did_not_go(cw, monkeypatch):
    from src.mail import sender

    monkeypatch.setattr(sender, "deliver", lambda msg, cfg=None: False)
    row, c = approve_setup(cw, centre_name="Без письма")
    r = c.post(f"{URL}/{row.id}/approve", json={})
    assert r.status_code == 200 and r.json()["invite"]["email_sent"] is False
    assert cw.db.query(Organization).filter_by(name="Без письма").count() == 1
    assert audit_rows(cw.db, "demo_request.approved")[0].details["email_sent"] is False


def test_approve_validates_the_overrides(cw):
    row, c = approve_setup(cw)
    r = c.post(f"{URL}/{row.id}/approve", json={"owner_email": "not-an-email", "org_name": "x" * 201})
    assert r.status_code == 422 and {"owner_email", "org_name"} <= set(r.json()["fields"])


# ── decline ──────────────────────────────────────────────────────────────────────────────────


def test_decline_with_a_letter_and_the_reason(cw):
    row, c = approve_setup(cw)
    r = c.post(f"{URL}/{row.id}/decline", json={"reason": "Пока работаем только с центрами от 50 учеников", "send_email": True})
    assert r.status_code == 200 and r.json()["status"] == "declined" and r.json()["decline_reason"].startswith("Пока работаем")
    mails = letters_to(cw.outbox, row.email)
    assert len(mails) == 1 and mails[0]["Subject"] == "Ответ по вашей заявке — ALGO"
    assert "Пока работаем только с центрами от 50 учеников" in body_of(mails[0]) and "Комментарий команды" in body_of(mails[0], "html")
    rows = audit_rows(cw.db, "demo_request.declined", target_id=str(row.id))
    assert len(rows) == 1 and rows[0].reason.startswith("Пока работаем") and rows[0].details["email_sent"] is True


def test_decline_without_a_letter_sends_nothing(cw):
    row, c = approve_setup(cw)
    r = c.post(f"{URL}/{row.id}/decline", json={"send_email": False})
    assert r.status_code == 200 and r.json()["decline_reason"] is None and letters_to(cw.outbox, row.email) == []
    assert audit_rows(cw.db, "demo_request.declined")[0].details == {"send_email": False}
    assert cw.db.query(Organization).count() >= 0 and r.json()["org"] is None
    assert c.post(f"{URL}/{row.id}/decline", json={}).status_code == 409
