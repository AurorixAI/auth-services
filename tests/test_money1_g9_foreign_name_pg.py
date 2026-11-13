"""Money-1 G-9 (MONEY1_G1_REVIEW N12): the file of the journal never prints the name of a user of ANOTHER centre in its title block."""
import csv
import io

from tests.pg_world import needs_pg, pg_session, world  # noqa: F401
from tests.test_pay2_api_pg import API, fixed_today, owner  # noqa: F401

pytestmark = needs_pg


def _title(w, **params):
    r = owner(w).get(f"{API}/org/payments/journal/export", params={"from": "2026-10-01", "to": "2026-10-31", "format": "csv", **params})
    assert r.status_code == 200, r.text
    return "\n".join(" ".join(row) for row in csv.reader(io.StringIO(r.text.lstrip("﻿")), delimiter=";") if row)


def test_a_user_of_another_centre_is_never_named_in_the_title(world, fixed_today):
    world.foreign_admin.first_name, world.foreign_admin.last_name = "Чужаков", "Посторонний"
    world.db.commit()
    for key in ("teacher_id", "created_by"):
        text = _title(world, **{key: str(world.foreign_admin.id)})
        assert "Чужаков" not in text and "Посторонний" not in text
        assert "нет в центре" in text


def test_a_user_of_the_centre_is_named(world, fixed_today):
    assert world.teacher.first_name in _title(world, teacher_id=str(world.teacher.id))
    assert world.admin.first_name in _title(world, created_by=str(world.admin.id))
