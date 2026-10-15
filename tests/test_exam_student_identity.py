"""N13 P1.4: strict student identification at exam entry (verify_group_student).

Only an *exact* ID or whole-word full name enters; suffixes, substrings and
silent "first candidate" picks are refused.  PostgreSQL tests use the shared
disposable world; the pure normalisation tests need no database.
"""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.auth import student_identity as si
from src.models.auth import StudentGroup

from tests.pg_world import make_group, make_student, needs_pg, pg_session, world  # noqa: F401  (fixtures)

NAMESAKES = "введите ваш ID"


# ---- pure normalisation ------------------------------------------------------


def test_normalisation_rules():
    assert si.normalize_name("  АЛЁНА   Ёлкина ") == "алена елкина"
    assert si.normalize_id(" te _ 031 ") == "te-031"
    assert si.normalize_id("TE–031") == si.normalize_id("te-031") == si.normalize_id("TE_031")
    assert si.is_digits_only("031") and not si.is_digits_only("te-031") and not si.is_digits_only("")
    assert si._display_number("TE-031") == "31" and si._display_number("NC1-007") == "7"
    assert si._display_number("legacy") is None and si._display_number(None) is None


def test_huge_digit_string_is_just_a_miss():
    student = SimpleNamespace(id=uuid4(), display_id="TE-031", student_number=None, first_name="А", last_name="Б")
    assert si.match_by_id([student], "9" * 6000).student is None


# ---- PostgreSQL: the real roster lookup -------------------------------------------


@pytest.fixture
def exam(world):
    """A fresh exam group with distinct IDs/names; helpers return the verify response."""
    db, org = world.db, world.org
    group = make_group(db, org, world.teacher, "Экзамен-группа")
    mk = lambda first, last, display, **kw: make_student(db, org, group, first, last, display)
    world.exam_group = group
    world.a = mk("Тимур", "Рахимов", "TE-031")
    world.a.student_number = "S-77"
    world.b = mk("Зарина", "Ахмедова", "TE-131")
    world.c = mk("Расул", "Назаров", "TE-021")
    world.m1 = mk("Мария", "Иванова", "TE-040")
    world.m2 = mk("Мария", "Иванова", "TE-041")
    world.yolka = mk("Алексей", "Ёлкин", "TE-050")
    world.malika = mk("Малика", "Алиева", "TE-060")
    # former member: membership closed, still named in the (stale) primary group
    world.former = mk("Бывший", "Ученик", "TE-070")
    db.query(StudentGroup).filter_by(student_id=world.former.id).update({"is_active": False})
    # archived student whose membership row is still active
    world.archived = mk("Архивный", "Ученик", "TE-071")
    world.archived.status = "archived"
    db.commit()
    world.verify = lambda **kw: world.svc.verify_group_student(str(group.id), **kw)
    return world


def ok(response, student):
    assert response["enrolled"] is True, response
    assert response["student"]["id"] == str(student.id)
    assert response["group_name"] == "Экзамен-группа"
    return response


def refused(response, *fragments):
    assert response["enrolled"] is False and "student" not in response, response
    for fragment in fragments:
        assert fragment in response["message"], response["message"]
    return response


@needs_pg
def test_response_shape_is_unchanged(exam):
    r = ok(exam.verify(student_id="TE-031"), exam.a)
    assert set(r) == {"enrolled", "group_name", "student"}
    assert set(r["student"]) == {"id", "first_name", "last_name", "full_name", "display_id", "class_level"}
    assert r["student"]["full_name"] == "Рахимов Тимур" and r["student"]["display_id"] == "TE-031"
    r = refused(exam.verify(student_id="TE-999"))
    assert set(r) == {"enrolled", "group_name", "message"}
    refused(exam.verify(), "укажите имя")
    refused(exam.verify(student_id="  ", student_name=" "), "укажите имя")


@needs_pg
def test_id_suffix_is_not_enough(exam):
    # «1» ends TE-031/TE-131/TE-021/TE-041/TE-071 but is none of their numbers
    refused(exam.verify(student_id="1"), "не найден")
    refused(exam.verify(student_id="31x"))
    refused(exam.verify(student_id="E-031"))  # suffix of the display id
    refused(exam.verify(student_id="31-TE"))
    refused(exam.verify(student_id="XXTE-031"))  # input merely *ending* with an ID
    refused(exam.verify(student_id="TE-31"))  # not the same string, not a bare number


@needs_pg
def test_bare_number_matches_only_one_whole_number(exam):
    for typed in ("31", "031", "0031", " 31 "):
        ok(exam.verify(student_id=typed), exam.a)
    ok(exam.verify(student_id="131"), exam.b)  # TE-031 is not a suffix of TE-131 and vice versa
    ok(exam.verify(student_id="21"), exam.c)
    refused(exam.verify(student_id="3"))
    refused(exam.verify(student_id="0"))
    refused(exam.verify(student_id="2131"))


