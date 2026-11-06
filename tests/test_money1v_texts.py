"""Money-1 pack V: the words of the new answers (pure, no database).  Every sum has a non-breaking space between the number and «сум» (LESSONS 112)."""
from src.auth import payments_texts as tx

NB = " "


def test_v1_the_stale_sum_says_was_and_now_in_the_words_of_the_dictionary():
    assert tx.stale_text("debt", 100_000, 70_000) == f"Долг изменился: было 100{NB}000{NB}сум, сейчас 70{NB}000{NB}сум"
    assert tx.stale_text("refundable", 344_000, 244_000) == f"Сумма к возврату изменилась: было 344{NB}000{NB}сум, сейчас 244{NB}000{NB}сум"
    assert tx.stale_text("advance", 0, 5_000).startswith("Аванс изменился: было 0")


def test_v3_the_borders_of_a_sensible_price_and_its_question():
    from src.auth import pricing

    for old, new, unusual in ((500_000, 1_000_000, False), (500_000, 1_000_001, True), (500_000, 250_000, False), (500_000, 249_999, True),
                              (500_000, 0, True), (500_000, 500_000, False), (None, 5_000_000, False), (0, 400_000, False)):
        assert pricing.unusual_change(old, new) is unusual, (old, new)
    assert pricing.unusual_warnings(300_000, 3_000_000) == [{"code": "price_unusual", "text": f"Вы уверены: 3{NB}000{NB}000{NB}сум? Сейчас 300{NB}000{NB}сум"}]
    assert pricing.unusual_warnings(300_000, 310_000) == []


def test_v9_the_words_about_the_pay_of_the_teacher_in_the_windows_of_a_price_and_of_a_forgiveness():
    from src.auth import money_effects as me

    closes = {"from": "2026-10-06", "to": "2026-11-05", "label": "06.10–05.11"}
    owner_lines = [{"month": "2026-10", "state": "open", "changedBy": 133_333, "teacherName": "Эргашев К."}]
    got = me.forgiveness_block(closes, {"payroll": owner_lines})
    assert got["text"] == f"Прощение закроет период 06.10–05.11. Эти уроки будут оплачены преподавателю: +133{NB}333{NB}сум" and got["teacher_pay"] == 133_333
    admin = me.forgiveness_block(closes, {"payroll": [{"month": "2026-10", "state": "open"}]})
    assert admin["text"] == "Прощение закроет период 06.10–05.11. Эти уроки будут оплачены преподавателю" and admin["teacher_pay"] is None
    assert me.forgiveness_block(None, {"payroll": []}) is None
    assert me.teacher_share_text(480_000) == "Зарплата преподавателя за этого ученика тоже изменится: процент считается от фактической цены"
    assert me.teacher_share_text(0) == "Зарплата преподавателя за этого ученика станет 0: процент считается от фактической цены"
    assert me.teacher_share_text(None) == "Зарплата преподавателя за этого ученика снова считается от цены группы"
