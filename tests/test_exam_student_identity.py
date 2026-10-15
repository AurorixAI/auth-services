"""N13 P1.4: strict student identification at exam entry (verify_group_student).

Only an *exact* ID or whole-word full name enters; suffixes, substrings and
silent "first candidate" picks are refused.  PostgreSQL tests use the shared
disposable world; the pure normalisation tests need no database.
"""
import unicodedata
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
    assert si.normalize_id("TE\u2013031") == si.normalize_id("te-031") == si.normalize_id("TE_031") == si.normalize_id("te 031")
    assert si.is_digits_only("031") and not si.is_digits_only("te-031") and not si.is_digits_only("")
    assert si._display_number("TE-031") == "31" and si._display_number("NC1-007") == "7"
    assert si._display_number("legacy") is None and si._display_number(None) is None


def test_name_typography_is_ignored_on_both_sides():
    name = si.normalize_name
    # the apostrophe family is deleted
    assert name("Ma'rufjon") == name("Ma\u2019rufjon") == name("Ma\u02bcrufjon") == name("Ma`rufjon") == name("Ma\u00b4rufjon") == name("Marufjon") == "marufjon"
    assert name("O\u02bbg\u02bbiloy") == name("O'g'iloy") == name("O\u2018g\u2018iloy") == "ogiloy"
    # hyphens and dashes read as a space
    for dash in "-\u2010\u2011\u2012\u2013\u2014\u2212":
        assert name(f"Иванова{dash}Петрова") == "иванова петрова", repr(dash)
    # invisible characters from copy/paste
    assert name("Али\u200bса") == name("Али\u200c\u200dса") == name("Али\u2060са") == name("\ufeffАлиса") == name("Али\u00adса") == "алиса"
    # NFD «й» / «ё» compose, ё -> е
    assert name(unicodedata.normalize("NFD", "Юрий Гайдай")) == name("Юрий Гайдай") == "юрий гайдай"
    assert name(unicodedata.normalize("NFD", "Алёна")) == "алена"
    # Uzbek Cyrillic letters typed on a Russian keyboard
    assert name("Ғолиб Қодиров Ҳасан Ўктам") == name("Голиб Кодиров Хасан Уктам") == "голиб кодиров хасан уктам"
    # fullwidth / compatibility forms
    assert name("\uff21\uff4c\uff49") == "ali"


def test_id_typography_is_ignored_but_the_separator_stays():
    norm = si.normalize_id
    assert norm("ТЕ-031") == norm("те_031") == norm("TE 031") == norm("  te -  031 ") == "te-031"  # Cyrillic look-alikes
    assert norm("АВЕКМНОРСТХ") == "abekmhopctx" == norm("abekmhopctx")
    assert norm("NC1-001") == "nc1-001" and norm("NC10-01") == "nc10-01"
    assert norm("NC1-001") != norm("NC10-01") and norm("TE031") != norm("TE-031")
    assert norm("TE\u200b-031") == "te-031"


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
    world.marufjon = mk("Ma\u2019rufjon", "Karimov", "TE-081")  # stored with a typographic apostrophe
    world.ogiloy = mk("O\u02bbg\u02bbiloy", "Rahmonov", "TE-082")  # stored with the Uzbek modifier letters
    world.double = mk("Ольга", "Иванова-Петрова", "TE-083")
    world.yuriy = mk("Юрий", "Гайдай", "TE-084")
    world.golib = mk("Ғолиб", "Саидов", "TE-085")
    world.ali = mk("Али", "Каримов", "TE-086")
    world.ali_akbar = mk("Али Акбар", "Каримов", "TE-087")
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


GENERIC_HINT = "введите ID полностью, как в карточке ученика (буквы и цифры)"


@needs_pg
def test_ambiguous_bare_number_asks_for_the_full_id(exam):
    make_student(exam.db, exam.org, exam.exam_group, "Другой", "Тезка", "TE-0031")
    exam.db.commit()
    for typed in ("31", "031"):
        r = refused(exam.verify(student_id=typed), "Несколько учеников подходят под этот номер", GENERIC_HINT)
        assert "TE-" not in r["message"]  # the hint is fixed text, never built from a real student
    # the full IDs still work
    ok(exam.verify(student_id="TE-031"), exam.a)
    assert exam.verify(student_id="te-0031")["enrolled"] is True


@needs_pg
def test_bare_number_merges_student_number_and_display_number(exam):
    # «31» is the display number of TE-031 (a) and the student number of b: two people
    exam.b.student_number = "31"
    exam.db.commit()
    refused(exam.verify(student_id="31"), "Несколько учеников подходят под этот номер", GENERIC_HINT)
    ok(exam.verify(student_id="031"), exam.a)  # «031» is not the student number «31»: only the display number reads
    ok(exam.verify(student_id="TE-031"), exam.a)  # full IDs are unaffected
    ok(exam.verify(student_id="TE-131"), exam.b)
    # a number that is only a student number, or only a display number, is still unique
    exam.c.student_number = "9001"
    exam.db.commit()
    ok(exam.verify(student_id="9001"), exam.c)
    ok(exam.verify(student_id="21"), exam.c)
    # the same student by both readings is one student, not an ambiguity
    exam.c.student_number = "21"
    exam.db.commit()
    ok(exam.verify(student_id="21"), exam.c)


