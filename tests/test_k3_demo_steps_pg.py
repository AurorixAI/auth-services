"""K3 (A2-A5, A7, A8): the demo request in three steps.  Scenarios and invariants are those of ``artifacts/analysis/k3-landing-domains.md`` §8.1 / §8.2
(the numbers in the names), the contract is ``docs/K3_API.md``.

Letters never leave the machine (``cw`` replaces SMTP); Turnstile is replaced where a test needs it (``fake_turnstile``).
"""
from datetime import timedelta
from itertools import count
from uuid import uuid4

import pytest

from tests.console_helpers import API, advance_clock, body_of, cw, letters_to, needs_pg, request_body  # noqa: F401  (first: the import order of the application)
from src.auth import demo_requests as service
from src.models.auth import Organization
from src.models.platform import DemoRequest

pytestmark = needs_pg
URL = "/api/v1/public/demo-requests"
_ips = count(1)


def fresh_ip():
    n = next(_ips)
    return f"198.18.{n // 250}.{n % 250 + 1}"


def post(cw, body, ip=None, host="app.algolab.me"):
    return cw.new_client(host).post(URL, json=body, headers={"X-Real-IP": ip or fresh_ip()})


def patch(cw, request_id, body, ip=None):
    return cw.new_client("app.algolab.me").patch(f"{URL}/{request_id}", json=body, headers={"X-Real-IP": ip or fresh_ip()})


def step2(**over):
    """The body the landing sends at step 2: steps 1-2 only, with a request_id; the e-mail domain is unique (the limit of 10 an hour per domain)."""
    body = {
        "request_id": str(uuid4()), "centre_name": "Репетитор Алия", "org_type": "tutor", "city": "Самарканд", "address": "ул. Навои, 12",
        "students_count": 15, "branches_count": 0, "contact_name": "Каримова Алия Рустамовна", "position": "owner", "phone": "+998 90 123-45-67",
        "email": f"aliya-{uuid4().hex[:8]}@mail-{uuid4().hex[:6]}.test", "preferred_contact_time": "после 18:00", "locale": "ru",
        "source": {"utm_source": "instagram"}, "website": "",
    }
    body.update(over)
    return body


def row_of(cw, email):
    cw.db.expire_all()
    return cw.db.query(DemoRequest).filter_by(email=email).one()


def rows(cw):
    cw.db.expire_all()
    return cw.db.query(DemoRequest).all()


@pytest.fixture
def fake_turnstile(monkeypatch):
    """Turnstile switched on; ``answers`` is what Cloudflare says to the next calls (a list of dicts or an exception), ``calls`` the tokens it was asked about."""
    from src.auth import turnstile

    state = {"answers": [], "calls": [], "remote": []}
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "test-secret")
    monkeypatch.delenv("TURNSTILE_HOSTNAMES", raising=False)

    def siteverify(secret_key, token, remote_ip=None):
        state["calls"].append(token)
        state["remote"].append(remote_ip)
        answer = state["answers"].pop(0) if state["answers"] else {"success": token == "good"}
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(turnstile, "_siteverify", siteverify)
    return state


# ---- A2: unknown keys are 400, not silently cut (scenario 12, invariant 4) ----------------------------------------------------------------------

def test_s12_an_unknown_key_is_400_and_nothing_is_stored_or_sent(cw):
    r = post(cw, step2(budget_rub=5000, utm="x"))
    assert r.status_code == 400, r.text
    j = r.json()
    assert j["code"] == "unknown_fields" and set(j["fields"]) == {"budget_rub", "utm"} and "Неизвестные" in j["detail"]
    assert rows(cw) == [] and cw.outbox == []


def test_s12_the_step_3_keys_are_known_and_a_long_unknown_key_is_cut(cw):
    body = step2(needs=["payments"], current_tool="excel", heard_from="друзья", comment="ок", consent=True)
    assert post(cw, body).status_code == 201
    r = post(cw, step2(**{"x" * 80: 1}))
    assert r.status_code == 400 and list(r.json()["fields"]) == ["x" * 50]


def test_s12_an_unknown_key_in_step_3_is_400_too(cw):
    first = post(cw, step2()).json()
    r = patch(cw, first["id"], {"details_token": first["details_token"], "needs": ["payments"], "phone": "+998901112233"})
    assert r.status_code == 400 and r.json()["code"] == "unknown_fields" and "phone" in r.json()["fields"]


def test_a_non_object_body_is_422_not_500(cw):
    c = cw.new_client("app.algolab.me")
    assert c.post(URL, json=["a"]).status_code == 422
    assert c.patch(f"{URL}/{uuid4()}", json=["a"]).status_code == 422


# ---- S2 / S6: what is stored; step 2 alone is a whole request ----------------------------------------------------------------------------------

