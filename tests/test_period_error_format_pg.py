"""MN-11 (wave E): the same fault - «the end of the period is before its start» - answers the same 422 on every payments report:
``detail = {code: INVALID_PERIOD, message}`` (the receipts report used to answer a bare string)."""
import pytest

from tests.pg_world import make_client, needs_pg, pg_session, world  # noqa: F401  (fixtures)

pytestmark = needs_pg
API = "/api/v1/auth/org/payments"
WRONG = {"from": "2026-10-05", "to": "2026-10-04"}
EXPECTED = {"code": "INVALID_PERIOD", "message": "Конец периода раньше начала"}


@pytest.mark.parametrize("path, extra", [
    ("receipts", {"group_by": "day"}),
    ("receipts", {"group_by": "month"}),
    ("receipts/items", {"column": "received"}),
    ("income", {}),
    ("journal", {}),
])
def test_an_end_before_the_start_answers_one_422_everywhere(world, path, extra):
    r = make_client(world, user=world.owner).get(f"{API}/{path}", params={**WRONG, **extra})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == EXPECTED
