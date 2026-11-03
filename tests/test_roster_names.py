from types import SimpleNamespace

from src.auth.roster_names import FALLBACK_NAME, student_full_name


def student(last, first):
    return SimpleNamespace(last_name=last, first_name=first)


def test_last_name_then_first_name_trimmed():
    assert student_full_name(student("Иванов", "Иван")) == "Иванов Иван"
    assert student_full_name(student("Иванов", "")) == "Иванов"
    assert student_full_name(student(None, " Иван ")) == "Иван"


def test_nobody_is_a_student_without_a_name():
    assert student_full_name(student("", "")) == FALLBACK_NAME == "Ученик"
    assert student_full_name(student(None, None)) == "Ученик"
    assert student_full_name(None) == "Ученик"