@needs_pg
def test_prefix_digits_do_not_collide(exam):
    nc1 = make_student(exam.db, exam.org, exam.exam_group, "Первый", "Префиксов", "NC1-001")
    nc10 = make_student(exam.db, exam.org, exam.exam_group, "Второй", "Префиксов", "NC10-01")
    exam.db.commit()
    ok(exam.verify(student_id="nc1-001"), nc1)
    ok(exam.verify(student_id="NC10 01"), nc10)
    ok(exam.verify(student_id="nc10_01"), nc10)
    refused(exam.verify(student_id="NC1001"), "не найден")
    refused(exam.verify(student_id="NC10-001"), "не найден")


@needs_pg
def test_id_case_dash_space_variants(exam):
    for typed in ("TE-031", "te-031", "Te_031", "TE 031", "te   031", "  te - 031 ", "TE\u2013031", "TE\u200b-031"):
        ok(exam.verify(student_id=typed), exam.a)
    for cyrillic in ("ТЕ-031", "те 031", "Те_031"):  # Т, Е typed on a Russian keyboard
        ok(exam.verify(student_id=cyrillic), exam.a)
    ok(exam.verify(student_id="s_77"), exam.a)  # student number, same normalisation
    ok(exam.verify(student_id=str(exam.a.id).upper()), exam.a)  # UUID
    refused(exam.verify(student_id="TE031"))  # a missing separator is not a variant
    refused(exam.verify(student_id="TE-0 31"))  # a separator in the middle of the number is not either


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
    ok(exam.verify(student_name="Тимур Рахимов Алиевич"), exam.a)  # one extra word (a patronymic) is tolerated
    ok(exam.verify(student_name="Тимур Рахимов Алиевич Второй"), exam.a)  # up to two (N13 P1.5)
    refused(exam.verify(student_name="Тимур Рахимов Алиевич Второй Третий"), "не найден")  # three are not
    ok(exam.verify(query="Тимур Рахимов"), exam.a)


@needs_pg
def test_apostrophes_dashes_and_invisible_characters_do_not_matter(exam):
    # stored «Ma’rufjon» (U+2019), «Oʻgʻiloy» (U+02BB): any apostrophe, or none, enters
    for typed in ("Ma'rufjon Karimov", "Ma\u2019rufjon Karimov", "Marufjon Karimov", "MA`RUFJON karimov", "Karimov Ma\u02bcrufjon"):
        ok(exam.verify(student_name=typed), exam.marufjon)
    for typed in ("O'g'iloy Rahmonov", "Ogiloy Rahmonov", "O\u02bbg\u02bbiloy Rahmonov", "rahmonov oʻgʻiloy"):
        ok(exam.verify(student_name=typed), exam.ogiloy)
    # stored «Иванова-Петрова»: hyphen, any dash or a space
    for typed in ("Ольга Иванова-Петрова", "Ольга Иванова Петрова", "Ольга Иванова\u2013Петрова", "иванова\u2014петрова ольга"):
        ok(exam.verify(student_name=typed), exam.double)
    refused(exam.verify(student_name="Ольга Иванова"), "не найден")  # half of the double surname is not the name
    # copy/paste leftovers: zero-width characters, soft hyphen
    for typed in ("Тимур\u200b Рахимов", "Ти\u00adмур Рахимов", "\ufeffТимур Рахи\u200cмов", "Ти\u2060мур Рахимов"):
        ok(exam.verify(student_name=typed), exam.a)
    # NFD «й» (и + breve)
    ok(exam.verify(student_name=unicodedata.normalize("NFD", "Юрий Гайдай")), exam.yuriy)
    ok(exam.verify(student_name="Юрий Гайдай"), exam.yuriy)
    # Uzbek Cyrillic letters typed on a Russian keyboard
    ok(exam.verify(student_name="Голиб Саидов"), exam.golib)
    ok(exam.verify(student_name="Ғолиб Саидов"), exam.golib)
    # ID + name uses the same normalisation
    ok(exam.verify(student_id="TE-081", student_name="Marufjon"), exam.marufjon)
    ok(exam.verify(student_id="TE-085", student_name="Голиб"), exam.golib)


@needs_pg
def test_exact_name_beats_a_longer_name_that_contains_it(exam):
    # «Али Каримов» and «Али Акбар Каримов» both enter with their own exact names
    ok(exam.verify(student_name="Али Каримов"), exam.ali)
    ok(exam.verify(student_name="каримов али"), exam.ali)
    ok(exam.verify(student_name="Али Акбар Каримов"), exam.ali_akbar)
    ok(exam.verify(student_name="Каримов Акбар Али"), exam.ali_akbar)
    # a half name matches neither
    refused(exam.verify(student_name="Акбар Каримов"), "не найден")
    refused(exam.verify(student_name="Али Акбар"), "не найден")
    # one extra word on top of the shorter name would also cover it: still that one student only
    ok(exam.verify(student_name="Али Каримов Алиевич"), exam.ali)


