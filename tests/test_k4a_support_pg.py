"""K4a «Поддержка» on the real application and the disposable ``a2_auth`` PostgreSQL (K4_SPEC §5, scenarios 1-13; the invariants of §6).

The numbers of the tests are the numbers of the scenarios: ``test_s01_...`` is scenario 1. The helper tests at the end are the invariants and the mutation guards
(a route without the right, an author filter that is not there, a limit that is not counted).
"""
import hashlib
import json
import re
import threading
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy.orm import sessionmaker

from src.api import main_v2
from src.auth import support_rules as rules
from src.auth import support_service as svc
from src.models.auth import OrganizationMember, User
from src.models.platform import PlatformAudit
from src.models.support import SupportAttachment, SupportMessage, SupportTicket

from tests.console_helpers import PASSWORD, advance_clock, audit_rows, body_of, cw, letters_to, needs_pg  # noqa: F401  (cw is a fixture)
from tests.test_k2_1_pg import AUTH, HOST, centre, signed_in  # noqa: F401
from tests.test_k2_2_reset_pg import add_member, anonymous, member_of

SUPPORT = f"{AUTH}/support"
CONSOLE = "/api/v1/platform/support"
SUPPORT_BOX = "hello@example.test"  # the fixture sets MAIL_REPLY_TO: the mailbox of the support is the one the replies go to
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 200
LONG = "Не открывается журнал группы: страница белая после входа, ошибка. " * 3


# ------------------------------------------------------------------------------------------------------------- helpers
def create(client, text=LONG, **extra):
    return client.post(f"{SUPPORT}/tickets", json={"text": text, **extra})


def make(client, text=LONG, **extra):
    r = create(client, text, **extra)
    assert r.status_code == 201, r.text
    return r.json()


def attach(client, ticket_id, data=PNG, name="shot.png", ctype="image/png"):
    return client.post(f"{SUPPORT}/tickets/{ticket_id}/attachments", files={"file": (name, data, ctype)})


def mine(client):
    r = client.get(f"{SUPPORT}/tickets")
    assert r.status_code == 200, r.text
    return r.json()["items"]


def support_staff(cw, permissions=("support.manage",)):
    cw.console_staff = cw.make_staff(list(permissions))
    return cw.signed_in(cw.console_staff)


def ticket_row(cw, ticket_id):
    cw.db.expire_all()
    return cw.db.get(SupportTicket, ticket_id if not isinstance(ticket_id, str) else __import__("uuid").UUID(ticket_id))


def card(console, ticket_id):
    r = console.get(f"{CONSOLE}/tickets/{ticket_id}")
    assert r.status_code == 200, r.text
    return r.json()


def reply_staff(console, ticket_id, text="Проверили: ошибка на нашей стороне, исправим сегодня."):
    return console.post(f"{CONSOLE}/tickets/{ticket_id}/messages", json={"text": text})


def team_member(cw, centre_, org_role="teacher", first=None):
    user = add_member(cw, centre_, org_role, first=first)
    return user


# ============================================================================================================ 1
@needs_pg
def test_s01_a_teacher_writes_with_two_screenshots_the_team_sees_text_files_and_fields_without_ids_or_tokens_and_the_letter_goes_to_the_support_box(cw, centre, monkeypatch):
    teacher = signed_in(cw, centre.teacher)
    t = make(
        teacher,
        "Не сохраняется оплата ученика, кнопка крутится и ничего не происходит. Пожалуйста, посмотрите.",
        page="/teacher/students/6f1c2a3e-1111-4222-8333-944455556666?redirect=/login&token=SECRET123#frag",
        errors=[{"status": 409, "method": "post", "route_template": "/api/v1/students/6f1c2a3e-1111-4222-8333-944455556666/archive", "at": "2026-10-31T09:30:00Z",
                 "detail": "Ученик Иван Иванов состоит в группе «Математика 5-А»", "body": {"name": "Иван"}}],
    )
    assert attach(teacher, t["id"], PNG).status_code == 200 and attach(teacher, t["id"], JPG, "b.jpg", "image/jpeg").status_code == 200
    console = support_staff(cw)
    c = card(console, t["id"])
    assert c["text"].startswith("Не сохраняется оплата") and c["status"] == "new" and c["number"] == t["number"]
    assert c["org"] == {"id": str(centre.org.id), "name": centre.org.name}
    assert c["author"]["role"] == "teacher" and c["author"]["email"] == centre.teacher.email and c["author"]["has_access"] is True
    assert [a["content_type"] for a in c["attachments"]] == ["image/png", "image/jpeg"] and len(c["attachments"]) == 2
    assert c["page_template"] == "/teacher/students/[id]"
    assert c["client_errors"] == [{"status": 409, "method": "POST", "route_template": "/api/v1/students/[id]/archive", "at": "2026-10-31T09:30:00Z"}]
    blob = json.dumps(c, ensure_ascii=False)
    for secret in ("6f1c2a3e", "SECRET123", "redirect", "frag", "Иван Иванов", "Математика"):
        assert secret not in blob, secret
    for att in c["attachments"]:
        assert console.get(f"{CONSOLE}/attachments/{att['id']}").status_code == 200
    # the letter: the mailbox of the support, the number, the centre, the role, the text, the link to the card - and nothing of the author's mailbox
    mails = letters_to(cw.outbox, SUPPORT_BOX)
    assert len(mails) == 1
    text = body_of(mails[0])
    assert f"№{t['number']}" in text and centre.org.name in text and "преподаватель" in text and "Не сохраняется оплата" in text
    assert f"https://console.test/support?id={t['id']}" in text and "SECRET123" not in text


@needs_pg
def test_s01_on_a_stage_the_letter_goes_only_to_the_allow_list_and_the_team_sees_the_outcome(cw, centre, monkeypatch):
    monkeypatch.setenv("MAIL_STAGE_MODE", "true")
    monkeypatch.setenv("MAIL_ALLOWLIST", "somebody-else@example.test")
    teacher = signed_in(cw, centre.teacher)
    t = make(teacher)
    assert not letters_to(cw.outbox, SUPPORT_BOX)                                    # not in the list: nothing went out
    assert card(support_staff(cw), t["id"])["team_mail"] == "failed"                 # ... and the console says so
    monkeypatch.setenv("MAIL_ALLOWLIST", SUPPORT_BOX)
    t2 = make(teacher)
    assert len(letters_to(cw.outbox, SUPPORT_BOX)) == 1 and card(support_staff(cw), t2["id"])["team_mail"] == "sent"


