"""PAY-DUE П1, пункт 3 (ТЗ 3.4 / 10.6): the settings of the term and the rights to them (PostgreSQL, through the routes of the product).

Today Mon 12.10.2026, «Свой месяц» (payment date), 600 000 each, nobody paid: A joined 01.10 (D by 5 days = 05.10, by 10 = 10.10, by 14 = 14.10), B joined 06.10 (D = 10.10 / 15.10), C joined 09.10 (D = 13.10 / 18.10).
By 5 days: A and B are debtors, C is «К оплате» until 13.10.  By 10 days: A is the only debtor.  By 14: nobody."""
from sqlalchemy import func

from src.models.auth import Organization, OrgMoneyAudit

from tests.pay_due_helpers import API, client, dry_run, put_settings, summary, world, clock, pg_session, needs_pg  # noqa: F401  (fixtures)

pytestmark = needs_pg


def _state(w):
    w.db.expire_all()
    org = w.db.query(Organization).filter_by(id=w.org.id).one()
    return (dict((org.settings or {}).get("payments") or {}), w.db.query(func.count(OrgMoneyAudit.id)).filter(OrgMoneyAudit.org_id == w.org.id).scalar())


def test_the_defaults_are_five_days_and_two_lessons_where_the_field_is_absent(pg_session, clock):
    w, *_ = world(pg_session, clock)
    got = client(w, "owner").get(f"{API}/org/settings/payments").json()
    assert (got["pay_due_days"], got["pay_due_lessons"]) == (5, 2)
    s = summary(w)
    assert (s["pay_due_days"], s["pay_due_lessons"]) == (5, 2)


def test_the_owner_sets_the_term_and_the_summary_reads_it_back_with_the_journal_line(pg_session, clock):
    w, *_ = world(pg_session, clock)
    put_settings(w, pay_due_days=10, pay_due_lessons=3)
    assert client(w, "owner").get(f"{API}/org/settings/payments").json()["pay_due_days"] == 10
    s = summary(w)
    assert (s["pay_due_days"], s["pay_due_lessons"]) == (10, 3)            # the cache of the summary is dropped by the save
    (first, *_) = client(w, "owner").get(f"{API}/org/settings/payments/history").json()["items"]
    assert first["action"] == "pay_due_changed"
    assert first["details"]["before"] == {"days": 5, "lessons": 2} and first["details"]["after"] == {"days": 10, "lessons": 3}
    assert first["text"] == "Срок оплаты изменён: 5 → 10 дней; уроков без оплаты 2 → 3. Станут «К оплате»: 1; станут должниками: 0. Суммы не меняются"


def test_the_range_is_zero_to_fourteen_days_and_zero_to_four_lessons(pg_session, clock):
    w, *_ = world(pg_session, clock)
    for body in ({"pay_due_days": 15}, {"pay_due_days": -1}, {"pay_due_lessons": 5}, {"pay_due_lessons": -1}):
        put_settings(w, expect=422, **body)
    put_settings(w, pay_due_days=0, pay_due_lessons=0)
    put_settings(w, pay_due_days=14, pay_due_lessons=4)
    assert summary(w)["pay_due_days"] == 14


def test_the_term_is_read_with_the_money_right_and_written_by_the_owner_only(pg_session, clock):
    w, *_ = world(pg_session, clock)
    put_settings(w, pay_due_days=7)
    # an admin WITH the right to the money: the two numbers come with the summary, the settings themselves (GET and PUT, the dry run too) are the owner's
    s = summary(w, "admin")
    assert (s["pay_due_days"], s["pay_due_lessons"]) == (7, 2)
    assert client(w, "admin").get(f"{API}/org/settings/payments").status_code == 403
    assert client(w, "admin").put(f"{API}/org/settings/payments", json={"pay_due_days": 3}).status_code == 403
    assert client(w, "admin").put(f"{API}/org/settings/payments", params={"dry_run": "true"}, json={"pay_due_days": 3}).status_code == 403
    assert summary(w)["pay_due_days"] == 7                                  # nothing was written
    # a teacher has no way to it
    assert client(w, "A").get(f"{API}/org/settings/payments").status_code == 403
    assert client(w, "A").put(f"{API}/org/settings/payments", json={"pay_due_days": 3}).status_code == 403
    assert client(w, "A").get(f"{API}/org/payments/summary").status_code == 403


def test_an_admin_without_the_right_to_the_money_does_not_see_the_term(pg_session, clock):
    from tests.ops_helpers import grant_money_right

    w, *_ = world(pg_session, clock)
    grant_money_right(w, False)
    assert client(w, "admin").get(f"{API}/org/payments/summary").status_code == 403
    assert client(w, "admin").get(f"{API}/org/settings/payments").status_code == 403


def test_the_dry_run_writes_nothing_and_says_who_changes_name(pg_session, clock):
    w, *_ = world(pg_session, clock)
    before = _state(w)
    got = dry_run(w, pay_due_days=10)
    assert got["dry_run"] is True and got["pay_due_days"] == 5                # the answer shows the CURRENT settings
    assert got["pay_due_impact"] == {"to_due": 1, "to_debt": 0, "texts": ["1 ученик из должников станет «К оплате»"]}
    assert dry_run(w, pay_due_days=14)["pay_due_impact"]["to_due"] == 2
    assert dry_run(w, pay_due_days=0)["pay_due_impact"] == {"to_due": 0, "to_debt": 1, "texts": ["1 ученик станет должниками"]}
    assert dry_run(w, pay_due_days=5)["pay_due_impact"] is None             # the same term: nothing changes, no line
    assert _state(w) == before                                              # neither the setting nor the journal
    assert summary(w)["pay_due_days"] == 5


def test_the_plural_forms_of_the_line(pg_session, clock):
    from src.auth import payments_texts as tx

    assert tx.pay_due_impact_texts(1, 0) == ["1 ученик из должников станет «К оплате»"]
    assert tx.pay_due_impact_texts(2, 0) == ["2 ученика из должников станут «К оплате»"]
    assert tx.pay_due_impact_texts(5, 1) == ["5 учеников из должников станут «К оплате»", "1 ученик станет должниками"]
    assert tx.pay_due_impact_texts(0, 0) == []