def test_s2_all_fields_of_the_steps_are_stored_and_the_answer_carries_the_token(cw):
    body = step2(needs=["exams", "payments", "exams"], current_tool="other_system", heard_from="Instagram", comment="Хочу видеть долги", consent=True,
                 locale="uz", source={"utm_source": "instagram", "utm_campaign": "okt"})
    r = post(cw, body)
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["ok"] is True and j["id"] and j["details_token"] and j["details_token_expires_at"].endswith("Z") and "1 рабочего дня" in j["message"]
    row = row_of(cw, body["email"])
    assert str(row.id) == j["id"] and str(row.request_id) == body["request_id"]
    assert (row.org_type, row.address, row.branches_count, row.position, row.preferred_contact_time) == ("tutor", "ул. Навои, 12", 0, "owner", "после 18:00")
    assert (row.needs, row.current_tool, row.heard_from, row.comment, row.locale) == (["exams", "payments"], "other_system", "Instagram", "Хочу видеть долги", "uz")
    assert row.consent_at is not None and row.status == "new" and row.source == {"utm_source": "instagram", "utm_campaign": "okt"}
    assert (row.team_mail_status, row.applicant_mail_status) == ("sent", "sent")


def test_s6_step_2_alone_is_a_request_and_step_3_fields_stay_empty(cw):
    body = step2()
    assert post(cw, body).status_code == 201
    row = row_of(cw, body["email"])
    assert (row.needs, row.current_tool, row.heard_from, row.comment) == (None, None, None, None) and row.consent_at is None
    card = cw.client.get(f"{API}/demo-requests/{row.id}").json()
    assert card["needs"] == [] and card["current_tool"] is None and card["consent_at"] is None and card["org_type"] == "tutor"


def test_consent_is_not_required_and_false_means_no_date(cw):
    assert post(cw, step2(consent=False)).status_code == 201
    assert all(r.consent_at is None for r in rows(cw))


def test_the_old_form_of_the_product_still_works_without_any_new_field(cw):
    body = request_body()
    assert post(cw, body).status_code == 201
    row = row_of(cw, body["email"])
    assert row.org_type is None and row.request_id is None and row.locale == "ru"


def test_the_request_is_one_row_with_one_letter_to_the_team(cw):
    body = step2()
    assert post(cw, body).status_code == 201
    assert len(letters_to(cw.outbox, "owner-notify@example.test")) == 1 and len(letters_to(cw.outbox, body["email"])) == 1


# ---- S10 / S11: values ---------------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("phone, stored", [("+998 90 123-45-67", "+998901234567"), ("90 123 45 67", "+998901234567"), ("+7 900 123-45-67", "+79001234567")])
def test_s10_phones_are_accepted_and_normalised(cw, phone, stored):
    body = step2(phone=phone)
    assert post(cw, body).status_code == 201
    assert row_of(cw, body["email"]).phone == stored


@pytest.mark.parametrize("phone", ["12345", "абвгд", "+998 90 12"])
def test_s10_a_bad_phone_is_422_with_the_format_text(cw, phone):
    r = post(cw, step2(phone=phone))
    assert r.status_code == 422 and r.json()["fields"]["phone"].startswith("Телефон: формат")


@pytest.mark.parametrize(
    "over, field",
    [
        ({"org_type": "bank"}, "org_type"), ({"org_type": "Center"}, "org_type"), ({"org_type": 5}, "org_type"),
        ({"position": "ceo"}, "position"), ({"current_tool": "abacus"}, "current_tool"), ({"locale": "de"}, "locale"),
        ({"needs": ["payments", "hack"]}, "needs"), ({"needs": "payments"}, "needs"), ({"needs": [1]}, "needs"), ({"needs": ["payments"] * 21}, "needs"),
        ({"branches_count": -1}, "branches_count"), ({"branches_count": 1000}, "branches_count"), ({"branches_count": "много"}, "branches_count"),
        ({"address": "x" * 301}, "address"), ({"heard_from": "x" * 201}, "heard_from"), ({"preferred_contact_time": "x" * 101}, "preferred_contact_time"),
        ({"comment": "x" * 2001}, "comment"), ({"consent": "yes"}, "consent"), ({"request_id": "not-a-uuid"}, "request_id"),
        ({"address": "ул.\x00Навои"}, "address"), ({"preferred_contact_time": "после\n18"}, "preferred_contact_time"), ({"heard_from": "из\x07вестно"}, "heard_from"),
        ({"comment": "ок\x1b[31m"}, "comment"),
    ],
)
def test_s11_bad_values_of_the_new_fields_are_422_with_the_field(cw, over, field):
    r = post(cw, step2(**over))
    assert r.status_code == 422, r.text
    j = r.json()
    assert j["code"] == "validation_error" and field in j["fields"] and any("а" <= ch.lower() <= "я" for ch in j["fields"][field])
    assert rows(cw) == [] and cw.outbox == []


def test_s11_the_six_org_types_and_an_empty_needs_list_are_accepted(cw):
    from src.auth.demo_request_choices import ORG_TYPES

    for kind in ORG_TYPES:
        assert post(cw, step2(org_type=kind, needs=[])).status_code == 201
    assert len(ORG_TYPES) == 6 and sorted(r.org_type for r in rows(cw)) == sorted(ORG_TYPES) and all(r.needs == [] for r in rows(cw))