@needs_pg
def test_s01_the_role_the_centre_and_the_name_come_from_the_session_not_from_the_body(cw, centre):
    other = cw.make_centre()
    teacher = signed_in(cw, centre.teacher)
    t = make(teacher, org_id=str(other.org.id), author_role="owner", author_user_id=str(other.owner.id), status="resolved", number=1, author_name="Подмена")
    row = ticket_row(cw, t["id"])
    assert (row.org_id, row.author_user_id, row.author_role, row.status) == (centre.org.id, centre.teacher.id, "teacher", "new")
    assert row.author_name == f"{centre.teacher.first_name} {centre.teacher.last_name}"
    admin, owner = make(signed_in(cw, centre.admin)), make(signed_in(cw, centre.owner))
    assert [ticket_row(cw, x["id"]).author_role for x in (admin, owner)] == ["admin", "owner"]            # the three roles may write


@needs_pg
def test_s01_a_short_or_empty_text_and_an_unsigned_request_are_refused(cw, centre):
    teacher = signed_in(cw, centre.teacher)
    for text in ("", "   ", "коротко", "x" * 2001):
        r = create(teacher, text)
        assert r.status_code == 422 and r.json()["code"] == "validation_error" and "text" in r.json()["fields"], (text[:10], r.text)
    assert create(teacher, "x" * 2000).status_code == 201 and create(teacher, "x" * 10, client_key=str(uuid4())).status_code == 201
    assert anonymous(cw).post(f"{SUPPORT}/tickets", json={"text": LONG}).status_code == 401
    assert len(mine(teacher)) == 2


# ============================================================================================================ 2, 3
@needs_pg
def test_s02_the_owner_of_the_centre_does_not_see_the_ticket_of_his_teacher(cw, centre):
    teacher, owner, admin = signed_in(cw, centre.teacher), signed_in(cw, centre.owner), signed_in(cw, centre.admin)
    t = make(teacher)
    assert attach(teacher, t["id"]).status_code == 200
    att = teacher.get(f"{SUPPORT}/tickets/{t['id']}").json()["attachments"][0]["id"]
    for who in (owner, admin):
        assert mine(who) == []
        assert who.get(f"{SUPPORT}/tickets/{t['id']}").status_code == 404
        assert who.get(f"{SUPPORT}/attachments/{att}").status_code == 404
        assert who.post(f"{SUPPORT}/tickets/{t['id']}/messages", json={"text": "чужое"}).status_code == 404
        assert attach(who, t["id"]).status_code == 404
    assert len(mine(teacher)) == 1 and teacher.get(f"{SUPPORT}/attachments/{att}").status_code == 200


@needs_pg
def test_s02_the_owner_sees_his_own_ticket_and_the_ticket_of_the_owner_is_not_the_teachers(cw, centre):
    teacher, owner = signed_in(cw, centre.teacher), signed_in(cw, centre.owner)
    own = make(owner)
    make(teacher)
    assert [x["id"] for x in mine(owner)] == [own["id"]] and len(mine(teacher)) == 1
    assert teacher.get(f"{SUPPORT}/tickets/{own['id']}").status_code == 404


@needs_pg
def test_s03_another_centre_and_a_stranger_get_the_same_404_for_a_ticket_a_reply_a_file_and_a_download(cw, centre):
    other = cw.make_centre()
    teacher = signed_in(cw, centre.teacher)
    t = make(teacher)
    attach(teacher, t["id"])
    att = teacher.get(f"{SUPPORT}/tickets/{t['id']}").json()["attachments"][0]["id"]
    missing = str(uuid4())
    for who in (other.owner, other.admin, other.teacher):
        client = signed_in(cw, who)
        statuses = {
            client.get(f"{SUPPORT}/tickets/{t['id']}").status_code, client.get(f"{SUPPORT}/tickets/{missing}").status_code,
            client.get(f"{SUPPORT}/tickets/not-a-uuid").status_code, client.get(f"{SUPPORT}/attachments/{att}").status_code,
            client.get(f"{SUPPORT}/attachments/{missing}").status_code, client.post(f"{SUPPORT}/tickets/{t['id']}/messages", json={"text": "чужое"}).status_code,
            attach(client, t["id"]).status_code,
        }
        assert statuses == {404}, statuses                                                     # a missing id and somebody else's id look the same
        assert mine(client) == []
    r = signed_in(cw, other.teacher).get(f"{SUPPORT}/tickets/{t['id']}")
    assert r.json() == {"detail": "Обращение не найдено", "code": "not_found"}


# ============================================================================================================ 4
@needs_pg
def test_s04_the_size_the_content_and_the_count_of_screenshots(cw, centre):
    teacher = signed_in(cw, centre.teacher)
    t = make(teacher)["id"]
    big = PNG + b"\x00" * (int(2.1 * 1024 * 1024) - len(PNG))
    r = attach(teacher, t, big)
    assert r.status_code == 413 and "слишком большой" in r.json()["detail"] and "2 МБ" in r.json()["detail"]    # refused before the body is read (the middleware)
    just_over = PNG + b"\x00" * (rules.MAX_ATTACHMENT_BYTES - len(PNG) + 1)
    assert len(just_over) <= rules.MAX_ATTACHMENT_BYTES + 64 * 1024                                              # passes the middleware ...
    r = attach(teacher, t, just_over)
    assert r.status_code == 413 and r.json()["code"] == "file_too_big"                                           # ... and is refused by the route
    html = b"<html><script>alert(1)</script></html>"
    for data, name, ctype in ((html, "shot.png", "image/png"), (b"%PDF-1.4\n" + b"0" * 50, "a.png", "image/png"), (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "a.svg", "image/svg+xml"),
                              (b"GIF89a" + b"0" * 50, "a.jpg", "image/jpeg")):
        r = attach(teacher, t, data, name, ctype)
        assert r.status_code == 415 and r.json()["detail"] == rules.FILE_TYPE_TEXT, (name, r.text)               # the content decides, not the name or the type
    assert attach(teacher, t, b"").status_code == 422
    assert attach(teacher, t, PNG + b"\x00" * (rules.MAX_ATTACHMENT_BYTES - len(PNG))).status_code == 200      # exactly 2 MB goes
    assert attach(teacher, t, JPG).status_code == 200 and attach(teacher, t, PNG).status_code == 200
    fourth = attach(teacher, t, PNG)
    assert fourth.status_code == 409 and fourth.json()["code"] == "too_many_files" and "не больше 3" in fourth.json()["detail"]
    cw.db.expire_all()
    assert cw.db.query(SupportAttachment).filter(SupportAttachment.ticket_id == __import__("uuid").UUID(t)).count() == 3


@needs_pg
def test_s04_a_file_is_stored_with_its_hash_and_the_type_of_its_bytes(cw, centre):
    teacher = signed_in(cw, centre.teacher)
    t = make(teacher)["id"]
    data = JPG + b"unique-bytes-" + uuid4().bytes
    assert attach(teacher, t, data, "photo.png", "image/png").status_code == 200                                  # a JPEG under a .png name
    cw.db.expire_all()
    row = cw.db.query(SupportAttachment).one()
    assert (row.content_type, row.size, row.sha256, bytes(row.data)) == ("image/jpeg", len(data), hashlib.sha256(data).hexdigest(), data)
    assert row.uploaded_by_user_id == centre.teacher.id


