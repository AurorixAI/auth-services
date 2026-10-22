"""ROADMAP #126 (additive): ``price.set_by_name`` and ``price.scheduled`` in the money rows (payments/status, memberships), ``scheduled_override_count``
in GET /org/group-prices - without a query per row."""
import json
from pathlib import Path

from src.auth.payments_files import staff_name

from tests.pg_world import give_group_price, make_group, make_student, pg_session, world  # noqa: F401  (fixtures)
from tests.test_group_price_pg import cancel_override, get_override, put_override
from tests.test_opening_bulk_pg import Counter
from tests.test_payments_service_pg import API, day, mid, owner, pytestmark, row_of  # noqa: F401


def status_items(w):
    r = owner(w).get(f"{API}/org/payments/status", params={"limit": 500})
    assert r.status_code == 200, r.text
    return {i["student_id"]: i for i in r.json()["items"]}


def test_set_by_name_is_the_author_of_the_individual_price_in_force(world):
    give_group_price(world.db, world.group, 600_000, valid_from=day(-60))
    s0, s1 = world.students[0], world.students[1]
    put_override(world, s0, amount=400_000, reason="скидка", valid_from=day(-10))
    rows = status_items(world)
    me = staff_name(world.owner.first_name, world.owner.last_name)
    assert me and rows[str(s0.id)]["price"]["source"] == "override" and rows[str(s0.id)]["price"]["set_by_name"] == me
    assert rows[str(s1.id)]["price"]["source"] == "group" and rows[str(s1.id)]["price"]["set_by_name"] is None
    assert row_of(world, s0)["price"]["set_by_name"] == me                                          # the membership rows of a student too


def test_scheduled_is_the_nearest_future_individual_price_for_any_row(world):
    give_group_price(world.db, world.group, 600_000, valid_from=day(-60))
    s0, s1, s2 = world.students
    put_override(world, s0, amount=400_000, reason="скидка", valid_from=day(-10))
    put_override(world, s0, amount=350_000, reason="ещё скидка", valid_from=day(20))
    put_override(world, s0, amount=300_000, reason="потом", valid_from=day(50))
    put_override(world, s1, amount=450_000, reason="с декабря", valid_from=day(15))                   # a student on the GROUP price today
    rows = status_items(world)
    sched = rows[str(s0.id)]["price"]["scheduled"]
    assert (sched["amount"], sched["valid_from"], sched["reason"]) == (350_000, day(20).isoformat(), "ещё скидка") and sched["row_id"]
    assert rows[str(s0.id)]["price"]["amount"] == 400_000                                              # the price today is untouched
    assert rows[str(s1.id)]["price"]["source"] == "group" and rows[str(s1.id)]["price"]["scheduled"]["amount"] == 450_000
    assert rows[str(s2.id)]["price"]["scheduled"] is None
    # «цена группы снова» from a day = amount null
    put_override(world, s2, amount=None, valid_from=day(30), reason=None)
    got = status_items(world)[str(s2.id)]["price"]["scheduled"]
    assert got["amount"] is None and got["valid_from"] == day(30).isoformat()
    # the row id is the one the history and DELETE use; after the cancel the next one is the nearest
    hist = get_override(world, s0)["history"]
    first = next(h for h in hist if h["valid_from"] == day(20).isoformat())
    assert sched["row_id"] == first["id"]
    cancel_override(world, first["id"], s0)
    assert status_items(world)[str(s0.id)]["price"]["scheduled"]["amount"] == 300_000


def test_scheduled_override_count_per_group_counts_students_not_rows(world):
    give_group_price(world.db, world.group, 600_000, valid_from=day(-60))
    other = make_group(world.db, world.org, world.teacher2, "Вторая")
    make_student(world.db, world.org, other, "Эльза", "Новая", "Z-1")
    world.db.commit()
    s0, s1, s2 = world.students
    put_override(world, s0, amount=350_000, reason="первая", valid_from=day(20))
    put_override(world, s0, amount=300_000, reason="вторая", valid_from=day(50))                            # the same student: counted once
    put_override(world, s1, amount=450_000, reason="третья", valid_from=day(15))
    put_override(world, s2, amount=400_000, reason="четвёртая", valid_from=day(-5))                            # in force already: not scheduled
    items = {i["group_id"]: i for i in owner(world).get(f"{API}/org/group-prices").json()["items"]}
    assert items[str(world.group.id)]["scheduled_override_count"] == 2 and items[str(world.group.id)]["students_with_override"] == 1
    assert items[str(other.id)]["scheduled_override_count"] == 0
    from src.models.auth import Student

    world.db.get(Student, s1.id).status = "archived"                                                  # an archived student is not counted
    world.db.commit()
    items = {i["group_id"]: i for i in owner(world).get(f"{API}/org/group-prices").json()["items"]}
    assert items[str(world.group.id)]["scheduled_override_count"] == 1


def test_the_new_fields_need_no_query_per_row(world):
    give_group_price(world.db, world.group, 600_000, valid_from=day(-60))
    counts = []
    for extra in (0, 40):
        for i in range(extra):
            make_student(world.db, world.org, world.group, f"Н{i}", f"Ф{i}", f"Q-{extra}-{i}")
        world.db.commit()
        for s in world.students[:2]:
            put_override(world, s, amount=350_000 + extra, reason="план", valid_from=day(20 + extra))
        with Counter(world.db) as c:
            r = owner(world).get(f"{API}/org/payments/status", params={"limit": 500})
        assert r.status_code == 200 and len(r.json()["items"]) >= 3 + extra
        with Counter(world.db) as g:
            assert owner(world).get(f"{API}/org/group-prices").status_code == 200
        counts.append((c.n, g.n))
    assert abs(counts[0][0] - counts[1][0]) <= 1 and counts[0][1] == counts[1][1], counts


def test_the_frozen_fixtures_know_the_new_price_keys():
    root = Path(__file__).resolve().parent / "fixtures"
    for name in ("pay2/pay2_contract.json", "logicA/logicA_contract.json"):
        schema = json.loads((root / name).read_text(encoding="utf-8"))["components"]["schemas"]
        assert {"set_by_name", "scheduled"} <= set(schema["PriceOut"]["properties"]) and "PriceScheduled" in schema
        assert not {"set_by_name", "scheduled"} & set(schema["PriceOut"].get("required", []))          # additive: nothing becomes required