def test_the_org_types_of_the_form_are_the_six_the_database_allows():
    import re

    allowed = lambda text: set(re.findall(r"'([a-z_]+)'", text))
    from src.auth.demo_request_choices import ORG_TYPES

    organisation = [c for c in Organization.__table__.constraints if "org_type IN" in str(getattr(c, "sqltext", ""))][0]
    request = [c for c in DemoRequest.__table__.constraints if "org_type IN" in str(getattr(c, "sqltext", ""))][0]
    assert set(ORG_TYPES) == allowed(str(organisation.sqltext)) == allowed(str(request.sqltext))


# ---- A3: request_id (scenario 5, invariant 3) -----------------------------------------------------------------------------------------------------

def test_s5_a_repeat_with_the_same_request_id_is_200_the_same_request_and_no_second_letter(cw):
    body = step2()
    first = post(cw, body)
    second = post(cw, {**body, "centre_name": "Другое название", "comment": "изменено"})       # the body of a repeat is ignored
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json() == first.json() and second.json()["details_token"]
    assert len(rows(cw)) == 1 and rows(cw)[0].centre_name == "Репетитор Алия" and rows(cw)[0].comment is None
    assert len(letters_to(cw.outbox, "owner-notify@example.test")) == 1 and len(letters_to(cw.outbox, body["email"])) == 1 and len(cw.outbox) == 2


def test_a_repeat_costs_no_limit_so_a_retry_loop_is_never_429(cw):
    body, ip = step2(), "203.0.113.77"
    codes = [post(cw, body, ip=ip).status_code for _ in range(9)]                              # the limit of one address is 5 an hour
    assert codes == [201] + [200] * 8
    assert post(cw, step2(), ip=ip).status_code == 201                                           # and the budget of the address is untouched: this is its 2nd request


def test_a_repeat_needs_no_new_turnstile_token_because_a_token_is_single_use(cw, fake_turnstile):
    body = step2(turnstile_token="good")
    assert post(cw, body).status_code == 201
    assert post(cw, {**body, "turnstile_token": "used-already"}).status_code == 200
    assert fake_turnstile["calls"] == ["good"]


def test_s5_two_simultaneous_requests_with_one_id_leave_one_row_and_one_letter(cw, monkeypatch):
    """The twin passes the lookup (nothing there yet) and loses at the unique index: it gets 200 with the winner's request and sends nothing."""
    body = step2()
    first = post(cw, body)
    real, calls = service.find_by_request_id, []

    def blind_first_time(db, request_id):
        calls.append(request_id)
        return None if len(calls) == 1 else real(db, request_id)

    monkeypatch.setattr(service, "find_by_request_id", blind_first_time)
    second = post(cw, {**body, "centre_name": "Близнец"})
    assert (first.status_code, second.status_code) == (201, 200) and second.json()["id"] == first.json()["id"] and len(calls) == 2
    assert len(rows(cw)) == 1 and rows(cw)[0].centre_name == "Репетитор Алия"
    assert len(letters_to(cw.outbox, "owner-notify@example.test")) == 1 and len(cw.outbox) == 2


def test_two_requests_without_a_request_id_are_two_rows_as_before(cw):
    body = step2()
    del body["request_id"]
    assert (post(cw, body).status_code, post(cw, body).status_code) == (201, 201) and len(rows(cw)) == 2


def test_a_replay_of_an_old_request_has_no_token_any_more(cw, monkeypatch):
    body = step2()
    first = post(cw, body).json()
    advance_clock(monkeypatch, 2 * 3600 + 5)
    again = post(cw, body)
    assert again.status_code == 200 and again.json()["id"] == first["id"] and again.json()["details_token"] is None


# ---- A4: step 3 by the token (scenario 6, invariants 3-4) ------------------------------------------------------------------------------------------

def test_step_3_is_written_into_the_same_request_and_no_second_letter_goes(cw):
    body = step2()
    made = post(cw, body).json()
    before = len(cw.outbox)
    r = patch(cw, made["id"], {"details_token": made["details_token"], "needs": ["payments", "attendance"], "current_tool": "excel", "heard_from": "Instagram",
                               "comment": "Нужны долги по оплатам"})
    assert r.status_code == 200 and r.json() == {"ok": True, "id": made["id"]}
    assert len(rows(cw)) == 1 and len(cw.outbox) == before == 2
    row = row_of(cw, body["email"])
    assert (row.needs, row.current_tool, row.heard_from, row.comment) == (["payments", "attendance"], "excel", "Instagram", "Нужны долги по оплатам")
    assert (row.centre_name, row.org_type, row.status) == ("Репетитор Алия", "tutor", "new")           # steps 1-2 untouched


def test_step_3_sent_twice_is_harmless_and_a_missing_field_stays(cw):
    body = step2()
    made = post(cw, body).json()
    token = made["details_token"]
    assert patch(cw, made["id"], {"details_token": token, "needs": ["payments"], "comment": "первый"}).status_code == 200
    assert patch(cw, made["id"], {"details_token": token, "needs": ["exams"]}).status_code == 200
    row = row_of(cw, body["email"])
    assert (row.needs, row.comment) == (["exams"], "первый")
    assert patch(cw, made["id"], {"details_token": token, "needs": []}).status_code == 200 and row_of(cw, body["email"]).needs == []     # [] clears