@needs_pg
def test_s04_a_request_without_the_declared_length_is_refused_before_the_body(cw, centre):
    teacher = signed_in(cw, centre.teacher)
    t = make(teacher)["id"]
    r = teacher.post(f"{SUPPORT}/tickets/{t}/attachments", content=iter([b"x" * 10]), headers={"content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 411


@needs_pg
def test_s04_a_screenshot_cannot_be_added_to_a_resolved_ticket(cw, centre):
    teacher, console = signed_in(cw, centre.teacher), support_staff(cw)
    t = make(teacher)["id"]
    assert console.patch(f"{CONSOLE}/tickets/{t}/status", json={"status": "resolved"}).status_code == 200
    r = attach(teacher, t)
    assert r.status_code == 409 and r.json()["code"] == "ticket_closed"


# ============================================================================================================ 5
@needs_pg
def test_s05_the_sixth_ticket_of_the_day_is_refused_and_the_window_moves_with_the_clock(cw, centre, monkeypatch):
    teacher = signed_in(cw, centre.teacher)
    for _ in range(5):
        make(teacher)
    r = create(teacher)
    assert r.status_code == 429 and r.json()["detail"] == "Слишком много обращений за сутки — напишите на support@algolab.me" and r.json()["code"] == "rate_limited"
    assert 0 < int(r.headers["Retry-After"]) <= 86400
    assert len(mine(teacher)) == 5
    assert create(signed_in(cw, centre.admin)).status_code == 201                                                  # the limit of a person is his own
    advance_clock(monkeypatch, 86400 + 60)
    assert create(teacher).status_code == 201                                                                      # a day later: free again


@needs_pg
def test_s05_the_twenty_first_ticket_of_the_centre_is_refused_and_another_centre_is_not_touched(cw, centre):
    people = [centre.owner, centre.admin, centre.teacher, team_member(cw, centre, "teacher", first="Четвёртый")]
    clients = [signed_in(cw, p) for p in people]
    for client in clients:
        for _ in range(5):
            make(client)
    fifth = signed_in(cw, team_member(cw, centre, "teacher", first="Пятый"))
    r = create(fifth)
    assert r.status_code == 429 and r.json()["code"] == "rate_limited"
    other = cw.make_centre()
    assert create(signed_in(cw, other.teacher)).status_code == 201


@needs_pg
def test_s05_a_refused_ticket_spends_nobody_s_budget_and_a_repeat_with_the_same_key_is_free(cw, centre):
    teacher = signed_in(cw, centre.teacher)
    key = str(uuid4())
    first = make(teacher, client_key=key)
    for _ in range(4):
        again = create(teacher, client_key=key)
        assert again.status_code == 200 and again.json()["id"] == first["id"]                                    # the retry returns the same ticket
    for _ in range(4):
        make(teacher)                                                                                            # 1 + 4 = 5: the repeats did not count
    assert create(teacher).status_code == 429
    assert create(teacher, client_key=key).status_code == 200                                                    # even at the limit the old key answers


@needs_pg
def test_s05_replies_of_the_author_are_limited_to_thirty_a_day_per_ticket(cw, centre):
    teacher = signed_in(cw, centre.teacher)
    t = make(teacher)["id"]
    for i in range(30):
        assert teacher.post(f"{SUPPORT}/tickets/{t}/messages", json={"text": f"ответ {i}"}).status_code == 201
    r = teacher.post(f"{SUPPORT}/tickets/{t}/messages", json={"text": "тридцать первый"})
    assert r.status_code == 429 and r.json()["code"] == "rate_limited"
    other = make(teacher)["id"]
    assert teacher.post(f"{SUPPORT}/tickets/{other}/messages", json={"text": "в другом обращении можно"}).status_code == 201


# ============================================================================================================ 6
@needs_pg
def test_s06_the_team_answers_the_ticket_waits_the_author_gets_a_letter_without_the_words_and_reads_the_answer_in_the_cabinet(cw, centre):
    teacher = signed_in(cw, centre.teacher)
    t = make(teacher)
    console = support_staff(cw)
    answer = "Здравствуйте! Пароль от тестового ученика: Zebra-7731 — не пишите такое в обращениях. Ошибка исправлена."
    r = reply_staff(console, t["id"], answer)
    assert r.status_code == 201 and r.json()["status"] == "waiting"
    assert [m["from"] for m in r.json()["messages"]] == ["staff"] and r.json()["messages"][0]["mail"] == "pending"      # the answer first, the letter after the response
    assert card(console, t["id"])["messages"][0]["mail"] == "sent"
    mails = letters_to(cw.outbox, centre.teacher.email)
    assert len(mails) == 1
    msg = mails[0]
    plain, html = body_of(msg), body_of(msg, "html")
    assert f"№{t['number']}" in msg["Subject"] and "Есть ответ на обращение" in plain
    assert "Ответить можно только в кабинете ALGO" in plain and "https://app.test/teacher/support" in plain
    for leak in ("Zebra", "Пароль от тестового", "исправлена"):
        assert leak not in plain and leak not in html and leak not in msg["Subject"]
    assert msg["Reply-To"] is None                                                                               # an answer to the letter must not land in the Zoho box (Б1)
    assert not re.search(r"(token|code)=", plain) and "set-password" not in plain
    # in the cabinet the answer is there, from «the team», without a name or an outcome of a letter
    d = teacher.get(f"{SUPPORT}/tickets/{t['id']}").json()
    assert d["status"] == "waiting" and d["messages"] == [{"id": d["messages"][0]["id"], "from": "team", "text": answer, "created_at": d["messages"][0]["created_at"]}]
    assert mine(teacher)[0]["status"] == "waiting" and mine(teacher)[0]["last_reply_at"] is not None
    # the author answers: «В работе», a letter to the support box with the first words
    sent_before = len(letters_to(cw.outbox, SUPPORT_BOX))
    r = teacher.post(f"{SUPPORT}/tickets/{t['id']}/messages", json={"text": "Спасибо, теперь работает. Но завис отчёт по оплатам."})
    assert r.status_code == 201 and r.json()["status"] == "in_progress" and r.json()["messages"][-1]["from"] == "me"
    box = letters_to(cw.outbox, SUPPORT_BOX)
    assert len(box) == sent_before + 1 and "завис отчёт" in body_of(box[-1]) and f"№{t['number']}" in box[-1]["Subject"]
    cd = card(console, t["id"])
    assert cd["status"] == "in_progress" and [m["from"] for m in cd["messages"]] == ["staff", "author"] and cd["messages"][1]["mail"] == "sent"


@needs_pg
def test_s06_the_status_goes_by_the_team_and_every_change_and_answer_is_in_the_journal_without_the_words(cw, centre):
    teacher = signed_in(cw, centre.teacher)
    staff = cw.make_staff(["support.manage"])
    console = cw.signed_in(staff)
    t = make(teacher)
    assert console.patch(f"{CONSOLE}/tickets/{t['id']}/status", json={"status": "in_progress"}).json()["status"] == "in_progress"
    assert teacher.get(f"{SUPPORT}/tickets/{t['id']}").json()["status"] == "in_progress"
    console.patch(f"{CONSOLE}/tickets/{t['id']}/status", json={"status": "in_progress"})                         # the same status: no second row
    secret = "Закрытый текст ответа 123456"
    reply_staff(console, t["id"], secret)
    console.patch(f"{CONSOLE}/tickets/{t['id']}/status", json={"status": "resolved"})
    rows = audit_rows(cw.db, target_type="support_ticket", target_id=t["id"])
    assert [(r.action, r.actor_user_id) for r in rows] == [
        ("support.status_changed", staff.user.id), ("support.replied", staff.user.id), ("support.status_changed", staff.user.id)]
    assert [r.details.get("to") for r in rows if r.action == "support.status_changed"] == ["in_progress", "resolved"]
    blob = json.dumps([r.details for r in rows], ensure_ascii=False) + "".join(r.reason or "" for r in rows)
    assert secret not in blob and LONG[:20] not in blob and all(r.details["number"] == t["number"] for r in rows)
    history = card(console, t["id"])["history"]
    assert len(history) == 3 and history[1]["label"] == "Ответ команды на обращение" and history[0]["actor"]["email"] == staff.user.email
    journal = cw.client.get("/api/v1/platform/audit", params={"target_type": "support_ticket"}).json()["items"]
    assert len(journal) == 3 and journal[0]["target_label"] == f"Обращение №{t['number']}"


@needs_pg
def test_s06_a_reply_clicked_twice_is_one_reply_and_one_letter(cw, centre):
    teacher = signed_in(cw, centre.teacher)
    t = make(teacher)
    console = support_staff(cw)
    assert reply_staff(console, t["id"], "Один ответ").status_code == 201
    assert reply_staff(console, t["id"], "Один ответ").status_code == 200
    assert len(card(console, t["id"])["messages"]) == 1 and len(letters_to(cw.outbox, centre.teacher.email)) == 1
    assert teacher.post(f"{SUPPORT}/tickets/{t['id']}/messages", json={"text": "Мой ответ"}).status_code == 201
    assert teacher.post(f"{SUPPORT}/tickets/{t['id']}/messages", json={"text": "Мой ответ"}).status_code == 200
    assert len(teacher.get(f"{SUPPORT}/tickets/{t['id']}").json()["messages"]) == 2


@needs_pg
def test_s06_a_letter_that_fails_is_marked_failed_and_the_answer_stays_in_the_cabinet(cw, centre, monkeypatch):
    from src.mail import sender

    monkeypatch.setattr(sender, "deliver", lambda msg, cfg=None: False)
    teacher = signed_in(cw, centre.teacher)
    t = make(teacher)
    console = support_staff(cw)
    r = reply_staff(console, t["id"], "Ответ при сломанной почте")
    assert r.status_code == 201
    cd = card(console, t["id"])
    assert cd["messages"][0]["mail"] == "failed" and cd["team_mail"] == "failed"
    assert teacher.get(f"{SUPPORT}/tickets/{t['id']}").json()["messages"][0]["text"] == "Ответ при сломанной почте"       # invariant: the answer does not depend on the letter


@needs_pg
def test_s06_a_pending_letter_that_never_finished_is_shown_failed_after_two_minutes(cw, centre, monkeypatch):
    teacher = signed_in(cw, centre.teacher)
    t = make(teacher)
    cw.db.query(SupportTicket).filter(SupportTicket.id == __import__("uuid").UUID(t["id"])).update({"team_mail_status": "pending"})
    cw.db.commit()
    console = support_staff(cw)
    assert card(console, t["id"])["team_mail"] == "pending"
    advance_clock(monkeypatch, 130)
    assert card(console, t["id"])["team_mail"] == "failed"


# ============================================================================================================ 7
@needs_pg
def test_s07_fourteen_days_without_an_answer_close_the_ticket_by_the_clock_and_reopen_works_for_fourteen_days(cw, centre, monkeypatch):
    teacher = signed_in(cw, centre.teacher)
    console = support_staff(cw)
    t = make(teacher)["id"]
    reply_staff(console, t)
    advance_clock(monkeypatch, 13 * 86400)                                                                          # (the clock is set, not added: 13 days after the answer)
    assert teacher.get(f"{SUPPORT}/tickets/{t}").json()["status"] == "waiting"                                     # 13 days: still waiting
    advance_clock(monkeypatch, 14 * 86400 + 60)
    console = cw.signed_in(cw.console_staff)                                                                        # the console session of 12 h is over on that clock
    assert mine(teacher)[0]["status"] == "resolved"                                                                # the list closes it lazily
    d = teacher.get(f"{SUPPORT}/tickets/{t}").json()
    assert d["status"] == "resolved" and d["can_reopen"] is True and d["can_reply"] is False
    row = ticket_row(cw, t)
    assert row.resolved_by is None and row.resolved_at == row.last_staff_reply_at + timedelta(days=14)           # the logical moment, not the moment somebody looked
    r = teacher.post(f"{SUPPORT}/tickets/{t}/messages", json={"text": "Вопрос не решён, ошибка вернулась."})
    assert r.status_code == 201 and r.json()["status"] == "new" and r.json()["can_reply"] is True                  # «Открыть снова»: the team sees it as new
    assert card(console, t)["status"] == "new" and card(console, t)["resolved_at"] is None


@needs_pg
def test_s07_after_fourteen_days_the_button_is_gone_and_the_reply_is_refused(cw, centre, monkeypatch):
    teacher = signed_in(cw, centre.teacher)
    console = support_staff(cw)
    t = make(teacher)["id"]
    console.patch(f"{CONSOLE}/tickets/{t}/status", json={"status": "resolved"})
    advance_clock(monkeypatch, 14 * 86400 - 3600)
    assert teacher.get(f"{SUPPORT}/tickets/{t}").json()["can_reopen"] is True
    advance_clock(monkeypatch, 14 * 86400 + 3600)                                                                  # 14 days and 1 hour since it was resolved
    d = teacher.get(f"{SUPPORT}/tickets/{t}").json()
    assert d["can_reopen"] is False and d["can_reply"] is False and d["status"] == "resolved"
    r = teacher.post(f"{SUPPORT}/tickets/{t}/messages", json={"text": "Поздно, но спрошу"})
    assert r.status_code == 409 and r.json()["code"] == "ticket_closed" and "Создайте новое" in r.json()["detail"]
    assert ticket_row(cw, t).status == "resolved" and len(teacher.get(f"{SUPPORT}/tickets/{t}").json()["messages"]) == 0


@needs_pg
def test_s07_an_answer_of_the_author_in_the_waiting_state_resets_the_clock(cw, centre, monkeypatch):
    teacher = signed_in(cw, centre.teacher)
    console = support_staff(cw)
    t = make(teacher)["id"]
    reply_staff(console, t)
    advance_clock(monkeypatch, 10 * 86400)
    assert teacher.post(f"{SUPPORT}/tickets/{t}/messages", json={"text": "Отвечаю на 10-й день"}).json()["status"] == "in_progress"
    advance_clock(monkeypatch, 25 * 86400)                                                                         # 25 days since the team's answer, but the ticket is «В работе»
    assert teacher.get(f"{SUPPORT}/tickets/{t}").json()["status"] == "in_progress"


@needs_pg
def test_s07_the_console_list_closes_old_waiting_tickets_too_and_only_waiting_ones(cw, centre, monkeypatch):
    teacher = signed_in(cw, centre.teacher)
    console = support_staff(cw)
    waiting, new, working = make(teacher)["id"], make(teacher)["id"], make(teacher)["id"]
    reply_staff(console, waiting)
    console.patch(f"{CONSOLE}/tickets/{working}/status", json={"status": "in_progress"})
    advance_clock(monkeypatch, 15 * 86400)
    console = cw.signed_in(cw.console_staff)
    items = {i["id"]: i["status"] for i in console.get(f"{CONSOLE}/tickets").json()["items"]}
    assert items == {waiting: "resolved", new: "new", working: "in_progress"}


# ============================================================================================================ 8
@needs_pg
def test_s08_an_author_removed_from_the_centre_is_marked_in_the_console_his_api_is_closed_and_the_answer_is_by_hand(cw, centre):
    teacher_user = team_member(cw, centre, "teacher", first="Уволенный")
    teacher = signed_in(cw, teacher_user)
    t = make(teacher)
    console = support_staff(cw)
    assert card(console, t["id"])["author"]["has_access"] is True
    owner = signed_in(cw, centre.owner)
    assert owner.delete(f"{AUTH}/org/members/{member_of(cw, teacher_user).id}").status_code == 200
    assert teacher.get(f"{SUPPORT}/tickets").status_code in (401, 403)
    assert create(teacher).status_code in (401, 403)
    assert signed_in_attempt(cw, teacher_user).status_code in (401, 403)
    c = card(console, t["id"])
    assert c["author"]["has_access"] is False and c["author"]["access_note"] and c["author"]["email"] == teacher_user.email   # the e-mail is there for the manual answer
    listed = console.get(f"{CONSOLE}/tickets").json()["items"][0]
    assert listed["author"]["has_access"] is False
    cw.outbox.clear()
    r = reply_staff(console, t["id"], "Ответ человеку без доступа")
    assert r.status_code == 201 and r.json()["messages"][0]["mail"] == "skipped"                                   # no automatic letter: «откройте кабинет» is useless to him
    assert not letters_to(cw.outbox, teacher_user.email)


def signed_in_attempt(cw, user):
    client = cw.new_client(host=HOST)
    return client.post(f"{AUTH}/login", json={"email": user.email, "password": PASSWORD})


@needs_pg
def test_s08_a_blocked_user_and_a_blocked_centre_are_marked_and_each_has_its_own_words(cw, centre):
    console = support_staff(cw)
    a = team_member(cw, centre, "teacher", first="Блок")
    b = team_member(cw, centre, "teacher", first="Центр")
    ta, tb = make(signed_in(cw, a)), make(signed_in(cw, b))
    cw.db.expire_all()
    cw.db.get(User, a.id).platform_blocked_at = datetime(2026, 10, 1)
    cw.db.get(User, a.id).is_active = False
    cw.db.commit()
    assert card(console, ta["id"])["author"]["access_note"] == "аккаунт заблокирован"
    assert card(console, tb["id"])["author"]["has_access"] is True
    org = cw.db.get(type(centre.org), centre.org.id)
    org.blocked_at = datetime(2026, 10, 2)
    cw.db.commit()
    assert card(console, tb["id"])["author"]["access_note"] == "центр заблокирован"


@needs_pg
def test_s08_a_blocked_centre_or_user_cannot_create_or_read_and_the_ticket_stays(cw, centre):
    teacher_user = team_member(cw, centre, "teacher", first="Блокируемый")
    teacher = signed_in(cw, teacher_user)
    t = make(teacher)
    org = cw.db.get(type(centre.org), centre.org.id)
    org.blocked_at = datetime(2026, 10, 2)
    cw.db.commit()
    assert create(teacher).status_code in (401, 403) and teacher.get(f"{SUPPORT}/tickets").status_code in (401, 403)
    assert teacher.get(f"{SUPPORT}/tickets/{t['id']}").status_code in (401, 403)
    org.blocked_at = None
    cw.db.get(User, teacher_user.id).is_active = False
    cw.db.commit()
    assert create(teacher).status_code in (401, 403)
    assert cw.db.query(SupportTicket).filter(SupportTicket.author_user_id == teacher_user.id).count() == 1
    assert card(support_staff(cw), t["id"])["author"]["has_access"] is False


# ============================================================================================================ 9
@needs_pg
def test_s09_a_staff_member_without_support_manage_gets_403_on_every_support_route_of_the_console(cw, centre):
    t = make(signed_in(cw, centre.teacher))
    attach(signed_in(cw, centre.teacher), t["id"])
    att = signed_in(cw, centre.teacher).get(f"{SUPPORT}/tickets/{t['id']}").json()["attachments"][0]["id"]
    routes = [(path, [m.upper() for m in ops]) for path, ops in main_v2.app.openapi()["paths"].items() if path.startswith(CONSOLE)]
    assert len(routes) >= 6  and sum(len(m) for _, m in routes) >= 6                                              # nothing of the section is forgotten by this test
    others = ["requests.view", "requests.manage", "orgs.view", "orgs.manage", "users.view", "users.manage", "audit.view", "impersonate.view"]
    poor = cw.signed_in(cw.make_staff(others))
    anon = cw.new_client()
    for route_path, methods in routes:
        path = route_path.replace("{ticket_id}", t["id"]).replace("{attachment_id}", att)
        for method in methods:
            sent = poor.request(method, path, json={"text": "ответ", "status": "resolved"} if method != "GET" else None)
            assert sent.status_code == 403 and sent.json()["code"] == "forbidden", (method, path, sent.status_code)
            assert anon.request(method, path, json={}).status_code == 401, (method, path)
    assert ticket_row(cw, t["id"]).status == "new"                                                                 # nothing was changed by the refused calls
    assert cw.db.query(SupportMessage).count() == 0


@needs_pg
def test_s09_the_roles_that_have_the_right_and_the_catalogue(cw, centre):
    from src.auth import platform_permissions as perms
    from src.models.platform import PlatformRole

    assert next(p for p in perms.CATALOG if p.key == "support.manage").active is True
    roles = {r.name: r.permissions for r in cw.db.query(PlatformRole).filter(PlatformRole.is_template.is_(True))}
    assert "support.manage" in roles["Владелец платформы"] and "support.manage" in roles["Поддержка"]
    assert "support.manage" not in roles["Продажи"] and "support.manage" not in roles["Контент"] and "support.manage" not in roles["Финансы"]
    t = make(signed_in(cw, centre.teacher))
    assert cw.client.get(f"{CONSOLE}/tickets/{t['id']}").status_code == 200                                         # the owner of the platform
    by_role = cw.signed_in(cw.make_staff(roles["Поддержка"], role_name="Поддержка копия"))
    assert by_role.get(f"{CONSOLE}/tickets/{t['id']}").status_code == 200
    sales = cw.signed_in(cw.make_staff(roles["Продажи"], role_name="Продажи копия"))
    assert sales.get(f"{CONSOLE}/tickets/{t['id']}").status_code == 403


@needs_pg
def test_s09_the_cabinet_cookie_does_not_open_the_console_and_the_console_cookie_does_not_open_the_cabinet(cw, centre):
    teacher = signed_in(cw, centre.teacher)
    t = make(teacher)
    host_swapped = cw.new_client(host=HOST)
    assert host_swapped.get(f"{CONSOLE}/tickets").status_code == 404                                               # the console API does not exist on the cabinet's host
    console = support_staff(cw)
    assert console.get(f"{SUPPORT}/tickets").status_code in (401, 403)                                             # a console session is not a cabinet session


# ============================================================================================================ 10 (stored and shown as text) / 11
@needs_pg
def test_s10_markup_in_the_texts_is_kept_as_typed_and_is_escaped_in_the_letters(cw, centre):
    evil = "<img src=x onerror=alert(1)> и <script>alert(2)</script>"
    teacher = signed_in(cw, centre.teacher)
    t = make(teacher, evil)
    r = teacher.get(f"{SUPPORT}/tickets/{t['id']}")
    assert r.headers["content-type"].startswith("application/json") and r.json()["text"] == evil
    teacher.post(f"{SUPPORT}/tickets/{t['id']}/messages", json={"text": evil + "!"})
    console = support_staff(cw)
    c = card(console, t["id"])
    assert c["text"] == evil and c["messages"][0]["text"] == evil + "!"
    for msg in letters_to(cw.outbox, SUPPORT_BOX):
        html = body_of(msg, "html")
        assert "<img src=x" not in html and "<script" not in html and "onerror=alert(1)>" not in html
        assert "&lt;img" in html


def test_s11_the_template_of_a_page_has_no_ids_no_codes_no_tokens_no_query():
    cases = {
        "/teacher/students/6f1c2a3e-1111-4222-8333-944455556666": "/teacher/students/[id]",
        "/teacher/students/6f1c2a3e-1111-4222-8333-944455556666/": "/teacher/students/[id]",
        "/owner/groups/123": "/owner/groups/[id]",
        "/set-password/AbCdEf_-0123456789xyzTOKEN": "/set-password/[id]",
        "/set-password/onlylowercaseletterstoken": "/set-password/[id]",
        "/d/ABC123": "/d/[id]",
        "/d/abcdef": "/d/[id]",
        "/e/XyZ9": "/e/[id]",
        "/invite/abcdefghijklmnop": "/invite/[id]",
        "/confirm-email/tokentokentoken": "/confirm-email/[id]",
        "/diagnostic/AAAA1111": "/diagnostic/[id]",
        "/exams/by-code/abcdef": "/exams/by-code/[id]",
        "/owner/payments/thisisaverylongrunofletterstoken": "/owner/payments/[id]",
        "/owner/payments/abcdefghijklmnopqrstuvwx": "/owner/payments/abcdefghijklmnopqrstuvwx",
        "/owner/payments/abcdefghijklmnopqrstuvwxy": "/owner/payments/[id]",
        "/api/v1/exams/by-code/abcdef/session-status/xyzxyz": "/api/v1/exams/by-code/[id]/session-status/[id]",
        "/login?redirect=/owner/payments&token=SECRET#hash": "/login",
        "/owner/payments?student=6f1c2a3e": "/owner/payments",
        "https://app.algolab.me/admin/staff?x=1#y": "/admin/staff",
        "/owner/students/[id]": "/owner/students/[id]",
        "/teacher/Students/Иван": "/teacher/[id]/[id]",
        "": "",
        None: "",
        12: "",
        "/": "/",
    }
    for raw, expected in cases.items():
        got = rules.page_template(raw)
        assert got == expected, (raw, got)
        assert "?" not in got and "#" not in got and "=" not in got
    long = "/" + "/".join(["a"] * 20)
    assert rules.page_template(long).count("/") == 8                                                                  # a bounded path


def test_s11_the_errors_keep_only_status_method_template_and_time():
    items = [{"status": 409, "method": "patch", "route_template": "/api/v1/groups/12/students?x=1", "at": 1788168600000, "detail": "Ученик Иван", "message": "m", "body": {"a": 1}, "headers": {"c": "d"}}]
    got = rules.client_errors(items)
    assert got == [{"status": 409, "method": "PATCH", "route_template": "/api/v1/groups/[id]/students", "at": "2026-08-31T09:30:00Z"}]
    assert set(got[0]) == {"status", "method", "route_template", "at"}
    many = [{"status": 500, "method": "GET", "route_template": f"/api/v1/x{i}", "at": None} for i in range(9)]
    assert len(rules.client_errors(many)) == 5 and rules.client_errors(many)[-1]["route_template"] == "/api/v1/[id]"
    junk = [None, 5, "x", {"status": "409", "method": "GET"}, {"status": 99, "method": "GET"}, {"status": 600, "method": "GET"}, {"status": 409, "method": "TRACE"}, {"status": True, "method": "GET"}]
    assert rules.client_errors(junk) == [] and rules.client_errors("nope") == [] and rules.client_errors(None) == []


@needs_pg
def test_s11_the_ticket_keeps_the_template_not_the_path_and_the_browser_is_the_header_cut_short(cw, centre):
    teacher = signed_in(cw, centre.teacher)
    agent = "Mozilla/5.0 " + "X" * 400
    r = teacher.post(f"{SUPPORT}/tickets", json={"text": LONG, "page": "/set-password/SECRETTOKEN123", "browser": "FROM-BODY"}, headers={"user-agent": agent})
    row = ticket_row(cw, r.json()["id"])
    assert row.page_template == "/set-password/[id]" and row.browser == agent[:200] and "FROM-BODY" not in row.browser
    r = teacher.post(f"{SUPPORT}/tickets", json={"text": LONG, "page": "/d/Qwerty12"})
    assert ticket_row(cw, r.json()["id"]).page_template == "/d/[id]"
    r = teacher.post(f"{SUPPORT}/tickets", json={"text": LONG, "errors": "not a list", "page": ["x"]})
    row = ticket_row(cw, r.json()["id"])
    assert row.client_errors == [] and row.page_template == ""


# ============================================================================================================ 12
@needs_pg
def test_s12_two_parallel_sends_with_the_same_key_make_one_ticket(cw, centre):
    maker = sessionmaker(cw.db.get_bind(), autoflush=False)
    barrier = threading.Barrier(2)
    user = {"user_id": str(centre.teacher.id), "org_id": str(centre.org.id), "org_role": "teacher"}
    key = str(uuid4())
    results, errors = [], []

    def go():
        db = maker()
        try:
            barrier.wait(timeout=10)
            ticket, created = svc.create_ticket(db, user, {"text": LONG, "client_key": key}, "ua")
            db.commit()
            results.append((str(ticket.id), created))
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))
            db.rollback()
        finally:
            db.close()

    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert not errors, errors
    assert len({r[0] for r in results}) == 1 and sorted(r[1] for r in results) == [False, True]
    cw.db.expire_all()
    assert cw.db.query(SupportTicket).filter(SupportTicket.author_user_id == centre.teacher.id).count() == 1


