"""PAY-TAILS А6.9: the release check «было -> станет» (``src/auth/pay_tails_report.py``, ``scripts/pay_tails_before_after.py``): read only, ids and sums."""
import json
from datetime import date, datetime

from src.auth import pay_tails_report as report
from src.models.auth import StudentRefund

from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay_tails_archive_pg import archive, restore, alia_left
from tests.test_pay_tails_contract_pg import c2
from tests.test_pay_tails_money_pg import D, fixed_today, membership  # noqa: F401  (fixture)
from tests.test_pay2_api_pg import stay

pytestmark = needs_pg


def test_the_dump_lists_the_refund_that_the_new_rule_does_not_allow_and_the_diff_prints_the_changes(world, fixed_today):
    # an «old» refund of 126 000 as the probe receipt was (money of a forgiveness): the new limit of its day is 0 -> in the list; the diff of an «old» file against the new one names the account
    g, s, made = c2(world, fixed_today)
    m = membership(world, s, g)
    world.db.add(StudentRefund(org_id=world.org.id, membership_id=m.id, student_id=s.id, group_id=g.id, amount=126_000, method="cash", reason="проба", effective_on=D(2026, 11, 21),
                               created_by=world.owner.id, created_at=datetime(2026, 11, 21, 8), receipt_no=1))
    world.db.commit()
    after = report.dump(world.db, [world.org.id], D(2026, 11, 21))
    (over,) = after["refunds_over_limit"]
    assert (over["membership"], over["amount"], over["limit"], over["receipt"]) == (str(m.id), 126_000, 0, "В-1")            # А7.9: the receipt is named
    now = after["memberships"][str(m.id)]
    assert (now["charged"], now["written_off"], now["written_off_granted"]) == (294_000, 220_000, 220_000)      # the refund already left the cash: money 74 000, the forgiveness covers the other 220 000
    before = json.loads(json.dumps(after))
    before["memberships"][str(m.id)].update(advance=126_000, refundable=126_000, written_off=99_000, transferred_out=126_000)       # what the OLD image said
    lines = report.diff(before, after)
    assert lines[0].startswith("memberships=") and "changed=1" in lines[0] and "transfers_changed=1" in lines[0] and "refunds_over_limit=1" in lines[0]
    assert any(str(m.id) in ln and "advance: 126000 -> 0" in ln and "written_off: 99000 -> 220000" in ln for ln in lines[1:])
    assert any("REFUND OVER LIMIT" in ln and "receipt=В-1" in ln and f"membership={m.id}" in ln for ln in lines)
    assert after["memberships"][str(m.id)]["transferred_in"] == 0 and "transferred_in" in report.FIELDS
    before["memberships"][str(m.id)]["transferred_in"] = 5_000
    assert any(str(m.id) in ln and "transferred_in: 5000 -> 0" in ln for ln in report.diff(before, after)[1:])                  # А7.9: the receiving side of a transfer is a field of its own
    same = report.diff(after, after)
    assert same[0].startswith("memberships=4 changed=0") and not [ln for ln in same if " -> " in ln]                          # nothing changed: no «было -> станет» line (the list of refunds stays)


def test_the_dump_lists_a_stay_that_overlaps_an_archive_interval_and_nothing_else(world, fixed_today):
    group, s = alia_left(world, fixed_today)
    fixed_today(D(2026, 11, 14))
    archive(world, group)
    fixed_today(D(2026, 11, 18))
    restore(world, group)
    assert report.dump(world.db, [world.org.id], D(2026, 11, 18))["crossing_archive"] == []            # Alia left on 11.11, before the archive of 14.11
    from tests.money1b_helpers import add, fresh_student

    b = fresh_student(world, "Задним числом")
    fixed_today(D(2026, 11, 19))
    add(world, b, group=group, mode="add")
    stay(world, membership(world, b, group), D(2026, 11, 16))                                         # written back into the interval 14.11 - 18.11
    (found,) = report.dump(world.db, [world.org.id], D(2026, 11, 19))["crossing_archive"]
    assert found["membership"] == str(membership(world, b, group).id)


def test_the_dump_lists_a_membership_of_the_common_month_with_a_pause_and_a_forgiveness(world, fixed_today):
    # А7.9: the accounts that the fix of the capacity per credit (К1) may change: ОМ, a live pause, a forgiveness.  The plain ОМ without a pause is not on the list.
    from tests.test_pay_tails_k1_pg import with_pause_in_november
    from tests.test_pay_tails_money_pg import forgive

    g, s = with_pause_in_november(world, fixed_today)
    fixed_today(D(2026, 12, 2))
    forgive(world, s, g, 300_000)
    m = membership(world, s, g)
    (found,) = report.dump(world.db, [world.org.id], D(2026, 12, 2))["om_pause_forgiven"]
    assert found == {"membership": str(m.id), "pauses": 1, "granted": 300_000}
    assert "om_pause_forgiven=1" in report.diff({"memberships": {}, "om_pause_forgiven": []}, {"memberships": {}, "refunds_over_limit": [], "crossing_archive": [], "om_pause_forgiven": [found]})[0]