def test_step_3_with_nothing_to_add_or_a_bad_value_is_422(cw):
    made = post(cw, step2()).json()
    assert patch(cw, made["id"], {"details_token": made["details_token"]}).status_code == 422
    r = patch(cw, made["id"], {"details_token": made["details_token"], "needs": ["payments", "magic"]})
    assert r.status_code == 422 and "needs" in r.json()["fields"]


def test_step_3_token_cases_are_403_and_change_nothing(cw, monkeypatch):
    one, two = post(cw, step2()).json(), post(cw, step2()).json()
    good = {"needs": ["payments"]}
    forged = one["details_token"][:-4] + "0000"
    for token in (None, "", "garbage", forged, two["details_token"], one["details_token"].replace(".", ":")):
        r = patch(cw, one["id"], {**good, **({"details_token": token} if token is not None else {})})
        assert r.status_code == 403 and r.json()["code"] == "invalid_details_token", (token, r.text)
    assert all(r.needs is None for r in rows(cw))
    advance_clock(monkeypatch, 2 * 3600 + 5)
    r = patch(cw, one["id"], {**good, "details_token": one["details_token"]})
    assert r.status_code == 403 and r.json()["code"] == "details_token_expired" and all(r.needs is None for r in rows(cw))


def test_step_3_for_an_unknown_request_is_404_and_a_bad_id_too(cw):
    ghost = uuid4()
    expires = 4_102_444_800
    token = f"{ghost.hex}.{expires}.{service._signature(ghost, expires)}"
    r = patch(cw, ghost, {"details_token": token, "needs": ["payments"]})
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    assert patch(cw, "not-a-uuid", {"details_token": token, "needs": ["payments"]}).status_code == 404


def test_step_3_after_the_approval_is_409(cw):
    body = step2()
    made = post(cw, body).json()
    assert cw.client.post(f"{API}/demo-requests/{made['id']}/approve", json={}).status_code == 200
    r = patch(cw, made["id"], {"details_token": made["details_token"], "needs": ["payments"]})
    assert r.status_code == 409 and r.json()["code"] == "already_processed" and row_of(cw, body["email"]).needs is None


def test_step_3_has_its_own_limit_per_address(cw):
    made = post(cw, step2()).json()
    ip = "203.0.113.88"
    codes = [patch(cw, made["id"], {"details_token": made["details_token"], "comment": f"v{i}"}, ip=ip).status_code for i in range(21)]
    assert codes == [200] * 20 + [429]


# ---- A5: the order of the checks; the ceilings of the letters ------------------------------------------------------------------------------------

def test_s7_the_honeypot_comes_first_before_turnstile_and_the_limits(cw, fake_turnstile):
    ip = "203.0.113.91"
    for _ in range(6):                                                                           # spend the limit of the address honestly (5 an hour) ...
        post(cw, step2(turnstile_token="good"), ip=ip)
    fake_turnstile["calls"].clear()
    r = post(cw, step2(website="http://spam.example", turnstile_token="bad"), ip=ip)               # ... a bot with a bad token from the spent address: still the silent 201
    assert r.status_code == 201 and r.json() == {"ok": True, "message": service.ACCEPTED_TEXT}
    assert fake_turnstile["calls"] == [] and len(rows(cw)) == 5


def test_a5_turnstile_comes_before_the_limits_and_a_refused_token_spends_no_limit(cw, fake_turnstile):
    ip = "203.0.113.92"
    assert [post(cw, step2(turnstile_token="good"), ip=ip).status_code for _ in range(5)] == [201] * 5
    r = post(cw, step2(turnstile_token="bad"), ip=ip)                                             # the address is spent, but the token is judged first
    assert r.status_code == 400 and r.json()["code"] == "captcha_failed"
    assert post(cw, step2(turnstile_token="good"), ip=ip).status_code == 429                      # a good token meets the limit
    other = "203.0.113.93"
    assert [post(cw, step2(turnstile_token="bad"), ip=other).status_code for _ in range(10)] == [400] * 10
    assert [post(cw, step2(turnstile_token="good"), ip=other).status_code for _ in range(5)] == [201] * 5     # 10 refusals cost the address nothing


def test_a5_without_a_token_or_a_key_turnstile_is_skipped_or_refuses_as_set(cw, fake_turnstile, monkeypatch):
    assert post(cw, step2()).status_code == 400                                                  # key set, no token
    assert fake_turnstile["calls"] == []
    monkeypatch.delenv("TURNSTILE_SECRET_KEY")
    assert post(cw, step2()).status_code == 201                                                  # no key: the check is off


def test_a5_when_cloudflare_is_down_the_request_is_accepted(cw, fake_turnstile, caplog):
    fake_turnstile["answers"] = [ConnectionError("no route")]
    body = step2(turnstile_token="good")
    with caplog.at_level("WARNING"):
        assert post(cw, body).status_code == 201                                                 # invariant 2: a real request is not lost for somebody else's outage
    assert len(rows(cw)) == 1 and "turnstile" in caplog.text


