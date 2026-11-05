"""The sentence of the money effects (``money_effects.text_of``): a sheet that comes back to its saved total says so and is not asked to be saved again (stage re-walk 05.11:
«изменится на −45 000 сум (всего к сохранённой −0 сум)»; the front had the same wording fixed as N-4)."""
from src.auth.money_effects import signed, text_of


def line(changed_by: int, drift_after: int, state: str = "open") -> dict:
    return {"month": "2026-11", "state": state, "teacherName": "Эргашев К.", "changedBy": changed_by, "driftAfter": drift_after}


def test_zero_has_no_sign():
    assert (signed(0), signed(45_000), signed(-45_000)) == ("0", "+45 000", "−45 000")


def test_back_to_the_saved_total_is_named_and_not_asked_to_be_saved_again():
    preview = text_of([], [line(-45_000, 0)], done=False)
    assert "снова как сохранённая" in preview and "−0" not in preview and "к сохранённой" not in preview
    done = text_of([], [line(-45_000, 0)], done=True)
    assert "пересохраните" not in done and "снова как сохранённая" in done


def test_a_sheet_that_moves_away_from_the_saved_total_is_still_asked_to_be_saved_again():
    done = text_of([], [line(45_000, 120_000)], done=True)
    assert "(всего к сохранённой +120 000 сум)" in done and "пересохраните её" in done
    assert "к сохранённой" not in text_of([], [line(45_000, 45_000)], done=True)
