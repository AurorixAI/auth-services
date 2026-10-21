"""LOGIC-A: the error codes of the new / changed operations with their texts - the ``x-errors`` of ``logicA_contract.json`` (the front
shows ``detail.message`` as the server wrote it; this table is what the contract promises).  ``{N}`` / ``{names}`` / ``{dates}`` are filled
by the server."""
from __future__ import annotations

from typing import Dict, List, Tuple

PAY_LEFT: List[Tuple[int, str, str]] = [
    (422, "PAYMENT_EXCEEDS_DEBT", "Ученик ушёл: принять можно не больше долга {N} сум"),
    (422, "NOTHING_TO_PAY", "Ученик ушёл, долга нет: принимать нечего"),
]
OPENING: List[Tuple[int, str, str]] = [
    (422, "OPENING_RANGE", "Оплачено до — не раньше даты зачисления | Долг на дату — не позже дня зачисления"),
    (409, "OPENING_NOT_ALLOWED", "Остаток нельзя внести: ученик пришёл переводом"),
    (422, "REASON_REQUIRED", "У ученика уже есть оплаты: укажите причину изменения остатка (от 3 до 500 символов)"),
    (409, "VOID_BREAKS_REFUND", "Сначала отмените возврат: без этой оплаты возвращать нечего"),
    (409, "STUDENT_ARCHIVED", "Ученик в архиве — сначала верните его"),
]
PAUSE: List[Tuple[int, str, str]] = [
    (422, "PAUSE_RANGE", "Пауза начинается не раньше чем 30 дней назад | Пауза не длиннее 366 дней"),
    (409, "PAUSE_OVERLAP", "Пауза пересекается с другой паузой этого ученика"),
    (409, "PAUSE_OVERLAPS_ATTENDED", "Ученик был отмечен на уроках {dates} — на эти даты паузу поставить нельзя"),
    (422, "PAUSE_LOCKED", "Паузу нельзя сократить или отменить: дни старше 30 дней уже не меняются"),
    (409, "PAUSE_BREAKS_REFUND", "Сначала отмените возврат: без паузы вернуть нечего"),
    (409, "MEMBERSHIP_CLOSED", "Ученик уже не в этой группе: паузу поставить нельзя"),
]
LEAVE: List[Tuple[int, str, str]] = [
    (409, "BALANCE_CHANGED", "Данные оплат изменились — проверьте суммы"),
    (422, "METHOD_REQUIRED", "Укажите способ возврата"),
    (422, "REASON_REQUIRED", "Причина: от 3 до 500 символов"),
    (403, "", "Нет доступа к оплатам"),
]
PRICE: List[Tuple[int, str, str]] = [
    (409, "PRICE_BACKDATE_WITH_PAYMENTS", "Нельзя изменить цену задним числом: у {N} учеников уже есть оплаты за период, который это затронет ({names}). "
                                          "Разницу за прошлое закройте прощением долга или возвратом."),
    (409, "PRICE_CONFLICT", "Цену только что изменили. Обновите страницу и повторите."),
    (409, "PRICE_MARKER", "Эту запись создала система при уходе из группы: её нельзя отменить"),
]
LESSON_TYPE: List[Tuple[int, str, str]] = [(422, "LESSON_TYPE_RANGE", "Тип занятий можно менять с начала прошлого месяца по сегодня")]
PAYROLL: List[Tuple[int, str, str]] = [
    (422, "FINGERPRINT_REQUIRED", "Откройте расчёт заново: сохранение без проверки данных невозможно"),
    (409, "SHEET_DATA_CHANGED", "Данные изменились после расчёта — пересчитайте и проверьте суммы"),
    (422, "DIVISOR_RANGE", "Делитель: от 1 до 31"),
]
PAYOUT: List[Tuple[int, str, str]] = [
    (409, "SHEET_NOT_SAVED", "Сначала сохраните ведомость"),
    (409, "DUPLICATE_PAYOUT", "Выплата уже внесена"),
    (409, "ALREADY_VOIDED", "Выплата уже отменена"),
    (422, "AMOUNT_RANGE", "Сумма: от 1 до 10 000 000 000"),
    (422, "PAID_ON_RANGE", "Дата выплаты не может быть в будущем | Дата выплаты старше 3 лет"),
    (422, "METHOD_REQUIRED", "Укажите способ выплаты"),
    (422, "REASON_REQUIRED", "Причина: от 3 до 500 символов"),
]

ERRORS_BY_OPERATION: Dict[Tuple[str, str], List[Tuple[int, str, str]]] = {
    ("get", "/org/memberships/{membership_id}/payments/preview"): PAY_LEFT[:0],
    ("post", "/org/memberships/{membership_id}/payments"): PAY_LEFT,
    ("post", "/org/payments/{payment_id}/replace"): PAY_LEFT,
    ("put", "/org/memberships/{membership_id}/opening-balance"): OPENING,
    ("post", "/org/opening-balances/{opening_id}/void"): OPENING[3:4],
    ("post", "/org/memberships/{membership_id}/pauses"): PAUSE,
    ("patch", "/org/pauses/{pause_id}"): PAUSE + [(422, "REASON_REQUIRED", "Причина: от 3 до 500 символов (обязательна, когда изменение трогает деньги)")],
    ("post", "/org/pauses/{pause_id}/void"): PAUSE[3:5] + [(422, "REASON_REQUIRED", "Причина: от 3 до 500 символов")],
    ("post", "/org/groups/{group_id}/students/{student_id}/leave"): LEAVE,
    ("post", "/org/students/{student_id}/archive"): LEAVE,
    ("post", "/org/groups/{group_id}/students/{student_id}"): [(422, "OPENING_NOT_ALLOWED", "Остаток нельзя внести: ученик переводится из другой группы, его деньги переходят вместе с ним"), OPENING[0]],
    ("put", "/org/groups/{group_id}/price"): PRICE[:2],
    ("put", "/org/memberships/{membership_id}/price-override"): PRICE[:2],
    ("delete", "/org/memberships/{membership_id}/price-override/{row_id}"): PRICE[2:],
    ("put", "/org/groups/{group_id}/lesson-type"): LESSON_TYPE,
    ("put", "/org/payroll/settings"): PAYROLL[2:],
    ("post", "/org/payroll/sheets"): PAYROLL[:2],
    ("patch", "/org/payroll/sheets/{sheet_id}"): PAYROLL[:2],
    ("post", "/org/payroll/sheets/{sheet_id}/payouts"): PAYOUT,
    ("post", "/org/payroll/payouts/{payout_id}/void"): [PAYOUT[2], PAYOUT[6]],
}