def test_s7_a_bot_that_fills_the_trap_spends_no_budget_of_real_applicants(cw):
    ip = "203.0.113.94"
    for i in range(70):                                                                          # more than the 60 an hour of the whole form, all from one address and one mailbox
        assert post(cw, step2(website="x", email="bot@spam.test"), ip=ip).status_code == 201
    assert rows(cw) == [] and cw.outbox == []
    assert [post(cw, step2(), ip=ip).status_code for _ in range(5)] == [201] * 5                 # the same address still has its whole budget
    assert [post(cw, step2()).status_code for _ in range(55)] == [201] * 55                      # and the form still has 60 an hour in all
    assert post(cw, step2()).status_code == 429                                                  # the 61st is the global limit of real requests: by design


def test_a5_the_applicant_gets_one_letter_a_day_per_address_and_the_request_is_kept(cw, monkeypatch):
    email = "same-day@mail-one.test"
    first, second = step2(email=email), step2(email=email)
    assert (post(cw, first).status_code, post(cw, second).status_code) == (201, 201)
    a, b = sorted(rows(cw), key=lambda r: r.created_at)
    assert (a.applicant_mail_status, b.applicant_mail_status, b.team_mail_status) == ("sent", "cap", "sent")
    assert len(letters_to(cw.outbox, email)) == 1 and len(letters_to(cw.outbox, "owner-notify@example.test")) == 2
    advance_clock(monkeypatch, 24 * 3600 + 5)                                                    # the next day the address is free again
    assert post(cw, step2(email=email)).status_code == 201 and len(letters_to(cw.outbox, email)) == 2


def test_a5_the_ceilings_are_one_per_address_and_100_in_all():
    assert (service.APPLICANT_LETTERS_PER_ADDRESS, service.APPLICANT_LETTERS_PER_DAY) == (1, 100)


def test_a5_over_the_daily_ceiling_of_the_form_the_request_is_kept_and_the_console_says_why(cw, monkeypatch):
    monkeypatch.setattr(service, "APPLICANT_LETTERS_PER_DAY", 3)
    made = [post(cw, step2()) for _ in range(5)]
    assert [r.status_code for r in made] == [201] * 5 and len(rows(cw)) == 5
    states = [r.applicant_mail_status for r in sorted(rows(cw), key=lambda r: r.created_at)]
    assert states == ["sent"] * 3 + ["cap"] * 2
    assert len(letters_to(cw.outbox, "owner-notify@example.test")) == 5 and len(cw.outbox) == 3 + 5   # the team is told of every request
    capped = cw.client.get(f"{API}/demo-requests/{made[4].json()['id']}").json()
    assert capped["mail"] == {"team": "sent", "applicant": "cap"}


# ---- A8 / scenario 13: the outcome of the letters is on the card -----------------------------------------------------------------------------------

def test_s13_smtp_down_the_request_is_saved_201_and_the_card_says_the_letters_did_not_go(cw, monkeypatch):
    from src.mail import sender

    monkeypatch.setattr(sender, "deliver", lambda msg, cfg=None: False)
    body = step2()
    r = post(cw, body)
    assert r.status_code == 201 and r.json()["ok"] is True                                       # invariant 2
    row = row_of(cw, body["email"])
    assert (row.team_mail_status, row.applicant_mail_status) == ("failed", "failed")
    card = cw.client.get(f"{API}/demo-requests/{row.id}").json()
    assert card["mail"] == {"team": "failed", "applicant": "failed"}
    listed = cw.client.get(f"{API}/demo-requests").json()["items"][0]
    assert listed["mail"] == {"team": "failed", "applicant": "failed"}


def test_s13_a_crash_of_the_mail_code_is_the_same_failed(cw, monkeypatch):
    from src.mail import sender

    def boom(msg, cfg=None):
        raise ConnectionRefusedError("smtp")

    monkeypatch.setattr(sender, "deliver", boom)
    body = step2()
    assert post(cw, body).status_code == 201
    assert row_of(cw, body["email"]).team_mail_status == "failed"


def test_a8_no_address_for_the_team_is_a_failed_team_letter(cw, monkeypatch):
    monkeypatch.delenv("ADMIN_NOTIFICATION_EMAIL")
    body = step2()
    assert post(cw, body).status_code == 201
    row = row_of(cw, body["email"])
    assert (row.team_mail_status, row.applicant_mail_status) == ("failed", "sent")


def test_a8_the_card_of_an_old_request_has_no_mail_state(cw):
    body = request_body()
    post(cw, body)
    row = row_of(cw, body["email"])
    cw.db.query(DemoRequest).filter_by(id=row.id).update({"team_mail_status": None, "applicant_mail_status": None})
    cw.db.commit()
    assert cw.client.get(f"{API}/demo-requests/{row.id}").json()["mail"] is None


def test_a8_the_updated_at_of_the_card_is_not_moved_by_the_outcome_of_the_letters(cw):
    body = step2()
    post(cw, body)
    row = row_of(cw, body["email"])
    assert row.updated_at == row.created_at and row.team_mail_status == "sent"


