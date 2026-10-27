"""W1: the people of the journal «Замены» carry their org role in this centre (the front names the OWNER as the owner, not as an administrator) (PostgreSQL)."""
import pytest

from tests.b2_helpers import API, build_d2, clock, needs_pg, pg_session  # noqa: F401  (fixtures)
from tests.ops_helpers import client
from tests.test_w1_cancel_pg import restore, sheet

pytestmark = needs_pg
SUB = f"{API}/org/substitutions"


@pytest.fixture
def w(pg_session, clock):
    clock.set("2026-10-21 14:00")
    return build_d2(pg_session)


def roles_in(item):
    """Every person of an item: {where: (id, role)}."""
    out = {"teacher": item["teacher"]}
    for key in ("by", "substitute"):
        if (item.get("decision") or {}).get(key):
            out[f"decision.{key}"] = item["decision"][key]
    for key in ("clearedBy", "clearedTo"):
        if item.get(key):
            out[key] = item[key]
    return {k: (v["id"], v.get("role")) for k, v in out.items()}


def test_an_owners_cancel_and_restore_come_back_with_role_owner(w, clock):
    sheet(w, "owner", w.G1, "lesson-l1", "2026-10-26", reason="Ошибка")
    items = client(w, "owner").get(SUB, params={"status": "decided"}).json()["items"]
    mine = [i for i in items if i["group"]["id"] == str(w.G1.id) and i["lesson"]["date"] == "2026-10-26"]
    assert mine and all(roles_in(i)["decision.by"] == (str(w.owner.id), "owner") for i in mine)
    clock.set("2026-10-21 14:01")
    restore(w, "owner", w.G1, "lesson-l1", "2026-10-26")
    items = client(w, "owner").get(SUB, params={"status": "decided"}).json()["items"]
    mine = [i for i in items if i["group"]["id"] == str(w.G1.id) and i["lesson"]["date"] == "2026-10-26"]
    restored = [i for i in mine if i["decision"]["closedReason"] == "LESSON_RESTORED"]
    assert len(restored) == 1 and roles_in(restored[0])["decision.by"] == (str(w.owner.id), "owner")      # «Вернул урок» by the owner - never «администратор»
    assert all(r == (str(w.owner.id), "owner") for k, r in roles_in(restored[0]).items() if k == "decision.by")


def test_an_admin_is_an_admin_and_the_history_events_carry_roles_too(w, clock):
    sheet(w, "admin", w.G1, "lesson-l1", "2026-10-26", reason="Ошибка")
    items = client(w, "owner").get(SUB, params={"status": "decided"}).json()["items"]
    (item,) = [i for i in items if i["group"]["id"] == str(w.G1.id) and i["lesson"]["date"] == "2026-10-26"]
    assert item["decision"]["by"]["role"] == "admin" and item["teacher"]["role"] in ("admin", "teacher", "owner")
    detail = client(w, "owner").get(f"{SUB}/{item['id']}")
    assert detail.status_code == 200 and detail.json()["events"]
    assert all(e["actor"] is None or e["actor"]["role"] in ("owner", "admin", "teacher") for e in detail.json()["events"])