@needs_pg
def test_ambiguous_bare_number_asks_for_the_full_id(exam):
    make_student(exam.db, exam.org, exam.exam_group, "Другой", "Тезка", "TE-0031")
    exam.db.commit()
    for typed in ("31", "031"):
        refused(exam.verify(student_id=typed), "Несколько учеников подходят под этот номер", "введите ID полностью", "TE-031")
    # the full IDs still work
    ok(exam.verify(student_id="TE-031"), exam.a)
    assert exam.verify(student_id="te-0031")["enrolled"] is True


@needs_pg
def test_id_case_dash_space_variants(exam):
    for typed in ("TE-031", "te-031", "Te_031", "  te - 031 ", "TE-0 31", "TE–031"):
        ok(exam.verify(student_id=typed), exam.a)
    ok(exam.verify(student_id="s_77"), exam.a)  # student number, same normalisation
    refused(exam.verify(student_id="TE031"))  # a missing dash is not a variant (spaces are only dropped)
    ok(exam.verify(student_id=str(exam.a.id).upper()), exam.a)  # UUID


@needs_pg
def test_name_substring_and_single_word_are_rejected(exam):
    refused(exam.verify(student_name="ал"), "Введите имя и фамилию полностью или ID")
    refused(exam.verify(student_name="Малика"), "Введите имя и фамилию полностью или ID")
    refused(exam.verify(student_name="ал алиева"), "не найден")  # partial word
    refused(exam.verify(student_name="Малика Али"), "не найден")
    refused(exam.verify(student_name="Тимур Рахим"), "не найден")
    refused(exam.verify(student_name="Тимур Назаров"), "не найден")  # two real students' words
    refused(exam.verify(student_name="Неизвестный Ученик"), "не найден")


@needs_pg
def test_full_name_in_both_orders_and_yo(exam):
    ok(exam.verify(student_name="Тимур Рахимов"), exam.a)
    ok(exam.verify(student_name="рахимов   ТИМУР"), exam.a)
    ok(exam.verify(student_name="  Тимур  Рахимов  "), exam.a)
    ok(exam.verify(student_name="Алексей Елкин"), exam.yolka)  # е for ё
    ok(exam.verify(student_name="ёлкин алексей"), exam.yolka)
    ok(exam.verify(student_name="Тимур Рахимов Алиевич"), exam.a)  # an extra patronymic does not weaken it
    ok(exam.verify(query="Тимур Рахимов"), exam.a)


@needs_pg
def test_namesakes_must_use_the_id(exam):
    refused(exam.verify(student_name="Мария Иванова"), NAMESAKES)
    refused(exam.verify(student_name="иванова мария"), NAMESAKES)
    ok(exam.verify(student_id="TE-040", student_name="Мария Иванова"), exam.m1)
    ok(exam.verify(student_id="41"), exam.m2)


@needs_pg
def test_id_with_name_checks_the_owner_by_whole_words(exam):
    ok(exam.verify(student_id="TE-031", student_name="Тимур Рахимов"), exam.a)
    ok(exam.verify(student_id="TE-031", student_name="рахимов"), exam.a)
    ok(exam.verify(student_id="TE-031", student_name="Тимур"), exam.a)
    for wrong in ("Зарина Ахмедова", "Тимур Назаров", "Тим", "Рахим", "Тимур Рахимов Иванов"):
        refused(exam.verify(student_id="TE-031", student_name=wrong), "не соответствуют")
    # an unknown ID is reported as such, whatever the name
    refused(exam.verify(student_id="TE-999", student_name="Тимур Рахимов"), "не найден")


@needs_pg
def test_only_the_current_active_roster_enters(exam):
    # a student of another group of the same org, and one of a foreign org
    elsewhere = exam.students[0]  # in world.group, not in the exam group
    refused(exam.verify(student_id=elsewhere.display_id), "не найден")
    refused(exam.verify(student_id=str(elsewhere.id)), "не найден")
    refused(exam.verify(student_name="Алия Каримова"), "не найден")
    refused(exam.verify(student_id=exam.foreign_student.display_id), "не найден")
    # closed membership
    refused(exam.verify(student_id="TE-070"), "не найден")
    refused(exam.verify(student_id="70"))
    refused(exam.verify(student_name="Бывший Ученик"), "не найден")
    # archived student, even with a leftover active membership
    refused(exam.verify(student_id="TE-071"), "не найден")
    refused(exam.verify(student_name="Архивный Ученик"), "не найден")
    # a group without a roster / an unknown group
    assert exam.svc.verify_group_student(str(uuid4()), student_id="TE-031")["message"] == "Группа не найдена"
    empty = make_group(exam.db, exam.org, exam.teacher, "Пустая")
    exam.db.commit()
    refused(exam.svc.verify_group_student(str(empty.id), student_id="TE-031"), "нет зарегистрированных")


@needs_pg
def test_free_form_query_is_an_id_when_it_is_one(exam):
    ok(exam.verify(query="te-031"), exam.a)
    ok(exam.verify(query="31"), exam.a)
    ok(exam.verify(query="Тимур Рахимов"), exam.a)
    refused(exam.verify(query="Тимур"), "Введите имя и фамилию полностью или ID")
    refused(exam.verify(query="1"))