def test_a8_the_letter_to_the_team_shows_every_new_field(cw):
    body = step2(needs=["payments", "exams"], current_tool="excel", heard_from="Инстаграм", comment="Нужны долги", consent=True, branches_count=3, locale="uz",
                 org_type="tutoring_center", position="director", address="ул. Навои, 12")
    assert post(cw, body).status_code == 201
    letter = letters_to(cw.outbox, "owner-notify@example.test")[0]
    for kind in ("plain", "html"):
        text = body_of(letter, kind)
        for expected in ("Репетиторский центр", "Директор", "ул. Навои, 12", "после 18:00", "Оплаты и долги, Экзамены", "Excel", "Инстаграм", "Узбекский", "Нужны долги",
                         "Филиалов", "Согласие на обработку данных"):
            assert expected in text, (kind, expected)
        assert "tutoring_center" not in text and "payments" not in text                         # words, never the codes
    assert " 3" in body_of(letter)


def test_a8_zero_branches_reads_as_no_and_text_of_a_stranger_in_the_new_fields_is_defanged(cw):
    body = step2(branches_count=0, address="https://evil.example/pay", heard_from="www.evil.example", preferred_contact_time="mailto:x@evil.example")
    assert post(cw, body).status_code == 201
    text = body_of(letters_to(cw.outbox, "owner-notify@example.test")[0])
    assert "Филиалов: нет" in text and "https[:]//evil" in text and "www[.]evil" in text and "mailto[:]" in text
    assert "https://evil" not in text and "mailto:x" not in text


# ---- A7: the approval takes the type and the address from the request; the warning about the e-mail ------------------------------------------------

def approve(cw, request_id, **body):
    return cw.client.post(f"{API}/demo-requests/{request_id}/approve", json=body)


@pytest.mark.parametrize("kind", ["tutor", "private_school", "university", "tutoring_center", "other", "center"])
def test_s16_the_approval_creates_the_centre_with_the_type_and_the_address_of_the_request(cw, kind):
    body = step2(org_type=kind, address="пр. Амира Темура, 5")
    made = post(cw, body).json()
    r = approve(cw, made["id"])
    assert r.status_code == 200, r.text
    cw.db.expire_all()
    org = cw.db.get(Organization, r.json()["org"]["id"])
    assert (org.org_type, org.address, org.city, org.phone, org.email, org.name) == (kind, "пр. Амира Темура, 5", "Самарканд", "+998901234567", body["email"], "Репетитор Алия")


def test_s16_a_request_without_a_type_is_approved_as_a_centre_as_before(cw):
    body = request_body()
    post(cw, body)
    r = approve(cw, row_of(cw, body["email"]).id)
    cw.db.expire_all()
    org = cw.db.get(Organization, r.json()["org"]["id"])
    assert (org.org_type, org.address) == ("center", None)


def test_s16_a_double_approval_is_one_centre(cw):
    made = post(cw, step2()).json()
    assert approve(cw, made["id"]).status_code == 200
    r = approve(cw, made["id"])
    assert r.status_code == 409 and r.json()["code"] == "already_processed"
    cw.db.expire_all()
    assert cw.db.query(Organization).filter(Organization.email == rows(cw)[0].email).count() == 1


def test_s16_the_card_of_the_centre_shows_the_type_and_the_tasks_of_the_request(cw):
    body = step2(needs=["payroll"], current_tool="notebook", heard_from="друзья", branches_count=2, org_type="private_school")
    made = post(cw, body).json()
    org = approve(cw, made["id"]).json()["org"]
    card = cw.client.get(f"{API}/orgs/{org['id']}").json()
    assert card["org_type"] == "private_school" and card["address"] == "ул. Навои, 12"
    assert card["request"]["id"] == made["id"] and card["request"]["needs"] == ["payroll"] and card["request"]["current_tool"] == "notebook"
    assert card["request"]["heard_from"] == "друзья" and card["request"]["branches_count"] == 2 and card["request"]["position"] == "owner"
    own = cw.make_centre()
    assert cw.client.get(f"{API}/orgs/{own.org.id}").json()["request"] is None


def test_s9_an_e_mail_already_in_algo_is_flagged_on_the_card_and_the_list_and_the_applicant_notices_nothing(cw):
    mailbox = f"client-{uuid4().hex[:8]}@center.test"                                                                              # users stay between tests
    existing = cw.make_user(email=mailbox)
    fresh = step2(email=f"newcomer-{uuid4().hex[:8]}@center.test")
    known = step2(email=mailbox.upper())
    r_known, r_fresh = post(cw, known), post(cw, fresh)
    assert r_known.status_code == r_fresh.status_code == 201
    assert set(r_known.json()) == set(r_fresh.json()) and r_known.json()["message"] == r_fresh.json()["message"]                  # the same answer: no leak of who is a client
    assert len(letters_to(cw.outbox, mailbox)) == 1                                                                   # and the same letter
    card_known = cw.client.get(f"{API}/demo-requests/{r_known.json()['id']}").json()
    card_fresh = cw.client.get(f"{API}/demo-requests/{r_fresh.json()['id']}").json()
    assert card_known["email_in_algo"] is True and card_fresh["email_in_algo"] is False
    listed = {row["id"]: row["email_in_algo"] for row in cw.client.get(f"{API}/demo-requests").json()["items"]}
    assert listed == {r_known.json()["id"]: True, r_fresh.json()["id"]: False}
    assert existing.email == mailbox
    r = approve(cw, r_known.json()["id"])
    assert r.status_code == 409 and r.json()["code"] == "email_in_use"                                                             # the approval still refuses