@needs_pg
def test_s12_parallel_sends_without_a_key_cannot_pass_the_daily_limit_together(cw, centre):
    maker = sessionmaker(cw.db.get_bind(), autoflush=False)
    barrier = threading.Barrier(8)
    user = {"user_id": str(centre.teacher.id), "org_id": str(centre.org.id), "org_role": "teacher"}
    outcome = []

    def go():
        db = maker()
        try:
            barrier.wait(timeout=10)
            svc.create_ticket(db, user, {"text": LONG}, "ua")
            db.commit()
            outcome.append("ok")
        except Exception as exc:  # noqa: BLE001
            outcome.append(getattr(exc, "code", repr(exc)))
            db.rollback()
        finally:
            db.close()

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert sorted(outcome) == ["ok"] * 5 + ["rate_limited"] * 3, outcome                                            # the lock counts them one by one (LESSONS 69)
    cw.db.expire_all()
    assert cw.db.query(SupportTicket).filter(SupportTicket.author_user_id == centre.teacher.id).count() == 5


@needs_pg
def test_s12_a_key_belongs_to_its_author(cw, centre):
    key = str(uuid4())
    a, b = signed_in(cw, centre.teacher), signed_in(cw, centre.admin)
    ta, tb = make(a, client_key=key), make(b, client_key=key)
    assert ta["id"] != tb["id"]                                                                                      # somebody else's key does not return somebody else's ticket


