"""The public «Запросить демо» request: validation (Russian), honeypot, rate limit, repeat detection, letters, SMTP failure."""
import pytest

from tests.console_helpers import API, actions, audit_rows, body_of, cw, letters_to, needs_pg, request_body  # noqa: F401
from src.models.platform import DemoRequest

pytestmark = needs_pg
URL = "/api/v1/public/demo-requests"


def post(cw, body, ip="203.0.113.10", host="app.algolab.me"):
    return cw.new_client(host).post(URL, json=body, headers={"X-Real-IP": ip})


def test_a_valid_request_is_201_stored_and_two_letters_go(cw):
    body = request_body()
    r = post(cw, body)
    assert r.status_code == 201 and r.json()["ok"] is True and "1 рабочего дня" in r.json()["message"]
    row = cw.db.query(DemoRequest).filter_by(email=body["email"]).one()
    assert (row.status, row.is_repeat, row.phone, row.students_count) == ("new", False, "+998901234567", 120)
    assert row.source == {"utm_source": "telegram", "utm_medium": "post"} and row.ip_hash and "203.0.113" not in row.ip_hash
    applicant = letters_to(cw.outbox, body["email"])
    owner = letters_to(cw.outbox, "owner-notify@example.test")
    assert len(applicant) == 1 and len(owner) == 1 and len(cw.outbox) == 2
    assert applicant[0]["Subject"] == "Заявка получена — ALGO" and owner[0]["Subject"].startswith("[ALGO] Новая заявка: Учебный центр")
    assert "Здравствуйте, Иван Иванович!" in body_of(applicant[0])                     # «Фамилия Имя Отчество» -> «Имя Отчество»
    assert f"https://console.test/requests?id={row.id}" in body_of(owner[0])           # «Открыть в консоли»
    assert audit_rows(cw.db, "demo_request.received", target_id=str(row.id))[0].actor_user_id is None


@pytest.mark.parametrize(
    "over, field",
    [
        ({"contact_name": ""}, "contact_name"), ({"contact_name": "А"}, "contact_name"), ({"centre_name": "  "}, "centre_name"),
        ({"city": ""}, "city"), ({"email": "не-почта"}, "email"), ({"email": "a@b"}, "email"), ({"phone": "123"}, "phone"),
        ({"phone": "+998 90 abc"}, "phone"), ({"students_count": -1}, "students_count"), ({"students_count": "много"}, "students_count"),
        ({"comment": "x" * 2001}, "comment"), ({"contact_name": "x" * 121}, "contact_name"),
    ],
)
def test_bad_fields_are_422_in_russian(cw, over, field):
    r = post(cw, request_body(**over))
    assert r.status_code == 422, r.text
    j = r.json()
    assert j["code"] == "validation_error" and j["detail"] == "Проверьте заполнение формы" and field in j["fields"]
    assert any("а" <= ch.lower() <= "я" for ch in j["fields"][field])                  # a Russian text
    assert cw.outbox == []


def test_missing_body_and_missing_fields_are_422_not_500(cw):
    c = cw.new_client("app.algolab.me")
    assert c.post(URL, json={}).status_code == 422
    assert c.post(URL, content=b"not json", headers={"Content-Type": "application/json"}).status_code == 422
    assert c.post(URL, json=["a"]).status_code == 422


def test_honeypot_is_201_without_a_record_or_a_letter(cw):
    before = cw.db.query(DemoRequest).count()
    r = post(cw, request_body(website="http://spam.example"))
    assert r.status_code == 201 and r.json()["ok"] is True
    assert cw.db.query(DemoRequest).count() == before and cw.outbox == []


def test_the_sixth_request_from_one_address_in_an_hour_is_429(cw):
    codes = [post(cw, request_body(), ip="198.51.100.5").status_code for _ in range(6)]
    assert codes == [201] * 5 + [429]
    r = post(cw, request_body(), ip="198.51.100.5")
    assert r.status_code == 429 and r.json()["code"] == "rate_limited" and int(r.headers["Retry-After"]) > 0
    assert post(cw, request_body(), ip="198.51.100.6").status_code == 201             # another address has its own budget


def test_the_fourth_request_with_one_email_is_429_whatever_the_case(cw):
    codes = [post(cw, request_body(email="same@example.test"), ip=f"192.0.2.{i}").status_code for i in range(1, 5)]
    assert codes == [201, 201, 201, 429]
    assert post(cw, request_body(email="SAME@Example.test"), ip="192.0.2.99").status_code == 429


def test_a_repeat_from_the_same_email_is_marked_and_the_applicant_gets_the_usual_letter(cw):
    first = request_body(email="again@example.test")
    assert post(cw, first, ip="203.0.113.21").status_code == 201
    assert post(cw, first, ip="203.0.113.22").status_code == 201                       # not an error for the applicant
    rows = cw.db.query(DemoRequest).filter_by(email="again@example.test").order_by(DemoRequest.created_at).all()
    assert [r.is_repeat for r in rows] == [False, True] and rows[1].repeat_of_id == rows[0].id
    assert len(letters_to(cw.outbox, "again@example.test")) == 2
    notify = letters_to(cw.outbox, "owner-notify@example.test")[-1]
    assert "ПОВТОРНАЯ ЗАЯВКА" in body_of(notify) and "Повторная заявка" in body_of(notify, "html")


def test_a_request_after_30_days_is_not_a_repeat(cw):
    from datetime import timedelta
    from src.auth.platform_common import utcnow

    assert post(cw, request_body(email="old@example.test"), ip="203.0.113.31").status_code == 201
    row = cw.db.query(DemoRequest).filter_by(email="old@example.test").one()
    row.created_at = utcnow() - timedelta(days=31)
    cw.db.commit()
    assert post(cw, request_body(email="old@example.test"), ip="203.0.113.32").status_code == 201
    assert [r.is_repeat for r in cw.db.query(DemoRequest).filter_by(email="old@example.test").order_by(DemoRequest.created_at)] == [False, False]


def test_smtp_down_the_request_is_still_saved_and_the_error_is_logged(cw, monkeypatch, caplog):
    from src.mail import sender

    def smtp_down(msg, cfg=None):
        raise ConnectionRefusedError("smtp.example.test:587")

    monkeypatch.setattr(sender, "deliver", smtp_down)
    body = request_body()
    with caplog.at_level("ERROR"):
        r = post(cw, body)
    assert r.status_code == 201
    assert cw.db.query(DemoRequest).filter_by(email=body["email"]).count() == 1
    assert "Failed to send a letter" in caplog.text and "smtp.example.test" not in r.text


def test_phone_forms_are_normalised():
    from src.auth.demo_requests import normalise_phone

    assert normalise_phone("+998 90 123-45-67") == "+998901234567"
    assert normalise_phone("998901234567") == "+998901234567"
    assert normalise_phone("(90) 123 45 67") == "+998901234567"
    assert normalise_phone("90 123 45 67") == "+998901234567"
    assert normalise_phone("+7 999 123-45-67") == "+79991234567"
    for bad in ("", "12345", "+998 90 123", "phone", "+998901234567890123"):
        assert normalise_phone(bad) is None


def test_the_public_route_works_from_any_host_but_the_console_api_does_not(cw):
    assert post(cw, request_body(), host="algolab.me").status_code == 201
