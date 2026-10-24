"""MN-10 / MN-43 through the real endpoints (PostgreSQL): the payments journal (xlsx + csv), the statement (xlsx + csv), the payroll sheet."""
import csv
import io
import re
from datetime import date, datetime

import pytest
from openpyxl import load_workbook

from src.models.auth import Student, StudentGroup
from tests.pg_world import needs_pg, pg_session, world  # noqa: F401  (fixtures)
from tests.test_pay2_api_pg import API, e1_world, fixed_today, owner  # noqa: F401  (fixtures + helpers)

pytestmark = needs_pg


def hostile_student(world, m):
    student = world.db.get(Student, m.student_id)
    student.last_name, student.first_name = "=Тест", "-Анвар"
    world.db.commit()
    return student


def test_statement_csv_prefixes_names_and_tab_cr_cells_and_parses_back(world, fixed_today):
    m = e1_world(world)
    hostile_student(world, m)
    r = owner(world).get(f"{API}/org/memberships/{m.id}/statement/export", params={"format": "csv"})
    assert r.status_code == 200
    rows = list(csv.reader(io.StringIO(r.text.lstrip("﻿")), delimiter=";"))
    cells = [c for line in rows for c in line]
    assert any("'=Тест" in c for c in cells), cells[:12]
    numbers = re.compile(r"-\d+")                                   # a negative balance is a NUMBER (typed, not text): Excel reads it as a number
    loose = [c for c in cells if c[:1] in ("=", "+", "-", "@", "\t", "\r") and not numbers.fullmatch(c)]
    assert not loose, loose


def test_statement_xlsx_shows_the_names_as_typed(world, fixed_today):
    m = e1_world(world)
    hostile_student(world, m)
    r = owner(world).get(f"{API}/org/memberships/{m.id}/statement/export", params={"format": "xlsx"})
    ws = load_workbook(io.BytesIO(r.content)).active
    hot = [c for row in ws.iter_rows() for c in row if isinstance(c.value, str) and "=Тест" in c.value]
    assert hot and all(c.data_type == "s" and "'=Тест" not in c.value for c in hot)
    assert all(c.quotePrefix for c in hot if c.value.startswith("="))
    assert all(c.data_type != "f" for row in ws.iter_rows() for c in row)