# ============================================================================================================ 13
@needs_pg
def test_s13_over_fifty_letters_a_day_the_letter_is_skipped_and_the_answer_is_in_the_cabinet(cw, centre):
    from src.api.rate_limit import hit_window
    from src.auth import password_reset

    for _ in range(rules.SUPPORT_MAIL_PER_DAY):
        hit_window(rules.SUPPORT_MAIL_KEY, 86400)                                                                     # 50 letters of the day are spent
    teacher = signed_in(cw, centre.teacher)
    t = make(teacher)
    console = support_staff(cw)
    assert card(console, t["id"])["team_mail"] == "skipped" and not letters_to(cw.outbox, SUPPORT_BOX)
    r = reply_staff(console, t["id"], "Ответ, когда письма кончились")
    assert r.status_code == 201 and card(console, t["id"])["messages"][0]["mail"] == "skipped" and not letters_to(cw.outbox, centre.teacher.email)
    assert teacher.get(f"{SUPPORT}/tickets/{t['id']}").json()["messages"][0]["text"] == "Ответ, когда письма кончились"
    teacher.post(f"{SUPPORT}/tickets/{t['id']}/messages", json={"text": "Ответ автора"})
    assert card(console, t["id"])["messages"][1]["mail"] == "skipped"
    from src.api import rate_limit

    assert rate_limit.peek_window("rl:forgot_platform_day")[0] == 0                                                   # the budget of «Забыли пароль» is not touched
    assert password_reset.platform_has_room() is True


