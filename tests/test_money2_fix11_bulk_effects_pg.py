"""Money-2 FIX11 S1: the answer of the bulk adding says, per student, the same ``effects`` as the answer of the single transfer (what moves / stays / ends, in the words of the server), in the dry run
and in the real answer (PostgreSQL).  The numbers are the ones of ``test_money2_transfer_a_pg`` (A: 300 000 a month, a lesson every day, the transfer on 22.11: 02..22.11 = 21 lessons of 30 = 210 000)."""
from datetime import date

from src.auth import payments_texts as tx
from tests.pg_world import make_client, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_money2_transfer_a_pg import groups3, joined, pay
from tests.test_pay2_api_pg import API, fixed_today, owner  # noqa: F401  (fixture)

pytestmark = needs_pg


def bulk(w, to, ids, *, source=None, mode="transfer", dry_run=False, client=None):
    body = {"student_ids": [str(i) for i in ids], **({"from_group_id": str(source.id)} if source is not None else {})}
    r = (client or owner(w)).post(f"{API}/org/groups/{to.id}/students/bulk", params={"mode": mode, **({"dry_run": "true"} if dry_run else {})}, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_s1_a_bulk_transfer_names_the_debt_that_stays_the_advance_that_moves_and_the_clean_case(world, fixed_today):
    # S0 owes 210 000 in A (nothing paid); S1 paid exactly 210 000 (no advance, no debt); S2 paid 600 000: an advance of 390 000 travels.
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0, s1, s2 = world.students[:3]
    for s in (s0, s1, s2):
        joined(world, s, a, date(2026, 11, 2))
    pay(world, s1, a, amount=210_000)
    pay(world, s2, a, amount=600_000)
    dry = bulk(world, b, [s0.id, s1.id, s2.id], source=a, dry_run=True)
    real = bulk(world, b, [s0.id, s1.id, s2.id], source=a)
    assert dry["dry_run"] is True and real["dry_run"] is False and real["added"] == 3
    for answer in (dry, real):
        by_id = {s["student_id"]: s["effects"] for s in answer["students"]}
        debt = [x for x in by_id[str(s0.id)]["stays"] if x["code"] == "debt"]
        assert len(debt) == 1 and debt[0]["amount"] == 210_000 and debt[0]["group_name"] == a.name
        assert debt[0]["text"] == f"Останется долг {tx.money(210_000)}"      # PAY-TAILS Б1.3: the short sentence of the total
        assert by_id[str(s0.id)]["money_text"] is None
        assert by_id[str(s1.id)]["money_text"] == "Деньги не изменятся: в прошлой группе нет аванса и долга" and by_id[str(s1.id)]["moves"] == []
        (money,) = [m for m in by_id[str(s2.id)]["moves"] if m["code"] == "money"]
        assert money["amount"] == 390_000
        (note,) = [w for w in answer["warnings"] if w["code"] == "payment_carried_over" and w["student_id"] == str(s2.id)]
        assert tx.money(390_000) in note["message"]                                       # the same sum as the line of «warnings»
    assert {s["student_id"]: s["effects"] for s in dry["students"]} == {s["student_id"]: s["effects"] for s in real["students"]}      # the window says what the server writes


def test_s1_an_ordinary_bulk_adding_has_no_effects_and_a_caller_without_the_money_right_cannot_transfer(world, fixed_today):
    a, b, c = groups3(world, fixed_today, date(2026, 11, 22))
    s0, s1 = world.students[:2]
    joined(world, s0, a, date(2026, 11, 2))
    joined(world, s1, a, date(2026, 11, 2))
    added = bulk(world, c, [s0.id], mode="add")
    assert added["added"] == 1 and [s["effects"] for s in added["students"]] == [None]
    admin = make_client(world, user=world.admin, org_role="admin")                # no right to money: the transfer is refused, so no sum-less words can come out of it
    refused = bulk(world, b, [s1.id], source=a, client=admin, dry_run=True)
    assert refused["added"] == 0 and refused["students"] == [] and "переносит" in refused["skipped_details"][0]["reason"]
