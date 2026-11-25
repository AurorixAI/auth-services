"""PAY-TAILS step 8.14.15 (the addition of the coordinator, B11.50 E7): ``group_history`` of the student profile carries ``membership_id`` - the participation of THIS group (the screen opens the window of the pause of a former stay by it)."""
from datetime import date as D

from sqlalchemy import event

from tests.answers_dump import dump
from tests.money1b_helpers import add
from tests.pg_world import make_group, needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, fixed_today, membership, owner  # noqa: F401  (fixture)
from tests.test_pay_tails_b14_pg import pauses_of, world_cut

pytestmark = needs_pg


def test_the_profile_history_names_the_participation_of_each_group_and_the_number_of_queries_did_not_grow(world, fixed_today):
    # By hand: the student is in the group g (a stay with the pause 17-30.11 cut to 17-19.11 by the leaving of 19.11: a FORMER stay) and, after that, in the group h (ACTIVE): two items of the history; each ``membership_id`` is the id of
    # the participation of THAT group; the list of the pauses of the former participation has the one pause (closed by the leaving).  Queries of the profile: 10 -> 10 (the head f6784a3 measured on the same world: 10; a loaded attribute adds none).
    g, s, pause_id = world_cut(world, fixed_today)
    h = make_group(world.db, world.org, world.teacher, "Вторая")
    add(world, s, group=h, mode="add")
    count = [0]

    def tick(*a, **k):
        count[0] += 1

    engine = world.db.get_bind()
    event.listen(engine, "before_cursor_execute", tick)
    try:
        r = owner(world).get(f"{API}/org/students/{s.id}/profile")
    finally:
        event.remove(engine, "before_cursor_execute", tick)
    assert r.status_code == 200, r.text
    by_group = {i["group_id"]: i for i in r.json()["group_history"]}
    assert by_group[str(g.id)]["membership_id"] == str(membership(world, s, g).id) and by_group[str(g.id)]["is_active"] is False
    assert by_group[str(h.id)]["membership_id"] == str(membership(world, s, h).id) and by_group[str(h.id)]["is_active"] is True
    assert by_group[str(g.id)]["membership_id"] != by_group[str(h.id)]["membership_id"]
    pauses = pauses_of(world, s, g)
    assert [p["id"] for p in pauses] == [pause_id] and pauses[0]["days_locked"] is True
    assert count[0] <= 10, count[0]                                     # was 10 on the head f6784a3
    dump("student_profile_former_stay", r.json())
    dump("pauses_list_former_stay", pauses)