@needs_pg
def test_s13_fifty_one_tickets_from_several_centres_the_last_one_is_skipped(cw):
    centres = [cw.make_centre(members=("teacher",)) for _ in range(11)]
    statuses = []
    for c in centres:
        client = signed_in(cw, c.teacher)
        for _ in range(5):
            t = make(client)
            statuses.append(ticket_row(cw, t["id"]).team_mail_status)
            if len(statuses) == 51:
                break
        if len(statuses) == 51:
            break
    assert statuses[:50] == ["sent"] * 50 and statuses[50] == "skipped"
    assert len(letters_to(cw.outbox, SUPPORT_BOX)) == 50


# ============================================================================================================ the rest: what the author must never see; the list of the team
@needs_pg
def test_the_author_sees_no_name_of_the_team_no_outcome_of_a_letter_and_no_field_of_the_team(cw, centre):
    teacher = signed_in(cw, centre.teacher)
    t = make(teacher, page="/teacher/journal", errors=[{"status": 500, "method": "GET", "route_template": "/api/v1/x", "at": None}])
    attach(teacher, t["id"])
    staff = cw.make_staff(["support.manage"])
    reply_staff(cw.signed_in(staff), t["id"], "Ответ")
    d = teacher.get(f"{SUPPORT}/tickets/{t['id']}")
    blob = d.text
    for forbidden in (staff.user.first_name, str(staff.user.id), "staff", "mail", "page_template", "browser", "client_errors", "author", "org_id", "sha256", "team_mail", "resolved_by", "Команды"):
        assert forbidden not in blob, forbidden
    assert set(d.json()) == {"id", "number", "status", "preview", "created_at", "updated_at", "last_reply_at", "text", "resolved_at", "messages", "attachments", "can_reply", "can_reopen", "can_attach"}
    assert set(d.json()["attachments"][0]) == {"id", "content_type", "size", "index"}
    assert set(mine(teacher)[0]) == {"id", "number", "status", "preview", "created_at", "updated_at", "last_reply_at"}
    for r in (teacher.get(f"{SUPPORT}/tickets/{uuid4()}"), teacher.get(f"{SUPPORT}/attachments/{uuid4()}")):
        assert r.status_code == 404 and set(r.json()) == {"detail", "code"}