def test_s9_the_warning_goes_after_the_approval_because_the_e_mail_is_the_new_owner(cw):
    made = post(cw, step2()).json()
    assert cw.client.get(f"{API}/demo-requests/{made['id']}").json()["email_in_algo"] is False
    card = approve(cw, made["id"]).json()["request"]
    assert card["status"] == "approved" and card["email_in_algo"] is False


def test_s9_a_repeat_with_the_same_email_is_marked(cw):
    email = "again@mail-x.test"
    first, second = post(cw, step2(email=email)).json(), post(cw, step2(email=email)).json()
    card = cw.client.get(f"{API}/demo-requests/{second['id']}").json()
    assert card["is_repeat"] is True and [s["id"] for s in card["same_email"]] == [first["id"]]


# ---- round after R3 / security review ---------------------------------------------------------------------------------------------------------------

def test_r2_a_replay_with_another_email_is_409_gets_no_token_and_touches_nothing(cw):
    body = step2()
    first = post(cw, body)
    other = post(cw, {**body, "email": "victim@elsewhere.test", "centre_name": "Чужой"})
    assert first.status_code == 201 and other.status_code == 409
    j = other.json()
    assert j["code"] == "request_id_conflict" and "details_token" not in j and "id" not in j and "ok" not in j
    assert [(r.email, r.centre_name) for r in rows(cw)] == [(body["email"], "Репетитор Алия")] and len(cw.outbox) == 2
    assert post(cw, {"request_id": body["request_id"]}).status_code == 409                       # no e-mail at all is not the owner either


def test_r2_squatting_the_victim_gets_409_not_accepted_and_retries_with_a_new_id(cw):
    attacker = step2(email="attacker@mail-a.test")
    assert post(cw, attacker).status_code == 201
    victim = step2(request_id=attacker["request_id"], email="victim@mail-v.test")
    r = post(cw, victim)
    assert r.status_code == 409 and r.json()["code"] == "request_id_conflict"                    # not «Заявка принята»: the victim knows to send again
    assert [x.email for x in rows(cw)] == ["attacker@mail-a.test"]
    retry = post(cw, {**victim, "request_id": str(uuid4())})
    assert retry.status_code == 201 and sorted(x.email for x in rows(cw)) == ["attacker@mail-a.test", "victim@mail-v.test"]


def test_r2_the_same_mailbox_in_any_case_and_spaces_is_the_same_request(cw):
    body = step2(email="Mixed.Case@mail-m.test")
    first = post(cw, body)
    again = post(cw, {**body, "email": "  MIXED.case@MAIL-M.test "})
    assert again.status_code == 200 and again.json() == first.json()


def test_r2_a_twin_that_loses_the_insert_race_with_another_email_is_409(cw, monkeypatch):
    body = step2()
    assert post(cw, body).status_code == 201
    real, calls = service.find_by_request_id, []

    def blind_first_time(db, request_id):
        calls.append(request_id)
        return None if len(calls) == 1 else real(db, request_id)

    monkeypatch.setattr(service, "find_by_request_id", blind_first_time)
    r = post(cw, {**body, "email": "twin@mail-t.test"})
    assert r.status_code == 409 and r.json()["code"] == "request_id_conflict" and len(calls) == 2
    assert [x.email for x in rows(cw)] == [body["email"]] and len(cw.outbox) == 2


@pytest.mark.parametrize("bad", [str(__import__("uuid").uuid1()), "00000000-0000-0000-0000-000000000000", str(__import__("uuid").uuid5(__import__("uuid").NAMESPACE_DNS, "x"))])
def test_r2_the_request_id_must_be_a_uuid_v4(cw, bad):
    r = post(cw, step2(request_id=bad))
    assert r.status_code == 422 and "request_id" in r.json()["fields"] and rows(cw) == []


def test_r5_the_letter_is_reserved_only_for_the_request_that_was_created(cw, monkeypatch):
    body = step2()
    first = post(cw, body)
    reserved = []
    real = service.reserve_applicant_letter

    def counting(email):
        reserved.append(email)
        return real(email)

    monkeypatch.setattr(service, "reserve_applicant_letter", counting)
    again = post(cw, body)                                                                       # a repeat: no reservation
    real_find, seen = service.find_by_request_id, []

    def blind_first_time(db, request_id):
        seen.append(1)
        return None if len(seen) == 1 else real_find(db, request_id)

    monkeypatch.setattr(service, "find_by_request_id", blind_first_time)
    twin = post(cw, body)                                                                        # the twin that lost the race: no reservation either
    assert (first.status_code, again.status_code, twin.status_code) == (201, 200, 200)
    assert reserved == []
    assert row_of(cw, body["email"]).applicant_mail_status == "sent"                              # the one letter of the day was not eaten by the twins
    assert len(letters_to(cw.outbox, body["email"])) == 1