@needs_pg
def test_namesakes_must_use_the_id(exam):
    refused(exam.verify(student_name="Мария Иванова"), NAMESAKES)
    refused(exam.verify(student_name="иванова мария"), NAMESAKES)
    refused(exam.verify(student_name="Мария Иванова Алиевна"), NAMESAKES)  # the extra word does not pick one
    ok(exam.verify(student_id="TE-040", student_name="Мария Иванова"), exam.m1)
    ok(exam.verify(student_id="41"), exam.m2)
    # two exact namesakes of a longer name
    make_student(exam.db, exam.org, exam.exam_group, "Али Акбар", "Каримов", "TE-088")
    exam.db.commit()
    refused(exam.verify(student_name="Али Акбар Каримов"), NAMESAKES)


@needs_pg
def test_id_with_name_checks_the_owner(exam):
    ok(exam.verify(student_id="TE-031", student_name="Тимур Рахимов"), exam.a)
    ok(exam.verify(student_id="TE-031", student_name="рахимов"), exam.a)  # only the surname
    ok(exam.verify(student_id="TE-031", student_name="Тимур"), exam.a)
    # first and last name with extra words (a patronymic) are fine: the ID is the key
    ok(exam.verify(student_id="TE-031", student_name="Тимур Рахимов Алиевич"), exam.a)
    ok(exam.verify(student_id="TE-031", student_name="Рахимов Тимур Алиевич Второй"), exam.a)
    ok(exam.verify(student_id="ТЕ 031", student_name="тимур рахимов"), exam.a)
    for wrong in ("Зарина Ахмедова", "Тимур Назаров", "Тим", "Рахим", "Тимур Иванов", "Алиевич"):
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


# ---- N13 P1.5: patronymics, punctuation, bidi marks, the ID hint -------------------


@needs_pg
def test_name_only_entry_tolerates_two_extra_words_like_an_uzbek_patronymic(exam):
    ok(exam.verify(student_name="Karimov Ma'rufjon Akram o'g'li"), exam.marufjon)
    ok(exam.verify(student_name="Ma\u2019rufjon Karimov Akram qizi"), exam.marufjon)
    ok(exam.verify(student_name="Рахимов Тимур Акрам угли"), exam.a)
    ok(exam.verify(student_name="Тимур Рахимов Акрамович Второй"), exam.a)
    refused(exam.verify(student_name="Ma'rufjon Karimov Akram o'g'li Jr Sr"), "не найден")  # three extra words
    # the exact name still wins over a longer typed name that covers a shorter one
    ok(exam.verify(student_name="Али Акбар Каримов"), exam.ali_akbar)
    # two namesakes still ask for the ID
    refused(exam.verify(student_name="Мария Иванова Акрам угли"), NAMESAKES)


@needs_pg
def test_commas_and_punctuation_between_words_are_ignored(exam):
    for typed in ("Рахимов, Тимур", "Рахимов,Тимур", "Тимур; Рахимов.", "«Тимур» (Рахимов)", "Рахимов , Тимур , Алиевич"):
        ok(exam.verify(student_name=typed), exam.a)
    ok(exam.verify(student_name="Karimov, Ma'rufjon"), exam.marufjon)
    ok(exam.verify(student_id="TE-031", student_name="Рахимов, Тимур"), exam.a)
    refused(exam.verify(student_name="Рахимов, Иван"), "не найден")


@needs_pg
def test_lrm_rlm_and_horizontal_bar_are_handled(exam):
    for typed in ("Тимур\u200e Рахимов", "\u200fТимур Рахимов\u200f", "Ти\u200eмур Рахи\u200fмов"):
        ok(exam.verify(student_name=typed), exam.a)
    ok(exam.verify(student_name="Ольга Иванова\u2015Петрова"), exam.double)       # U+2015 is a dash
    ok(exam.verify(student_id="TE\u2015031"), exam.a)
    ok(exam.verify(student_id="\u200eTE-031\u200f"), exam.a)


def test_normalisation_of_the_new_characters():
    assert si.normalize_name("Иванов,\u200e Иван") == "иванов иван"
    assert si.normalize_name("Иванова\u2015Петрова") == "иванова петрова"
    assert si.normalize_name("Akram o'g'li") == "akram ogli"
    assert si.normalize_name("a.b;c:d!e?f") == "a b c d e f"
    assert si.normalize_id("TE\u2015031") == "te-031"
    assert si.normalize_id("\u200fTE-031\u200e") == "te-031"


@needs_pg
def test_name_not_found_points_to_the_id_on_the_student_card(exam):
    result = exam.verify(student_name="Неизвестный Ученик")
    assert result["enrolled"] is False
    assert "или введите ID из карточки ученика" in result["message"]
    assert "Неизвестный Ученик" in result["message"] and exam.exam_group.name in result["message"]
    # the other refusals keep their own words
    assert "карточки ученика" not in exam.verify(student_name="ал")["message"]
    assert "карточки ученика" not in exam.verify(student_id="TE-031", student_name="Зарина Ахмедова")["message"]