@needs_pg
def test_the_download_is_an_attachment_with_nosniff_the_type_of_its_bytes_and_no_cache(cw, centre):
    teacher = signed_in(cw, centre.teacher)
    t = make(teacher)
    attach(teacher, t["id"], JPG, "evil.html", "text/html")
    att = teacher.get(f"{SUPPORT}/tickets/{t['id']}").json()["attachments"][0]["id"]
    for client, url in ((teacher, f"{SUPPORT}/attachments/{att}"), (support_staff(cw), f"{CONSOLE}/attachments/{att}")):
        r = client.get(url)
        assert r.status_code == 200 and r.content == JPG
        assert r.headers["content-type"] == "image/jpeg" and r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["content-disposition"].startswith("attachment;") and ".jpg" in r.headers["content-disposition"]
        assert "no-store" in r.headers["cache-control"] and r.headers["content-security-policy"] == "sandbox"
    cw.db.query(SupportAttachment).update({"data": b"<html>swapped</html>"})                                         # a corrupted row is not served as something else
    cw.db.commit()
    assert teacher.get(f"{SUPPORT}/attachments/{att}").status_code == 404


@needs_pg
def test_the_team_list_filters_by_status_centre_role_and_searches_the_number_and_the_text(cw, centre):
    other = cw.make_centre()
    t1 = make(signed_in(cw, centre.teacher), "Не открывается журнал группы Альфа")
    t2 = make(signed_in(cw, centre.owner), "Хочу выгрузить оплаты в Excel, как это сделать")
    t3 = make(signed_in(cw, other.admin), "Ошибка при добавлении ученика 100%_готово")
    console = support_staff(cw)
    attach(signed_in(cw, centre.teacher), t1["id"])
    console.patch(f"{CONSOLE}/tickets/{t2['id']}/status", json={"status": "in_progress"})

    def ids(**params):
        r = console.get(f"{CONSOLE}/tickets", params=params)
        assert r.status_code == 200, r.text
        return {i["id"] for i in r.json()["items"]}

    assert ids() == {t1["id"], t2["id"], t3["id"]}
    assert ids(status="new") == {t1["id"], t3["id"]} and ids(status="new,in_progress") == {t1["id"], t2["id"], t3["id"]} and ids(status="resolved") == set()
    assert ids(org_id=str(other.org.id)) == {t3["id"]} and ids(org=other.org.name[:-2]) == {t3["id"]} and ids(org="нет такого центра") == set() and ids(role="owner") == {t2["id"]} and ids(role="teacher") == {t1["id"]}
    assert ids(q="журнал") == {t1["id"]} and ids(q=f"№{t2['number']}") == {t2["id"]} and ids(q=f"#{t3['number']}") == {t3["id"]} and ids(q="100%_") == {t3["id"]}
    assert ids(q="%") == {t3["id"]}                                                                                    # a LIKE sign is a character, not a wildcard
    body = console.get(f"{CONSOLE}/tickets").json()
    assert body["counts"] == {"new": 2, "in_progress": 1, "waiting": 0, "resolved": 0, "all": 3}
    first = next(i for i in body["items"] if i["id"] == t1["id"])
    assert first["attachments_count"] == 1 and first["author"]["role_text"] == "Преподаватель" and first["org"]["name"] == centre.org.name
    assert console.get(f"{CONSOLE}/summary").json() == {"new": 2, "open": 3}
    assert console.get(f"{CONSOLE}/tickets", params={"status": "bogus"}).status_code == 422
    assert console.get(f"{CONSOLE}/tickets", params={"role": "student"}).status_code == 422
    assert console.get(f"{CONSOLE}/tickets/{uuid4()}").status_code == 404 and console.get(f"{CONSOLE}/tickets/zzz").status_code == 404
    assert console.patch(f"{CONSOLE}/tickets/{t1['id']}/status", json={"status": "new"}).status_code == 422          # the team cannot make a ticket «new» again
    assert console.post(f"{CONSOLE}/tickets/{t1['id']}/messages", json={"text": ""}).status_code == 422


