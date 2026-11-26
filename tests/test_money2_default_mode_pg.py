"""Money-2 T16: a NEW centre is created with the payment mode «Общий месяц» (calendar month, payment day the 1st) WRITTEN in its settings; a centre without the entry is read as before (PostgreSQL)."""
from uuid import uuid4

from tests.console_helpers import API, cw, needs_pg  # noqa: F401  (fixtures)
from tests.test_console_requests_pg import URL, approve_setup
from src.auth.payments_service import PaymentService
from src.models.auth import Organization

pytestmark = needs_pg

EXPLICIT = {"period_mode": "calendar", "billing_anchor": "fixed_day", "billing_day": 1}


def _mode_of(db, org):
    s = PaymentService(db).get_settings(str(org.id))
    return {"period_mode": s.period_mode, "billing_anchor": s.billing_anchor, "billing_day": s.billing_day}


def test_t16_the_centre_made_from_a_request_of_the_landing_has_the_common_month_written_explicitly(cw):
    row, c = approve_setup(cw, centre_name=f"Центр Т16 {uuid4().hex[:6]}")
    assert c.post(f"{URL}/{row.id}/approve", json={}).status_code == 200
    org = cw.db.query(Organization).filter_by(name=row.centre_name).one()
    block = (org.settings or {}).get("payments") or {}
    assert {k: block.get(k) for k in EXPLICIT} == EXPLICIT                                   # written, not left to the reading rule
    assert _mode_of(cw.db, org) == EXPLICIT


def test_t16_the_centre_registered_by_the_owner_has_it_too_and_a_centre_without_the_entry_is_read_as_before(cw):
    from src.auth.schemas import OrgApplicationRequest
    from src.auth.service import AuthService

    name = f"Т16 регистрация {uuid4().hex[:6]}"
    AuthService(cw.db).apply_organization(OrgApplicationRequest(org_name=name, org_type="center", email=f"t16-{uuid4().hex[:8]}@example.test",
                                                               password="secret-pass-123", first_name="Иван", last_name="Иванов"))
    org = cw.db.query(Organization).filter_by(name=name).one()   # by the name, not "the newest": parallel workers create centres at the same time
    assert {k: (org.settings or {}).get("payments", {}).get(k) for k in EXPLICIT} == EXPLICIT
    # a centre created without the entry (every centre that exists before this change): the reading rule is the old one - the payment date, no common month
    old = Organization(name=f"Т16 старый {uuid4().hex[:6]}", org_type="center", status="active")
    cw.db.add(old)
    cw.db.commit()
    assert _mode_of(cw.db, old) == {"period_mode": "calendar", "billing_anchor": "payment_date", "billing_day": 1}
