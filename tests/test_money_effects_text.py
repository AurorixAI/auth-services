"""The sentence of the money effects (``money_effects.text_of``): a sheet that comes back to its saved total says so and is not asked to be saved again (stage re-walk 05.11:
«изменится на −45 000 сум (всего к сохранённой −0 сум)»; the front had the same wording fixed as N-4)."""
from src.auth.money_effects import signed, text_of


def line(changed_by: int, drift_after: int, state: str = "open") -> dict:
    return {"month": "2026-11", "state": state, "teacherName": "Эргашев К.", "changedBy": changed_by, "driftAfter": drift_after}


def test_zero_has_no_sign():
    assert (signed(0), signed(45_000), signed(-45_000)) == ("0", "+45 000", "−45 000")


def test_back_to_the_saved_total_is_named_and_not_asked_to_be_saved_again():
    preview = text_of([], [line(-45_000, 0)], done=False)
    assert "снова как сохранённая" in preview and "−0" not in preview and "к сохранённой" not in preview
    done = text_of([], [line(-45_000, 0)], done=True)
    assert "сохраните новую сумму" not in done and "снова как сохранённая" in done


def test_a_sheet_that_moves_away_from_the_saved_total_is_still_asked_to_be_saved_again():
    done = text_of([], [line(45_000, 120_000)], done=True)
    assert "(к сохранённой сумме: +120 000 сум)" in done and "сохраните новую сумму" in done
    assert "ведомост" not in done and done.startswith("Зарплата за ноябрь 2026 (Эргашев К.) изменилась на +45 000 сум")
    assert "к сохранённой" not in text_of([], [line(45_000, 45_000)], done=True)


def test_a_closed_month_is_named_and_the_next_step_is_said_once():
    done = text_of([], [line(45_000, 120_000, "closed")], done=True)
    assert done.endswith("— месяц закрыт, сохраните новую сумму") and "ведомост" not in done and "пересохран" not in done
    refused = text_of([], [line(45_000, 45_000, "closed")], done=False, closed_blocks=True)
    assert refused.endswith("(месяц закрыт: сначала переоткройте его)")
    assert text_of([], [line(45_000, 45_000, "closed")], done=False).endswith("(месяц закрыт)")


def test_the_return_of_a_cancelled_lesson_says_recalculation_never_deduction():
    from src.auth.money_effects import deduction_text

    gone = [{"amount": 40_000}, {"amount": 80_000}]
    assert deduction_text(gone, done=False) == ("Ученикам снимем перерасчёт за этот урок: всего 120 000 сум (2 ученика); "
                                                "урок снова ждёт отметки — в зарплату преподавателя он войдёт, когда его проведут")
    assert deduction_text(gone[:1], done=True, past=False) == "Ученикам снят перерасчёт за этот урок: всего 40 000 сум (1 ученик); урок снова в расписании"
    assert deduction_text([], done=True).startswith("Перерасчёт учеников не изменился") and "вычет" not in deduction_text([], done=False)


def test_a_pack_spends_or_gets_back_one_lesson_per_student_in_the_one_wording():
    from src.auth.money_effects import recalc_text, text_of

    took = [{"studentId": "a", "delta": -1}, {"studentId": "b", "delta": -1}]
    gave = [{"studentId": "a", "delta": 1}]
    assert text_of(took, [], done=False) == "Из пакета спишется 1 урок (у 2 учеников)"
    assert text_of(took, [], done=True) == "Из пакета списан 1 урок (у 2 учеников)"
    assert text_of(gave, [], done=False) == "В пакет вернётся 1 урок (у 1 ученика)"
    assert text_of(gave, [], done=True) == "В пакет возвращён 1 урок (у 1 ученика)"
    # the recalculation of «Урока не было» lowers the debt at once (invariant I2: charged - paid - recalculated == debt - advance), the sentence says so
    added = [{"amount": 46_000}, {"amount": 46_000}]
    assert recalc_text(added, done=False) == "Ученикам — перерасчёт: долг уменьшится сразу на 92 000 сум (2 ученика)"
    assert recalc_text(added, done=True).startswith("Ученикам — перерасчёт: долг уменьшился сразу")