@needs_pg
def test_a_reply_of_the_team_to_a_resolved_ticket_opens_it_as_waiting_and_clears_the_closing(cw, centre):
    teacher, console = signed_in(cw, centre.teacher), support_staff(cw)
    t = make(teacher)["id"]
    console.patch(f"{CONSOLE}/tickets/{t}/status", json={"status": "resolved"})
    assert card(console, t)["resolved_by"] is not None
    r = reply_staff(console, t, "Уточнение уже после закрытия")
    assert r.json()["status"] == "waiting" and r.json()["resolved_at"] is None and r.json()["resolved_by"] is None
    assert teacher.get(f"{SUPPORT}/tickets/{t}").json()["can_reply"] is True


@needs_pg
def test_the_mails_have_no_reply_to_only_for_the_answer_and_the_letters_to_the_team_are_short(cw, centre):
    teacher = signed_in(cw, centre.teacher)
    long_text = "Слово " * 200
    t = make(teacher, long_text[:1900])
    reply_staff(support_staff(cw), t["id"], "Ответ")
    to_team, to_author = letters_to(cw.outbox, SUPPORT_BOX)[0], letters_to(cw.outbox, centre.teacher.email)[0]
    assert to_team["Reply-To"] == SUPPORT_BOX and to_author["Reply-To"] is None
    text = body_of(to_team)
    assert text.count("Слово") <= 51 and "…" in text                                                                    # 300 characters, not the whole text
    assert len(body_of(to_author)) < 900


@needs_pg
def test_the_default_box_of_the_support_is_support_at_algolab(cw, centre, monkeypatch):
    monkeypatch.setenv("MAIL_REPLY_TO", "")
    make(signed_in(cw, centre.teacher))
    assert len(letters_to(cw.outbox, "support@algolab.me")) == 1


@needs_pg
def test_the_numbers_run_on_and_are_unique(cw, centre):
    teacher = signed_in(cw, centre.teacher)
    numbers = [make(teacher)["number"] for _ in range(3)]
    assert numbers == sorted(numbers) and len(set(numbers)) == 3 and numbers[1] == numbers[0] + 1


@needs_pg
def test_a_cross_site_request_is_refused_on_every_mutating_route_of_the_cabinet(cw, centre):
    teacher = signed_in(cw, centre.teacher)
    t = make(teacher)["id"]
    evil = {"sec-fetch-site": "cross-site"}
    assert teacher.post(f"{SUPPORT}/tickets", json={"text": LONG}, headers=evil).status_code == 403
    assert teacher.post(f"{SUPPORT}/tickets/{t}/messages", json={"text": "x"}, headers=evil).status_code == 403
    assert teacher.post(f"{SUPPORT}/tickets/{t}/attachments", files={"file": ("a.png", PNG, "image/png")}, headers=evil).status_code == 403
    assert len(mine(teacher)) == 1


@needs_pg
def test_the_console_answer_carries_no_cors_and_no_cache(cw, centre):
    t = make(signed_in(cw, centre.teacher))
    r = support_staff(cw).get(f"{CONSOLE}/tickets/{t['id']}", headers={"origin": "https://console.stage.algolab.me"})
    assert r.headers["cache-control"] == "no-store" and "access-control-allow-origin" not in r.headers
    assert support_staff(cw).get(f"{CONSOLE}/tickets", headers={"origin": "https://evil.example"}).status_code == 403