def test_r5_a_validation_error_does_not_eat_the_daily_letter(cw):
    email = "valid-later@mail-vl.test"
    assert post(cw, step2(email=email, phone="123")).status_code == 422
    assert post(cw, step2(email=email)).status_code == 201
    assert len(letters_to(cw.outbox, email)) == 1 and row_of(cw, email).applicant_mail_status == "sent"


def test_r6_aliases_of_one_mailbox_share_the_daily_letter(cw):
    sent_to = ["john.doe@gmail.com", "johndoe+promo@googlemail.com", "J.o.h.n.d.o.e+x@GMAIL.com"]
    for email in sent_to:
        assert post(cw, step2(email=email)).status_code == 201
    states = [r.applicant_mail_status for r in sorted(rows(cw), key=lambda r: r.created_at)]
    assert states == ["sent", "cap", "cap"] and len(cw.outbox) == 1 + 3                           # one letter to the applicant, three to the team


def test_r6_the_plus_tag_goes_for_every_domain_but_dots_only_for_gmail(cw):
    for email in ("first+a@mail-p.test", "first+b@mail-p.test"):
        assert post(cw, step2(email=email)).status_code == 201
    assert sorted(r.applicant_mail_status for r in rows(cw)) == ["cap", "sent"]
    for email in ("a.b@mail-d.test", "ab@mail-d.test"):                                            # a dot is a different mailbox outside gmail
        assert post(cw, step2(email=email)).status_code == 201
    assert [r.applicant_mail_status for r in rows(cw) if r.email.endswith("mail-d.test")] == ["sent", "sent"]


def test_r7_the_request_block_of_the_centre_card_needs_the_right_to_see_requests(cw):
    made = post(cw, step2(needs=["payments"], current_tool="excel")).json()
    org = approve(cw, made["id"]).json()["org"]
    finance = cw.signed_in(cw.make_staff(["finance.view", "orgs.view"], role_name="Финансы как шаблон"))       # the template «Финансы»: no requests.view
    card = finance.get(f"{API}/orgs/{org['id']}")
    assert card.status_code == 200 and "request" not in card.json() and card.json()["org_type"] == "tutor"
    assert '"needs"' not in card.text and "excel" not in card.text and made["id"] not in card.text
    sales = cw.signed_in(cw.make_staff(["orgs.view", "requests.view"]))
    shown = sales.get(f"{API}/orgs/{org['id']}").json()
    assert shown["request"]["id"] == made["id"] and shown["request"]["needs"] == ["payments"]
    assert cw.client.get(f"{API}/orgs/{org['id']}").json()["request"]["current_tool"] == "excel"            # the platform owner has every right


def test_r8_a_link_in_the_name_or_the_centre_is_defanged_in_every_letter_to_the_applicant_and_in_the_subject(cw):
    body = step2(contact_name="Иван http://evil.example/x", centre_name="Центр pay.evil.example")
    made = post(cw, body).json()
    applicant = letters_to(cw.outbox, body["email"])[0]
    for kind in ("plain", "html"):
        text = body_of(applicant, kind)
        assert "http://evil" not in text and "pay.evil.example" not in text and "http[:]//evil" in text and "pay[.]evil[.]example" in text, kind
    team = letters_to(cw.outbox, "owner-notify@example.test")[0]
    assert "evil.example" not in team["Subject"] and "pay[.]evil[.]example" in team["Subject"]
    approve(cw, made["id"])
    welcome = letters_to(cw.outbox, body["email"])[-1]
    assert "http://evil" not in body_of(welcome) and "pay.evil.example" not in body_of(welcome, "html")
    cw.client.post(f"{API}/demo-requests/{post(cw, step2(contact_name='Анна http://evil.example/y')).json()['id']}/decline", json={"send_email": True, "reason": ""})
    declined = [m for m in cw.outbox if m["Subject"].startswith("Ответ по вашей заявке")][0]
    assert "http://evil" not in body_of(declined) and "http[:]//evil" in body_of(declined)


def test_r9_the_hostname_of_the_answer_must_be_one_of_the_list(cw, fake_turnstile, monkeypatch):
    monkeypatch.setenv("TURNSTILE_HOSTNAMES", "algolab.me, WWW.algolab.me")
    for answer, code in (
        ({"success": True, "hostname": "evil.example"}, 400), ({"success": True}, 400), ({"success": True, "hostname": ""}, 400),
        ({"success": True, "hostname": "algolab.me"}, 201), ({"success": True, "hostname": "www.ALGOLAB.me"}, 201), ({"success": False, "hostname": "algolab.me"}, 400),
    ):
        fake_turnstile["answers"] = [answer]
        assert post(cw, step2(turnstile_token="t")).status_code == code, answer
    assert len(rows(cw)) == 2


def test_r9_an_empty_list_of_hostnames_does_not_compare(cw, fake_turnstile):
    fake_turnstile["answers"] = [{"success": True, "hostname": "anything.example"}]
    assert post(cw, step2(turnstile_token="t")).status_code == 201


def test_r9_the_address_of_the_client_goes_to_cloudflare(cw, fake_turnstile):
    assert post(cw, step2(turnstile_token="good"), ip="203.0.113.150").status_code == 201
    assert fake_turnstile["remote"] == ["203.0.113.150"]
